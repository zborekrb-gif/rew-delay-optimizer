#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kalibrator predykcja–pomiar — poprawka walidacji i zapisu, etap 2.

Wczytuje przewidywanie projektanta z CSV oraz pełny pomiar przed Dirac z REW
(L main, L sub, L strona, R main, R sub, R strona, L+R). Aktualizuje rekordy
kalibracyjne i liczy liczbę niezależnych topologii osobno od liczby stanów.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Dict, Any, Optional
import numpy as np

from rew_mdat import load_measurements_by_roles_from_mdat
from rew_scoring import common_grid, measurement_to_complex, db
from rew_acoustic_crossover_designer import _score_side, _phase_energy_score, _dirac_burden_risk, dip_depth, _band, _safe_mean, _clip
from rew_acoustic_biquad_designer import _main_sub_phase_score, _technical_integration_score, SafetyMetrics

ROLES = ["L main", "L sub", "L strona", "R main", "R sub", "R strona", "L+R"]


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}: wpisz niepusty identyfikator.")
    return value.strip()


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}: oczekiwano liczby, nie wartości logicznej.")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{label}: niepoprawna liczba.") from None
    if not math.isfinite(result):
        raise ValueError(f"{label}: NaN/Inf nie jest dopuszczalne.")
    return result


def _finite_tree(value: object, label: str = "Kalibracja") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{label}: NaN/Inf nie jest dopuszczalne.")
    if isinstance(value, dict):
        for key, item in value.items():
            _finite_tree(item, f"{label}/{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _finite_tree(item, f"{label}/{index}")


def _source(path: Path, label: str) -> Path:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"{label}: nie znaleziono pliku: {path}")
    return path


def _hash(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _read_prediction(path: Path, candidate_code: str) -> Dict[str, float]:
    candidate_code = _identity(candidate_code, "Kod kandydata")
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "custom_code" not in reader.fieldnames:
            raise ValueError("CSV predykcji nie zawiera kolumny custom_code. Nie można potwierdzić kandydata.")
        rows = list(reader)
    if not rows:
        raise ValueError("CSV predykcji jest pusty.")
    matches = [r for r in rows if str(r.get("custom_code", "")).strip() == candidate_code]
    if len(matches) != 1:
        found = ", ".join(str(r.get("custom_code", "")) for r in rows[:12])
        raise ValueError(f"Nie znaleziono jednoznacznie kandydata {candidate_code!r}. Dostępne: {found}")
    r = matches[0]
    def f(*keys: str) -> float:
        values = []
        for key in keys:
            value = r.get(key)
            if value is not None and str(value).strip():
                values.append(_finite(value, key))
        if not values:
            raise ValueError("Brak wartości predykcji: " + " / ".join(keys))
        if any(value != values[0] for value in values[1:]):
            raise ValueError("Sprzeczne wartości w równoważnych kolumnach predykcji: " + " / ".join(keys))
        return values[0]
    return {
        "side_l": f("side_l_base", "side_l_base_simulation_0_100"),
        "side_r": f("side_r_base", "side_r_base_simulation_0_100"),
        "technical": f("technical_base_simulation", "technical_base_simulation_0_100"),
    }


def _score_measured_lr(actual_lr: np.ndarray, l_side: np.ndarray, r_side: np.ndarray, freq: np.ndarray, xo_hz: float) -> tuple[float, float, float]:
    """Ocena rzeczywiście zmierzonego L+R, nie sumy odtworzonej w pamięci."""
    m = _band(freq, max(35.0, xo_hz - 35.0), min(180.0, xo_hz + 45.0))
    if np.count_nonzero(m) < 8:
        m = _band(freq, 55.0, 145.0)
    if np.count_nonzero(m) < 8:
        return 0.0, 99.0, 99.0
    lr_db = db(actual_lr)[m]
    comp = np.maximum(db(l_side)[m], db(r_side)[m])
    construct = _safe_mean(lr_db - comp)
    dip = dip_depth(lr_db)
    asym = abs(_safe_mean(db(l_side)[m]) - _safe_mean(db(r_side)[m]))
    score = 78.0 + construct * 7.0 - dip * 8.5 - max(0.0, asym - 1.5) * 5.0
    null_risk = _clip(dip * 11.0 + max(0.0, -construct) * 12.0 + max(0.0, asym - 2.0) * 5.0)
    return _clip(score), float(null_risk), float(dip)


def _measured_metrics(mdat: Path, xo_hz: float) -> Dict[str, float]:
    project = load_measurements_by_roles_from_mdat(mdat, ROLES)
    ms = project.measurements_by_role
    freq = common_grid([ms[r] for r in ROLES], points=1500)
    freq = freq[(freq >= 20.0) & (freq <= 500.0)]
    z = {r: measurement_to_complex(ms[r], freq) for r in ROLES}

    l_side, _, _ = _score_side(z["L strona"], z["L main"], z["L sub"], freq, xo_hz)
    r_side, _, _ = _score_side(z["R strona"], z["R main"], z["R sub"], freq, xo_hz)
    lr_score, null_risk, _ = _score_measured_lr(z["L+R"], z["L strona"], z["R strona"], freq, xo_hz)
    phase_l, _ = _main_sub_phase_score(z["L main"], z["L sub"], freq, xo_hz)
    phase_r, _ = _main_sub_phase_score(z["R main"], z["R sub"], freq, xo_hz)
    phase_lr = _phase_energy_score(z["L strona"], z["R strona"], freq, xo_hz)
    burden = _dirac_burden_risk(z["L strona"], z["R strona"], freq, xo_hz)
    safe = SafetyMetrics(True, 0.0, 0.0, 0.0, -99.0, -99.0, 0.0, 0.0, "pomiar realnego, stabilnego filtru")
    technical = _technical_integration_score(
        side_min=min(l_side, r_side), side_mean=0.5*(l_side+r_side),
        lr=lr_score, main_sub_phase_min=min(phase_l, phase_r),
        main_sub_phase_mean=0.5*(phase_l+phase_r), phase_lr=phase_lr,
        null_risk=null_risk, burden=burden, safety=safe,
    )
    return {
        "side_l": float(l_side), "side_r": float(r_side), "technical": float(technical),
        "lr_sum": float(lr_score), "phase_l": float(phase_l), "phase_r": float(phase_r),
    }


def _aggregate(records: list[dict[str, Any]]) -> Dict[str, Any]:
    if not records:
        return {
            "validation_state_count": 0, "independent_topology_count": 0,
            "side_bias_l": 0.0, "side_bias_r": 0.0, "side_sigma_l": 0.0,
            "side_sigma_r": 0.0, "technical_bias": 0.0, "technical_sigma": 0.0,
        }
    bl = np.asarray([_finite(r["bias_side_l"], "bias_side_l") for r in records], dtype=float)
    br = np.asarray([_finite(r["bias_side_r"], "bias_side_r") for r in records], dtype=float)
    bt = np.asarray([_finite(r["bias_technical"], "bias_technical") for r in records], dtype=float)
    topology_ids = [_identity(r.get("topology_id"), "Topologia rekordu") for r in records]
    return {
        "validation_state_count": len(records),
        "independent_topology_count": len(set(topology_ids)),
        "side_bias_l": float(np.mean(bl)), "side_bias_r": float(np.mean(br)),
        "side_sigma_l": float(np.std(bl, ddof=1)) if len(bl) > 1 else 0.0,
        "side_sigma_r": float(np.std(br, ddof=1)) if len(br) > 1 else 0.0,
        "technical_bias": float(np.mean(bt)),
        "technical_sigma": float(np.std(bt, ddof=1)) if len(bt) > 1 else 0.0,
    }


def _validate_records(records: object) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        raise ValueError("Historia kalibracji musi być listą rekordów.")
    seen = set()
    for row in records:
        if not isinstance(row, dict):
            raise ValueError("Historia kalibracji zawiera niepoprawny rekord.")
        _identity(row.get("topology_id"), "Topologia rekordu")
        _identity(row.get("candidate_code"), "Kandydat rekordu")
        state = _identity(row.get("state_id"), "Stan rekordu")
        if state in seen:
            raise ValueError(f"Powtórzony identyfikator stanu w historii: {state}")
        seen.add(state)
        for key in ("bias_side_l", "bias_side_r", "bias_technical"):
            _finite(row.get(key), key)
    return records


def _publish_pair(out_json: Path, json_text: str, report: Path, report_text: str) -> None:
    """Publish complete files exclusively; the JSON is the final commit marker.

    A hard link creates a destination atomically without replacing an existing
    file. If the filesystem cannot do this, fail and leave prior files intact.
    No calibration input is opened for writing. A handled failure rolls back
    this operation's published links and removes all staging files.
    """
    out_json.parent.mkdir(parents=True, exist_ok=True)
    staged = []
    published = []
    try:
        for target, text in ((report, report_text), (out_json, json_text)):
            fd, temporary = tempfile.mkstemp(prefix="." + target.name + ".", suffix=".tmp", dir=target.parent)
            temporary = Path(temporary)
            staged.append((target, temporary))
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
        for target, temporary in staged:
            os.link(temporary, target)
            published.append((target, temporary))
    except OSError as exc:
        for target, temporary in reversed(published):
            try:
                if target.samefile(temporary):
                    target.unlink()
            except OSError:
                pass
        raise ValueError("Nie zapisano kalibracji. Wybierz nową nazwę w folderze obsługującym bezpieczny zapis; istniejące pliki nie zostały zastąpione.") from exc
    finally:
        for _, temporary in staged:
            temporary.unlink(missing_ok=True)


def update_calibration(*, prediction_csv: Path, candidate_code: str, measured_mdat: Path,
                       xo_hz: float, topology_id: str, state_id: str,
                       calibration_json: Path, out_json: Path) -> Path:
    candidate_code = _identity(candidate_code, "Kod kandydata")
    topology_id = _identity(topology_id, "Identyfikator topologii")
    state_id = _identity(state_id, "Identyfikator stanu")
    xo_hz = _finite(xo_hz, "Częstotliwość XO")
    if xo_hz <= 0:
        raise ValueError("Częstotliwość XO musi być dodatnia.")
    prediction_csv = _source(prediction_csv, "Predykcja")
    measured_mdat = _source(measured_mdat, "Pomiar")
    calibration_json = _source(calibration_json, "Kalibracja wejściowa")
    out_json = Path(out_json).expanduser().resolve()
    if out_json.suffix.lower() != ".json":
        raise ValueError("Nowy plik kalibracji musi mieć rozszerzenie .json.")
    report = out_json.with_suffix(".md")
    inputs = (prediction_csv, measured_mdat, calibration_json)
    if out_json in inputs or report in inputs:
        raise ValueError("Wynik musi mieć nową nazwę; nie wolno nadpisywać plików źródłowych.")
    if any(path.exists() or path.is_symlink() for path in (out_json, report)):
        raise ValueError("Plik wyniku lub raportu już istnieje. Podaj nową nazwę; poprzednie pliki pozostają bez zmian.")
    source_hashes = {p: _hash(p) for p in inputs}
    pred = _read_prediction(prediction_csv, candidate_code)
    data = json.loads(calibration_json.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Kalibracja wejściowa musi być obiektem JSON.")
    _finite_tree(data)
    records = _validate_records(data.get("custom_biquad_records", []))
    previous = next((r for r in records if r["state_id"].strip() == state_id), None)
    if previous and (previous["topology_id"].strip() != topology_id or previous["candidate_code"].strip() != candidate_code):
        raise ValueError("Ten identyfikator stanu należy do innej topologii lub kandydata. Podaj nowy identyfikator.")
    measured = _measured_metrics(measured_mdat, xo_hz)
    if not isinstance(measured, dict) or not {"side_l", "side_r", "technical"} <= measured.keys():
        raise ValueError("Brakuje wymaganych wyników pomiaru kalibracyjnego.")
    measured = {key: _finite(value, "Pomiar/" + key) for key, value in measured.items()}
    record = {
        "topology_id": topology_id,
        "state_id": state_id,
        "candidate_code": candidate_code,
        "prediction_csv": prediction_csv.name,
        "measured_mdat": measured_mdat.name,
        "xo_hz": float(xo_hz),
        "pred_side_l": pred["side_l"], "pred_side_r": pred["side_r"], "pred_technical": pred["technical"],
        "measured_side_l": measured["side_l"], "measured_side_r": measured["side_r"], "measured_technical": measured["technical"],
        "bias_side_l": measured["side_l"]-pred["side_l"],
        "bias_side_r": measured["side_r"]-pred["side_r"],
        "bias_technical": measured["technical"]-pred["technical"],
        "prediction_csv_sha256": source_hashes[prediction_csv],
        "measured_mdat_sha256": source_hashes[measured_mdat],
        "calibration_input_sha256": source_hashes[calibration_json],
        "verification_scope": "PREDICTION_ID_AND_SOURCE_HASHES",
    }
    records = [r for r in records if r["state_id"].strip() != state_id]
    records.append(record)
    data["schema_version"] = 2
    data["custom_biquad_records"] = records
    aggregate = _aggregate(records)
    custom = dict(data.get("custom_biquad", {}))
    custom.update(aggregate)
    custom.pop("sample_count", None)
    data["custom_biquad"] = custom
    _finite_tree(data)
    json_text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    report_text = (
        "# Aktualizacja kalibracji predykcja–pomiar\n\n"
        f"- Topologia: **{topology_id}**\n- Stan: **{state_id}**\n- Kandydat: `{candidate_code}`\n"
        f"- Predykcja L/P/techniczna: **{pred['side_l']:.2f} / {pred['side_r']:.2f} / {pred['technical']:.2f}**\n"
        f"- Pomiar L/P/techniczna: **{measured['side_l']:.2f} / {measured['side_r']:.2f} / {measured['technical']:.2f}**\n"
        f"- Bias L/P/techniczny: **{record['bias_side_l']:+.2f} / {record['bias_side_r']:+.2f} / {record['bias_technical']:+.2f}**\n"
        f"- Stany walidacyjne: **{aggregate['validation_state_count']}**\n"
        f"- Niezależne topologie: **{aggregate['independent_topology_count']}**\n\n"
        "Ten sam topology_id jest liczony jako jeden niezależny kształt, nawet gdy ma kilka stanów delayu.\n\n"
        "Powiązanie obejmuje dokładny kod predykcji i SHA-256 źródeł. Nie potwierdza automatycznie ustawień sprzętu, warunków sesji ani tego, że opisane ID odpowiada rzeczywistemu kształtowi filtru.\n"
    )
    if any(_hash(path) != digest for path, digest in source_hashes.items()):
        raise ValueError("Plik źródłowy zmienił się podczas obliczeń. Nie zapisano kalibracji; uruchom zadanie ponownie.")
    _publish_pair(out_json, json_text, report, report_text)
    return out_json


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Kalibrator predykcja–pomiar REWDO v20.31")
    ap.add_argument("--prediction-csv", required=True)
    ap.add_argument("--candidate-code", required=True)
    ap.add_argument("--measured-mdat", required=True)
    ap.add_argument("--xo-hz", required=True, type=float)
    ap.add_argument("--topology-id", required=True, help="Ten sam kształt filtru zawsze z tym samym identyfikatorem")
    ap.add_argument("--state-id", required=True, help="Unikatowy stan pomiarowy, np. ABD92_RSUB17.52")
    ap.add_argument("--calibration-json", required=True)
    ap.add_argument("--out-json", required=True)
    args = ap.parse_args(argv)
    try:
        out = update_calibration(
            prediction_csv=Path(args.prediction_csv), candidate_code=args.candidate_code,
            measured_mdat=Path(args.measured_mdat), xo_hz=float(args.xo_hz),
            topology_id=args.topology_id, state_id=args.state_id,
            calibration_json=Path(args.calibration_json), out_json=Path(args.out_json),
        )
    except Exception as exc:
        print("BŁĄD KALIBRATORA:", exc)
        return 2
    print("[OK] Zapisano:", out)
    print("[OK] Raport:", out.with_suffix('.md'))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
