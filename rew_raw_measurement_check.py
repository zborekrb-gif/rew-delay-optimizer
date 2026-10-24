#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kontrola pomiaru RAW — v20.23.

Kontroluje, czy bazowy plik RAW do rankingu crossoverów został zmierzony tak,
żeby nadawał się nie tylko do integracji sub-main, ale także do późniejszej
oceny korpusu / obecności / blasku. To nie próbuje odczytać ustawień SHD z urządzenia;
weryfikuje to, co da się sprawdzić z .mdat i nazw pomiarów.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from rew_common import MdatImportedProject, infer_base_delay_from_filename, infer_main_delay_from_filename
from rew_mdat import (
    import_mdat_measurements,
    mdat_import_health_checks,
    write_mdat_health_report,
    write_mdat_numeric_import_report,
)
from rew_crossover import is_raw_xover_off_project
from rew_app_metadata import APP_VERSION
from rew_api_client import verify_measurement_state


RAW_ROLES = ["L main", "R main", "L sub", "R sub"]


@dataclass
class RawCheckItem:
    category: str
    status: str
    message: str
    recommendation: str = ""


def _md_escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _status_rank(status: str) -> int:
    return {"OK": 0, "INFO": 1, "UWAGA": 2, "RYZYKO": 3, "BŁĄD": 4}.get(str(status), 9)


def _band_available(project: MdatImportedProject, low: float, high: float) -> bool:
    """Return whether a band is practically available in the sweep.

    v20.07: REW/mdat exports often start slightly above the requested
    frequency, e.g. 20.14 Hz for a nominal 20 Hz sweep. Earlier versions
    required an exact <= 20.0 Hz start, which made the report claim that
    the 20-300 Hz band was unavailable even for a full 20 Hz-20 kHz RAW.
    Use a small tolerance at both band edges for reporting availability.
    This does not change ranking/scoring math; it only prevents false
    "brak pełnego pasma" warnings.
    """
    edge_tol_hz = 0.5
    return (
        float(project.freq_start_hz) <= float(low) + edge_tol_hz
        and float(project.freq_end_hz) >= float(high) - edge_tol_hz
    )


def _role_name_text(project: MdatImportedProject) -> str:
    return " ".join([str(project.path), *[str(v) for v in (project.role_to_name or {}).values()]]).lower()


def _delay_guess_report(project: MdatImportedProject) -> Tuple[List[RawCheckItem], Dict[str, float]]:
    items: List[RawCheckItem] = []
    delays: Dict[str, float] = {}
    main_path_delay = float(infer_main_delay_from_filename(project.path))
    if abs(main_path_delay) > 0.05:
        items.append(RawCheckItem(
            "Delaye RAW", "RYZYKO",
            f"Nazwa pliku wskazuje opóźnienie kolumn ustawione podczas pomiaru: {main_path_delay:.2f} ms.",
            "Do pomiarów bez zwrotnicy DSP (RAW) ustaw opóźnienia w SHD na 0,00 ms. Jeżeli to był świadomy pomiar kontrolny, nie używaj go jako podstawowego RAW."
        ))
    else:
        items.append(RawCheckItem("Delaye RAW", "OK", "Nazwa pliku nie sugeruje niezerowego delayu mainów.", "W RAW delaye SHD powinny być 0,00 ms."))
    delays["main_from_filename_ms"] = main_path_delay

    for role in RAW_ROLES:
        name = project.role_to_name.get(role, "")
        if "sub" in role.lower():
            d = float(infer_base_delay_from_filename(name))
            delays[f"{role}_delay_from_name_ms"] = d
            if abs(d) > 0.05:
                items.append(RawCheckItem(
                    "Delaye RAW", "RYZYKO",
                    f"Nazwa pomiaru `{name}` sugeruje delay suba {d:.2f} ms.",
                    "Dla podstawowego RAW suby też powinny mieć 0,00 ms. Nie mieszaj pomiaru kontrolnego z pomiarem bez zwrotnicy DSP używanym do obliczeń."
                ))
        elif "main" in role.lower():
            d = float(infer_main_delay_from_filename(name))
            delays[f"{role}_delay_from_name_ms"] = d
            if abs(d) > 0.05:
                items.append(RawCheckItem(
                    "Delaye RAW", "RYZYKO",
                    f"Nazwa pomiaru `{name}` sugeruje delay maina {d:.2f} ms.",
                    "Dla podstawowego RAW mainy powinny mieć 0,00 ms."
                ))
    return items, delays


def analyze_raw_project(project: MdatImportedProject) -> Tuple[List[RawCheckItem], Dict[str, object]]:
    """Zwraca listę ostrzeżeń i metadane diagnostyczne dla bazowego RAW."""
    items: List[RawCheckItem] = []
    meta: Dict[str, object] = {
        "file": Path(project.path).name,
        "freq_start_hz": float(project.freq_start_hz),
        "freq_end_hz": float(project.freq_end_hz),
        "freq_step_hz": float(project.freq_step_hz),
        "points": int(project.points),
    }

    state = (project.scan or {}).get("measurement_state", {}) if isinstance(project.scan, dict) else {}
    api_state_verified, api_state_missing = verify_measurement_state(state)
    meta["measurement_state"] = state
    meta["api_state_verified"] = api_state_verified

    errors, warnings = mdat_import_health_checks(project)
    if errors:
        for e in errors:
            items.append(RawCheckItem("Import danych REW", "BŁĄD", e, "Najpierw popraw import/nazwy pomiarów w REW."))
    else:
        items.append(RawCheckItem("Import danych REW", "OK", "Import numeryczny MDAT/API jest spójny dla L main, R main, L sub, R sub."))
    for w in warnings:
        items.append(RawCheckItem("Import danych REW", "UWAGA", w))

    for role in RAW_ROLES:
        name = project.role_to_name.get(role, "")
        if name:
            items.append(RawCheckItem("Role", "OK", f"{role}: `{name}`"))
        else:
            items.append(RawCheckItem("Role", "BŁĄD", f"Brakuje roli {role}.", "Nazwij pomiary np. RAW_L_main, RAW_R_main, RAW_L_sub, RAW_R_sub."))

    if api_state_verified:
        items.append(RawCheckItem("RAW / crossover OFF", "OK", "Migawka API zawiera jawne potwierdzenie RAW i crossover SHD OFF."))
        items.append(RawCheckItem("Dirac / PEQ", "OK", "Potwierdzono Dirac OFF i PEQ OFF podczas wszystkich zaznaczonych sweepów."))
    elif is_raw_xover_off_project(project):
        items.append(RawCheckItem("RAW / crossover OFF", "OK", "Projekt wygląda jak pomiar z wyłączoną zwrotnicą SHD (RAW)."))
    else:
        detail = ", ".join(api_state_missing) if bool((project.scan or {}).get("is_rew_api_snapshot")) else ""
        items.append(RawCheckItem(
            "RAW / crossover OFF", "RYZYKO",
            "Migawka API nie ma kompletnego potwierdzenia RAW." if detail else "Nie widzę jednoznacznego znacznika RAW/XOVER_OFF w nazwie pliku albo pomiarów.",
            ("Utwórz migawkę ponownie i potwierdź: " + detail) if detail else "Do rankingu RAW używaj nazw typu RAW_SHD_XOVER_OFF_REL120_DELAY0 oraz RAW_L_main / RAW_L_sub."
        ))

    f0, f1 = float(project.freq_start_hz), float(project.freq_end_hz)
    if f0 <= 20.5:
        items.append(RawCheckItem("Zakres pomiaru", "OK", f"Dolny zakres {f0:.1f} Hz jest dobry do subów."))
    elif f0 <= 30.0:
        items.append(RawCheckItem("Zakres pomiaru", "UWAGA", f"Pomiar zaczyna się od {f0:.1f} Hz.", "Lepiej mierzyć od 15–20 Hz."))
    else:
        items.append(RawCheckItem("Zakres pomiaru", "RYZYKO", f"Pomiar zaczyna się dopiero od {f0:.1f} Hz.", "Do subów i integracji użyj sweepu od 15–20 Hz."))

    if f1 >= 19000.0:
        items.append(RawCheckItem("Zakres pomiaru", "OK", f"Górny zakres {f1:.0f} Hz pozwala później oceniać też presence/glow i glass."))
    elif f1 >= 8000.0:
        items.append(RawCheckItem("Zakres pomiaru", "OK", f"Górny zakres {f1:.0f} Hz wystarcza do analizy do 8 kHz."))
    elif f1 >= 1000.0:
        items.append(RawCheckItem("Zakres pomiaru", "UWAGA", f"Górny zakres {f1:.0f} Hz wystarczy do basu i niższej średnicy, ale nie do blasku 1–4 kHz ani szklistości 4–8 kHz.", "Dla nowych RAW i post-Dirac mierz najlepiej 20 Hz–20 kHz, minimum do 8 kHz."))
    elif f1 >= 500.0:
        items.append(RawCheckItem("Zakres pomiaru", "UWAGA", f"Górny zakres {f1:.0f} Hz wystarczy do wyboru XO/delay, ale nie do oceny korpusu/obecności/blasku."))
    else:
        items.append(RawCheckItem("Zakres pomiaru", "RYZYKO", f"Górny zakres {f1:.0f} Hz jest zbyt niski dla pełnej kontroli RAW.", "Mierz co najmniej do 500–1000 Hz, a najlepiej do 20 kHz."))

    bands = [
        ("Sub/XO 20–300 Hz", 20.0, 300.0, "integracja sub-main, czas, sumowanie"),
        ("Korpus 125–220 Hz", 125.0, 220.0, "podparcie ciała średnicy"),
        ("Lower mid 300–1000 Hz", 300.0, 1000.0, "rdzeń średnicy / punkt odniesienia"),
        ("Obecność i blask 1–4 kHz", 1000.0, 4000.0, "blask, obecność, artykulacja"),
        ("Ryzyko szklistości 4–8 kHz", 4000.0, 8000.0, "ryzyko szklistości/natarczywości"),
    ]
    availability: Dict[str, bool] = {}
    for label, lo, hi, desc in bands:
        ok = _band_available(project, lo, hi)
        availability[label] = ok
        items.append(RawCheckItem(
            "Dostępność pasm", "OK" if ok else "INFO",
            f"{label}: {'dostępne' if ok else 'brak pełnego pasma'} — {desc}."
        ))
    meta["band_availability"] = availability

    text = _role_name_text(project)
    if api_state_verified:
        items.append(RawCheckItem("REL / filtr analogowy", "OK", "W migawce API potwierdzono filtr analogowy REL ustawiony na 120 Hz."))
        items.append(RawCheckItem("Delaye RAW", "OK", "W migawce API potwierdzono delaye wszystkich wyjść 0,00 ms."))
        meta["main_gain_db"] = float(state.get("mainGainDb", 0.0))
        meta["sub_gain_db"] = float(state.get("subGainDb", 0.0))
    else:
        if "rel120" in text or "rel_120" in text or "rel 120" in text:
            items.append(RawCheckItem("REL / filtr analogowy", "OK", "Nazwa sugeruje REL ustawiony wysoko / 120 Hz."))
        else:
            items.append(RawCheckItem(
                "REL / filtr analogowy", "INFO",
                "Nie widzę w nazwie znacznika REL120.",
                "Jeżeli używasz REL przez low-level, do RAW ustaw filtr REL wysoko / 120 Hz i wpisz to w nazwie pliku."
            ))
        delay_items, delays = _delay_guess_report(project)
        items.extend(delay_items)
        meta.update(delays)

    inv_tokens = [" inv", "_inv", "-inv", " polarity", "polaryz"]
    if api_state_verified:
        items.append(RawCheckItem("Polaryzacja", "OK", "W migawce API potwierdzono polaryzację normalną/OFF."))
        items.append(RawCheckItem("Timing reference", "OK", "Potwierdzono tę samą metodę timing reference i nieruchomy mikrofon dla wszystkich sweepów."))
    elif any(tok in text for tok in inv_tokens):
        items.append(RawCheckItem("Polaryzacja", "RYZYKO", "Nazwa sugeruje INV/polaryzację w pomiarze RAW.", "Podstawowy RAW rób z polaryzacją normalną/OFF. INV testuj osobno w trybie diagnostycznym."))
        items.append(RawCheckItem("Timing reference", "INFO", "Z pliku nie da się potwierdzić timing reference.", "Używaj tej samej metody timingowej dla wszystkich pomiarów."))
    else:
        items.append(RawCheckItem("Polaryzacja", "OK", "Nazwa nie sugeruje odwróconej polaryzacji. Podstawowy RAW powinien być OFF/OFF."))
        items.append(RawCheckItem(
            "Timing reference", "INFO",
            "Z pliku .mdat nie da się pewnie potwierdzić, czy użyto acoustic timing reference.",
            "W REW używaj tej samej metody timingowej dla L main, R main, L sub i R sub. Bez tego faza/delay mogą być mniej wiarygodne."
        ))
    items.append(RawCheckItem(
        "Checklist przed pomiarem", "INFO",
        "Dirac OFF, crossover SHD OFF, PEQ OFF, delaye 0,00 ms, polaryzacja OFF, mikrofon nieruchomo, poziomy subów docelowe i niezmieniane między sweepami.",
        "Nazwy: RAW_000_L_main_XOVER_OFF_DIRAC_OFF_DELAY0 itd."
    ))
    return items, meta


def _overall_status(items: Iterable[RawCheckItem]) -> str:
    worst = max((_status_rank(i.status) for i in items), default=0)
    if worst >= _status_rank("BŁĄD"):
        return "BŁĄD — nie używać do rankingu"
    if worst >= _status_rank("RYZYKO"):
        return "RYZYKO — użyj tylko świadomie albo powtórz RAW"
    if worst >= _status_rank("UWAGA"):
        return "OK Z UWAGAMI"
    return "OK"


def _workflow_statuses(items: Iterable[RawCheckItem], meta: Dict[str, object]) -> Dict[str, Tuple[str, str]]:
    """v19.67: oddziela przydatność RAW do XO/delay od pełnej oceny średnicy."""
    item_list = list(items)
    has_error = any(str(i.status).upper().startswith("BŁĄD") for i in item_list)
    risky_core = any(
        str(i.status).upper().startswith("RYZYKO") and i.category in {"Import danych REW", "Role", "RAW / crossover OFF", "Delaye RAW", "Polaryzacja"}
        for i in item_list
    )
    avail = meta.get("band_availability", {}) if isinstance(meta.get("band_availability", {}), dict) else {}
    f1 = float(meta.get("freq_end_hz", 0.0) or 0.0)

    if has_error:
        xo = ("BŁĄD", "Brak kompletu ról albo import nie jest spójny — nie używaj do rankingu.")
    elif risky_core:
        xo = ("RYZYKO", "Można policzyć tylko świadomie: nazwy/delaye/polaryzacja sugerują, że to może nie być czysty RAW.")
    elif f1 >= 220.0 and float(meta.get("freq_start_hz", 999.0) or 999.0) <= 30.0:
        xo = ("OK", "Plik nadaje się do rankingu crossover/delay C1 + REL.")
    else:
        xo = ("UWAGA", "Zakres jest nietypowy; ranking XO/delay może być ograniczony.")

    if has_error:
        body = ("BŁĄD", "Najpierw popraw import RAW.")
    elif bool(avail.get("Korpus 125–220 Hz")):
        body = ("OK", "Dostępne dane do oceny fundamentu dolnej średnicy 125–220 Hz.")
    elif f1 >= 160.0:
        body = ("UWAGA", "Część pasma korpusu jest dostępna, ale zakres 125–220 Hz nie jest pełny.")
    else:
        body = ("BRAK DANYCH", "Brak pełnego pasma korpusu 125–220 Hz.")

    if bool(avail.get("Obecność i blask 1–4 kHz")):
        presence = ("OK", "Dostępne dane do presence/glow 1–4 kHz.")
    else:
        presence = ("BRAK DANYCH", "Ten RAW nie pozwala ocenić prawdziwego blasku/obecności 1–4 kHz.")

    if bool(avail.get("Ryzyko szklistości 4–8 kHz")):
        glass = ("OK", "Dostępne dane do ryzyka szklistości 4–8 kHz.")
    else:
        glass = ("BRAK DANYCH", "Ten RAW nie pozwala ocenić szklistości/natarczywości 4–8 kHz.")

    if xo[0] == "OK" and body[0] in {"OK", "UWAGA"} and presence[0] == "BRAK DANYCH":
        final = ("OK DO XO / OGRANICZONY DO ŚREDNICY", "Używaj do wyboru crossovera i delayów; nie wyciągaj z niego wniosków o pełnym blasku pianina/wokalu.")
    elif xo[0] == "OK" and presence[0] == "OK":
        final = ("OK", "Plik ma wystarczający zakres także do szerszej kontroli barwy.")
    elif xo[0] in {"RYZYKO", "UWAGA"}:
        final = (xo[0], "Ranking można traktować diagnostycznie; najlepiej potwierdzić realnym pomiarem slotów.")
    else:
        final = (xo[0], xo[1])

    return {
        "XO / delay ranking": xo,
        "Korpus 125–220 Hz": body,
        "Obecność i blask 1–4 kHz": presence,
        "Ryzyko szklistości 4–8 kHz": glass,
        "Status finalny": final,
    }


def write_raw_measurement_check_report(project: MdatImportedProject, out_dir: str | Path) -> Tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    items, meta = analyze_raw_project(project)
    md_path = out / "RAW_MEASUREMENT_CHECK.md"
    csv_path = out / "RAW_MEASUREMENT_CHECK.csv"

    status = _overall_status(items)
    workflow = _workflow_statuses(items, meta)
    final_status, final_message = workflow["Status finalny"]
    lines: List[str] = [
        f"# RAW MEASUREMENT CHECK — REWDO {APP_VERSION}",
        "",
        f"**Status ogólny:** {status}",
        f"**Status workflow:** {final_status}",
        "",
        f"- Plik: `{Path(project.path).name}`",
        f"- Zakres: **{project.freq_start_hz:.2f}–{project.freq_end_hz:.2f} Hz**",
        f"- Punkty: **{project.points}**",
        f"- Krok: **{project.freq_step_hz:.9f} Hz**",
        "",
        "## Werdykt praktyczny",
        "",
        final_message,
        "",
        "## Statusy rozbite według zastosowania",
        "",
        "| Zastosowanie | Status | Znaczenie |",
        "|---|---|---|",
    ]
    for k, (st, msg) in workflow.items():
        lines.append(f"| {_md_escape(k)} | **{_md_escape(st)}** | {_md_escape(msg)} |")
    lines.extend(["", "## Kontrola szczegółowa", ""])

    lines.extend([
        "",
        "| Kategoria | Status | Komunikat | Zalecenie |",
        "|---|---|---|---|",
    ])
    for item in sorted(items, key=lambda x: (_status_rank(x.status), x.category)):
        lines.append(f"| {_md_escape(item.category)} | **{_md_escape(item.status)}** | {_md_escape(item.message)} | {_md_escape(item.recommendation)} |")

    lines.extend([
        "",
        "## Jak mierzyć idealny RAW do tej aplikacji",
        "",
        "```text",
        "Dirac: OFF",
        "Crossover SHD: OFF / brak HPF-LPF w SHD",
        "PEQ: OFF",
        "Delaye SHD: L main 0,00 ms, R main 0,00 ms, L sub 0,00 ms, R sub 0,00 ms",
        "Polaryzacja: OFF/OFF",
        "REL: filtr wysoko / 120 Hz, poziom docelowy, bez zmian między pomiarami",
        "Przemiatanie: najlepiej 20 Hz – 20 kHz; minimum 15 Hz – 8 kHz dla oceny korpusu/obecności/blasku",
        "Timing: ta sama metoda acoustic timing reference dla wszystkich czterech pomiarów",
        "Mikrofon: jeden punkt odsłuchowy, nie ruszać między sweepami",
        "Pomiary obowiązkowe: L main, R main, L sub, R sub — każdy osobno",
        "```",
        "",
        "## Nazwy zalecane",
        "",
        "```text",
        "RAW_000_L_main_XOVER_OFF_DIRAC_OFF_DELAY0",
        "RAW_000_R_main_XOVER_OFF_DIRAC_OFF_DELAY0",
        "RAW_000_L_sub_XOVER_OFF_DIRAC_OFF_DELAY0_REL120",
        "RAW_000_R_sub_XOVER_OFF_DIRAC_OFF_DELAY0_REL120",
        "```",
        "",
        "## Po co zakres do 8 kHz albo 20 kHz",
        "",
        "- Do samego wyboru crossovera/delayu wystarczy niższy zakres, ale aplikacja ma teraz pilnować również korpusu, obecności/blasku i ryzyka szklistości.",
        "- Jeżeli pomiar kończy się poniżej 4 kHz, aplikacja nie może uczciwie ocenić blasku średnicy 1–4 kHz.",
        "- Jeżeli kończy się poniżej 8 kHz, nie oceni ryzyka szklistości/natarczywości 4–8 kHz.",
    ])
    md_path.write_text("\n".join(lines), encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["category", "status", "message", "recommendation"])
        for item in items:
            w.writerow([item.category, item.status, item.message, item.recommendation])
    return md_path, csv_path


def analyze_raw_mdat(path: str | Path, out_dir: str | Path) -> Tuple[Path, Path]:
    md_path, csv_path, _blocked = _analyze_raw_mdat_with_status(path, out_dir)
    return md_path, csv_path


def _analyze_raw_mdat_with_status(path: str | Path, out_dir: str | Path) -> Tuple[Path, Path, bool]:
    project = import_mdat_measurements(path)
    # v20.29: tryb 0 zawsze zapisuje audyt wyboru fazy, także przy statusie
    # BLOCKED. Dzięki temu użytkownik dostaje przyczynę blokady i numery
    # kandydatur bez uruchamiania rankingu.
    write_mdat_health_report(project, out_dir)
    write_mdat_numeric_import_report(project, out_dir)
    items, _meta = analyze_raw_project(project)
    blocked = any(str(item.status).upper().startswith("BŁĄD") for item in items)
    md_path, csv_path = write_raw_measurement_check_report(project, out_dir)
    return md_path, csv_path, blocked


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Kontrola pomiaru RAW — v20.31 — kontrola pomiaru z wyłączoną zwrotnicą DSP (RAW) z MDAT albo migawki REW API")
    p.add_argument("--mdat", required=True, help="Plik REW .mdat z pomiarami RAW L/R main + L/R sub")
    p.add_argument("--out", default="raw_measurement_check", help="Folder raportu")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        md_path, csv_path, blocked = _analyze_raw_mdat_with_status(args.mdat, args.out)
    except Exception as exc:
        print("BŁĄD RAW MEASUREMENT CHECK:", exc)
        print("")
        print("Wskazówka v20.31:")
        print("- jeżeli uruchamiasz starszą paczkę v19.67/v19.76/v19.69, użyj tej wersji v20.07;")
        print("- jeżeli plik nadal nie przejdzie, w REW wyeksportuj cztery pomiary jako TXT: L main, R main, L sub, R sub;")
        print("- możesz też zapisać projekt ponownie w REW jako nowy .mdat i uruchomić check jeszcze raz.")
        return 2
    print("RAW MEASUREMENT CHECK gotowy.")
    print("Raport:", md_path)
    print("CSV:", csv_path)
    if blocked:
        print("BŁĄD: raport został zapisany, ale plik nie może być użyty do rankingu.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
