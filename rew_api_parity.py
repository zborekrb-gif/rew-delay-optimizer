# -*- coding: utf-8 -*-
"""Kontrola zgodności natywnej migawki REW API z plikiem MDAT."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

import numpy as np

DEFAULT_LIMITS = {
    "splMedianAbsDb": 0.05,
    "splP95AbsDb": 0.20,
    "phaseMedianAbsDeg": 0.50,
    "phaseP95AbsDeg": 2.00,
}


def _percentile(values: np.ndarray, q: float) -> float:
    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    return float(np.percentile(finite, q)) if finite.size else float("inf")


def compare_projects(api_project: Any, mdat_project: Any, *, low_hz: float = 20.0, high_hz: float = 250.0, roles: Sequence[str] = ("L main", "L sub", "R main", "R sub"), limits: Mapping[str, float] = DEFAULT_LIMITS) -> Dict[str, Any]:
    rows = []
    for role in roles:
        a = api_project.measurements_by_role[role]
        b = mdat_project.measurements_by_role[role]
        lo = max(float(a.freq[0]), float(b.freq[0]), float(low_hz))
        hi = min(float(a.freq[-1]), float(b.freq[-1]), float(high_hz))
        target = np.asarray(a.freq[(a.freq >= lo) & (a.freq <= hi)], dtype=float)
        if target.size < 16:
            raise ValueError(f"Za mało wspólnych punktów dla roli {role}: {target.size}.")
        b_spl = np.interp(target, b.freq, b.spl_db)
        spl_abs = np.abs(np.interp(target, a.freq, a.spl_db) - b_spl)
        a_phase = np.interp(target, a.freq, np.unwrap(np.deg2rad(a.phase_deg)))
        b_phase = np.interp(target, b.freq, np.unwrap(np.deg2rad(b.phase_deg)))
        phase_delta = np.rad2deg(np.angle(np.exp(1j * (a_phase - b_phase))))
        phase_abs = np.abs(phase_delta)
        rows.append({
            "role": role,
            "points": int(target.size),
            "rangeHz": [float(target[0]), float(target[-1])],
            "splMedianAbsDb": float(np.median(spl_abs)),
            "splP95AbsDb": _percentile(spl_abs, 95),
            "splMaxAbsDb": float(np.max(spl_abs)),
            "phaseMedianAbsDeg": float(np.median(phase_abs)),
            "phaseP95AbsDeg": _percentile(phase_abs, 95),
            "phaseMaxAbsDeg": float(np.max(phase_abs)),
        })
    aggregate = {
        key: max(float(row[key]) for row in rows)
        for key in ("splMedianAbsDb", "splP95AbsDb", "phaseMedianAbsDeg", "phaseP95AbsDeg")
    }
    failures = [key for key, limit in limits.items() if aggregate[key] > float(limit)]
    return {
        "status": "PASS" if not failures else "FAIL",
        "createdUtc": datetime.now(timezone.utc).isoformat(),
        "rangeHz": [float(low_hz), float(high_hz)],
        "limits": {k: float(v) for k, v in limits.items()},
        "aggregateWorstRole": aggregate,
        "failedMetrics": failures,
        "roles": rows,
    }


def compare_api_snapshot_to_mdat(snapshot_path: str | Path, mdat_path: str | Path, *, update_snapshot: bool = True) -> Dict[str, Any]:
    from rew_mdat import import_mdat_measurements
    from rew_api_client import read_snapshot, verify_snapshot_response_integrity

    snapshot_path = Path(snapshot_path)
    mdat_path = Path(mdat_path)
    payload = read_snapshot(snapshot_path)
    integrity_ok, problems = verify_snapshot_response_integrity(payload)
    if not integrity_ok:
        raise ValueError("Migawka API nie ma potwierdzonej natywnej odpowiedzi bez wygładzania: " + "; ".join(problems))
    api_project = import_mdat_measurements(snapshot_path)
    mdat_project = import_mdat_measurements(mdat_path)
    if not bool(getattr(mdat_project, "phase_metrics_allowed", True)):
        raise ValueError(
            "MDAT Phase Truth Guard nie potwierdził fazy pliku offline; kontrola API–MDAT "
            "nie może porównywać przypadkowej tablicy: "
            + str(getattr(mdat_project, "phase_truth_source", "") or "status BLOCKED")
        )
    result = compare_projects(api_project, mdat_project)
    result["mdatFile"] = mdat_path.name
    result["snapshotFile"] = snapshot_path.name
    if update_snapshot:
        payload["parityCheck"] = result
        # Po dopisaniu FAIL integralność ma blokować eksport.
        ok_after, problems_after = verify_snapshot_response_integrity(payload)
        payload["responseIntegrity"] = {"status": "PASS" if ok_after else "FAIL", "problems": problems_after}
        temp = snapshot_path.with_suffix(snapshot_path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temp.replace(snapshot_path)
    return result


def parity_report_markdown(result: Mapping[str, Any]) -> str:
    lines = [
        "# Kontrola zgodności REW API – MDAT",
        "",
        f"**Status:** {result.get('status', '—')}",
        "",
        "| Rola | SPL mediana | SPL P95 | Faza mediana | Faza P95 |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in result.get("roles", []):
        lines.append(
            f"| {row['role']} | {row['splMedianAbsDb']:.4f} dB | {row['splP95AbsDb']:.4f} dB | "
            f"{row['phaseMedianAbsDeg']:.3f}° | {row['phaseP95AbsDeg']:.3f}° |"
        )
    lines += ["", "Progi PASS: mediana SPL ≤ 0,05 dB, P95 SPL ≤ 0,20 dB, mediana fazy ≤ 0,5°, P95 fazy ≤ 2,0°."]
    return "\n".join(lines) + "\n"
