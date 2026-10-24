#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Amplitude-only gain bridge from a RAW session to another gain state.

The bridge intentionally does not use phase. It combines MAIN, SUB and the
measured RAW side SUM to infer their acoustic cosine relationship, then solves
the second measured side SUM for the subwoofer gain multiplier. This keeps the
workflow valid when an L+R bridge trace cannot be measured safely or cannot use
an acoustic timing reference with a two-channel output device.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import numpy as np

from rew_mdat import (
    _MdatNumericRecord,
    _decode_mdat_numeric_records,
    _scan_java_serialized_float_arrays,
    scan_mdat_project,
)


@dataclass
class GainBridgeSide:
    side: str
    gain_db: float
    p10_db: float
    p90_db: float
    valid_points: int
    total_points: int
    raw_sum_count: int
    raw_repeat_spread_median_db: float
    raw_repeat_spread_p90_db: float
    total_sum_delta_median_db: float
    classification: str
    unique_points: int
    ambiguous_points: int
    invalid_points: int
    ambiguous_fraction: float
    monotonic_fraction: float
    ambiguous_low_median_db: float
    ambiguous_high_median_db: float


@dataclass
class GainBridgeAudit:
    status: str
    raw_path: Path
    bridge_path: Path
    left: GainBridgeSide
    right: GainBridgeSide
    reason: str


def _records(path: Path, required_roles: set[str]) -> List[_MdatNumericRecord]:
    data = path.read_bytes()
    scan = scan_mdat_project(path)
    infos = list(scan.get("measurements", []))
    records, warnings = _decode_mdat_numeric_records(
        data,
        infos,
        _scan_java_serialized_float_arrays(data),
    )
    required_names = {info.name for info in infos if info.role in required_roles}
    relevant_warnings = [
        warning for warning in warnings
        if any(warning.startswith(name + ":") for name in required_names)
    ]
    if relevant_warnings:
        raise ValueError("; ".join(relevant_warnings[:4]))
    return records


def _by_role(records: List[_MdatNumericRecord]) -> Dict[str, List[_MdatNumericRecord]]:
    result: Dict[str, List[_MdatNumericRecord]] = {}
    for record in records:
        result.setdefault(record.info.role, []).append(record)
    return result


def _interp_amp(record: _MdatNumericRecord, grid: np.ndarray) -> np.ndarray:
    spl = np.interp(grid, record.freq, record.spl_db)
    return np.power(10.0, spl / 20.0)


def _estimate_side_gain(
    side: str,
    main: _MdatNumericRecord,
    sub: _MdatNumericRecord,
    raw_sums: List[_MdatNumericRecord],
    bridge_sum: _MdatNumericRecord,
    expected_gain_db: float = 6.0,
) -> GainBridgeSide:
    if not raw_sums:
        raise ValueError(f"{side}: brak RAW_{side}_SUM do mostka poziomu.")
    lo = max(35.0, float(main.freq[0]), float(sub.freq[0]), float(bridge_sum.freq[0]), *(float(x.freq[0]) for x in raw_sums))
    hi = min(140.0, float(main.freq[-1]), float(sub.freq[-1]), float(bridge_sum.freq[-1]), *(float(x.freq[-1]) for x in raw_sums))
    if hi <= lo:
        raise ValueError(f"{side}: brak wspólnego pasma 35–140 Hz.")
    base = max([main, sub, bridge_sum, *raw_sums], key=lambda record: record.freq.size)
    grid = np.asarray(base.freq[(base.freq >= lo) & (base.freq <= hi)], dtype=float)
    if grid.size < 40:
        raise ValueError(f"{side}: za mało punktów wspólnego pasma do mostka poziomu.")

    main_amp = _interp_amp(main, grid)
    sub_amp = _interp_amp(sub, grid)
    raw_sum_amps = np.vstack([_interp_amp(record, grid) for record in raw_sums])
    raw_sum_amp = np.median(raw_sum_amps, axis=0)
    bridge_amp = _interp_amp(bridge_sum, grid)

    denom = 2.0 * main_amp * sub_amp
    cosine = (np.square(raw_sum_amp) - np.square(main_amp) - np.square(sub_amp)) / np.maximum(denom, 1e-30)
    feasible = (cosine >= -1.02) & (cosine <= 1.02)
    cosine = np.clip(cosine, -1.0, 1.0)
    discriminant = np.square(main_amp * cosine) - np.square(main_amp) + np.square(bridge_amp)
    root = np.sqrt(np.maximum(discriminant, 0.0))
    denom_sub = np.maximum(sub_amp, 1e-30)
    root_plus = (-main_amp * cosine + root) / denom_sub
    root_minus = (-main_amp * cosine - root) / denom_sub
    root_plus_db = 20.0 * np.log10(np.maximum(root_plus, 1e-30))
    root_minus_db = 20.0 * np.log10(np.maximum(root_minus, 1e-30))

    # expected_gain_db jest wyłącznie późniejszą kontrolą zgodności. Nie wolno
    # używać go do rozstrzygania gałęzi, bo wtedy oczekiwanie staje się dowodem.
    _ = expected_gain_db
    main_db = 20.0 * np.log10(np.maximum(main_amp, 1e-30))
    sub_db = 20.0 * np.log10(np.maximum(sub_amp, 1e-30))
    base_valid = (
        feasible
        & (discriminant >= 0.0)
        & (np.abs(main_db - sub_db) <= 28.0)
    )
    plus_admissible = (
        base_valid & np.isfinite(root_plus_db) & (root_plus > 0.0)
        & (root_plus_db >= -12.0) & (root_plus_db <= 12.0)
    )
    minus_admissible = (
        base_valid & np.isfinite(root_minus_db) & (root_minus > 0.0)
        & (root_minus_db >= -12.0) & (root_minus_db <= 12.0)
    )
    unique = np.logical_xor(plus_admissible, minus_admissible)
    ambiguous = plus_admissible & minus_admissible
    selected_linear = np.where(plus_admissible, root_plus, root_minus)
    selected_db = 20.0 * np.log10(np.maximum(selected_linear, 1e-30))

    # |M + gS|^2 jest parabolą. Gałąź jest monotonicznie identyfikowalna
    # między stanem RAW g=1 i stanem bridge tylko wtedy, gdy w tym przedziale
    # nie leży wierzchołek -M*cos(phi)/S.
    vertex = -main_amp * cosine / denom_sub
    interval_low = np.minimum(1.0, selected_linear)
    interval_high = np.maximum(1.0, selected_linear)
    crosses_vertex = (vertex > interval_low + 1e-9) & (vertex < interval_high - 1e-9)
    monotonic_unique = unique & ~crosses_vertex
    valid = base_valid & monotonic_unique & np.isfinite(selected_db)

    base_count = int(np.count_nonzero(base_valid))
    unique_count = int(np.count_nonzero(unique))
    ambiguous_count = int(np.count_nonzero(ambiguous))
    valid_count = int(np.count_nonzero(valid))
    ambiguous_fraction = float(ambiguous_count / max(1, base_count))
    monotonic_fraction = float(valid_count / max(1, unique_count))
    if ambiguous_fraction > 0.10:
        classification = "AMBIGUOUS"
    elif valid_count < 40 or valid_count / max(1, base_count) < 0.20:
        classification = "INVALID"
    else:
        classification = "UNIQUE"

    values = selected_db[valid]
    gain_value = float(np.median(values)) if values.size else float("nan")
    p10_value = float(np.percentile(values, 10.0)) if values.size else float("nan")
    p90_value = float(np.percentile(values, 90.0)) if values.size else float("nan")
    if ambiguous_count:
        ambiguous_low = np.minimum(root_plus_db[ambiguous], root_minus_db[ambiguous])
        ambiguous_high = np.maximum(root_plus_db[ambiguous], root_minus_db[ambiguous])
        ambiguous_low_median = float(np.median(ambiguous_low))
        ambiguous_high_median = float(np.median(ambiguous_high))
    else:
        ambiguous_low_median = float("nan")
        ambiguous_high_median = float("nan")
    raw_sum_db = 20.0 * np.log10(np.maximum(raw_sum_amps, 1e-30))
    repeat_spread = np.max(raw_sum_db, axis=0) - np.min(raw_sum_db, axis=0)
    total_delta = 20.0 * np.log10(np.maximum(bridge_amp, 1e-30) / np.maximum(raw_sum_amp, 1e-30))
    return GainBridgeSide(
        side=side,
        gain_db=gain_value,
        p10_db=p10_value,
        p90_db=p90_value,
        valid_points=valid_count,
        total_points=int(grid.size),
        raw_sum_count=len(raw_sums),
        raw_repeat_spread_median_db=float(np.median(repeat_spread)),
        raw_repeat_spread_p90_db=float(np.percentile(repeat_spread, 90.0)),
        total_sum_delta_median_db=float(np.median(total_delta)),
        classification=classification,
        unique_points=unique_count,
        ambiguous_points=ambiguous_count,
        invalid_points=int(grid.size - unique_count - ambiguous_count),
        ambiguous_fraction=ambiguous_fraction,
        monotonic_fraction=monotonic_fraction,
        ambiguous_low_median_db=ambiguous_low_median,
        ambiguous_high_median_db=ambiguous_high_median,
    )


def analyze_gain_bridge(raw_path: Path, bridge_path: Path, expected_gain_db: float = 6.0) -> GainBridgeAudit:
    expected_gain_db = float(expected_gain_db)
    if not np.isfinite(expected_gain_db) or not 0.0 <= expected_gain_db <= 12.0:
        raise ValueError("Oczekiwany skok gainu musi być skończoną wartością 0..12 dB.")
    raw_by_role = _by_role(_records(Path(raw_path), {"L main", "L sub", "R main", "R sub", "L sum", "R sum"}))
    bridge_by_role = _by_role(_records(Path(bridge_path), {"L sum", "R sum"}))
    required_raw = ["L main", "L sub", "R main", "R sub", "L sum", "R sum"]
    missing_raw = [role for role in required_raw if not raw_by_role.get(role)]
    if missing_raw:
        raise ValueError("RAW: brakuje ról " + ", ".join(missing_raw) + ".")
    for role in ("L main", "L sub", "R main", "R sub"):
        if len(raw_by_role.get(role, [])) != 1:
            raise ValueError(f"RAW wymaga dokładnie jednego pomiaru {role}.")
    for role in ("L sum", "R sum"):
        if len(raw_by_role.get(role, [])) < 2:
            raise ValueError(f"RAW wymaga co najmniej dwóch powtórzeń {role}, aby ocenić powtarzalność.")
    for role in ("L sum", "R sum"):
        if len(bridge_by_role.get(role, [])) != 1:
            raise ValueError(f"Gain bridge wymaga dokładnie jednego pomiaru {role}.")

    left = _estimate_side_gain(
        "L",
        raw_by_role["L main"][0], raw_by_role["L sub"][0], raw_by_role["L sum"], bridge_by_role["L sum"][0],
        expected_gain_db=expected_gain_db,
    )
    right = _estimate_side_gain(
        "R",
        raw_by_role["R main"][0], raw_by_role["R sub"][0], raw_by_role["R sum"], bridge_by_role["R sum"][0],
        expected_gain_db=expected_gain_db,
    )

    widths = [left.p90_db - left.p10_db, right.p90_db - right.p10_db]
    checks = [
        left.classification == "UNIQUE",
        right.classification == "UNIQUE",
        np.isfinite(left.gain_db),
        np.isfinite(right.gain_db),
        abs(left.gain_db - expected_gain_db) <= 0.75,
        abs(right.gain_db - expected_gain_db) <= 0.75,
        abs(left.gain_db - right.gain_db) <= 0.50,
        max(widths) <= 1.75,
        left.valid_points / max(1, left.total_points) >= 0.65,
        right.valid_points / max(1, right.total_points) >= 0.65,
        left.raw_repeat_spread_median_db <= 0.50,
        right.raw_repeat_spread_median_db <= 0.50,
        left.raw_repeat_spread_p90_db <= 1.00,
        right.raw_repeat_spread_p90_db <= 1.00,
        left.ambiguous_fraction <= 0.10,
        right.ambiguous_fraction <= 0.10,
        left.monotonic_fraction >= 0.85,
        right.monotonic_fraction >= 0.85,
    ]
    if all(checks):
        status = "VERIFIED"
        reason = "Obie strony potwierdzają oczekiwany skok gainu bez użycia fazy ani L+R bridge."
    else:
        status = "BLOCKED"
        reason = (
            "Mostek poziomu nie potwierdza jednoznacznego, monotonicznego i zgodnego skoku gainu po obu stronach. "
            f"Klasyfikacja L/R: {left.classification}/{right.classification}; "
            f"udział niejednoznaczny {left.ambiguous_fraction:.1%}/{right.ambiguous_fraction:.1%}."
        )
    return GainBridgeAudit(status, Path(raw_path), Path(bridge_path), left, right, reason)


def write_gain_bridge_report(audit: GainBridgeAudit, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "GAIN_BRIDGE_CHECK.md"
    lines = [
        "# Gain Bridge Check — v20.31",
        "",
        f"- Status: **{audit.status}**",
        f"- RAW: `{audit.raw_path.name}`",
        f"- Bridge: `{audit.bridge_path.name}`",
        f"- Metoda: **amplitudowa MAIN/SUB/SUM; faza i L+R bridge nie są wymagane**",
        f"- Wniosek: {audit.reason}",
        "",
        "| Strona | Klasa | Wyliczony gain suba | P10–P90 | Unique / ambiguous / invalid | Niejednoznaczność | Monotoniczność | Powtórzenia SUM | Powtarzalność RAW mediana / P90 | Zmiana całej sumy |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for side in (audit.left, audit.right):
        lines.append(
            f"| {side.side} | **{side.classification}** | **{side.gain_db:+.2f} dB** | {side.p10_db:+.2f}…{side.p90_db:+.2f} dB | "
            f"{side.unique_points}/{side.ambiguous_points}/{side.invalid_points} | {side.ambiguous_fraction:.1%} | "
            f"{side.monotonic_fraction:.1%} | {side.raw_sum_count} | {side.raw_repeat_spread_median_db:.2f} / "
            f"{side.raw_repeat_spread_p90_db:.2f} dB | {side.total_sum_delta_median_db:+.2f} dB |"
        )
        if side.ambiguous_points:
            lines.append(
                f"| {side.side} — gałęzie niejednoznaczne | — | {side.ambiguous_low_median_db:+.2f} / "
                f"{side.ambiguous_high_median_db:+.2f} dB | — | — | — | — | — | — | — |"
            )
    lines.extend([
        "",
        "Brak `LR_SUM_SUB0` jest prawidłowy: ten plik kalibruje zmianę poziomu osobno dla L i R. "
        "Pominięcie pomiaru stereo zapobiega przesterowaniu i nie ogranicza wyznaczenia gainu subwooferów.",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="REWDO v20.31 — amplitudowy Gain Bridge z kontrolą obu gałęzi")
    parser.add_argument("--raw", required=True, help="Pełny RAW z MAIN/SUB/SUM przy niższym gainie subów")
    parser.add_argument("--bridge", required=True, help="Plik z L_SUM_SUB0 i R_SUM_SUB0")
    parser.add_argument("--out", required=True, help="Folder raportu")
    parser.add_argument("--expected-db", type=float, default=6.0, help="Oczekiwany skok gainu")
    args = parser.parse_args(argv)
    try:
        audit = analyze_gain_bridge(Path(args.raw), Path(args.bridge), float(args.expected_db))
        report = write_gain_bridge_report(audit, Path(args.out))
    except Exception as exc:
        print("BŁĄD GAIN BRIDGE:", exc)
        return 2
    print("GAIN BRIDGE:", audit.status)
    print("L/R:", f"{audit.left.gain_db:+.2f}", f"{audit.right.gain_db:+.2f}", "dB")
    print("Raport:", report)
    return 0 if audit.status == "VERIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
