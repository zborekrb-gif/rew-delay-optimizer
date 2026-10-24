#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Candidate Safety Gate — oddzielony od integralności wejścia."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np


class InputIntegrityError(ValueError):
    """Błąd wejścia: proces kończy się kodem 2."""


class NoSafeCandidateError(RuntimeError):
    """Analiza była poprawna, lecz nie wolno wyeksportować kandydata (kod 3)."""


@dataclass(frozen=True)
class CandidateSafetyResult:
    input_integrity_ok: bool
    candidate_safe_to_export: bool
    reasons: tuple[str, ...]
    effective_xo_hz: float
    effective_xo_min_hz: float
    effective_xo_max_hz: float


def _number(candidate: object, name: str, default: float = float("nan")) -> float:
    try:
        return float(getattr(candidate, name))
    except (AttributeError, TypeError, ValueError):
        return float(default)


def evaluate_candidate_safety(
    candidate: object,
    *,
    input_integrity_ok: bool,
    phase_integrity_ok: bool,
    shd_representable: bool = True,
    gain_state_verified: bool = True,
    gain_state_complete: bool = True,
    reference_anchor_verified: bool = True,
    effective_xo_min_hz: float = 70.0,
    effective_xo_max_hz: float = 105.0,
    minimum_side_score: float = 55.0,
    minimum_body_score: float = 45.0,
    minimum_lr_sum_score: float = 45.0,
) -> CandidateSafetyResult:
    reasons: list[str] = []
    if not input_integrity_ok:
        reasons.append("Input Integrity Gate nie przeszedł")

    decision_class = str(getattr(candidate, "decision_class", "")).upper()
    if "DIAGNOSTYKA" in decision_class or not decision_class:
        reasons.append("klasa decyzji nie dopuszcza eksportu")

    side_min = _number(candidate, "side_min")
    body = _number(candidate, "body_score")
    lr_sum = _number(candidate, "lr_sum_score")
    effective_xo = _number(candidate, "effective_xo_hz")
    if not np.isfinite(side_min) or side_min < float(minimum_side_score):
        reasons.append(f"minimum strony {side_min:.1f}/100 < {float(minimum_side_score):.1f}")
    if not np.isfinite(body) or body < float(minimum_body_score):
        reasons.append(f"ciągłość 100–220 Hz {body:.1f}/100 < {float(minimum_body_score):.1f}")
    if not np.isfinite(lr_sum) or lr_sum < float(minimum_lr_sum_score):
        reasons.append(f"suma L+R {lr_sum:.1f}/100 < {float(minimum_lr_sum_score):.1f}")
    if not np.isfinite(effective_xo) or not float(effective_xo_min_hz) <= effective_xo <= float(effective_xo_max_hz):
        reasons.append(
            f"efektywne XO {effective_xo:.1f} Hz poza limitem "
            f"{float(effective_xo_min_hz):.1f}–{float(effective_xo_max_hz):.1f} Hz"
        )
    if not phase_integrity_ok:
        reasons.append("integralność fazy/sumy nie została potwierdzona")
    if not shd_representable:
        reasons.append("filtr nie jest reprezentowalny w miniDSP SHD")
    if not gain_state_complete:
        reasons.append("brak pełnego stanu Dirac i odsłuchowego")
    if not gain_state_verified:
        reasons.append("stan gainu nie został potwierdzony Gain Bridge")
    if not reference_anchor_verified:
        reasons.append("referencja delayu nie jest zweryfikowana albo pozostaje w konflikcie")

    finite_fields = (
        "acoustic_score", "side_min", "body_score", "lr_sum_score",
        "effective_xo_hz", "l_sub_gain_db", "r_sub_gain_db",
        "local_gain_l_db", "local_gain_r_db", "final_gain_l_db", "final_gain_r_db",
    )
    for field in finite_fields:
        value = getattr(candidate, field, None)
        if value is not None and not np.isfinite(_number(candidate, field)):
            reasons.append(f"NaN/Inf w polu {field}")
    for field in (
        "l_sub_gain_db", "r_sub_gain_db", "local_gain_l_db", "local_gain_r_db",
        "final_gain_l_db", "final_gain_r_db",
    ):
        value = getattr(candidate, field, None)
        if value is not None and abs(_number(candidate, field)) > 12.0:
            reasons.append(f"brak headroomu: {field}={_number(candidate, field):+.1f} dB")

    return CandidateSafetyResult(
        input_integrity_ok=bool(input_integrity_ok),
        candidate_safe_to_export=bool(input_integrity_ok and not reasons),
        reasons=tuple(dict.fromkeys(reasons)),
        effective_xo_hz=effective_xo,
        effective_xo_min_hz=float(effective_xo_min_hz),
        effective_xo_max_hz=float(effective_xo_max_hz),
    )


def combine_required_state_safety(
    base: CandidateSafetyResult,
    state_results: Mapping[str, CandidateSafetyResult],
    *,
    required_states: tuple[str, ...] = ("dirac", "listening"),
) -> CandidateSafetyResult:
    """Połącz bramkę kandydata z obowiązkowymi stanami gainu.

    Brak któregokolwiek stanu jest blokadą. Dobry wynik w jednym stanie nigdy
    nie kompensuje porażki w drugim.
    """
    reasons = list(base.reasons)
    all_states_ok = True
    for state in required_states:
        result = state_results.get(state)
        if result is None:
            reasons.append(f"brak oceny wymaganego stanu {state}")
            all_states_ok = False
            continue
        if not result.candidate_safe_to_export:
            all_states_ok = False
            reasons.extend(f"{state}: {reason}" for reason in result.reasons)
    return CandidateSafetyResult(
        input_integrity_ok=base.input_integrity_ok,
        candidate_safe_to_export=bool(base.candidate_safe_to_export and all_states_ok),
        reasons=tuple(dict.fromkeys(reasons)),
        effective_xo_hz=base.effective_xo_hz,
        effective_xo_min_hz=base.effective_xo_min_hz,
        effective_xo_max_hz=base.effective_xo_max_hz,
    )


def write_quarantine(
    out_dir: str | Path,
    candidates: Iterable[object],
    results: Iterable[CandidateSafetyResult],
    *,
    mode: str,
) -> tuple[Path, Path, Path]:
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    for candidate, result in zip(candidates, results):
        rows.append({
            "mode": mode,
            "code": str(getattr(candidate, "code", "")),
            "decision_class": str(getattr(candidate, "decision_class", "")),
            "candidate_safe_to_export": result.candidate_safe_to_export,
            "effective_xo_hz": result.effective_xo_hz,
            "effective_xo_min_hz": result.effective_xo_min_hz,
            "effective_xo_max_hz": result.effective_xo_max_hz,
            "side_min": _number(candidate, "side_min"),
            "body_score": _number(candidate, "body_score"),
            "lr_sum_score": _number(candidate, "lr_sum_score"),
            "global_gain_l_db": getattr(candidate, "global_gain_l_db", None),
            "global_gain_r_db": getattr(candidate, "global_gain_r_db", None),
            "local_gain_l_db": getattr(candidate, "l_sub_gain_db", getattr(candidate, "local_gain_l_db", None)),
            "local_gain_r_db": getattr(candidate, "r_sub_gain_db", getattr(candidate, "local_gain_r_db", None)),
            "final_gain_l_db": getattr(candidate, "final_gain_l_db", None),
            "final_gain_r_db": getattr(candidate, "final_gain_r_db", None),
            "reasons": list(result.reasons),
        })

    json_path = root / "QUARANTINE_DIAGNOSTIC.json"
    csv_path = root / "QUARANTINE_DIAGNOSTIC.csv"
    marker_path = root / "NIE_WGRYWAC_DO_MINIDSP.txt"
    json_path.write_text(json.dumps({"mode": mode, "candidates": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fieldnames = list(rows[0]) if rows else ["mode", "code", "candidate_safe_to_export", "reasons"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            serialised = dict(row)
            serialised["reasons"] = " | ".join(row.get("reasons", []))
            writer.writerow(serialised)
    marker_path.write_text(
        "NIE WGRYWAĆ DO miniDSP.\n"
        "Analiza została wykonana, lecz żaden kandydat nie przeszedł Candidate Safety Gate.\n"
        "Szczegóły: QUARANTINE_DIAGNOSTIC.json oraz QUARANTINE_DIAGNOSTIC.csv.\n",
        encoding="utf-8",
    )
    return json_path, csv_path, marker_path
