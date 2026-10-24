# -*- coding: utf-8 -*-
"""Import i diagnostyka projektów REW .mdat."""
from __future__ import annotations

from rew_common import *
from rew_api_client import decode_frequency_response, is_rew_api_snapshot, read_snapshot, verify_snapshot_response_integrity
from rew_measurement_validation import (
    MeasurementValidationError,
    validate_measurement_project,
    validate_numeric_response_arrays,
)

def _java_serialized_strings_from_bytes(data: bytes, max_len: int = 4096) -> List[Tuple[int, str]]:
    """
    Lekki skaner stringów z serializacji Javy używanej przez pliki REW .mdat.

    To NIE jest pełny parser .mdat. W v19.3 służy do bezpiecznego rozpoznawania nazw pomiarów i mapowania ról.
    Numeryczny import SPL/fazy jest realizowany przez osobny lekki parser tablic float[].
    Jeżeli REW zmieni format, aplikacja ma nadal działać ze stabilnym importem TXT.
    """
    found: List[Tuple[int, str]] = []
    n = len(data)
    for i in range(0, max(0, n - 3)):
        if data[i] != 0x74:  # TC_STRING
            continue
        ln = int.from_bytes(data[i + 1 : i + 3], "big", signed=False)
        if ln <= 0 or ln > max_len or i + 3 + ln > n:
            continue
        raw = data[i + 3 : i + 3 + ln]
        try:
            s = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not s:
            continue
        # Odrzuć losowe fragmenty binarne, ale pozwól na polskie znaki i opisy REW.
        printable = sum(1 for ch in s if ch.isprintable() or ch in "\r\n\t")
        if printable / max(1, len(s)) >= 0.95:
            found.append((i, s))
    return found


def _md_escape(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def infer_mdat_measurement_role(name: str) -> str:
    """
    Heurystyczne mapowanie nazwy pomiaru z REW .mdat na rolę w aplikacji.

    Przykłady obsługiwane w v19.3:
    - 75_BW48_L_main -> L main
    - 75_BW48_L_sub_0.0 -> L sub
    - L+R_DiracOFF -> L+R Dirac OFF
    - L DiracON -> L Dirac ON
    """
    raw = str(name).strip()
    low = raw.lower()

    # v20.15 DIRAC BASS STRATEGY GUARD:
    # - P traktujemy jako prawy kanał (polskie "prawy"), tak samo jak R.
    # - Separatorami mogą być spacje, _, -, / oraz +, bo w REW często pojawia się L/P albo L+P.
    # - To jest szczególnie ważne dla trybu 8: P_MAIN / P_SUB mają mapować się na R main / R sub.
    sep_class = r"[_\s\-/\+]"
    compact = re.sub(r"[_\s\-/\+]+", "_", low)
    compact_no_sep = re.sub(r"[_\s\-/\+]+", "", low)

    lr_token = bool(re.search(r"(^|" + sep_class + r")(lr|lp)(" + sep_class + r"|$)", low))
    is_lr = (
        ("l+r" in low) or ("l + r" in low) or ("l/r" in low) or ("l / r" in low) or
        ("l+p" in low) or ("l + p" in low) or ("l/p" in low) or ("l / p" in low) or
        lr_token or
        (("lr" == compact_no_sep[:2] or "lp" == compact_no_sep[:2]) and "dirac" in low)
    )
    is_l = bool(re.search(r"(^|" + sep_class + r")l(" + sep_class + r"|$)", low))
    is_r = bool(re.search(r"(^|" + sep_class + r")(r|p)(" + sep_class + r"|$)", low))

    if "dirac" in low:
        if "diracon" in compact_no_sep or "dirac_on" in compact:
            suffix = "Dirac ON"
        elif "diracoff" in compact_no_sep or "dirac_off" in compact:
            suffix = "Dirac OFF"
        else:
            suffix = "Dirac"
        if is_lr:
            return f"L+R {suffix}"
        if is_l:
            return f"L {suffix}"
        if is_r:
            return f"R {suffix}"
        return suffix

    # Pełne pomiary stron i sumy stereo używane przez kalibrator predykcja–pomiar.
    has_strona = "strona" in low or "side" in low
    if has_strona and is_l and not is_lr:
        return "L strona"
    if has_strona and is_r and not is_lr:
        return "R strona"

    if is_lr:
        return "L+R"

    # v20.29: powtórzenia SUM1/SUM2/SUM3 są pełnoprawnymi pomiarami
    # kontrolnymi.  Poprzedni wzorzec rozpoznawał wyłącznie osobny token SUM.
    has_sum = bool(re.search(r"(^|" + sep_class + r")(sum|suma)\d*($|" + sep_class + r")", low))
    if has_sum and is_l and not is_r:
        return "L sum"
    if has_sum and is_r and not is_l:
        return "R sum"

    if re.search(r"(^|" + sep_class + r")l" + sep_class + r"+main($|" + sep_class + r")", low) or re.search(r"(^|_)l_main($|_)", compact):
        return "L main"
    if re.search(r"(^|" + sep_class + r")(r|p)" + sep_class + r"+main($|" + sep_class + r")", low) or re.search(r"(^|_)(r|p)_main($|_)", compact):
        return "R main"
    if re.search(r"(^|" + sep_class + r")l" + sep_class + r"+sub($|" + sep_class + r")", low) or re.search(r"(^|_)l_sub($|_)", compact):
        return "L sub"
    if re.search(r"(^|" + sep_class + r")(r|p)" + sep_class + r"+sub($|" + sep_class + r")", low) or re.search(r"(^|_)(r|p)_sub($|_)", compact):
        return "R sub"

    return "Nieprzypisany"


def infer_mdat_profile_from_names(names: List[str]) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Próba odczytu profilu z nazw pomiarów w .mdat, np. 75_BW48_L_main.
    """
    joined = " ".join(names)
    xo_token = r"(?:[6-9]\d|1[01]\d|120)"
    patterns = [
        rf"(?i)({xo_token})\s*[_\-\s]*(?:hz)?\s*[_\-\s]*(BW|LR)\s*(24|48)?",
        rf"(?i)({xo_token})\s*[_\-\s]*(BW|LR)(24|48)",
    ]
    for pat in patterns:
        m = re.search(pat, joined)
        if not m:
            continue
        cross = f"{int(m.group(1))} Hz"
        filt = str(m.group(2)).upper()
        slope = int(m.group(3)) if m.lastindex and m.group(3) else None
        return cross, filt, slope
    # Fallback: sam crossover bez filtra
    m = re.search(rf"(?i)({xo_token})\s*hz", joined)
    if m:
        return f"{int(m.group(1))} Hz", None, None
    return None, None, None



def _snapshot_measurement_records(path: str | Path) -> Tuple[Dict[str, object], List[Dict[str, object]]]:
    """Odczytaj rekordy migawki API i ujednolić podstawowe metadane."""
    payload = read_snapshot(path)
    records: List[Dict[str, object]] = []
    for index, raw_record in enumerate(payload.get("measurements", []), start=1):
        if not isinstance(raw_record, dict):
            continue
        summary = raw_record.get("summary", {})
        if not isinstance(summary, dict):
            summary = {}
        name = str(summary.get("title") or summary.get("name") or f"Pomiar API {index}").strip()
        role = str(raw_record.get("role") or infer_mdat_measurement_role(name)).strip() or "Nieprzypisany"
        records.append({
            "index": index,
            "name": name,
            "role": role,
            "summary": summary,
            "record": raw_record,
        })
    return payload, records


def scan_rew_api_snapshot(path: str | Path) -> Dict[str, object]:
    """Zwróć mapowanie ról dla lokalnej migawki REW 5.40 API."""
    p = Path(path)
    payload, records = _snapshot_measurement_records(p)
    measurements: List[MdatMeasurementInfo] = []
    for rec in records:
        role = str(rec["role"])
        name = str(rec["name"])
        if role == "Nieprzypisany":
            continue
        base_delay = infer_base_delay_from_filename(name) if role in ("L sub", "R sub") else None
        summary = rec.get("summary", {})
        notes = str(summary.get("notes", "")) if isinstance(summary, dict) else ""
        measurements.append(MdatMeasurementInfo(
            index=int(rec["index"]),
            name=name,
            role=role,
            position=int(rec["index"]),
            base_delay_ms=base_delay,
            notes=notes,
        ))
    names = [m.name for m in measurements]
    profile_cross, profile_filter, profile_slope = infer_mdat_profile_from_names(names)
    found_roles = {m.role for m in measurements}
    required = ["L main", "L sub", "R main", "R sub"]
    return {
        "path": str(p),
        "is_rew_mdat": False,
        "is_rew_api_snapshot": True,
        "source_type": "REW 5.40 API snapshot",
        "api_base_url": str(payload.get("apiBaseUrl", "")),
        "created_utc": str(payload.get("createdUtc", "")),
        "measurement_state": payload.get("measurementState", {}),
        "acquisition": payload.get("acquisition", {}),
        "response_integrity": payload.get("responseIntegrity", {}),
        "parity_check": payload.get("parityCheck", {}),
        "measurements": measurements,
        "profile_cross": profile_cross,
        "profile_filter": profile_filter,
        "profile_slope": profile_slope,
        "missing_required": [r for r in required if r not in found_roles],
        "found_roles": sorted(found_roles),
        "strings_count": 0,
        "file_size": p.stat().st_size,
    }


def import_rew_api_snapshot(path: str | Path, required_roles: Optional[List[str]] = None) -> MdatImportedProject:
    """Import SPL i fazy z migawki API bez wymuszania wspólnego PPO.

    Najpierw dekoduje wszystkie role, potem wybiera najgęstszą natywną oś we
    wspólnym zakresie. Dzięki temu wynik nie zależy od kolejności rekordów i nie
    wymaga wygładzania logarytmicznego po stronie REW.
    """
    p = Path(path)
    payload, records = _snapshot_measurement_records(p)
    integrity_ok, integrity_problems = verify_snapshot_response_integrity(payload)
    required_roles = list(required_roles or ["L main", "L sub", "R main", "R sub"])
    by_role: Dict[str, List[Dict[str, object]]] = {}
    for rec in records:
        by_role.setdefault(str(rec["role"]), []).append(rec)

    decoded_roles: Dict[str, Tuple[str, np.ndarray, np.ndarray, np.ndarray]] = {}
    role_to_name: Dict[str, str] = {}
    for role in required_roles:
        matches = by_role.get(role, [])
        if not matches:
            found = ", ".join(sorted(by_role)) or "brak rozpoznanych ról"
            raise ValueError(f"Brakuje pomiaru {role} w migawce REW API. Rozpoznane role: {found}.")
        if len(matches) > 1:
            names = ", ".join(str(x["name"]) for x in matches)
            raise ValueError(f"Niejednoznaczna rola {role} w migawce API: {names}.")
        rec = matches[0]
        raw_record = rec.get("record", {})
        response = raw_record.get("frequencyResponse", {}) if isinstance(raw_record, dict) else {}
        if not isinstance(response, dict):
            raise ValueError(f"Brak frequencyResponse w migawce API dla {rec['name']}.")
        decoded = decode_frequency_response(response)
        freq = np.asarray(decoded["frequency_hz"], dtype=float)
        spl = np.asarray(decoded["spl_db"], dtype=float)
        phase = np.asarray(decoded["phase_deg"], dtype=float)
        numeric = validate_numeric_response_arrays(freq, spl, phase, name=str(rec["name"]))
        if not numeric.ok:
            raise MeasurementValidationError(numeric)
        decoded_roles[role] = (str(rec["name"]), freq, spl, phase)
        role_to_name[role] = str(rec["name"])

    lo = max(float(values[1][0]) for values in decoded_roles.values())
    hi = min(float(values[1][-1]) for values in decoded_roles.values())
    if hi <= lo:
        raise ValueError("Pomiary API nie mają wspólnego zakresu częstotliwości.")
    candidates = []
    for role, (_name, freq, _spl, _phase) in decoded_roles.items():
        mask = (freq >= lo) & (freq <= hi)
        axis = freq[mask]
        if axis.size >= 16:
            spacing = float(np.nanmedian(np.diff(axis))) if axis.size > 1 else float("inf")
            candidates.append((axis.size, -spacing, role, axis))
    if not candidates:
        raise ValueError("Pomiary API mają mniej niż 16 wspólnych punktów.")
    candidates.sort(reverse=True, key=lambda item: (item[0], item[1]))
    canonical_freq = np.asarray(candidates[0][3], dtype=float)

    imported: Dict[str, Measurement] = {}
    for role, (name, freq, spl, phase) in decoded_roles.items():
        if freq.size == canonical_freq.size and np.allclose(freq, canonical_freq, rtol=1e-9, atol=1e-9):
            target_spl = spl.copy()
            target_phase = phase.copy()
        else:
            target_spl = np.interp(canonical_freq, freq, spl)
            unwrapped = np.unwrap(np.deg2rad(phase))
            target_phase = np.rad2deg(np.interp(canonical_freq, freq, unwrapped))
        imported[role] = Measurement(name, canonical_freq.copy(), target_spl, target_phase)

    scan = scan_rew_api_snapshot(p)
    step = float(np.nanmedian(np.diff(canonical_freq)))
    truth_status = "API_INTEGRITY" if integrity_ok else "BLOCKED"
    if integrity_ok:
        truth_source = "REW API frequencyResponse + kontrola integralności migawki"
    else:
        truth_source = "REW API: kontrola integralności nie przeszła — " + "; ".join(integrity_problems)
    project = MdatImportedProject(
        path=str(p), measurements_by_role=imported, role_to_name=role_to_name, scan=scan,
        freq_start_hz=float(canonical_freq[0]), freq_end_hz=float(canonical_freq[-1]),
        freq_step_hz=step, points=int(canonical_freq.size),
        phase_truth_status=truth_status,
        phase_truth_source=truth_source,
        phase_truth_diagnostics={
            "status": truth_status,
            "phase_metrics_allowed": integrity_ok,
            "source": truth_source,
            "problems": list(integrity_problems),
        },
        phase_metrics_allowed=integrity_ok,
    )
    validate_measurement_project(project)
    return project



def scan_mdat_project(path: str | Path) -> Dict[str, object]:
    """
    v19.3 MDAT Primary Workflow: rozpoznaje plik .mdat i mapuje nazwy pomiarów.
    Dane SPL/fazy wczytuje import_mdat_measurements().
    """
    p = Path(path)
    if is_rew_api_snapshot(p):
        return scan_rew_api_snapshot(p)
    data = p.read_bytes()
    is_rew_mdat = b"REW Measurement Data File" in data[:256] or b"roomeqwizard.MeasData" in data[:2_000_000]
    if not is_rew_mdat:
        raise ValueError("To nie wygląda na plik REW .mdat albo format nie jest obsługiwany przez importer v19.3.")

    strings = _java_serialized_strings_from_bytes(data)
    measurements: List[MdatMeasurementInfo] = []
    seen = set()
    for pos, value in strings:
        name = value.strip()
        if not name or name in seen:
            continue
        # v20.15: nie odrzucamy nazw typu L/P albo L+P.
        # Nadal odrzucamy oczywiste ścieżki/eksporty, żeby nie mapować nazw plików jako pomiarów.
        low_name = name.lower()
        if low_name.startswith(("excl:", "incl:")):
            continue
        slash_is_channel_label = bool(re.search(r"(?i)(^|[\s_\-])l\s*/\s*[pr]($|[\s_\-])", name))
        looks_like_file = low_name.endswith((".txt", ".wav", ".mdat", ".csv"))
        looks_like_path = "\\" in name or ("/" in name and not slash_is_channel_label)
        if looks_like_path or looks_like_file:
            continue
        role = infer_mdat_measurement_role(name)
        if role == "Nieprzypisany":
            continue
        seen.add(name)
        base_delay = None
        if role in ("L sub", "R sub"):
            base_delay = infer_base_delay_from_filename(name)
        measurements.append(
            MdatMeasurementInfo(
                index=len(measurements) + 1,
                name=name,
                role=role,
                position=pos,
                base_delay_ms=base_delay,
            )
        )

    profile_cross, profile_filter, profile_slope = infer_mdat_profile_from_names([m.name for m in measurements])
    required = ["L main", "L sub", "R main", "R sub"]
    found_roles = {m.role for m in measurements}
    missing_required = [r for r in required if r not in found_roles]

    return {
        "path": str(p),
        "is_rew_mdat": is_rew_mdat,
        "measurements": measurements,
        "profile_cross": profile_cross,
        "profile_filter": profile_filter,
        "profile_slope": profile_slope,
        "missing_required": missing_required,
        "found_roles": sorted(found_roles),
        "strings_count": len(strings),
        "file_size": p.stat().st_size,
    }


def mdat_scan_summary_text(scan: Dict[str, object]) -> str:
    measurements: List[MdatMeasurementInfo] = scan.get("measurements", [])  # type: ignore[assignment]
    profile_cross = scan.get("profile_cross")
    profile_filter = scan.get("profile_filter")
    profile_slope = scan.get("profile_slope")
    missing = scan.get("missing_required", [])

    lines = []
    source_type = str(scan.get("source_type") or ("REW MDAT" if scan.get("is_rew_mdat") else "dane REW"))
    lines.append("Import danych REW — MDAT / API v20.31")
    lines.append(f"Źródło: {source_type}")
    if scan.get("is_rew_api_snapshot"):
        integrity = scan.get("response_integrity", {})
        acquisition = scan.get("acquisition", {})
        lines.append(f"Dane API: {integrity.get('status', 'NIEZWERYFIKOWANE')} | tryb {acquisition.get('mode', '—')}")
    lines.append(f"Plik: {Path(str(scan.get('path', ''))).name}")
    lines.append(f"Rozpoznane pomiary: {len(measurements)}")
    if profile_cross or profile_filter or profile_slope:
        prof = " ".join(str(x) for x in [profile_cross, profile_filter, profile_slope] if x)
        lines.append(f"Wykryty profil: {prof}")
    if missing:
        lines.append("Brakuje do optymalizacji: " + ", ".join(str(x) for x in missing))
    else:
        lines.append("Znaleziono role wymagane do optymalizacji: L main, L sub, R/P main, R/P sub.")
    for m in measurements:
        delay_txt = "" if m.base_delay_ms is None else f", baza delay {m.base_delay_ms:.2f} ms"
        lines.append(f"- {m.role}: {m.name}{delay_txt}")
    lines.append("")
    lines.append("v19.57: wybrany .mdat jest podstawowym źródłem danych; aplikacja wczytuje SPL/fazę i uruchamia optymalizację bez ręcznych eksportów TXT. PBIS Safe / Sound Quality Safe pozostaje ostrożniejszą warstwą wyboru wariantu.")
    return "\n".join(lines)


def write_mdat_scan_report(path: str | Path, out_dir: str | Path) -> Tuple[Path, Path, Dict[str, object]]:
    scan = scan_mdat_project(path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md_path = out / "MDAT_IMPORT.md"
    csv_path = out / "mdat_measurements.csv"
    measurements: List[MdatMeasurementInfo] = scan["measurements"]  # type: ignore[assignment]

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["index", "role", "name", "base_delay_ms", "position"],
        )
        writer.writeheader()
        for m in measurements:
            writer.writerow(
                {
                    "index": m.index,
                    "role": m.role,
                    "name": m.name,
                    "base_delay_ms": "" if m.base_delay_ms is None else f"{m.base_delay_ms:.4f}",
                    "position": m.position,
                }
            )

    profile_cross = scan.get("profile_cross") or "nie wykryto"
    profile_filter = scan.get("profile_filter") or ""
    profile_slope = scan.get("profile_slope") or ""
    missing = scan.get("missing_required", [])
    status = "GOTOWE DO MAPOWANIA" if not missing else "NIEPEŁNE MAPOWANIE"

    lines = [
        "# Import danych REW — MDAT / API v20.31",
        "",
        f"**Status:** {status}",
        "",
        f"- Plik: `{Path(str(scan.get('path'))).name}`",
        f"- Źródło: **{str(scan.get('source_type') or ('REW MDAT' if scan.get('is_rew_mdat') else 'dane REW'))}**",
        f"- Rozmiar: {int(scan.get('file_size', 0)) / (1024 * 1024):.2f} MB",
        f"- Wykryty profil: **{profile_cross} {profile_filter}{profile_slope}**".strip(),
        f"- Rozpoznane pomiary: **{len(measurements)}**",
        "",
        "## Mapowanie pomiarów",
        "",
        "| # | Rola | Nazwa w REW / MDAT | Bazowy delay |",
        "|---:|---|---|---:|",
    ]
    for m in measurements:
        base_txt = "" if m.base_delay_ms is None else f"{m.base_delay_ms:.2f} ms"
        lines.append(f"| {m.index} | {_md_escape(m.role)} | `{_md_escape(m.name)}` | {base_txt} |")

    lines.extend(
        [
            "",
            "## Co to oznacza",
            "",
            "- v19.3 traktuje wybrany `.mdat` jako główne wejście, wypisuje pomiary, proponuje role i wczytuje SPL/fazę bez ręcznego eksportu TXT.",
            "- Import numeryczny używa tablic zapisanych w projekcie REW: częstotliwość / SPL / faza.",
            "- Główny silnik optymalizacji może teraz startować bezpośrednio z jednego pliku `.mdat`.",
            "- Eksporty TXT z REW zostają jako tryb awaryjny i porównawczy.",
            "",
            "## Braki / kontrola",
            "",
        ]
    )
    if missing:
        for item in missing:
            lines.append(f"- Brakuje roli: **{item}**")
    else:
        lines.append("- Znaleziono wszystkie podstawowe role: **L main, L sub, R main, R sub**.")

    lines.extend(
        [
            "",
            "## Zalecany workflow teraz",
            "",
            "1. W REW zapisuj projekt jako `.mdat`.",
            "2. W aplikacji wybierz `.mdat` i uruchom optymalizację bez eksportów TXT.",
            "3. Sprawdź w raporcie, które pomiary zostały przypisane do L main / L sub / R main / R sub.",
            "4. Eksportów TXT używaj tylko wtedy, gdy chcesz ręcznie wymusić konkretne pomiary.",
            "",
        ]
    )

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, csv_path, scan

def _scan_java_serialized_float_arrays(data: bytes, min_len: int = 16, max_len: int = 200_000) -> List[Dict[str, object]]:
    """
    Lekki skaner tablic float[] w plikach REW .mdat.

    REW zapisuje projekty .mdat jako serializację Javy. Interesujące nas dane
    magnitude/phase są zwykle w blokach:
        TC_ARRAY (0x75), TC_REFERENCE (0x71), handle, int length, float32...
    Nie jest to pełny deserializator Javy, ale świadomie mały parser do danych
    frequency/SPL/phase potrzebnych przez optimizer.
    """
    arrays: List[Dict[str, object]] = []
    marker = b"\x75\x71\x00\x7e"  # TC_ARRAY + class reference
    idx = 0
    while True:
        idx = data.find(marker, idx)
        if idx < 0:
            break
        if idx + 10 <= len(data):
            try:
                n = struct.unpack(">i", data[idx + 6: idx + 10])[0]
            except struct.error:
                n = -1
            arr_start = idx + 10
            arr_end = arr_start + 4 * n
            if min_len <= n <= max_len and arr_end <= len(data):
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", RuntimeWarning)
                        values = np.frombuffer(data[arr_start:arr_end], dtype=">f4").astype(float)
                    if values.size == n and np.all(np.isfinite(values[: min(n, 64)])):
                        arrays.append(
                            {
                                "header_pos": idx,
                                "data_start": arr_start,
                                "data_end": arr_end,
                                "length": n,
                                "values": values,
                            }
                        )
                except Exception:
                    pass
        idx += 1
    return arrays


def _looks_like_rew_spl_array(values: np.ndarray) -> bool:
    if values.size < 20 or not np.all(np.isfinite(values)):
        return False
    vmin = float(np.nanmin(values))
    vmax = float(np.nanmax(values))
    med = float(np.nanmedian(values))
    std = float(np.nanstd(values))
    # SPL z REW zwykle siedzi w rozsądnym zakresie. Faza zawinięta też bywa
    # "rozsądna", dlatego o wyborze SPL decyduje też kolejność po nazwie pomiaru.
    return (-160.0 <= vmin <= 140.0) and (-120.0 <= vmax <= 170.0) and (-80.0 <= med <= 120.0) and std > 0.02


def _looks_like_rew_phase_array(values: np.ndarray) -> bool:
    if values.size < 20 or not np.all(np.isfinite(values)):
        return False
    vmin = float(np.nanmin(values))
    vmax = float(np.nanmax(values))
    std = float(np.nanstd(values))
    # v20.30 FULL-RANGE SUB PHASE:
    # Pełnopasmowy 256k RAW z oddzielnym DAC-em subwooferów potrafi zapisać
    # odwiniętą fazę przekraczającą milion stopni. Limit ±300 000° odrzucał
    # prawidłowe tablice L/R sub i pozostawiał jedynie inną reprezentację
    # fazy, mimo że zmierzone MAIN + SUB odtwarzały SUM po użyciu właściwej
    # tablicy. Nadal odrzucamy impulsy/okna o małym std oraz wartości
    # ewidentnie poza zakresem spotkanym w plikach REW.
    return (-5_000_000.0 <= vmin <= 5_000_000.0) and (-5_000_000.0 <= vmax <= 5_000_000.0) and std > 5.0




def _candidate_freq_axis_from_values(values: np.ndarray, points: int) -> Optional[Tuple[np.ndarray, float, float, float, float, str]]:
    """
    Próbuje rozpoznać tablicę częstotliwości zapisaną bezpośrednio w .mdat.

    Zwraca (freq, f_start, f_end, f_step, score, reason). Akceptujemy oś
    liniową albo prawie logarytmiczną, ale dla silnika optymalizacji
    preferowana jest oś rosnąca i bez duplikatów. Krok w metadanych raportujemy
    jako medianę diff dla osi nieliniowej.
    """
    if values.size != int(points) or points < 2:
        return None
    v = np.asarray(values, dtype=float)
    if not np.all(np.isfinite(v)):
        return None
    if np.nanmin(v) <= 0:
        return None
    diffs = np.diff(v)
    if not np.all(diffs > 0):
        return None
    f_start = float(v[0])
    f_end = float(v[-1])
    if not (0.05 <= f_start < f_end <= 100_000.0):
        return None
    if f_end < 80.0:
        return None

    med_step = float(np.nanmedian(diffs))
    if not np.isfinite(med_step) or med_step <= 0:
        return None

    # stabilność osi liniowej i logarytmicznej
    lin_cv = float(np.nanstd(diffs) / max(abs(med_step), 1e-12))
    logv = np.log(v)
    logdiffs = np.diff(logv)
    log_med = float(np.nanmedian(logdiffs))
    log_cv = float(np.nanstd(logdiffs) / max(abs(log_med), 1e-12)) if log_med != 0 else 999.0

    if lin_cv > 0.25 and log_cv > 0.25:
        return None

    score = 0.0
    if 5.0 <= f_start <= 30.0:
        score += 4.0
    elif f_start <= 50.0:
        score += 2.0
    if 120.0 <= f_end <= 500.0:
        score += 4.0
    elif f_end >= 1_000.0:
        score += 2.0
    if lin_cv <= 0.01:
        score += 3.0
        reason = f"bezpośrednia oś częstotliwości float[] liniowa, CV={lin_cv:.4g}"
    elif log_cv <= 0.03:
        score += 2.0
        reason = f"bezpośrednia oś częstotliwości float[] logarytmiczna, CV={log_cv:.4g}"
    else:
        score += 1.0
        reason = f"bezpośrednia oś częstotliwości float[] prawie rosnąca, linCV={lin_cv:.4g}, logCV={log_cv:.4g}"

    # Typowe pomiary REW do subów zaczynają się w okolicach 10/20 Hz
    # i mają koniec w okolicach 250/500/20k. Dajemy tylko premię, nie warunek.
    if 15.0 <= f_start <= 25.0:
        score += 1.0
    if 240.0 <= f_end <= 260.0:
        score += 1.0

    return v.copy(), f_start, f_end, med_step, score, reason


def _infer_mdat_freq_axis_from_float_arrays(data: bytes, points: int) -> Optional[Tuple[np.ndarray, float, float, float, str]]:
    """
    Fallback v19.70: niektóre warianty .mdat nie mają metadanych osi po tablicy
    fazy, ale zapisują częstotliwości jako osobną tablicę float[] o tej samej
    długości co SPL/faza.
    """
    candidates: List[Tuple[float, np.ndarray, float, float, float, str]] = []
    try:
        arrays = _scan_java_serialized_float_arrays(data, min_len=max(2, int(points)), max_len=max(2, int(points)))
    except Exception:
        arrays = []
    for entry in arrays:
        values = entry.get("values")
        if not isinstance(values, np.ndarray):
            continue
        cand = _candidate_freq_axis_from_values(values, int(points))
        if cand is None:
            continue
        freq, f_start, f_end, f_step, score, reason = cand
        candidates.append((float(score), freq, f_start, f_end, f_step, reason))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    score, freq, f_start, f_end, f_step, reason = candidates[0]
    return freq, f_start, f_end, f_step, f"{reason}; score={score:.1f}"


def _infer_mdat_freq_axis_from_global_metadata(data: bytes, points: int) -> Optional[Tuple[np.ndarray, float, float, float, str]]:
    """
    Fallback v19.70: szuka metadanych osi częstotliwości w całym pliku, nie tylko
    w oknie po tablicy fazy. To pomaga przy wariantach REW, w których SampledData
    przenosi metadane przed/po innym bloku serializacji.
    """
    if points <= 1:
        return None

    def make_axis(f_end: float, step: float) -> Optional[Tuple[np.ndarray, float, float, float]]:
        if not (np.isfinite(f_end) and np.isfinite(step)):
            return None
        if not (0.001 <= step <= 2_000.0 and 1.0 <= f_end <= 100_000.0):
            return None
        f_start = float(f_end) - float(points - 1) * float(step)
        if not (0.05 <= f_start <= f_end):
            return None
        freq = f_start + np.arange(points, dtype=float) * float(step)
        return freq, f_start, float(f_end), float(step)

    n_bytes = struct.pack(">i", int(points))
    candidates: List[Tuple[float, np.ndarray, float, float, float, str]] = []
    pos = data.find(n_bytes, 0, len(data))
    checked = 0
    while pos >= 0 and checked < 10000:
        checked += 1
        block = data[pos + 4: pos + 4 + 160]
        if len(block) >= 40:
            vals = np.frombuffer(block[: (len(block) // 4) * 4], dtype=">f4").astype(float)
            limit = min(vals.size, 32)
            pairs: List[Tuple[int, int]] = []
            if vals.size >= 9:
                pairs.append((0, 8))
            for end_i in range(limit):
                for step_i in range(limit):
                    if end_i != step_i:
                        pairs.append((end_i, step_i))
            for end_i, step_i in pairs:
                candidate = make_axis(float(vals[end_i]), float(vals[step_i]))
                if candidate is None:
                    continue
                freq, f_start, f_end, step = candidate
                score = 0.0
                if 5.0 <= f_start <= 30.0:
                    score += 5.0
                elif f_start <= 50.0:
                    score += 2.0
                if 120.0 <= f_end <= 500.0:
                    score += 5.0
                elif 1_000.0 <= f_end <= 30_000.0:
                    score += 2.0
                if 0.01 <= step <= 5.0:
                    score += 2.0
                if end_i == 0 and step_i == 8:
                    score += 1.0
                # Lokalizacja w pliku: metadane zwykle są w pobliżu bloków danych.
                reason = f"globalne metadane osi: pos={pos}, f_end_idx={end_i}, step_idx={step_i}"
                candidates.append((score, freq, f_start, f_end, step, reason))
        pos = data.find(n_bytes, pos + 1, len(data))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    score, freq, f_start, f_end, step, reason = candidates[0]
    if score < 4.0:
        return None
    return freq, f_start, f_end, step, f"{reason}; score={score:.1f}"

def _infer_mdat_freq_axis(data: bytes, after_pos: int, points: int) -> Tuple[np.ndarray, float, float, float]:
    """
    Próbuje odczytać liniową oś częstotliwości z metadanych SampledData REW.

    W znanych plikach REW po tablicy fazy znajduje się blok z liczbą punktów,
    f_end i krokiem FFT. Gdy znajdziemy f_end oraz step, f_start wynika z:
        f_start = f_end - (N - 1) * step
    """
    if points <= 1:
        raise ValueError("Za mało punktów do odtworzenia osi częstotliwości z .mdat.")

    n_bytes = struct.pack(">i", int(points))
    search_start = max(0, int(after_pos))
    search_end = min(len(data), search_start + 12_000)

    def make_axis(f_end: float, step: float) -> Optional[Tuple[np.ndarray, float, float, float]]:
        if not (np.isfinite(f_end) and np.isfinite(step)):
            return None
        if not (0.001 <= step <= 2_000.0 and 1.0 <= f_end <= 100_000.0):
            return None
        f_start = float(f_end) - float(points - 1) * float(step)
        if not (0.1 <= f_start <= f_end):
            return None
        freq = f_start + np.arange(points, dtype=float) * float(step)
        return freq, f_start, float(f_end), float(step)

    # Najczęstszy układ w REW: int N, float f_end, ..., float step jako 9. float po N.
    pos = data.find(n_bytes, search_start, search_end)
    while pos >= 0:
        block = data[pos + 4: pos + 4 + 120]
        if len(block) >= 40:
            vals = np.frombuffer(block[: (len(block) // 4) * 4], dtype=">f4").astype(float)
            if vals.size >= 9:
                direct = make_axis(float(vals[0]), float(vals[8]))
                if direct is not None:
                    return direct

            # Awaryjnie: przeszukaj pary f_end/step w pierwszych kilkunastu floatach.
            limit = min(vals.size, 24)
            for end_i in range(limit):
                for step_i in range(limit):
                    if end_i == step_i:
                        continue
                    candidate = make_axis(float(vals[end_i]), float(vals[step_i]))
                    if candidate is not None:
                        # Preferuj pary, przy których start/step/end pojawiają się
                        # jako liczby sensowne dla FFT i zakresu REW.
                        freq, f_start, f_end, step = candidate
                        if f_start >= 1.0 and f_end <= 24_000.0 and step < 50.0:
                            return candidate
        pos = data.find(n_bytes, pos + 1, search_end)

    # v19.70 fallback #1: globalne metadane osi w innym miejscu pliku.
    global_meta = _infer_mdat_freq_axis_from_global_metadata(data, points)
    if global_meta is not None:
        freq, f_start, f_end, f_step, reason = global_meta
        return freq, f_start, f_end, f_step

    # v19.70 fallback #2: bezpośrednia tablica częstotliwości float[].
    direct_axis = _infer_mdat_freq_axis_from_float_arrays(data, points)
    if direct_axis is not None:
        freq, f_start, f_end, f_step, reason = direct_axis
        return freq, f_start, f_end, f_step

    raise ValueError(
        "Nie udało się odtworzyć osi częstotliwości z .mdat. "
        "Ten wariant pliku REW może wymagać eksportu TXT albo aktualizacji parsera. "
        "Spróbuj v19.70+ lub wyeksportuj z REW cztery pomiary jako TXT: "
        "L main, R/P main, L sub, R/P sub."
    )


@dataclass
class _MdatPhaseCandidate:
    """One phase-like float array belonging to a single REW measurement."""

    ordinal: int
    position: int
    freq: np.ndarray
    phase_deg: np.ndarray


@dataclass
class _MdatNumericRecord:
    """SPL plus every structurally plausible phase candidate for a trace."""

    info: MdatMeasurementInfo
    freq: np.ndarray
    spl_db: np.ndarray
    phase_candidates: List[_MdatPhaseCandidate]


def _decode_mdat_numeric_records(
    data: bytes,
    measurements_info: List[MdatMeasurementInfo],
    float_arrays: Optional[List[Dict[str, object]]] = None,
) -> Tuple[List[_MdatNumericRecord], List[str]]:
    """Decode all named traces without silently selecting a phase array.

    REW's Java serialization can contain more than one same-length array after
    SPL.  v20.28 selected the first array that passed a broad phase heuristic.
    v20.29 keeps all candidates and lets a measured MAIN+SUB=SUM relationship
    choose the physically consistent one.
    """
    arrays_all = list(float_arrays or _scan_java_serialized_float_arrays(data))
    infos = sorted(measurements_info, key=lambda x: int(x.position))
    records: List[_MdatNumericRecord] = []
    warnings_out: List[str] = []
    for index, info in enumerate(infos):
        seg_start = int(info.position)
        seg_end = int(infos[index + 1].position) if index + 1 < len(infos) else len(data)
        arrays = [
            item for item in arrays_all
            if seg_start < int(item["data_start"]) < seg_end
        ]
        arrays.sort(key=lambda item: int(item["data_start"]))

        spl_entry: Optional[Dict[str, object]] = None
        for entry in arrays:
            values = entry.get("values")
            if isinstance(values, np.ndarray) and _looks_like_rew_spl_array(values):
                spl_entry = entry
                break
        if spl_entry is None:
            warnings_out.append(f"{info.name}: brak czytelnej tablicy SPL.")
            continue

        spl_values = spl_entry.get("values")
        if not isinstance(spl_values, np.ndarray):
            warnings_out.append(f"{info.name}: nieprawidłowa tablica SPL.")
            continue
        points = int(spl_entry["length"])
        spl_start = int(spl_entry["data_start"])
        phase_entries: List[Dict[str, object]] = []
        for entry in arrays:
            if int(entry["data_start"]) <= spl_start or int(entry["length"]) != points:
                continue
            values = entry.get("values")
            if isinstance(values, np.ndarray) and _looks_like_rew_phase_array(values):
                phase_entries.append(entry)
        if not phase_entries:
            warnings_out.append(f"{info.name}: brak tablicy fazy o długości zgodnej z SPL.")
            continue

        # Oś jest metadanym konkretnego pomiaru, a nie samej liczby punktów.
        # Dwa sweepy mogą mieć tyle samo próbek, ale inny start/krok/koniec.
        # Cache po ``points`` powodował wtedy ciche przypisanie osi pierwszego
        # pomiaru do kolejnych śladów i mógł zafałszować fazę oraz delay.
        axis_meta: Optional[Tuple[np.ndarray, float, float, float]] = None
        last_error: Optional[Exception] = None
        for entry in reversed(phase_entries):
            try:
                axis_meta = _infer_mdat_freq_axis(data, int(entry["data_end"]), points)
                break
            except Exception as exc:  # pragma: no cover - only exotic REW variants
                last_error = exc
        if axis_meta is None:
            warnings_out.append(f"{info.name}: nie udało się odtworzyć osi częstotliwości ({last_error}).")
            continue

        freq, _f_start, _f_end, _f_step = axis_meta
        candidates: List[_MdatPhaseCandidate] = []
        for ordinal, entry in enumerate(phase_entries, start=1):
            phase = entry.get("values")
            if not isinstance(phase, np.ndarray) or phase.size != spl_values.size:
                continue
            candidates.append(_MdatPhaseCandidate(
                ordinal=ordinal,
                position=int(entry["data_start"]),
                freq=np.asarray(freq, dtype=float).copy(),
                phase_deg=np.asarray(phase, dtype=float).copy(),
            ))
        if not candidates:
            warnings_out.append(f"{info.name}: wszystkie kandydatury fazy zostały odrzucone.")
            continue
        records.append(_MdatNumericRecord(
            info=info,
            freq=np.asarray(freq, dtype=float).copy(),
            spl_db=np.asarray(spl_values, dtype=float).copy(),
            phase_candidates=candidates,
        ))
    return records, warnings_out


def _record_complex(record: _MdatNumericRecord, candidate: _MdatPhaseCandidate, grid: np.ndarray) -> np.ndarray:
    """Interpolate SPL and unwrapped phase, then reconstruct complex pressure."""
    spl = np.interp(grid, record.freq, record.spl_db)
    unwrapped = np.unwrap(np.deg2rad(candidate.phase_deg))
    phase = np.interp(grid, candidate.freq, unwrapped)
    return np.power(10.0, spl / 20.0) * np.exp(1j * phase)


def _phase_truth_reconstruction_metrics(
    main_record: _MdatNumericRecord,
    main_candidate: _MdatPhaseCandidate,
    sub_record: _MdatNumericRecord,
    sub_candidate: _MdatPhaseCandidate,
    sum_record: _MdatNumericRecord,
    sum_candidate: _MdatPhaseCandidate,
) -> Optional[Dict[str, float]]:
    """Measure how closely complex MAIN+SUB recreates a measured SUM trace."""
    lo = max(20.0, float(main_record.freq[0]), float(sub_record.freq[0]), float(sum_record.freq[0]))
    hi = min(250.0, float(main_record.freq[-1]), float(sub_record.freq[-1]), float(sum_record.freq[-1]))
    if hi <= lo:
        return None
    base = max((main_record, sub_record, sum_record), key=lambda rec: rec.freq.size)
    grid = np.asarray(base.freq[(base.freq >= lo) & (base.freq <= hi)], dtype=float)
    if grid.size < 16:
        return None

    z_main = _record_complex(main_record, main_candidate, grid)
    z_sub = _record_complex(sub_record, sub_candidate, grid)
    z_sum = _record_complex(sum_record, sum_candidate, grid)
    main_db = 20.0 * np.log10(np.maximum(np.abs(z_main), 1e-15))
    sub_db = 20.0 * np.log10(np.maximum(np.abs(z_sub), 1e-15))
    measured_db = 20.0 * np.log10(np.maximum(np.abs(z_sum), 1e-15))

    # The guard is interested in the acoustic overlap, not frequencies at
    # which one source is 40 dB down and cannot validate relative phase.
    overlap = (grid >= 35.0) & (grid <= 180.0) & (np.abs(main_db - sub_db) <= 28.0)
    floor = float(np.nanmedian(measured_db)) - 24.0
    overlap &= measured_db >= floor
    if np.count_nonzero(overlap) < 16:
        overlap = (grid >= max(25.0, lo)) & (grid <= min(210.0, hi))
    if np.count_nonzero(overlap) < 16:
        return None

    predicted = z_main + z_sub
    predicted_db = 20.0 * np.log10(np.maximum(np.abs(predicted), 1e-15))
    spl_error = predicted_db[overlap] - measured_db[overlap]
    phase_error = np.rad2deg(np.angle(predicted[overlap] / np.where(np.abs(z_sum[overlap]) > 1e-15, z_sum[overlap], 1e-15)))
    phase_error = (phase_error + 180.0) % 360.0 - 180.0
    return {
        "spl_rms_db": float(np.sqrt(np.mean(np.square(spl_error)))),
        "spl_median_abs_db": float(np.median(np.abs(spl_error))),
        "phase_rms_deg": float(np.sqrt(np.mean(np.square(phase_error)))),
        "phase_median_abs_deg": float(np.median(np.abs(phase_error))),
        "points": float(np.count_nonzero(overlap)),
        "band_low_hz": float(np.min(grid[overlap])),
        "band_high_hz": float(np.max(grid[overlap])),
    }


def _relative_phase_equivalence_metrics(
    main_record: _MdatNumericRecord,
    main_a: _MdatPhaseCandidate,
    sub_record: _MdatNumericRecord,
    sub_a: _MdatPhaseCandidate,
    main_b: _MdatPhaseCandidate,
    sub_b: _MdatPhaseCandidate,
) -> Optional[Dict[str, float]]:
    """Compare two candidate pairs by the MAIN-to-SUB phase they imply.

    Multiple REW arrays may encode the same useful relative phase with a
    slightly different absolute phase or correction trace. A small score
    margin is dangerous only when the candidate pairs imply materially
    different MAIN/SUB alignment. The optimizer consumes that relative phase,
    so equivalent pairs must not trigger a false AMBIGUOUS block.
    """
    lo = max(35.0, float(main_record.freq[0]), float(sub_record.freq[0]))
    hi = min(180.0, float(main_record.freq[-1]), float(sub_record.freq[-1]))
    if hi <= lo:
        return None
    base = main_record if main_record.freq.size >= sub_record.freq.size else sub_record
    grid = np.asarray(base.freq[(base.freq >= lo) & (base.freq <= hi)], dtype=float)
    if grid.size < 16:
        return None

    main_db = np.interp(grid, main_record.freq, main_record.spl_db)
    sub_db = np.interp(grid, sub_record.freq, sub_record.spl_db)
    overlap = np.abs(main_db - sub_db) <= 28.0
    if np.count_nonzero(overlap) < 16:
        overlap = np.ones(grid.size, dtype=bool)

    def relative_phase(main: _MdatPhaseCandidate, sub: _MdatPhaseCandidate) -> np.ndarray:
        main_phase = np.interp(grid, main.freq, np.unwrap(np.deg2rad(main.phase_deg)))
        sub_phase = np.interp(grid, sub.freq, np.unwrap(np.deg2rad(sub.phase_deg)))
        return main_phase - sub_phase

    delta = np.rad2deg(np.angle(np.exp(1j * (relative_phase(main_a, sub_a) - relative_phase(main_b, sub_b)))))
    abs_delta = np.abs(delta[overlap])
    return {
        "rms_deg": float(np.sqrt(np.mean(np.square(abs_delta)))),
        "p95_deg": float(np.percentile(abs_delta, 95.0)),
        "max_deg": float(np.max(abs_delta)),
        "points": float(abs_delta.size),
    }


def _select_phase_for_side(
    label: str,
    main_record: _MdatNumericRecord,
    sub_record: _MdatNumericRecord,
    sum_records: List[_MdatNumericRecord],
) -> Tuple[Dict[str, _MdatPhaseCandidate], Dict[str, object]]:
    """Select component phase candidates using one or more measured SUMs."""
    selected: Dict[str, _MdatPhaseCandidate] = {}
    if not sum_records:
        if len(main_record.phase_candidates) == 1 and len(sub_record.phase_candidates) == 1:
            selected["main"] = main_record.phase_candidates[0]
            selected["sub"] = sub_record.phase_candidates[0]
            return selected, {
                "side": label,
                "status": "STRUCTURAL_ONLY",
                "reason": "Brak zmierzonego SUM; obie składowe mają po jednej kandydaturze fazy.",
                "sum_count": 0,
            }
        return selected, {
            "side": label,
            "status": "AMBIGUOUS",
            "reason": "Brak zmierzonego SUM przy wielu kandydaturach fazy.",
            "sum_count": 0,
            "main_candidates": len(main_record.phase_candidates),
            "sub_candidates": len(sub_record.phase_candidates),
        }

    pair_results: List[Dict[str, object]] = []
    for main_candidate in main_record.phase_candidates:
        for sub_candidate in sub_record.phase_candidates:
            refs: List[Dict[str, object]] = []
            for sum_record in sum_records:
                choices: List[Tuple[float, _MdatPhaseCandidate, Dict[str, float]]] = []
                for sum_candidate in sum_record.phase_candidates:
                    metrics = _phase_truth_reconstruction_metrics(
                        main_record, main_candidate, sub_record, sub_candidate,
                        sum_record, sum_candidate,
                    )
                    if metrics is None:
                        continue
                    score = float(metrics["spl_rms_db"]) + float(metrics["phase_rms_deg"]) / 20.0
                    choices.append((score, sum_candidate, metrics))
                if not choices:
                    continue
                choices.sort(key=lambda item: item[0])
                score, sum_candidate, metrics = choices[0]
                refs.append({
                    "name": sum_record.info.name,
                    "sum_candidate": int(sum_candidate.ordinal),
                    "score": float(score),
                    **metrics,
                })
            if not refs:
                continue
            pair_score = float(np.median([float(item["score"]) for item in refs]))
            pair_results.append({
                "main_candidate": main_candidate,
                "sub_candidate": sub_candidate,
                "score": pair_score,
                "references": refs,
                "spl_rms_db": float(np.median([float(item["spl_rms_db"]) for item in refs])),
                "phase_rms_deg": float(np.median([float(item["phase_rms_deg"]) for item in refs])),
                "spl_worst_db": float(max(float(item["spl_rms_db"]) for item in refs)),
                "phase_worst_deg": float(max(float(item["phase_rms_deg"]) for item in refs)),
            })

    if not pair_results:
        return selected, {
            "side": label,
            "status": "FAILED",
            "reason": "Nie udało się policzyć rekonstrukcji MAIN + SUB względem SUM.",
            "sum_count": len(sum_records),
        }

    pair_results.sort(key=lambda item: float(item["score"]))
    best = pair_results[0]
    runner = pair_results[1] if len(pair_results) > 1 else None
    margin = float(runner["score"]) - float(best["score"]) if runner is not None else float("inf")
    passes = (
        float(best["spl_rms_db"]) <= 0.90
        and float(best["phase_rms_deg"]) <= 12.0
        and float(best["spl_worst_db"]) <= 1.50
        and float(best["phase_worst_deg"]) <= 25.0
    )
    runner_equivalence: Optional[Dict[str, float]] = None
    runner_equivalent = False
    if runner is not None and margin < 0.35:
        runner_main = runner["main_candidate"]
        runner_sub = runner["sub_candidate"]
        assert isinstance(runner_main, _MdatPhaseCandidate)
        assert isinstance(runner_sub, _MdatPhaseCandidate)
        runner_equivalence = _relative_phase_equivalence_metrics(
            main_record,
            best["main_candidate"],
            sub_record,
            best["sub_candidate"],
            runner_main,
            runner_sub,
        )
        if runner_equivalence is not None:
            runner_equivalent = (
                float(runner_equivalence["rms_deg"]) <= 3.5
                and float(runner_equivalence["p95_deg"]) <= 6.0
                and float(runner_equivalence["max_deg"]) <= 12.0
            )
    ambiguous = runner is not None and margin < 0.35 and not runner_equivalent

    main_candidate = best["main_candidate"]
    sub_candidate = best["sub_candidate"]
    assert isinstance(main_candidate, _MdatPhaseCandidate)
    assert isinstance(sub_candidate, _MdatPhaseCandidate)
    if passes and not ambiguous:
        selected["main"] = main_candidate
        selected["sub"] = sub_candidate
        status = "VERIFIED"
        if runner_equivalent:
            reason = (
                "Zespolona rekonstrukcja MAIN + SUB odtwarza zmierzony SUM; "
                "druga bliska kandydatura daje równoważną fazę względną MAIN/SUB."
            )
        else:
            reason = "Zespolona rekonstrukcja MAIN + SUB odtwarza zmierzony SUM."
    elif ambiguous:
        status = "AMBIGUOUS"
        reason = f"Dwie kandydatury fazy dają zbyt podobny wynik (margines {margin:.3f})."
    else:
        status = "FAILED"
        reason = "Najlepsza kandydatura nie odtwarza zmierzonego SUM w bezpiecznych progach."

    diagnostics: Dict[str, object] = {
        "side": label,
        "status": status,
        "reason": reason,
        "sum_count": len(sum_records),
        "main_candidates": len(main_record.phase_candidates),
        "sub_candidates": len(sub_record.phase_candidates),
        "selected_main_candidate": int(main_candidate.ordinal),
        "selected_sub_candidate": int(sub_candidate.ordinal),
        "spl_rms_db": float(best["spl_rms_db"]),
        "phase_rms_deg": float(best["phase_rms_deg"]),
        "spl_worst_db": float(best["spl_worst_db"]),
        "phase_worst_deg": float(best["phase_worst_deg"]),
        "runner_margin": None if not np.isfinite(margin) else float(margin),
        "runner_equivalent": bool(runner_equivalent),
        "runner_relative_phase_rms_deg": None if runner_equivalence is None else float(runner_equivalence["rms_deg"]),
        "runner_relative_phase_p95_deg": None if runner_equivalence is None else float(runner_equivalence["p95_deg"]),
        "runner_relative_phase_max_deg": None if runner_equivalence is None else float(runner_equivalence["max_deg"]),
        "references": best["references"],
    }
    return selected, diagnostics


def _phase_truth_guard(
    records: List[_MdatNumericRecord],
    required_roles: List[str],
) -> Tuple[Dict[str, _MdatPhaseCandidate], Dict[str, object]]:
    """Resolve phase candidates and return a hard-gate diagnostic."""
    by_role: Dict[str, List[_MdatNumericRecord]] = {}
    for record in records:
        by_role.setdefault(record.info.role, []).append(record)

    chosen: Dict[str, _MdatPhaseCandidate] = {}
    sides: List[Dict[str, object]] = []
    definitions = [
        ("L", "L main", "L sub", ["L sum", "L strona"]),
        ("R", "R main", "R sub", ["R sum", "R strona"]),
    ]
    for label, main_role, sub_role, sum_roles in definitions:
        if main_role not in required_roles or sub_role not in required_roles:
            continue
        main_records = by_role.get(main_role, [])
        sub_records = by_role.get(sub_role, [])
        if len(main_records) != 1 or len(sub_records) != 1:
            continue
        sum_records = [record for role in sum_roles for record in by_role.get(role, [])]
        side_selected, diagnostic = _select_phase_for_side(
            label, main_records[0], sub_records[0], sum_records,
        )
        sides.append(diagnostic)
        if "main" in side_selected:
            chosen[main_role] = side_selected["main"]
        if "sub" in side_selected:
            chosen[sub_role] = side_selected["sub"]

    # Roles outside a main/sub pair are safe only when structurally unique.
    for role in required_roles:
        if role in chosen:
            continue
        role_records = by_role.get(role, [])
        if len(role_records) == 1 and len(role_records[0].phase_candidates) == 1:
            chosen[role] = role_records[0].phase_candidates[0]

    side_statuses = [str(item.get("status", "")) for item in sides]
    unresolved = [role for role in required_roles if role not in chosen]
    if unresolved or any(status in {"AMBIGUOUS", "FAILED"} for status in side_statuses):
        status = "BLOCKED"
        allowed = False
        source = "Phase Truth Guard: faza niejednoznaczna albo niezgodna ze zmierzonym SUM"
    elif any(status == "VERIFIED" for status in side_statuses):
        status = "VERIFIED" if all(status == "VERIFIED" for status in side_statuses) else "VERIFIED_PARTIAL"
        allowed = True
        source = "Phase Truth Guard: zespolona rekonstrukcja MAIN + SUB ≈ zmierzony SUM"
    else:
        status = "STRUCTURAL_ONLY"
        allowed = True
        source = "MDAT: jedna strukturalna kandydatura fazy na pomiar; brak sum kontrolnych"

    selected = {
        role: {
            "candidate": int(candidate.ordinal),
            "array_position": int(candidate.position),
        }
        for role, candidate in chosen.items()
    }
    return chosen, {
        "status": status,
        "phase_metrics_allowed": allowed,
        "source": source,
        "selected": selected,
        "sides": sides,
        "unresolved_roles": unresolved,
    }


def import_mdat_measurements(path: str | Path, required_roles: Optional[List[str]] = None) -> MdatImportedProject:
    """
    Pełny import numeryczny .mdat: L/R/P main + L/R/P sub jako Measurement.
    Wymaga nazw pomiarów pozwalających na mapowanie roli, np.:
    75_BW48_L_main, 75_BW48_R_main albo 75_BW48_P_main, 75_BW48_L_sub_0.0, 75_BW48_R_sub_0.0 albo 75_BW48_P_sub_0.0.
    """
    p = Path(path)
    if is_rew_api_snapshot(p):
        return import_rew_api_snapshot(p, required_roles=required_roles)
    data = p.read_bytes()
    scan = scan_mdat_project(p)
    measurements_info: List[MdatMeasurementInfo] = list(scan.get("measurements", []))  # type: ignore[arg-type]
    if not measurements_info:
        raise ValueError("Nie znaleziono pomiarów w .mdat z nazwami możliwymi do mapowania.")

    required_roles = list(required_roles or ["L main", "L sub", "R main", "R sub"])
    role_info: Dict[str, MdatMeasurementInfo] = {}
    for role in required_roles:
        matches = [m for m in measurements_info if m.role == role]
        if not matches:
            found = ", ".join(sorted({m.role for m in measurements_info})) or "brak rozpoznanych ról"
            raise ValueError(
                f"Brakuje pomiaru {role} w .mdat. "
                "Wybrany tryb wymaga jednoznacznych nazw wszystkich wymaganych pomiarów. "
                f"Rozpoznane role: {found}. "
                "Zmień nazwy pomiarów w REW albo użyj właściwego pliku RAW main/sub."
            )
        if len(matches) > 1:
            names = ", ".join(m.name for m in sorted(matches, key=lambda x: int(x.position)))
            raise ValueError(
                f"Niejednoznaczna rola {role}: znaleziono {len(matches)} pomiary ({names}). "
                "Projektant nie wybiera już po cichu pierwszego pomiaru. Zostaw po jednym L main, L sub, R main i R sub albo zapisz osobny projekt MDAT."
            )
        role_info[role] = matches[0]

    float_arrays = _scan_java_serialized_float_arrays(data)
    if not float_arrays:
        raise ValueError("Nie znaleziono tablic float[] w .mdat.")
    numeric_records, decode_warnings = _decode_mdat_numeric_records(
        data, measurements_info, float_arrays,
    )
    records_by_name = {record.info.name: record for record in numeric_records}
    missing_numeric = [role for role, info in role_info.items() if info.name not in records_by_name]
    if missing_numeric:
        raise ValueError(
            "Nie udało się numerycznie odczytać wymaganych pomiarów: "
            + ", ".join(missing_numeric)
            + ". " + "; ".join(decode_warnings[:4])
        )

    selected_phase, truth = _phase_truth_guard(numeric_records, required_roles)
    imported: Dict[str, Measurement] = {}
    role_to_name: Dict[str, str] = {}
    for role in required_roles:
        info = role_info[role]
        record = records_by_name[info.name]
        candidate = selected_phase.get(role)
        if candidate is None:
            # Keep the project inspectable so the health report can explain the
            # block, but never let consumers rank phase: health-check adds a
            # hard error from phase_metrics_allowed=False.
            candidate = record.phase_candidates[0]
        imported[role] = Measurement(
            info.name,
            record.freq.copy(),
            record.spl_db.copy(),
            candidate.phase_deg.copy(),
        )
        role_to_name[role] = info.name

    first = imported[required_roles[0]]
    f_start = float(first.freq[0])
    f_end = float(first.freq[-1])
    f_step = float(np.nanmedian(np.diff(first.freq))) if first.freq.size > 1 else 0.0
    points = int(first.freq.size)
    truth["decode_warnings"] = decode_warnings
    scan["phase_truth"] = truth

    project = MdatImportedProject(
        path=str(p),
        measurements_by_role=imported,
        role_to_name=role_to_name,
        scan=scan,
        freq_start_hz=f_start,
        freq_end_hz=f_end,
        freq_step_hz=f_step,
        points=points,
        phase_truth_status=str(truth.get("status", "NOT_CHECKED")),
        phase_truth_source=str(truth.get("source", "")),
        phase_truth_diagnostics=truth,
        phase_metrics_allowed=bool(truth.get("phase_metrics_allowed", False)),
    )
    validate_measurement_project(project)
    return project


def mdat_measurement_path_labels(project: MdatImportedProject) -> Dict[str, str]:
    return {role: f"{project.path}::{project.role_to_name.get(role, role)}" for role in ["L main", "L sub", "R main", "R sub"]}



def mdat_import_health_checks(project: MdatImportedProject) -> Tuple[List[str], List[str]]:
    """
    Walidacja techniczna direct MDAT importu.
    Zwraca (errors, warnings). Errors oznaczają, że nie należy uruchamiać optymalizacji.
    """
    errors: List[str] = []
    warnings_out: List[str] = []
    required_roles = ["L main", "L sub", "R main", "R sub"]

    if not bool(getattr(project, "phase_metrics_allowed", True)):
        detail = str(getattr(project, "phase_truth_source", "") or "Phase Truth Guard nie potwierdził fazy")
        errors.append(
            "MDAT Phase Truth Guard: ranking fazy/delayu zablokowany — " + detail + "."
        )
    elif str(getattr(project, "phase_truth_status", "")) == "STRUCTURAL_ONLY":
        warnings_out.append(
            "MDAT Phase Truth Guard: brak zmierzonych SUM; użyto jedynej strukturalnej tablicy fazy. "
            "Dla najwyższej pewności dodaj L_SUM i R_SUM wykonane ze wspólną timing reference."
        )

    for role in required_roles:
        if role not in project.measurements_by_role:
            errors.append(f"Brakuje roli: {role}.")
            continue
        m = project.measurements_by_role[role]
        if m.freq.size == 0 or m.spl_db.size == 0 or m.phase_deg.size == 0:
            errors.append(f"{role}: puste dane.")
            continue
        if not (m.freq.size == m.spl_db.size == m.phase_deg.size):
            errors.append(f"{role}: różne długości freq/SPL/faza.")
        if not np.all(np.isfinite(m.freq)):
            errors.append(f"{role}: oś częstotliwości zawiera NaN/Inf.")
        if not np.all(np.isfinite(m.spl_db)):
            errors.append(f"{role}: SPL zawiera NaN/Inf.")
        if not np.all(np.isfinite(m.phase_deg)):
            errors.append(f"{role}: faza zawiera NaN/Inf.")
        if m.freq.size > 1 and not np.all(np.diff(m.freq) > 0):
            errors.append(f"{role}: oś częstotliwości nie jest rosnąca.")
        if m.spl_db.size:
            spl_min = float(np.nanmin(m.spl_db))
            spl_max = float(np.nanmax(m.spl_db))
            if spl_min < -180 or spl_max > 180:
                warnings_out.append(f"{role}: nietypowy zakres SPL {spl_min:.1f}..{spl_max:.1f} dB.")
        if m.phase_deg.size:
            ph_abs = float(np.nanmax(np.abs(m.phase_deg)))
            if ph_abs > 80_000:
                warnings_out.append(f"{role}: bardzo duża wartość fazy odwiniętej: ±{ph_abs:.0f}°.")

    ref: Optional[Measurement] = None
    for role in required_roles:
        if role in project.measurements_by_role:
            ref = project.measurements_by_role[role]
            break

    if ref is not None:
        for role in required_roles:
            m = project.measurements_by_role.get(role)
            if m is None:
                continue
            if m.freq.size != ref.freq.size:
                errors.append(f"{role}: inna liczba punktów osi częstotliwości niż referencja.")
            elif m.freq.size and not np.allclose(m.freq, ref.freq, rtol=0, atol=1e-6):
                errors.append(f"{role}: oś częstotliwości nie zgadza się z referencją.")

    if project.points < 50:
        warnings_out.append(f"Mało punktów pomiaru: {project.points}.")
    if project.freq_end_hz < 120:
        warnings_out.append(f"Górny zakres importu to tylko {project.freq_end_hz:.1f} Hz; niektóre pasma oceny mogą być poza pomiarem.")
    if project.freq_start_hz > 30:
        warnings_out.append(f"Dolny zakres importu zaczyna się od {project.freq_start_hz:.1f} Hz.")

    return errors, warnings_out


def write_mdat_health_report(project: MdatImportedProject, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "MDAT_HEALTH_CHECK.md"
    errors, warnings_out = mdat_import_health_checks(project)

    lines = [
        "# MDAT health-check v20.31 — Phase Truth Guard",
        "",
        f"- Plik: `{Path(project.path).name}`",
        f"- Punkty: **{project.points}**",
        f"- Zakres: **{project.freq_start_hz:.4f}–{project.freq_end_hz:.4f} Hz**",
        f"- Krok: **{project.freq_step_hz:.9f} Hz**",
        f"- Phase Truth Guard: **{_md_escape(getattr(project, 'phase_truth_status', 'NOT_CHECKED'))}**",
        f"- Źródło fazy: **{_md_escape(getattr(project, 'phase_truth_source', '') or '—')}**",
        "",
        "## Role i dane",
        "",
        "| Rola | Nazwa | Punkty | SPL min/max | Faza min/max |",
        "|---|---|---:|---:|---:|",
    ]
    for role in ["L main", "L sub", "R main", "R sub"]:
        m = project.measurements_by_role.get(role)
        if m is None:
            lines.append(f"| {role} | — | — | — | — |")
            continue
        spl_min = float(np.nanmin(m.spl_db)) if m.spl_db.size else float("nan")
        spl_max = float(np.nanmax(m.spl_db)) if m.spl_db.size else float("nan")
        ph_min = float(np.nanmin(m.phase_deg)) if m.phase_deg.size else float("nan")
        ph_max = float(np.nanmax(m.phase_deg)) if m.phase_deg.size else float("nan")
        lines.append(
            f"| {role} | `{_md_escape(m.name)}` | {m.freq.size} | "
            f"{spl_min:.2f} / {spl_max:.2f} | {ph_min:.2f} / {ph_max:.2f} |"
        )

    truth = getattr(project, "phase_truth_diagnostics", {}) or {}
    sides = truth.get("sides", []) if isinstance(truth, dict) else []
    if isinstance(sides, list) and sides:
        lines.extend([
            "",
            "## Phase Truth Guard — rekonstrukcja zespolona",
            "",
            "| Strona | Status | SUM | Kandydat MAIN | Kandydat SUB | Błąd SPL RMS | Błąd fazy RMS | Margines |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ])
        for side in sides:
            if not isinstance(side, dict):
                continue
            margin = side.get("runner_margin")
            margin_text = "—" if margin is None else f"{float(margin):.3f}"
            spl_text = "—" if side.get("spl_rms_db") is None else f"{float(side['spl_rms_db']):.3f} dB"
            phase_text = "—" if side.get("phase_rms_deg") is None else f"{float(side['phase_rms_deg']):.2f}°"
            lines.append(
                f"| {_md_escape(side.get('side', ''))} | {_md_escape(side.get('status', ''))} | "
                f"{int(side.get('sum_count', 0) or 0)} | {side.get('selected_main_candidate', '—')} | "
                f"{side.get('selected_sub_candidate', '—')} | {spl_text} | {phase_text} | {margin_text} |"
            )
            if side.get("reason"):
                lines.append(f"|  |  |  |  |  |  | Uzasadnienie | {_md_escape(side.get('reason', ''))} |")

    lines.extend(["", "## Wynik", ""])
    if errors:
        lines.append("**STATUS: BŁĄD — nie uruchamiać optymalizacji.**")
        lines.append("")
        for e in errors:
            lines.append(f"- ❌ {e}")
    else:
        lines.append("**STATUS: OK — import nadaje się do optymalizacji.**")

    if warnings_out:
        lines.extend(["", "## Ostrzeżenia", ""])
        for w in warnings_out:
            lines.append(f"- ⚠️ {w}")
    else:
        lines.extend(["", "Brak ostrzeżeń technicznych."])

    path.write_text("\n".join(lines), encoding="utf-8")
    write_mdat_phase_truth_report(project, out)
    return path


def write_mdat_phase_truth_report(project: MdatImportedProject, out_dir: str | Path) -> Path:
    """Write the auditable phase-source decision introduced in v20.29."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "MDAT_PHASE_TRUTH_GUARD.md"
    truth = getattr(project, "phase_truth_diagnostics", {}) or {}
    selected = truth.get("selected", {}) if isinstance(truth, dict) else {}
    sides = truth.get("sides", []) if isinstance(truth, dict) else []
    lines = [
        "# MDAT Phase Truth Guard — v20.29",
        "",
        f"- Plik: `{Path(project.path).name}`",
        f"- Status: **{_md_escape(getattr(project, 'phase_truth_status', 'NOT_CHECKED'))}**",
        f"- Metryki fazowe i ranking delayu: **{'DOZWOLONE' if project.phase_metrics_allowed else 'ZABLOKOWANE'}**",
        f"- Źródło fazy: **{_md_escape(getattr(project, 'phase_truth_source', '') or '—')}**",
        "",
        "## Wybrane tablice fazy",
        "",
        "| Rola | Kandydat | Pozycja tablicy w MDAT |",
        "|---|---:|---:|",
    ]
    if isinstance(selected, dict) and selected:
        for role in sorted(selected):
            item = selected.get(role, {})
            if not isinstance(item, dict):
                continue
            lines.append(f"| {_md_escape(role)} | {item.get('candidate', '—')} | {item.get('array_position', '—')} |")
    else:
        lines.append("| — | — | — |")

    lines.extend([
        "",
        "## Zgodność MAIN + SUB ze zmierzonym SUM",
        "",
        "| Strona | Status | Powtórzenia SUM | SPL RMS | Faza RMS | Najgorszy SPL | Najgorsza faza | Margines do drugiej kandydatury |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ])
    side_notes: List[str] = []
    if isinstance(sides, list) and sides:
        for side in sides:
            if not isinstance(side, dict):
                continue
            def value(key: str, fmt: str, suffix: str = "") -> str:
                raw = side.get(key)
                return "—" if raw is None else format(float(raw), fmt) + suffix
            lines.append(
                f"| {_md_escape(side.get('side', ''))} | {_md_escape(side.get('status', ''))} | "
                f"{int(side.get('sum_count', 0) or 0)} | {value('spl_rms_db', '.3f', ' dB')} | "
                f"{value('phase_rms_deg', '.2f', '°')} | {value('spl_worst_db', '.3f', ' dB')} | "
                f"{value('phase_worst_deg', '.2f', '°')} | {value('runner_margin', '.3f')} |"
            )
            side_notes.append(f"- **{_md_escape(side.get('side', ''))}:** {_md_escape(side.get('reason', ''))}")
            references = side.get("references", [])
            if isinstance(references, list):
                for ref in references:
                    if not isinstance(ref, dict):
                        continue
                    side_notes.append(
                        f"  - `{_md_escape(ref.get('name', ''))}`: kandydat SUM {ref.get('sum_candidate', '—')}, "
                        f"SPL RMS {float(ref.get('spl_rms_db', float('nan'))):.3f} dB, "
                        f"faza RMS {float(ref.get('phase_rms_deg', float('nan'))):.2f}°, "
                        f"pasmo {float(ref.get('band_low_hz', float('nan'))):.1f}–{float(ref.get('band_high_hz', float('nan'))):.1f} Hz."
                    )
    else:
        lines.append("| — | — | — | — | — | — | — | — |")
    if side_notes:
        lines.extend(["", "### Uzasadnienie i pomiary kontrolne", ""])
        lines.extend(side_notes)

    decode_warnings = truth.get("decode_warnings", []) if isinstance(truth, dict) else []
    if isinstance(decode_warnings, list) and decode_warnings:
        lines.extend(["", "## Ostrzeżenia dekodera", ""])
        lines.extend(f"- {_md_escape(item)}" for item in decode_warnings)
    lines.extend([
        "",
        "## Reguła bezpieczeństwa",
        "",
        "Jeżeli faza ma kilka kandydatur i nie da się jednoznacznie potwierdzić jej zmierzoną sumą, "
        "REWDO nie uruchamia rankingu fazy, delayu ani polaryzacji. SPL może pozostać czytelny diagnostycznie, "
        "ale nie jest podstawą do zespolonego sumowania.",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_mdat_numeric_import_report(project: MdatImportedProject, out_dir: str | Path) -> Tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md_path = out / "MDAT_NUMERIC_IMPORT.md"
    csv_path = out / "mdat_numeric_measurements.csv"

    lines = [
        "# Import numeryczny REW — MDAT / API v20.31",
        "",
        f"- Plik: `{Path(project.path).name}`",
        f"- Źródło: **{str(project.scan.get('source_type') or ('REW MDAT' if project.scan.get('is_rew_mdat') else 'dane REW'))}**",
        f"- Zakres: **{project.freq_start_hz:.4f}–{project.freq_end_hz:.4f} Hz**",
        f"- Krok: **{project.freq_step_hz:.9f} Hz**",
        f"- Punkty: **{project.points}**",
        f"- Phase Truth Guard: **{_md_escape(getattr(project, 'phase_truth_status', 'NOT_CHECKED'))}**",
        f"- Źródło fazy: **{_md_escape(getattr(project, 'phase_truth_source', '') or '—')}**",
        "",
        "## Role użyte do optymalizacji",
        "",
        "| Rola | Nazwa pomiaru w REW / MDAT |",
        "|---|---|",
    ]
    for role in ["L main", "L sub", "R main", "R sub"]:
        lines.append(f"| {role} | `{_md_escape(project.role_to_name.get(role, ''))}` |")
    lines.extend([
        "",
        "Import SPL/fazy wykonano bez ręcznego eksportu TXT z REW — z MDAT albo lokalnej migawki API.",
        "Eksporty TXT pozostają tylko trybem awaryjnym.",
        "",
    ])
    md_path.write_text("\n".join(lines), encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["role", "measurement_name", "frequency_hz", "spl_db", "phase_deg"])
        for role in ["L main", "L sub", "R main", "R sub"]:
            m = project.measurements_by_role[role]
            for fr, sp, ph in zip(m.freq, m.spl_db, m.phase_deg):
                writer.writerow([role, m.name, f"{float(fr):.9f}", f"{float(sp):.6f}", f"{float(ph):.6f}"])
    return md_path, csv_path


def load_measurements_by_roles_from_mdat(path: str | Path, roles: List[str]) -> MdatImportedProject:
    """Wczytaj dowolny jednoznaczny zestaw ról rozpoznanych w nazwach REW."""
    project = import_mdat_measurements(path, required_roles=list(roles))
    if not project.phase_metrics_allowed:
        raise ValueError(
            "MDAT Phase Truth Guard zablokował dane fazowe: "
            + (project.phase_truth_source or project.phase_truth_status)
            + ". Dodaj zmierzone sumy kontrolne albo użyj zweryfikowanej migawki REW API."
        )
    return project


def load_optimizer_measurements_from_mdat(path: str | Path) -> Tuple[Measurement, Measurement, Measurement, Measurement, MdatImportedProject]:
    project = import_mdat_measurements(path)
    if not project.phase_metrics_allowed:
        raise ValueError(
            "MDAT Phase Truth Guard zablokował ranking fazy/delayu: "
            + (project.phase_truth_source or project.phase_truth_status)
            + ". Uruchom kontrolę MDAT i dodaj L_SUM/R_SUM ze wspólną timing reference."
        )
    return (
        project.measurements_by_role["L main"],
        project.measurements_by_role["L sub"],
        project.measurements_by_role["R main"],
        project.measurements_by_role["R sub"],
        project,
    )
