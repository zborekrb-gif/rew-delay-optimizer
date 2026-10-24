#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""REWDO v20.31 GUI — Safety Hotfix.

Prosty graficzny panel startowy dla trybów, które wcześniej były uruchamiane
plikami .bat. GUI nie zmienia matematyki analizy — buduje te same komendy CLI,
pozwala wybrać plik .mdat i folder wyników, uruchamia proces w tle oraz pokazuje
log w oknie.

Uruchamianie:
- Windows: dwuklik w REWDO_GUI.pyw
- Alternatywnie: python REWDO_GUI.py
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import traceback
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from rew_api_client import DEFAULT_REW_API_URL, is_rew_api_snapshot
from rew_api_gui import REWApiDialog
from rew_reference_registry import find_reference_conflicts, load_reference_record


def _subprocess_creationflags() -> int:
    """Ukryj okno konsoli dla procesów potomnych w Windows.

    Ważne dla buildów PyInstaller bez konsoli: GUI ma pozostać jedynym oknem,
    także gdy uruchamia wewnętrzny tryb CLI przez ten sam plik EXE.
    """
    if sys.platform.startswith("win"):
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0



def _text_score_for_encoding(s: str) -> int:
    """Heuristic score: prefer real Polish letters, penalize mojibake/replacement chars."""
    polish = "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ"
    bad = "�ÃÄÅĹÂ¤€šœžŸµ"
    return sum(4 for ch in s if ch in polish) - sum(5 for ch in s if ch == "�") - sum(2 for ch in s if ch in bad)


def _repair_log_text(text: str) -> str:
    """Repair the most common UTF-8/Windows-1250 mojibake before showing log.

    In a windowed PyInstaller EXE the child process may write either UTF-8 or
    the Windows ANSI code page. The GUI log should show Polish characters in
    both cases.
    """
    if not isinstance(text, str):
        text = str(text)

    candidates = [text]
    for enc in ("cp1250", "cp1252", "latin1"):
        try:
            candidates.append(text.encode(enc).decode("utf-8"))
        except Exception:
            pass

    best = max(candidates, key=_text_score_for_encoding)

    # Extra cleanup for double-decoded Windows/UTF-8 Polish mojibake seen in
    # some console/pipe combinations.
    replacements = {
        "BÅÄ„D": "BŁĄD",
        "BÅÄ…d": "Błąd",
        "ZAKOÅƒCZONO": "ZAKOŃCZONO",
        "zaÅ¼Ã³ÅÄ‡": "zażółć",
        "gÄ™Å›lÄ…": "gęślą",
        "jaÅºÅ„": "jaźń",
        "Å": "Ł", "Å‚": "ł",
        "Ä…": "ą", "Ä‡": "ć", "Ä™": "ę", "Å„": "ń",
        "Ã³": "ó", "Å›": "ś", "Åº": "ź", "Å¼": "ż",
        "Ä„": "Ą", "Ä†": "Ć", "Ä˜": "Ę", "Åƒ": "Ń",
        "Ã“": "Ó", "Åš": "Ś", "Å¹": "Ź", "Å»": "Ż",
        "Ĺ": "Ł", "Ĺ‚": "ł", "Ĺ„": "ń", "Ĺ›": "ś",
        "Ĺş": "ź", "Ĺź": "ż", "Ăł": "ó",
        "Ĺ\x81": "Ł", "Ĺ\x82": "ł", "Ĺ\x84": "ń",
        "Ĺ\x9b": "ś", "Ĺ\xba": "ź", "Ĺ\xbc": "ż",
    }
    for bad, good in replacements.items():
        best = best.replace(bad, good)

    return best.replace("\r\n", "\n").replace("\r", "\n")


def _decode_process_output(data: bytes | str) -> str:
    """Decode one stdout/stderr chunk from the worker process safely."""
    if isinstance(data, str):
        return _repair_log_text(data)

    candidates: list[str] = []
    for enc in ("utf-8", "cp1250", "mbcs", "cp1252"):
        try:
            candidates.append(data.decode(enc, errors="strict"))
        except Exception:
            pass
    if not candidates:
        candidates.append(data.decode("utf-8", errors="replace"))

    repaired = [_repair_log_text(c) for c in candidates]
    return max(repaired, key=_text_score_for_encoding)


def _default_log_font() -> tuple[str, int]:
    """Use a Windows-friendly font with complete Polish glyph coverage."""
    if sys.platform.startswith("win"):
        return ("Consolas", 9)
    return ("TkFixedFont", 9)


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _app_dir() -> Path:
    """Folder aplikacji dla trybu źródłowego i EXE ONEDIR.

    W źródłach: folder z plikami .py.
    W EXE ONEDIR: folder obok REWDO_GUI.exe. Tam trzymamy USER_*.yaml
    i tam domyślnie trafiają katalogi wyników.
    """
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = _app_dir()


def _bundled_file(name: str) -> Path:
    """Find a user/config file in source, ONEDIR root, or PyInstaller _internal."""
    candidates = [
        APP_DIR / name,
        APP_DIR / "_internal" / name,
    ]
    try:
        meipass = Path(getattr(sys, "_MEIPASS", ""))
        if str(meipass):
            candidates.append(meipass / name)
            candidates.append(meipass / "_internal" / name)
    except Exception:
        pass
    for c in candidates:
        try:
            if c.exists():
                return c
        except Exception:
            pass
    return APP_DIR / name


def _default_reference_90() -> Path:
    """Return the bundled/default 90 BW48 reference without creating estimates.

    v20.07 CLEAN: the GUI must not silently generate an estimated
    USER_REFERENCE_90BW48.yaml. If the file is missing, ranking modes and
    POST-DIRAC stop with a clear message, and the user can create a real
    reference via mode 4 — Verify User Slot SHD.
    """
    ref = _bundled_file("USER_REFERENCE_90BW48.yaml")
    if ref.exists():
        return ref
    return APP_DIR / "USER_REFERENCE_90BW48.yaml"


def _ensure_default_reference_90() -> Path:
    """Backward-compatible alias; no file is created in v20.07 CLEAN."""
    return _default_reference_90()



def _resolve_user_reference_path(path_text: str | Path | None) -> Path:
    """Return selected reference path or the default 90 BW48 reference."""
    raw = str(path_text or "").strip()
    if not raw:
        return _default_reference_90()
    try:
        return Path(raw).expanduser().resolve()
    except Exception:
        return Path(raw)


def _read_top_level_yaml_like(path: Path) -> Dict[str, str]:
    """Tiny, dependency-free reader for the simple USER_REFERENCE_*.yaml files.

    It is only used by the GUI to prevent selecting USER_REFERENCE_102BW48.yaml
    in the field that must contain the real 90 BW48 SHD reference.
    """
    data: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return data
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip().split(" #", 1)[0].strip().strip('"').strip("'")
        if key and key not in data:
            data[key] = val
    return data


def _to_float_or_none(value: object) -> Optional[float]:
    try:
        raw = str(value).strip().lower().replace(",", ".")
        if raw in {"", "none", "null", "nan", "—", "-"}:
            return None
        val = float(raw)
        if val != val:
            return None
        return val
    except Exception:
        return None


def _validate_real_reference_90(path: Path) -> tuple[bool, str]:
    """Validate the file selected in GUI field 'Plik referencji SHD 90'.

    v20.07: this field is intentionally strict. It must not point to
    USER_REFERENCE_102BW48.yaml, because that file is only a listening candidate
    profile and has null delays by design.
    """
    if not path.exists():
        return False, f"Plik nie istnieje: {path}"
    data = _read_top_level_yaml_like(path)
    if not data:
        return False, f"Nie mogę odczytać prostego YAML: {path}"
    missing = []
    for key in ("l_main_ms", "r_main_ms", "l_sub_ms", "r_sub_ms"):
        if _to_float_or_none(data.get(key)) is None:
            missing.append(key)
    if missing:
        extra = ""
        if "102" in path.name or str(data.get("role", "")).strip():
            extra = "\n\nWygląda na to, że wybrano profil kandydata 102, a nie realną referencję 90. USER_REFERENCE_102BW48.yaml ma delaye null i nie może być użyty jako 'Plik referencji SHD 90'."
        return False, "Brak realnych delayów SHD w polach: " + ", ".join(missing) + extra
    xo = _to_float_or_none(data.get("crossover_hz"))
    filt = str(data.get("filter", "")).strip().upper()
    if xo is not None and abs(xo - 90.0) > 0.51:
        return False, f"To pole wymaga realnej referencji 90 BW48, a plik ma crossover_hz={xo:g}."
    if filt and "BW48" not in filt.replace(" ", ""):
        return False, f"To pole wymaga referencji 90 BW48, a plik ma filter={filt}."
    try:
        record = load_reference_record(path)
    except ValueError as exc:
        return False, str(exc)
    companion = path.with_name("USER_REFERENCE_SLOT4MAX_90BW48.yaml")
    if companion.exists() and companion.resolve() != path.resolve():
        conflicts = find_reference_conflicts([path, companion])
        if conflicts:
            return False, conflicts[0].describe() + ". Nie wybieram delayu po cichu; rozstrzygnij 17,9/19,1 ms pomiarem."
    if not record.anchor_allowed:
        return False, (
            "Referencja nie jest zweryfikowana pomiarem. Wymagane: verified=true, "
            "verification_status=MEASURED lub USER_CONFIRMED, hash pomiaru i verified_at."
        )
    return True, "OK"


def _reference_required(mode: "Mode") -> bool:
    """Modes that read USER_REFERENCE_*.yaml through --user-reference."""
    return mode.key in {"fast_90", "post_dirac", "polarity", "global", "between", "acoustic_crossover", "slot4max", "acoustic_biquad"}



@dataclass(frozen=True)
class Mode:
    key: str
    label: str
    output_dir: str
    description: str
    needs_mdat: bool = True
    needs_bridge: bool = False
    verify_mode: bool = False


MODES: List[Mode] = [
    Mode(
        "raw_check",
        "0 — Kontrola pliku RAW",
        "raw_measurement_check",
        "Kontrola pomiarów bez zwrotnicy DSP (RAW): zakres pasma, role pomiarów, delay0, REL120 i kompletność danych. To pierwszy test jakości wejścia.",
    ),
    Mode(
        "gain_bridge",
        "0B — Gain Bridge RAW −6 dB → 0 dB",
        "GAIN_BRIDGE_RESULT",
        "Sprawdza amplitudowo rzeczywistą zmianę gainu subwooferów osobno dla L i R. Wymaga pełnego RAW oraz pliku bridge z L_SUM_SUB0 i R_SUM_SUB0.",
        needs_bridge=True,
    ),
    Mode(
        "fast_90",
        "1 — Szybki wybór kandydatów blisko 90 BW48",
        "ranking_REFERENCE_NEIGHBORHOOD_90",
        "Wybiera sloty do wpisania w SHD. RAW nie rozstrzyga finalnego zwycięzcy — wskazuje kandydatów do Diraca i pomiaru POST-DIRAC.",
    ),
    Mode(
        "post_dirac",
        "2 — Analiza po Diracu: tabela decyzji slotów L+P",
        "post_dirac_ab",
        "Najważniejszy tryb decyzyjny po Diracu: tabela pełnych slotów L+P, diagnostyka L/R i odporność L/MLP/P.",
    ),
    Mode(
        "polarity",
        "3 — Diagnostyka polaryzacji subów",
        "ranking_POLARITY_DIAGNOSTIC_MUSIC_SAFE",
        "Sprawdza, czy odwrócenie polaryzacji subów daje sensowny kierunek. Wyniki INV traktuj jako diagnostykę, nie automatyczną decyzję.",
    ),
    Mode(
        "verify_slot",
        "4 — Zapis realnej referencji SHD 90",
        "verify_user_slot",
        "Wpisz cztery delaye z konsoli miniDSP SHD. Narzędzie zapisze realną referencję 90 BW48 do YAML.",
        needs_mdat=False,
        verify_mode=True,
    ),
    Mode(
        "global",
        "5 — Skan globalny / nowe topologie",
        "ranking_GLOBAL_RAW_DIAGNOSTIC_ONLY",
        "Skan dalekich topologii delayów. Używaj do szukania nowych kierunków, nie do prostego zastąpienia 90.",
    ),
    Mode(
        "between",
        "6 — Kompromis 98–102 BW48",
        "ranking_BETWEEN_90_AND_102",
        "Szuka kompromisu między teksturą 90 a energią/sceną wyższych XO. 102 pozostaje kandydatem warunkowym.",
    ),
    Mode(
        "filter_generator",
        "7 — Generator filtrów teoretycznych miniDSP",
        "filtry_zwrotnicy_minidsp",
        "Generuje kontrolne warianty HPF/LPF z teorii BW/LR. To tryb pomocniczy, nie projektant akustyczny.",
        needs_mdat=False,
    ),
    Mode(
        "acoustic_crossover",
        "8 — Selektor standardowych BW48/LR48 z RAW",
        "zwrotnica_akustyczna_z_raw",
        "Wybiera standardowe kaskady BW48/LR48 i sprawdza je w stanie Dirac oraz odsłuchowym. Wymaga zweryfikowanego Gain Bridge.",
        needs_bridge=True,
    ),
    Mode(
        "slot4max",
        "8B — Slot 4 Shortlist Validator",
        "SLOT4_SHORTLIST_VALIDATOR",
        "Porównuje wyłącznie jawnie policzone warianty REFERENCE, SAFE i SEAM. Wymaga pełnego RAW, Gain Bridge i bezkonfliktowej referencji z dowodem pomiarowym.",
        needs_bridge=True,
    ),
    Mode(
        "acoustic_biquad",
        "9 — Projektant biquadów — GainState + Safety Gate",
        "acoustic_biquad_reference_safe",
        "Projektuje osobne częstotliwości i Q sekcji oraz sprawdza ten sam filtr w stanie Dirac i odsłuchowym. Gain Bridge jest obowiązkowy do eksportu.",
        needs_bridge=True,
    ),
    Mode(
        "time_domain",
        "10 — Analiza czasowa / 90 BW48 Fast",
        "time_domain_90bw48_fast",
        "Wyznacza rozrzut z 3 powtórzeń referencji i wydaje jeden werdykt dla kompletnego wariantu L/R/L+R. Ochrona body 100–220 Hz oraz spójny anchor group delay są obowiązkowe. Wymaga migawki REW API z impulsem.",
    ),
]

MODE_BY_LABEL: Dict[str, Mode] = {m.label: m for m in MODES}



def _python_exe() -> str:
    """Interpreter Python dla trybu źródłowego.

    W skompilowanym EXE GUI nie używa python.exe; uruchamia ten sam
    REWDO_GUI.exe z parametrem --rewdo-cli.
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def _cli_exe() -> Path:
    """Single-EXE helper path.

    In frozen/ONEDIR mode the GUI runs its own
    executable with:
        REWDO_GUI.exe --rewdo-cli <subcommand> ...
    This avoids the previous fragile two-EXE design (REWDO_GUI.exe +
    REWDO_CLI.exe), where the starter could fail before writing useful logs.
    """
    if _is_frozen():
        return Path(sys.executable).resolve()
    return Path()


def _script_command(script_filename: str, frozen_subcommand: str) -> List[str]:
    """Command prefix for source mode and frozen ONEDIR mode.

    Source:
        python -u rew_xxx.py ...

    Frozen ONEDIR:
        REWDO_GUI.exe --rewdo-cli <subcommand> ...

    This single-EXE design is more reliable on Windows than launching a
    separate REWDO_CLI.exe helper.
    """
    if _is_frozen():
        return [str(Path(sys.executable).resolve()), "--rewdo-cli", frozen_subcommand]
    return [_python_exe(), "-u", str(APP_DIR / script_filename)]


def _open_path(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        messagebox.showwarning("Nie udało się otworzyć", f"Nie mogę otworzyć:\n{path}\n\n{exc}")


def _append_extra_args(cmd: List[str], extra_text: str) -> List[str]:
    # Minimalny parser dla zaawansowanych argumentów: pozwala dopisać np.
    # --workers 2 albo --xo-min 94. Nie obsługuje cudzysłowów z odstępami,
    # bo standardowy workflow nie wymaga takich wartości.
    extra = [x.strip() for x in str(extra_text or "").split() if x.strip()]
    return cmd + extra


def build_command(
    *,
    mode: Mode,
    mdat_path: Path,
    out_dir: Path,
    reference: str,
    user_reference_path: Path,
    workers: int,
    extra_args: str,
    verify_values: Dict[str, str],
    bridge_path: Optional[Path] = None,
    allpass_enabled: bool = False,
    cut_only_enabled: bool = True,
    raw_sub_trim_db: float = -6.0,
    spatial_mdat_paths: Optional[Sequence[str]] = None,
    empirical_calibration_enabled: bool = False,
    delay_refinement_enabled: bool = True,
) -> List[str]:
    user_ref_90 = _resolve_user_reference_path(user_reference_path)

    # v20.07: PyInstaller single-EXE + multiprocessing on Windows can hang before
    # any result file is created. Default GUI runs ranking modes serially. Advanced
    # users can still override by adding a later "--workers N" in extra CLI args.
    if _is_frozen() and mode.key in {"fast_90", "polarity", "global", "between", "acoustic_crossover", "acoustic_biquad"} and int(workers) != 1:
        workers = 1

    if mode.key == "raw_check":
        cmd = _script_command("rew_raw_measurement_check.py", "raw_check") + ["--mdat", str(mdat_path), "--out", str(out_dir)]

    elif mode.key == "gain_bridge":
        if bridge_path is None:
            raise ValueError("Tryb Gain Bridge wymaga drugiego pliku MDAT.")
        cmd = _script_command("rew_gain_bridge.py", "gain_bridge") + [
            "--raw", str(mdat_path), "--bridge", str(bridge_path), "--out", str(out_dir),
        ]

    elif mode.key == "fast_90":
        cmd = _script_command("rew_crossover_ranking.py", "crossover_ranking") + [
            "--mdat", str(mdat_path), "--out", str(out_dir),
            "--profiles", "BW48",
            "--xo-min", "86", "--xo-max", "96", "--xo-step", "2",
            "--music-xo-cap", "92", "--caution-xo-cap", "102",
            "--delay-min", "-13", "--delay-max", "-7",
            "--shd-limit", "30",
            "--global-candidate-pool", "90",
            "--fast-ab",
            "--reference-neighborhood",
            "--reference-neighborhood-radius", "2.0",
            "--reference-xo", "90",
            "--reference-filter-type", "BW",
            "--reference-slope", "48",
            "--workers", str(workers),
            "--user-reference", str(user_ref_90),
        ]

    elif mode.key == "post_dirac":
        cmd = _script_command("rew_post_dirac_ab.py", "post_dirac") + [
            "--source", str(mdat_path),
            "--out", str(out_dir),
            "--reference", reference.strip() or "90_BW48_slot2",
            "--texture-reference-yaml", str(user_ref_90),
        ]

    elif mode.key == "polarity":
        cmd = _script_command("rew_crossover_ranking.py", "crossover_ranking") + [
            "--mdat", str(mdat_path), "--out", str(out_dir),
            "--profiles", "BW48,LR48",
            "--xo-min", "74", "--xo-max", "102", "--xo-step", "2",
            "--music-xo-cap", "92", "--caution-xo-cap", "102",
            "--delay-min", "-30", "--delay-max", "30",
            "--shd-limit", "30",
            "--refine-top", "5",
            "--refine-scene-top", "3",
            "--global-candidate-pool", "420",
            "--polarity-scan",
            "--workers", str(workers),
            "--user-reference", str(user_ref_90),
        ]

    elif mode.key == "global":
        cmd = _script_command("rew_crossover_ranking.py", "crossover_ranking") + [
            "--mdat", str(mdat_path), "--out", str(out_dir),
            "--profiles", "BW48,LR48",
            "--xo-min", "74", "--xo-max", "102", "--xo-step", "2",
            "--music-xo-cap", "92", "--caution-xo-cap", "102",
            "--delay-min", "-30", "--delay-max", "30",
            "--shd-limit", "30",
            "--global-candidate-pool", "90",
            "--fast-ab",
            "--reference-neighborhood",
            "--reference-neighborhood-radius", "2.0",
            "--reference-xo", "90",
            "--reference-filter-type", "BW",
            "--reference-slope", "48",
            "--workers", str(workers),
            "--user-reference", str(user_ref_90),
        ]

    elif mode.key == "between":
        cmd = _script_command("rew_crossover_ranking.py", "crossover_ranking") + [
            "--mdat", str(mdat_path), "--out", str(out_dir),
            "--profiles", "BW48",
            "--xo-min", "98", "--xo-max", "102", "--xo-step", "2",
            "--music-xo-cap", "92", "--caution-xo-cap", "102",
            "--delay-min", "-30", "--delay-max", "30",
            "--shd-limit", "30",
            "--global-candidate-pool", "90",
            "--fast-ab",
            "--reference-neighborhood",
            "--reference-neighborhood-radius", "2.0",
            "--reference-xo", "90",
            "--reference-filter-type", "BW",
            "--reference-slope", "48",
            "--workers", str(workers),
            "--user-reference", str(user_ref_90),
        ]

    elif mode.key == "filter_generator":
        cmd = _script_command("rew_filter_generator.py", "filter_generator") + [
            "--out", str(out_dir),
        ]

    elif mode.key == "acoustic_crossover":
        if bridge_path is None:
            raise ValueError("Tryb 8 wymaga zweryfikowanego pliku Gain Bridge MDAT.")
        cmd = _script_command("rew_acoustic_crossover_designer.py", "acoustic_crossover") + [
            "--mdat", str(mdat_path),
            "--out", str(out_dir),
            "--top", "8",
            "--gain-mode", "suggested",
            "--user-reference", str(user_ref_90),
            "--refine-radius", "0.4",
            "--refine-step", "0.1",
            "--gain-bridge", str(bridge_path),
            "--gain-bridge-target-delta-db", "2.0",
            "--raw-sub-trim-db", "-6.0",
            "--dirac-sub-trim-db", "-4.0",
            "--listening-sub-trim-db", "0.0",
        ]

    elif mode.key == "slot4max":
        if bridge_path is None:
            raise ValueError("Slot 4 Shortlist Validator wymaga pliku Gain Bridge MDAT.")
        cmd = _script_command("rew_slot4max_project.py", "slot4max") + [
            "--raw", str(mdat_path), "--bridge", str(bridge_path), "--out", str(out_dir),
            "--reference-yaml", str(user_ref_90),
        ]

    elif mode.key == "acoustic_biquad":
        if bridge_path is None:
            raise ValueError("Tryb 9 wymaga zweryfikowanego pliku Gain Bridge MDAT.")
        cmd = _script_command("rew_acoustic_biquad_designer.py", "acoustic_biquad") + [
            "--mdat", str(mdat_path),
            "--out", str(out_dir),
            "--search-level", "normal",
            "--user-reference", str(user_ref_90),
            "--shd-anchor-main", "30",
            "--search-repeats", "1",
            "--gain-bridge", str(bridge_path),
            "--dirac-sub-trim-db", "-4.0",
            "--listening-sub-trim-db", "0.0",
        ]
        measured_ref = _bundled_file("90_BW48_main30.mdat")
        if measured_ref.exists():
            cmd += ["--reference-mdat", str(measured_ref)]
        empirical_calibration = _bundled_file("USER_EMPIRICAL_CALIBRATION.json")
        if empirical_calibration.exists():
            cmd += ["--empirical-calibration", str(empirical_calibration)]
        if empirical_calibration_enabled:
            cmd += ["--enable-empirical-calibration"]
        if not delay_refinement_enabled:
            cmd += ["--no-delay-refinement"]
        cmd += ["--all-pass-mode", "compare" if allpass_enabled else "off"]
        cmd += ["--cut-only-mode", "on" if cut_only_enabled else "off"]
        cmd += ["--raw-sub-trim-db", f"{float(raw_sub_trim_db):.2f}"]
        for spatial_path in spatial_mdat_paths or []:
            if str(spatial_path).strip():
                cmd += ["--spatial-mdat", str(spatial_path).strip()]

    elif mode.key == "time_domain":
        cmd = _script_command("rew_time_domain_analyzer.py", "time_domain") + [
            "--snapshot", str(mdat_path),
            "--out", str(out_dir),
            "--reference", reference.strip() or "REF_SLOT2_90BW48_G0",
        ]

    elif mode.key == "verify_slot":
        required = ["l_main", "r_main", "l_sub", "r_sub"]
        missing = [k for k in required if not str(verify_values.get(k, "")).strip()]
        if missing:
            raise ValueError("Brakuje wartości delayów: " + ", ".join(missing))
        out_file = out_dir / "VERIFY_USER_SLOT.md"
        cmd = _script_command("rew_verify_user_slot.py", "verify_slot") + [
            "--l-main", verify_values["l_main"],
            "--r-main", verify_values["r_main"],
            "--l-sub", verify_values["l_sub"],
            "--r-sub", verify_values["r_sub"],
            "--out", str(out_file),
        ]
        if verify_values.get("xo"):
            cmd += ["--xo", verify_values["xo"]]
        if verify_values.get("filter"):
            cmd += ["--filter", verify_values["filter"]]
        if verify_values.get("l_pol"):
            cmd += ["--l-pol", verify_values["l_pol"]]
        if verify_values.get("r_pol"):
            cmd += ["--r-pol", verify_values["r_pol"]]
        if verify_values.get("ranking_csv"):
            cmd += ["--ranking-csv", verify_values["ranking_csv"]]
        if str(verify_values.get("save_ref", "")).lower() in {"1", "true", "yes", "tak", "t"}:
            cmd += ["--save-user-reference", str(user_ref_90)]

    else:
        raise ValueError(f"Nieznany tryb: {mode.key}")

    return _append_extra_args(cmd, extra_args)


def guess_main_report(mode: Mode, out_dir: Path) -> Optional[Path]:
    candidates = {
        "raw_check": ["RAW_MEASUREMENT_CHECK.md"],
        "gain_bridge": ["GAIN_BRIDGE_CHECK.md"],
        "fast_90": ["01_CO_TERAZ_ZROBIC_RAW.md", "SLOT_PLAN_DO_SHD.md", "RANKING_CROSSOVEROW_RAW.md"],
        "post_dirac": ["01_CO_TERAZ_ZROBIC_POST_DIRAC.md", "FINAL_DECISION_REPORT.md", "POST_DIRAC_AB_REPORT.md"],
        "polarity": ["RANKING_CROSSOVEROW_RAW.md", "SLOT_PLAN_DO_SHD.md"],
        "verify_slot": ["VERIFY_USER_SLOT.md"],
        "global": ["RANKING_CROSSOVEROW_RAW.md", "SLOT_PLAN_DO_SHD.md"],
        "between": ["SLOT_PLAN_DO_SHD.md", "RANKING_CROSSOVEROW_RAW.md"],
        "filter_generator": ["01_CO_TERAZ_ZROBIC_FILTRY.md", "02_IMPORT_MINIDSP_SHD.md"],
        "acoustic_crossover": ["01_CO_TERAZ_ZROBIC_AKUSTYCZNA_ZWROTNICA.md", "02_TOP_WARIANTY_AKUSTYCZNE.md"],
        "slot4max": ["SLOT4_SHORTLIST_VALIDATOR_REPORT.md", "01_CO_TERAZ_ZROBIC_SLOT4MAX.md"],
        "acoustic_biquad": ["01_CO_TERAZ_ZROBIC_ACOUSTIC_BIQUAD.md", "02_WARIANTY_REFERENCE_SAFE_KICK_CLEAN.md", "07_KALIBRACJA_REALNYMI_POMIARAMI.md"],
        "time_domain": ["01_WERDYKT_90BW48_FAST.md", "05_JAKOSC_DANYCH_I_METODA.md"],
    }.get(mode.key, [])
    for name in candidates:
        p = out_dir / name
        if p.exists():
            return p
    return None


class REWDOLauncher(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("REWDO v20.31 — SAFETY HOTFIX / GAINSTATE")
        self.geometry("1180x860")
        self.minsize(940, 680)

        self.proc: Optional[subprocess.Popen] = None
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.last_out_dir: Optional[Path] = None
        self.last_mode: Optional[Mode] = None

        # v20.07: live progress/heartbeat state. Long ranking runs may produce
        # no stdout for many minutes; without this GUI looked frozen even though
        # the worker process was alive.
        self.run_started_at: Optional[float] = None
        self.last_output_at: Optional[float] = None
        self.last_heartbeat_at: float = 0.0
        self.live_log_path: Optional[Path] = None
        self.last_file_snapshot: str = ""

        self.mode_var = tk.StringVar(value=MODES[0].label)
        self.mdat_var = tk.StringVar()
        self.bridge_var = tk.StringVar()
        self.api_url_var = tk.StringVar(value=DEFAULT_REW_API_URL)
        self.out_var = tk.StringVar()
        self.reference_var = tk.StringVar(value="90_BW48_slot2")
        self.user_reference_var = tk.StringVar(value=str(_default_reference_90()))
        self.workers_var = tk.IntVar(value=1)
        self.extra_args_var = tk.StringVar()
        self.open_after_var = tk.BooleanVar(value=True)
        self.allpass_var = tk.BooleanVar(value=False)
        self.cut_only_var = tk.BooleanVar(value=True)
        self.raw_sub_trim_var = tk.DoubleVar(value=-6.0)
        self.spatial_mdat_var = tk.StringVar()
        self.empirical_calibration_var = tk.BooleanVar(value=False)
        self.delay_refinement_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Gotowe.")
        self.elapsed_var = tk.StringVar(value="")
        self._log_expanded = False

        self.verify_vars = {
            "l_main": tk.StringVar(value="30.00"),
            "r_main": tk.StringVar(value="30.00"),
            "l_sub": tk.StringVar(value="20.40"),
            "r_sub": tk.StringVar(value="19.10"),
            "xo": tk.StringVar(value="90"),
            "filter": tk.StringVar(value="BW48"),
            "l_pol": tk.StringVar(value="OFF"),
            "r_pol": tk.StringVar(value="OFF"),
            "ranking_csv": tk.StringVar(),
            "save_ref": tk.BooleanVar(value=False),
        }

        self._build_ui()
        self._on_mode_change()
        self.after(100, self._poll_log)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        # v20.29: pionowy podział okna. Górny panel ustawień jest przewijany,
        # a dolny panel logu ma gwarantowaną wysokość i regulowany separator.
        # Dzięki temu log nie zapada się przy skalowaniu Windows ani w trybie 9.
        main_pane = tk.PanedWindow(
            root,
            orient=tk.VERTICAL,
            sashwidth=8,
            sashrelief="raised",
            borderwidth=0,
            background="#C9CDD3",
        )
        main_pane.pack(fill="both", expand=True)
        self.main_pane = main_pane

        settings_shell = ttk.Frame(main_pane)
        log_shell = ttk.Frame(main_pane)
        main_pane.add(settings_shell, minsize=330, stretch="always")
        main_pane.add(log_shell, minsize=190, stretch="always")

        settings_canvas = tk.Canvas(settings_shell, highlightthickness=0, borderwidth=0)
        settings_scroll = ttk.Scrollbar(settings_shell, orient="vertical", command=settings_canvas.yview)
        settings_canvas.configure(yscrollcommand=settings_scroll.set)
        settings_canvas.pack(side="left", fill="both", expand=True)
        settings_scroll.pack(side="right", fill="y")

        controls = ttk.Frame(settings_canvas, padding=(0, 0, 4, 0))
        controls.columnconfigure(0, weight=1)
        controls_window = settings_canvas.create_window((0, 0), window=controls, anchor="nw")

        def _refresh_settings_scroll(_event=None):
            try:
                settings_canvas.configure(scrollregion=settings_canvas.bbox("all"))
            except Exception:
                pass

        def _fit_settings_width(event=None):
            try:
                width = event.width if event is not None else settings_canvas.winfo_width()
                settings_canvas.itemconfigure(controls_window, width=max(1, width))
                _refresh_settings_scroll()
            except Exception:
                pass

        def _settings_mousewheel(event):
            try:
                if getattr(event, "num", None) == 4:
                    settings_canvas.yview_scroll(-3, "units")
                elif getattr(event, "num", None) == 5:
                    settings_canvas.yview_scroll(3, "units")
                else:
                    delta = getattr(event, "delta", 0)
                    if delta:
                        settings_canvas.yview_scroll(int(-delta / 120) * 3, "units")
            except Exception:
                pass

        def _bind_settings_wheel(_event=None):
            settings_canvas.bind_all("<MouseWheel>", _settings_mousewheel)
            settings_canvas.bind_all("<Button-4>", _settings_mousewheel)
            settings_canvas.bind_all("<Button-5>", _settings_mousewheel)

        def _unbind_settings_wheel(_event=None):
            settings_canvas.unbind_all("<MouseWheel>")
            settings_canvas.unbind_all("<Button-4>")
            settings_canvas.unbind_all("<Button-5>")

        controls.bind("<Configure>", _refresh_settings_scroll)
        settings_canvas.bind("<Configure>", _fit_settings_width)
        settings_canvas.bind("<Enter>", _bind_settings_wheel)
        settings_canvas.bind("<Leave>", _unbind_settings_wheel)

        # Tryb
        mode_frame = ttk.LabelFrame(controls, text="Tryb analizy")
        mode_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        mode_frame.columnconfigure(1, weight=1)

        ttk.Label(mode_frame, text="Tryb:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        mode_box = ttk.Combobox(mode_frame, textvariable=self.mode_var, values=[m.label for m in MODES], state="readonly")
        mode_box.grid(row=0, column=1, sticky="ew", padx=8, pady=6)
        mode_box.bind("<<ComboboxSelected>>", lambda _e: self._on_mode_change())

        self.desc_label = ttk.Label(mode_frame, text="", wraplength=980)
        self.desc_label.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))

        # Plik / folder
        io_frame = ttk.LabelFrame(controls, text="Pliki")
        io_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        io_frame.columnconfigure(1, weight=1)

        self.mdat_label = ttk.Label(io_frame, text="Dane REW:")
        self.mdat_label.grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self.mdat_entry = ttk.Entry(io_frame, textvariable=self.mdat_var)
        self.mdat_entry.grid(row=0, column=1, sticky="ew", padx=8, pady=6)
        input_buttons = ttk.Frame(io_frame)
        input_buttons.grid(row=0, column=2, sticky="ew", padx=8, pady=6)
        self.mdat_button = ttk.Button(input_buttons, text="MDAT offline…", command=self._browse_mdat)
        self.mdat_button.pack(side="left", fill="x", expand=True)
        self.api_button = ttk.Button(input_buttons, text="REW API…", command=self._open_rew_api)
        self.api_button.pack(side="left", fill="x", expand=True, padx=(6, 0))
        ttk.Label(
            io_frame,
            text="Tryb offline nadal używa pliku .mdat. Przycisk REW API pobiera wybrane pomiary z uruchomionego REW 5.40 do lokalnej migawki .rewapi.json.",
            wraplength=900,
        ).grid(row=4, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 7))

        self.bridge_label = ttk.Label(io_frame, text="Gain Bridge MDAT:")
        self.bridge_label.grid(row=1, column=0, sticky="w", padx=8, pady=6)
        self.bridge_entry = ttk.Entry(io_frame, textvariable=self.bridge_var)
        self.bridge_entry.grid(row=1, column=1, sticky="ew", padx=8, pady=6)
        self.bridge_button = ttk.Button(io_frame, text="Wybierz bridge MDAT…", command=self._browse_bridge)
        self.bridge_button.grid(row=1, column=2, sticky="ew", padx=8, pady=6)

        ttk.Label(io_frame, text="Folder wyników:").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(io_frame, textvariable=self.out_var).grid(row=2, column=1, sticky="ew", padx=8, pady=6)
        ttk.Button(io_frame, text="Wybierz folder…", command=self._browse_out).grid(row=2, column=2, sticky="ew", padx=8, pady=6)

        ttk.Label(io_frame, text="Realna referencja SHD 90:").grid(row=3, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(io_frame, textvariable=self.user_reference_var).grid(row=3, column=1, sticky="ew", padx=8, pady=6)
        ref_buttons = ttk.Frame(io_frame)
        ref_buttons.grid(row=3, column=2, sticky="ew", padx=8, pady=6)
        ttk.Button(ref_buttons, text="Wybierz YAML…", command=self._browse_user_reference).pack(side="left", fill="x", expand=True)
        ttk.Button(ref_buttons, text="Domyślna 90", command=self._reset_user_reference).pack(side="left", padx=(6, 0))

        # Opcje
        opt_frame = ttk.LabelFrame(controls, text="Opcje")
        opt_frame.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        for col in range(6):
            opt_frame.columnconfigure(col, weight=1 if col in {1, 3, 5} else 0)

        ttk.Label(opt_frame, text="Nazwa referencji / POST-DIRAC:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(opt_frame, textvariable=self.reference_var, width=22).grid(row=0, column=1, sticky="ew", padx=8, pady=6)

        ttk.Label(opt_frame, text="Workery:").grid(row=0, column=2, sticky="w", padx=8, pady=6)
        ttk.Spinbox(opt_frame, from_=1, to=16, textvariable=self.workers_var, width=6).grid(row=0, column=3, sticky="w", padx=8, pady=6)

        ttk.Checkbutton(opt_frame, text="Otwórz wyniki po zakończeniu", variable=self.open_after_var).grid(row=0, column=4, columnspan=2, sticky="w", padx=8, pady=6)

        ttk.Label(opt_frame, text="Dodatkowe argumenty CLI:").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(opt_frame, textvariable=self.extra_args_var).grid(row=1, column=1, columnspan=5, sticky="ew", padx=8, pady=6)
        self.allpass_check = ttk.Checkbutton(
            opt_frame,
            text="Tryb 9: oblicz all-pass OFF/ON; tylko zalecany AP trafia do ALLPASS_ON, odrzucony zostaje w diagnostyce",
            variable=self.allpass_var,
        )
        self.allpass_check.grid(row=2, column=0, columnspan=6, sticky="w", padx=8, pady=6)

        self.cut_only_check = ttk.Checkbutton(
            opt_frame,
            text="Tryb 9: dodatkowo oceniaj możliwość korekcji głównie przez tłumienie (diagnostyka tonalna)",
            variable=self.cut_only_var,
        )
        self.cut_only_check.grid(row=3, column=0, columnspan=4, sticky="w", padx=8, pady=6)
        ttk.Label(opt_frame, text="Metadane: tłumienie subów podczas RAW [dB]:").grid(row=3, column=4, sticky="e", padx=8, pady=6)
        self.raw_sub_trim_spin = ttk.Spinbox(
            opt_frame, from_=-30.0, to=6.0, increment=0.5, textvariable=self.raw_sub_trim_var, width=7
        )
        self.raw_sub_trim_spin.grid(row=3, column=5, sticky="w", padx=8, pady=6)

        ttk.Label(opt_frame, text="Dodatkowe RAW z innych pozycji:").grid(row=4, column=0, sticky="w", padx=8, pady=6)
        self.spatial_mdat_entry = ttk.Entry(opt_frame, textvariable=self.spatial_mdat_var)
        self.spatial_mdat_entry.grid(row=4, column=1, columnspan=4, sticky="ew", padx=8, pady=6)
        self.spatial_mdat_button = ttk.Button(opt_frame, text="Wybierz pliki…", command=self._browse_spatial_mdat)
        self.spatial_mdat_button.grid(row=4, column=5, sticky="ew", padx=8, pady=6)

        self.empirical_calibration_check = ttk.Checkbutton(
            opt_frame,
            text="Tryb 9: zastosuj ostrożną korektę empiryczną wyniku (obecna pewność: NISKA)",
            variable=self.empirical_calibration_var,
        )
        self.empirical_calibration_check.grid(row=5, column=0, columnspan=3, sticky="w", padx=8, pady=6)
        self.delay_refinement_check = ttk.Checkbutton(
            opt_frame,
            text="Tryb 9: dokładnie dostrój opóźnienia L/P niezależnie od kalibracji wyniku",
            variable=self.delay_refinement_var,
        )
        self.delay_refinement_check.grid(row=5, column=3, columnspan=3, sticky="w", padx=8, pady=6)

        # Verify panel
        self.verify_frame = ttk.LabelFrame(controls, text="Verify User Slot SHD — delaye z konsoli miniDSP")
        self.verify_frame.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        for col in range(8):
            self.verify_frame.columnconfigure(col, weight=1 if col in {1, 3, 5, 7} else 0)

        fields = [
            ("L main", "l_main"), ("R main", "r_main"), ("L sub", "l_sub"), ("R sub", "r_sub"),
            ("XO", "xo"), ("Filtr", "filter"), ("L pol", "l_pol"), ("R pol", "r_pol"),
        ]
        for i, (label, key) in enumerate(fields):
            r = i // 4
            c = (i % 4) * 2
            ttk.Label(self.verify_frame, text=label + ":").grid(row=r, column=c, sticky="w", padx=8, pady=5)
            ttk.Entry(self.verify_frame, textvariable=self.verify_vars[key], width=10).grid(row=r, column=c + 1, sticky="ew", padx=8, pady=5)

        ttk.Label(self.verify_frame, text="Ranking CSV:").grid(row=2, column=0, sticky="w", padx=8, pady=5)
        ttk.Entry(self.verify_frame, textvariable=self.verify_vars["ranking_csv"]).grid(row=2, column=1, columnspan=5, sticky="ew", padx=8, pady=5)
        ttk.Button(self.verify_frame, text="Wybierz CSV…", command=self._browse_ranking_csv).grid(row=2, column=6, sticky="ew", padx=8, pady=5)
        ttk.Checkbutton(self.verify_frame, text="Zapisz do wybranego pliku referencji", variable=self.verify_vars["save_ref"]).grid(row=2, column=7, sticky="w", padx=8, pady=5)

        # Status/progress
        status_frame = ttk.LabelFrame(controls, text="Status")
        status_frame.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        status_frame.columnconfigure(0, weight=1)

        self.status_label = ttk.Label(status_frame, textvariable=self.status_var, wraplength=980)
        self.status_label.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 2))
        self.elapsed_label = ttk.Label(status_frame, textvariable=self.elapsed_var)
        self.elapsed_label.grid(row=0, column=1, sticky="e", padx=8, pady=(6, 2))
        self.progress = ttk.Progressbar(status_frame, mode="indeterminate")
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(2, 8))

        # Dolny panel: przyciski i log są zawsze widoczne. Separator nad panelem
        # można przeciągać myszą, a przycisk pozwala szybko powiększyć log.
        btn_frame = ttk.Frame(log_shell)
        btn_frame.pack(fill="x", pady=(0, 6))
        btn_frame.columnconfigure(0, weight=1)

        ttk.Label(
            btn_frame,
            text="Przeciągnij poziomy separator, aby zmienić wysokość logu.",
            foreground="#555555",
        ).grid(row=0, column=0, sticky="w", padx=(4, 8), pady=3)
        self.run_button = ttk.Button(btn_frame, text="Uruchom analizę", command=self._start)
        self.run_button.grid(row=0, column=1, padx=5, pady=3)
        self.stop_button = ttk.Button(btn_frame, text="Przerwij", command=self._stop, state="disabled")
        self.stop_button.grid(row=0, column=2, padx=5, pady=3)
        ttk.Button(btn_frame, text="Otwórz folder wyników", command=self._open_last_out).grid(row=0, column=3, padx=5, pady=3)
        ttk.Button(btn_frame, text="Otwórz główny raport", command=self._open_main_report).grid(row=0, column=4, padx=5, pady=3)
        ttk.Button(btn_frame, text="Wyczyść log", command=self._clear_log).grid(row=0, column=5, padx=5, pady=3)
        self.log_size_button = ttk.Button(btn_frame, text="Powiększ log", command=self._toggle_log_pane)
        self.log_size_button.grid(row=0, column=6, padx=(5, 2), pady=3)

        log_frame = ttk.LabelFrame(log_shell, text="Log — zawsze widoczny")
        log_frame.pack(fill="both", expand=True)
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log_text = tk.Text(
            log_frame,
            wrap="word",
            height=10,
            state="disabled",
            font=_default_log_font(),
            relief="solid",
            borderwidth=1,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

        # Ustaw początkowo około 260 px dla logu. Robimy to po zmapowaniu okna,
        # ponieważ wcześniej PanedWindow nie zna jeszcze swojej wysokości.
        self.after(180, lambda: self._set_log_pane_height(260))

    def _set_log_pane_height(self, pixels: int) -> None:
        try:
            pane = self.main_pane
            self.update_idletasks()
            total = max(1, pane.winfo_height())
            upper_min = 330
            target_y = max(upper_min, total - max(190, int(pixels)))
            pane.sash_place(0, 0, target_y)
        except Exception:
            pass

    def _toggle_log_pane(self) -> None:
        self._log_expanded = not self._log_expanded
        if self._log_expanded:
            self._set_log_pane_height(430)
            self.log_size_button.configure(text="Zmniejsz log")
        else:
            self._set_log_pane_height(260)
            self.log_size_button.configure(text="Powiększ log")

    def _selected_mode(self) -> Mode:
        return MODE_BY_LABEL.get(self.mode_var.get(), MODES[0])

    def _default_out_dir(self, mode: Mode) -> Path:
        return APP_DIR / mode.output_dir

    def _on_mode_change(self) -> None:
        mode = self._selected_mode()
        self.desc_label.configure(text=mode.description)

        if not self.out_var.get().strip() or self.last_mode is None or Path(self.out_var.get()).name == self.last_mode.output_dir:
            self.out_var.set(str(self._default_out_dir(mode)))

        mdat_state = "normal" if mode.needs_mdat else "disabled"
        self.mdat_entry.configure(state=mdat_state)
        self.mdat_button.configure(state=mdat_state)
        self.api_button.configure(state="normal" if mode.needs_mdat and not mode.needs_bridge else "disabled")
        bridge_state = "normal" if mode.needs_bridge else "disabled"
        self.bridge_entry.configure(state=bridge_state)
        self.bridge_button.configure(state=bridge_state)

        # Opcje projektanta biquadów są dostępne wyłącznie w trybie 9.
        if mode.needs_bridge:
            self.mdat_label.configure(text="Pełny RAW MDAT:")
            self.bridge_label.configure(text="Gain Bridge MDAT:")
        elif mode.key == "time_domain":
            self.mdat_label.configure(text="Migawka REW API z impulsem:")
            if self.reference_var.get().strip() in {"", "90_BW48_slot2"}:
                self.reference_var.set("REF_SLOT2_90BW48_G0")
        else:
            self.mdat_label.configure(text="Dane REW:")
            if self.reference_var.get().strip() in {"90BW48_SLOT2_BASELINE", "REF_SLOT2_90BW48_G0"}:
                self.reference_var.set("90_BW48_slot2")

        mode9_state = "normal" if mode.key == "acoustic_biquad" else "disabled"
        self.allpass_check.configure(state=mode9_state)
        self.cut_only_check.configure(state=mode9_state)
        self.raw_sub_trim_spin.configure(state=mode9_state)
        self.spatial_mdat_entry.configure(state=mode9_state)
        self.spatial_mdat_button.configure(state=mode9_state)
        self.empirical_calibration_check.configure(state=mode9_state)
        self.delay_refinement_check.configure(state=mode9_state)

        # Verify frame aktywny tylko dla trybu 4
        verify_state = "normal" if mode.verify_mode else "disabled"
        for child in self.verify_frame.winfo_children():
            try:
                child.configure(state=verify_state)
            except Exception:
                pass

        self.last_mode = mode

    def _browse_mdat(self) -> None:
        initial = str(Path(self.mdat_var.get()).parent) if self.mdat_var.get() else str(APP_DIR)
        path = filedialog.askopenfilename(
            title="Wybierz plik REW .mdat lub migawkę API",
            initialdir=initial,
            filetypes=[("Dane REWDO", "*.mdat *.rewapi.json"), ("REW MDAT", "*.mdat"), ("Migawka REW API", "*.rewapi.json"), ("Wszystkie pliki", "*.*")],
        )
        if path:
            self.mdat_var.set(path)

    def _browse_bridge(self) -> None:
        initial = str(Path(self.bridge_var.get()).parent) if self.bridge_var.get() else str(APP_DIR)
        path = filedialog.askopenfilename(
            title="Wybierz plik Gain Bridge .mdat",
            initialdir=initial,
            filetypes=[("REW MDAT", "*.mdat"), ("Wszystkie pliki", "*.*")],
        )
        if path:
            self.bridge_var.set(path)

    def _browse_spatial_mdat(self) -> None:
        initial = str(Path(self.mdat_var.get()).parent) if self.mdat_var.get() else str(APP_DIR)
        paths = filedialog.askopenfilenames(
            title="Wybierz dodatkowe czyste RAW z innych pozycji",
            initialdir=initial,
            filetypes=[("REW MDAT", "*.mdat"), ("Wszystkie pliki", "*.*")],
        )
        if paths:
            self.spatial_mdat_var.set(" | ".join(paths))

    def _open_rew_api(self) -> None:
        """Otwórz bezpieczne, odczytowe połączenie z lokalnym REW 5.40."""
        current = self.mdat_var.get().strip()
        initial_dir = Path(current).parent if current else APP_DIR

        def use_snapshot(path: Path) -> None:
            self.mdat_var.set(str(path))
            try:
                self.api_url_var.set(dialog.url_var.get())
            except Exception:
                pass
            self._log(f"REW API: ustawiono migawkę wejściową {path}")
            self.status_var.set("Migawka REW API gotowa. Można uruchomić wybrany tryb tak samo jak dla MDAT.")

        time_mode = self._selected_mode().key == "time_domain"
        dialog = REWApiDialog(
            self,
            initial_url=self.api_url_var.get(),
            initial_dir=initial_dir,
            on_snapshot=use_snapshot,
            time_analysis_mode=time_mode,
            include_group_delay_default=True,
            include_impulse_default=time_mode,
        )
        dialog.grab_set()

    def _browse_out(self) -> None:
        initial = self.out_var.get() or str(APP_DIR)
        path = filedialog.askdirectory(title="Wybierz folder wyników", initialdir=initial)
        if path:
            self.out_var.set(path)

    def _browse_user_reference(self) -> None:
        current = _resolve_user_reference_path(self.user_reference_var.get())
        initial = str(current.parent) if current.parent.exists() else str(APP_DIR)
        path = filedialog.askopenfilename(
            title="Wybierz REALNĄ referencję SHD 90 BW48 YAML",
            initialdir=initial,
            filetypes=[("Referencja YAML", "*.yaml *.yml"), ("Wszystkie pliki", "*.*")],
        )
        if path:
            candidate = _resolve_user_reference_path(path)
            ok, reason = _validate_real_reference_90(candidate)
            if not ok:
                messagebox.showerror(
                    "To nie jest realna referencja SHD 90",
                    reason + "\n\nUżyj przycisku 'Domyślna 90' albo uruchom tryb 4 — Verify User Slot SHD i zapisz realną referencję 90."
                )
                return
            self.user_reference_var.set(str(candidate))

    def _reset_user_reference(self) -> None:
        self.user_reference_var.set(str(_default_reference_90()))

    def _browse_ranking_csv(self) -> None:
        initial = str(APP_DIR)
        path = filedialog.askopenfilename(
            title="Wybierz RANKING_CROSSOVEROW_RAW.csv",
            initialdir=initial,
            filetypes=[("CSV", "*.csv"), ("Wszystkie pliki", "*.*")],
        )
        if path:
            self.verify_vars["ranking_csv"].set(path)

    def _log(self, text: str) -> None:
        text = _repair_log_text(text)
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        if not text.endswith("\n"):
            self.log_text.insert("end", "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

        # v20.07: persistent live log. This is useful when the GUI is closed,
        # Windows buffers stdout, or the user needs to send diagnostics.
        try:
            if self.live_log_path is not None:
                with open(self.live_log_path, "a", encoding="utf-8", errors="replace") as f:
                    f.write(text)
                    if not text.endswith("\n"):
                        f.write("\n")
        except Exception:
            pass

    @staticmethod
    def _fmt_elapsed(seconds: float) -> str:
        seconds = max(0, int(seconds))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h:d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def _snapshot_out_dir(self) -> str:
        out = self.last_out_dir
        if not out or not out.exists():
            return "brak folderu wyników"
        try:
            files = [p for p in out.iterdir() if p.is_file()]
            interesting = [p for p in files if p.suffix.lower() in {".md", ".csv", ".txt", ".log"}]
            if not interesting:
                return "brak plików wynikowych"
            newest = max(interesting, key=lambda x: x.stat().st_mtime)
            return f"plików: {len(interesting)}, ostatni: {newest.name}, {time.strftime('%H:%M:%S', time.localtime(newest.stat().st_mtime))}"
        except Exception as exc:
            return f"nie udało się odczytać folderu: {exc}"

    def _write_live_header(self, mode: Mode, out_dir: Path, cmd: List[str]) -> None:
        try:
            self.live_log_path = out_dir / "REWDO_GUI_LIVE_RUN.log"
            header = [
                "=" * 80,
                "REWDO v20.31 — dziennik uruchomienia",
                time.strftime("%Y-%m-%d %H:%M:%S"),
                f"TRYB: {mode.label}",
                f"OUT:  {out_dir}",
                "CMD:  " + " ".join([f'"{c}"' if " " in c else c for c in cmd]),
                "=" * 80,
                "",
            ]
            self.live_log_path.write_text("\n".join(header), encoding="utf-8", errors="replace")
        except Exception:
            self.live_log_path = None

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _start(self) -> None:
        if self.proc is not None:
            messagebox.showinfo("Proces działa", "Analiza już trwa.")
            return

        mode = self._selected_mode()
        try:
            out_dir = Path(self.out_var.get()).expanduser().resolve()
            out_dir.mkdir(parents=True, exist_ok=True)
            mdat = Path(self.mdat_var.get()).expanduser().resolve() if mode.needs_mdat else Path()
            if mode.needs_mdat and not mdat.exists():
                messagebox.showerror("Brak danych", "Wybierz istniejący plik .mdat albo utwórz migawkę przez przycisk REW API.")
                return
            bridge = Path(self.bridge_var.get()).expanduser().resolve() if mode.needs_bridge else None
            if mode.needs_bridge and (bridge is None or not bridge.exists()):
                messagebox.showerror("Brak Gain Bridge", "Ten tryb wymaga istniejącego pliku Gain Bridge .mdat z L_SUM_SUB0 i R_SUM_SUB0.")
                return
            if mode.needs_bridge and (mdat.suffix.lower() != ".mdat" or bridge.suffix.lower() != ".mdat"):
                messagebox.showerror("Wymagane pliki MDAT", "Wybrany tryb wymaga pełnego RAW i osobnego Gain Bridge w formacie REW .mdat.")
                return
            if mode.key == "time_domain" and not is_rew_api_snapshot(mdat):
                messagebox.showerror(
                    "Wymagana migawka z impulsem",
                    "Tryb 10 wymaga pliku *.rewapi.json utworzonego przez REW API z zaznaczonymi opcjami Pobierz group delay i Pobierz impuls.",
                )
                return

            user_reference_path = _resolve_user_reference_path(self.user_reference_var.get())
            if _reference_required(mode):
                if not user_reference_path.exists():
                    messagebox.showerror(
                        "Brak pliku referencji",
                        "Wybrany plik referencji SHD nie istnieje:\n"
                        f"{user_reference_path}\n\n"
                        "Wybierz istniejący plik YAML albo użyj trybu 4, żeby zapisać realną referencję."
                    )
                    return
                ok, reason = _validate_real_reference_90(user_reference_path)
                if not ok:
                    messagebox.showerror(
                        "Błędny plik realnej referencji SHD 90",
                        reason + "\n\nW tym polu musi być USER_REFERENCE_90BW48.yaml z realnymi delayami main/sub. "
                        "Plik USER_REFERENCE_102BW48.yaml jest tylko profilem kandydata odsłuchowego i nie ma realnych delayów."
                    )
                    return
            if mode.verify_mode and self.verify_vars["save_ref"].get():
                try:
                    user_reference_path.parent.mkdir(parents=True, exist_ok=True)
                except Exception as exc:
                    messagebox.showerror("Błąd referencji", f"Nie mogę przygotować folderu referencji:\n{exc}")
                    return

            verify_values = {k: (v.get() if hasattr(v, "get") else str(v)) for k, v in self.verify_vars.items()}
            verify_values["save_ref"] = str(bool(self.verify_vars["save_ref"].get()))

            cmd = build_command(
                mode=mode,
                mdat_path=mdat,
                out_dir=out_dir,
                reference=self.reference_var.get(),
                user_reference_path=user_reference_path,
                workers=int(self.workers_var.get()),
                extra_args=self.extra_args_var.get(),
                verify_values=verify_values,
                bridge_path=bridge,
                allpass_enabled=bool(self.allpass_var.get()) if mode.key == "acoustic_biquad" else False,
                cut_only_enabled=bool(self.cut_only_var.get()) if mode.key == "acoustic_biquad" else False,
                raw_sub_trim_db=float(self.raw_sub_trim_var.get()) if mode.key == "acoustic_biquad" else -6.0,
                spatial_mdat_paths=[x.strip() for x in self.spatial_mdat_var.get().split("|") if x.strip()] if mode.key == "acoustic_biquad" else [],
                empirical_calibration_enabled=bool(self.empirical_calibration_var.get()) if mode.key == "acoustic_biquad" else True,
                delay_refinement_enabled=bool(self.delay_refinement_var.get()) if mode.key == "acoustic_biquad" else True,
            )
        except Exception as exc:
            messagebox.showerror("Błąd konfiguracji", str(exc))
            return

        self.last_out_dir = out_dir
        self.last_mode = mode
        self.run_started_at = time.time()
        self.last_output_at = self.run_started_at
        self.last_heartbeat_at = 0.0
        self.last_file_snapshot = ""
        self._write_live_header(mode, out_dir, cmd)

        self._log("\n" + "=" * 80)
        self._log("TRYB: " + mode.label)
        self._log("OUT:  " + str(out_dir))
        if mode.needs_mdat:
            source_kind = "REW 5.40 API snapshot" if is_rew_api_snapshot(mdat) else "MDAT offline"
            self._log("ŹRÓDŁO: " + source_kind + " — " + str(mdat))
        if mode.needs_bridge and bridge is not None:
            self._log("GAIN BRIDGE: " + str(bridge))
        selected_ref = _resolve_user_reference_path(self.user_reference_var.get())
        self._log("REF SHD 90: " + str(selected_ref) + (" [OK]" if selected_ref.exists() else " [BRAK / zostanie utworzony tylko przy zapisie]"))
        self._log("LIVE LOG: " + (str(self.live_log_path) if self.live_log_path else "[niedostępny]"))
        self._log("CMD:  " + " ".join([f'"{c}"' if " " in c else c for c in cmd]))
        self._log("=" * 80 + "\n")

        self.status_var.set("Analiza uruchomiona. Czekam na output procesu…")
        self.elapsed_var.set("00:00")
        try:
            self.progress.start(12)
        except Exception:
            pass
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

        t = threading.Thread(target=self._worker, args=(cmd, out_dir, mode), daemon=True)
        t.start()

    def _worker(self, cmd: List[str], out_dir: Path, mode: Mode) -> None:
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONUNBUFFERED", "1")
        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(APP_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                bufsize=0,
                creationflags=_subprocess_creationflags(),
            )
            self.log_queue.put(f"[INFO] Proces uruchomiony. PID={self.proc.pid}\n")
            assert self.proc.stdout is not None
            for raw_line in iter(self.proc.stdout.readline, b""):
                self.last_output_at = time.time()
                self.log_queue.put(_decode_process_output(raw_line))
            rc = self.proc.wait()
            self.log_queue.put(f"\n[ZAKOŃCZONO] Kod wyjścia: {rc}\n")
            if rc == 0 and self.open_after_var.get():
                self.log_queue.put("[INFO] Otwieram folder wyników.\n")
                self.after(100, lambda: _open_path(out_dir))
            elif rc != 0:
                self.log_queue.put("[BŁĄD] Analiza zakończona błędem. Sprawdź log powyżej.\n")
        except Exception as exc:
            self.log_queue.put(f"\n[BŁĄD GUI] {exc}\n")
        finally:
            self.proc = None
            self.after(0, self._process_finished)

    def _process_finished(self) -> None:
        try:
            self.progress.stop()
        except Exception:
            pass
        elapsed = ""
        if self.run_started_at is not None:
            elapsed = self._fmt_elapsed(time.time() - self.run_started_at)
        self.status_var.set(f"Zakończono. Czas: {elapsed}.")
        self.elapsed_var.set(elapsed)
        self.run_started_at = None
        self.last_output_at = None
        self.run_button.configure(state="normal")
        self.stop_button.configure(state="disabled")

    def _stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self._log("[INFO] Wysłano terminate do procesu.")
        except Exception as exc:
            self._log(f"[BŁĄD] Nie udało się przerwać: {exc}")

    def _poll_log(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.last_output_at = time.time()
                self._log(line)
        except queue.Empty:
            pass

        # v20.07: heartbeat independent of child stdout. Ranking may be CPU-bound
        # and silent for a long time; this proves the GUI has not frozen.
        if self.proc is not None and self.run_started_at is not None:
            now = time.time()
            elapsed = now - self.run_started_at
            quiet = now - (self.last_output_at or self.run_started_at)
            self.elapsed_var.set(self._fmt_elapsed(elapsed))
            self.status_var.set(
                f"Analiza działa… czas {self._fmt_elapsed(elapsed)}, "
                f"ostatni output {self._fmt_elapsed(quiet)} temu."
            )
            if quiet >= 300 and elapsed >= 300 and ("brak plików wynikowych" in self._snapshot_out_dir() or "plików: 1" in self._snapshot_out_dir()):
                # v20.07: a ranking with no child stdout and no output files for 5+ minutes is
                # not a normal "long calculation" anymore; it usually means an EXE/multiprocessing
                # child is stuck or MDAT import is taking abnormally long.
                if now - self.last_heartbeat_at >= 5.0:
                    self.status_var.set("UWAGA: brak postępu >5 min — zalecane Przerwij i uruchom RAW Check albo tryb z workers=1.")
            if now - self.last_heartbeat_at >= 5.0:
                snap = self._snapshot_out_dir()
                # Avoid repeating identical file snapshots every heartbeat; still
                # print a heartbeat every 15 s even if nothing changed.
                if snap != self.last_file_snapshot or now - self.last_heartbeat_at >= 15.0:
                    self._log(
                        f"[DZIAŁA] {time.strftime('%H:%M:%S')} | "
                        f"czas {self._fmt_elapsed(elapsed)} | "
                        f"cisza outputu {self._fmt_elapsed(quiet)} | {snap}"
                    )
                    self.last_file_snapshot = snap
                self.last_heartbeat_at = now

        self.after(200, self._poll_log)

    def _open_last_out(self) -> None:
        if self.last_out_dir and self.last_out_dir.exists():
            _open_path(self.last_out_dir)
        else:
            out = Path(self.out_var.get())
            if out.exists():
                _open_path(out)
            else:
                messagebox.showinfo("Brak wyników", "Nie ma jeszcze folderu wyników.")

    def _open_main_report(self) -> None:
        mode = self.last_mode or self._selected_mode()
        out = self.last_out_dir or Path(self.out_var.get())
        p = guess_main_report(mode, out)
        if p and p.exists():
            _open_path(p)
        else:
            messagebox.showinfo("Brak raportu", f"Nie znalazłem głównego raportu w:\n{out}")



def _crash_log_path() -> Path:
    """Where to write GUI startup/runtime crash logs.

    In ONEDIR EXE this is next to REWDO_GUI.exe, so the user can find it.
    """
    try:
        return APP_DIR / "REWDO_GUI_CRASH.log"
    except Exception:
        try:
            if getattr(sys, "frozen", False):
                return Path(sys.executable).resolve().parent / "REWDO_GUI_CRASH.log"
            return Path.cwd() / "REWDO_GUI_CRASH.log"
        except Exception:
            return Path("REWDO_GUI_CRASH.log")


def _write_crash_log(exc: BaseException, where: str = "GUI") -> Path:
    log_path = _crash_log_path()
    try:
        lines = [
            "=" * 78,
            f"REWDO v20.31 — dziennik błędu — {where}",
            time.strftime("%Y-%m-%d %H:%M:%S"),
            f"Python: {sys.version}",
            f"Executable: {sys.executable}",
            f"Frozen: {getattr(sys, 'frozen', False)}",
            f"APP_DIR: {APP_DIR}",
            "",
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            "",
        ]
        with open(log_path, "a", encoding="utf-8", errors="replace") as f:
            f.write("\n".join(lines))
    except Exception:
        pass
    return log_path


def _show_startup_crash(exc: BaseException, log_path: Path) -> None:
    """Show a visible error even for --windowed PyInstaller builds."""
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "REWDO GUI — błąd uruchamiania",
            "GUI nie wystartowało poprawnie.\n\n"
            f"Błąd: {exc}\n\n"
            f"Log zapisany tutaj:\n{log_path}\n\n"
            "Do diagnostyki użyj START_REWDO_GUI_DIAGNOSTIC.cmd z folderu builda."
        )
        root.destroy()
    except Exception:
        pass


def main() -> int:
    app = REWDOLauncher()
    app.mainloop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        log_path = _write_crash_log(exc, "startup")
        _show_startup_crash(exc, log_path)
        raise
