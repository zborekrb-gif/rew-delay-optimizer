#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Automatyczny kalibrator predykcja–pomiar dla REWDO v20.31.

Wczytuje przewidywanie projektanta z CSV oraz pełny pomiar przed Dirac z REW
(L main, L sub, L strona, R main, R sub, R strona, L+R). Aktualizuje rekordy
kalibracyjne i liczy liczbę niezależnych topologii osobno od liczby stanów.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Dict, Any, Optional
import numpy as np

from rew_mdat import load_measurements_by_roles_from_mdat
from rew_scoring import common_grid, measurement_to_complex, db
from rew_acoustic_crossover_designer import _score_side, _phase_energy_score, _dirac_burden_risk, dip_depth, _band, _safe_mean, _clip
from rew_acoustic_biquad_designer import _main_sub_phase_score, _technical_integration_score, SafetyMetrics

ROLES = ["L main", "L sub", "L strona", "R main", "R sub", "R strona", "L+R"]


def _read_prediction(path: Path, candidate_code: str) -> Dict[str, float]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise ValueError("CSV predykcji jest pusty.")
    matches = [r for r in rows if str(r.get("custom_code", "")).strip() == candidate_code]
    if not matches and len(rows) == 1:
        matches = rows
    if len(matches) != 1:
        found = ", ".join(str(r.get("custom_code", "")) for r in rows[:12])
        raise ValueError(f"Nie znaleziono jednoznacznie kandydata {candidate_code!r}. Dostępne: {found}")
    r = matches[0]
    def f(*keys: str) -> float:
        for key in keys:
            value = r.get(key)
            if value not in (None, ""):
                return float(value)
        raise ValueError("Brak kolumny predykcji: " + " / ".join(keys))
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
    bl = np.asarray([float(r["bias_side_l"]) for r in records], dtype=float)
    br = np.asarray([float(r["bias_side_r"]) for r in records], dtype=float)
    bt = np.asarray([float(r["bias_technical"]) for r in records], dtype=float)
    return {
        "validation_state_count": len(records),
        "independent_topology_count": len({str(r["topology_id"]) for r in records}),
        "side_bias_l": float(np.mean(bl)), "side_bias_r": float(np.mean(br)),
        "side_sigma_l": float(np.std(bl, ddof=1)) if len(bl) > 1 else 0.0,
        "side_sigma_r": float(np.std(br, ddof=1)) if len(br) > 1 else 0.0,
        "technical_bias": float(np.mean(bt)),
        "technical_sigma": float(np.std(bt, ddof=1)) if len(bt) > 1 else 0.0,
    }


def update_calibration(*, prediction_csv: Path, candidate_code: str, measured_mdat: Path,
                       xo_hz: float, topology_id: str, state_id: str,
                       calibration_json: Path, out_json: Path) -> Path:
    pred = _read_prediction(prediction_csv, candidate_code)
    measured = _measured_metrics(measured_mdat, xo_hz)
    data = json.loads(calibration_json.read_text(encoding="utf-8"))
    records = list(data.get("custom_biquad_records", []))
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
    }
    records = [r for r in records if str(r.get("state_id")) != state_id]
    records.append(record)
    data["schema_version"] = 2
    data["custom_biquad_records"] = records
    aggregate = _aggregate(records)
    custom = dict(data.get("custom_biquad", {}))
    custom.update(aggregate)
    custom.pop("sample_count", None)
    data["custom_biquad"] = custom
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")

    report = out_json.with_suffix(".md")
    report.write_text(
        "# Aktualizacja kalibracji predykcja–pomiar\n\n"
        f"- Topologia: **{topology_id}**\n- Stan: **{state_id}**\n- Kandydat: `{candidate_code}`\n"
        f"- Predykcja L/P/techniczna: **{pred['side_l']:.2f} / {pred['side_r']:.2f} / {pred['technical']:.2f}**\n"
        f"- Pomiar L/P/techniczna: **{measured['side_l']:.2f} / {measured['side_r']:.2f} / {measured['technical']:.2f}**\n"
        f"- Bias L/P/techniczny: **{record['bias_side_l']:+.2f} / {record['bias_side_r']:+.2f} / {record['bias_technical']:+.2f}**\n"
        f"- Stany walidacyjne: **{aggregate['validation_state_count']}**\n"
        f"- Niezależne topologie: **{aggregate['independent_topology_count']}**\n\n"
        "Ten sam topology_id jest liczony jako jeden niezależny kształt, nawet gdy ma kilka stanów delayu.\n",
        encoding="utf-8",
    )
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
