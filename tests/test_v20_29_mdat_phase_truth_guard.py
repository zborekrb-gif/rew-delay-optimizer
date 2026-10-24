from __future__ import annotations

from pathlib import Path

import numpy as np

from rew_common import MdatMeasurementInfo
from rew_mdat import (
    _MdatNumericRecord,
    _MdatPhaseCandidate,
    _looks_like_rew_phase_array,
    _phase_truth_guard,
    import_mdat_measurements,
    infer_mdat_measurement_role,
)
from rew_post_dirac_ab import _is_candidate_measurement_name, _role_from_name


def _record(name: str, role: str, freq: np.ndarray, z: np.ndarray, phases: list[np.ndarray]) -> _MdatNumericRecord:
    spl = 20.0 * np.log10(np.maximum(np.abs(z), 1e-15))
    candidates = [
        _MdatPhaseCandidate(i + 1, 1000 + i, freq.copy(), np.asarray(phase, dtype=float).copy())
        for i, phase in enumerate(phases)
    ]
    return _MdatNumericRecord(
        info=MdatMeasurementInfo(1, name, role, 100),
        freq=freq.copy(),
        spl_db=spl,
        phase_candidates=candidates,
    )


def _phase(z: np.ndarray) -> np.ndarray:
    return np.rad2deg(np.unwrap(np.angle(z)))


def _synthetic_records(with_sums: bool = True, two_candidates: bool = True) -> list[_MdatNumericRecord]:
    freq = np.linspace(20.0, 250.0, 629)
    main_mag = 1.0 / np.sqrt(1.0 + np.power(82.0 / freq, 8.0))
    sub_mag = 0.92 / np.sqrt(1.0 + np.power(freq / 96.0, 8.0))
    main_l = main_mag * np.exp(1j * np.deg2rad(-0.42 * freq))
    sub_l = sub_mag * np.exp(1j * np.deg2rad(-0.42 * freq - 24.0))
    main_r = 0.97 * main_mag * np.exp(1j * np.deg2rad(-0.39 * freq + 4.0))
    sub_r = 0.90 * sub_mag * np.exp(1j * np.deg2rad(-0.39 * freq - 19.0))

    def candidates(z: np.ndarray, wrong: float) -> list[np.ndarray]:
        correct = _phase(z)
        return [correct + wrong, correct] if two_candidates else [correct]

    records = [
        _record("L_MAIN", "L main", freq, main_l, candidates(main_l, 67.0)),
        _record("L_SUB", "L sub", freq, sub_l, candidates(sub_l, -51.0)),
        _record("R_MAIN", "R main", freq, main_r, candidates(main_r, 61.0)),
        _record("R_SUB", "R sub", freq, sub_r, candidates(sub_r, -47.0)),
    ]
    if with_sums:
        for repeat, jitter in enumerate((0.0, 0.08, -0.06), start=1):
            l_sum = (main_l + sub_l) * np.power(10.0, jitter / 20.0)
            r_sum = (main_r + sub_r) * np.power(10.0, -jitter / 20.0)
            records.append(_record(
                f"L_SUM{repeat}", "L sum", freq, l_sum,
                [_phase(l_sum) + 73.0, _phase(l_sum)] if two_candidates else [_phase(l_sum)],
            ))
            records.append(_record(
                f"R_SUM{repeat}", "R sum", freq, r_sum,
                [_phase(r_sum) - 69.0, _phase(r_sum)] if two_candidates else [_phase(r_sum)],
            ))
    return records


def test_sum_repeat_names_are_recognized():
    assert infer_mdat_measurement_role("S2_L_SUM1") == "L sum"
    assert infer_mdat_measurement_role("S2_R_SUM3") == "R sum"
    assert infer_mdat_measurement_role("S2_LR_SUM2") == "L+R"


def test_post_dirac_ignores_rew_excl_incl_metadata_before_lr_detection():
    real_excl = "EXCL: Głośniki (SHD Series), SPEAKER, L+R, volume: 1.000"
    timing_excl = "EXCL: Mikrofon (Umik-1), MICROPHONE, L, volume: 1.000. Timing signal peak level -30.6 dBFS"
    incl = "INCL: Głośniki (SHD Series), SPEAKER, L+R, volume: 1.000"
    for name in (real_excl, timing_excl, incl):
        assert _is_candidate_measurement_name(name) is False
        assert _role_from_name(name) == "other"
    assert _is_candidate_measurement_name("90_BW48_slot4_L+R") is True


def test_truth_guard_selects_second_candidate_from_three_measured_sums():
    selected, report = _phase_truth_guard(
        _synthetic_records(with_sums=True, two_candidates=True),
        ["L main", "L sub", "R main", "R sub"],
    )
    assert report["status"] == "VERIFIED"
    assert report["phase_metrics_allowed"] is True
    assert {role: candidate.ordinal for role, candidate in selected.items()} == {
        "L main": 2, "L sub": 2, "R main": 2, "R sub": 2,
    }
    for side in report["sides"]:
        assert side["sum_count"] == 3
        assert side["spl_rms_db"] < 0.10
        assert side["phase_rms_deg"] < 0.10


def test_full_range_unwrapped_sub_phase_above_300k_is_retained():
    phase = np.linspace(120.0, -1_250_000.0, 54_587)
    assert _looks_like_rew_phase_array(phase) is True


def test_truth_guard_accepts_low_margin_runner_with_equivalent_relative_phase():
    records = _synthetic_records(with_sums=True, two_candidates=False)
    for record in records:
        if record.info.role in {"L main", "R main"}:
            correct = record.phase_candidates[0].phase_deg.copy()
            record.phase_candidates = [
                _MdatPhaseCandidate(1, 1000, record.freq.copy(), correct),
                _MdatPhaseCandidate(2, 1001, record.freq.copy(), correct + 2.0),
            ]
    selected, report = _phase_truth_guard(
        records,
        ["L main", "L sub", "R main", "R sub"],
    )
    assert report["status"] == "VERIFIED"
    assert report["phase_metrics_allowed"] is True
    assert set(selected) == {"L main", "L sub", "R main", "R sub"}
    assert all(side["runner_equivalent"] is True for side in report["sides"])


def test_truth_guard_still_blocks_low_margin_runner_with_different_relative_phase():
    records = _synthetic_records(with_sums=True, two_candidates=False)
    for record in records:
        if record.info.role in {"L main", "R main"}:
            correct = record.phase_candidates[0].phase_deg.copy()
            record.phase_candidates = [
                _MdatPhaseCandidate(1, 1000, record.freq.copy(), correct),
                _MdatPhaseCandidate(2, 1001, record.freq.copy(), correct + 7.0),
            ]
    _selected, report = _phase_truth_guard(
        records,
        ["L main", "L sub", "R main", "R sub"],
    )
    assert report["status"] == "BLOCKED"
    assert report["phase_metrics_allowed"] is False
    assert all(side["runner_equivalent"] is False for side in report["sides"])


def test_truth_guard_blocks_multiple_candidates_without_measured_sums():
    selected, report = _phase_truth_guard(
        _synthetic_records(with_sums=False, two_candidates=True),
        ["L main", "L sub", "R main", "R sub"],
    )
    assert selected == {}
    assert report["status"] == "BLOCKED"
    assert report["phase_metrics_allowed"] is False
    assert set(report["unresolved_roles"]) == {"L main", "L sub", "R main", "R sub"}


def test_truth_guard_allows_unique_structural_candidate_without_sums():
    selected, report = _phase_truth_guard(
        _synthetic_records(with_sums=False, two_candidates=False),
        ["L main", "L sub", "R main", "R sub"],
    )
    assert set(selected) == {"L main", "L sub", "R main", "R sub"}
    assert report["status"] == "STRUCTURAL_ONLY"
    assert report["phase_metrics_allowed"] is True


def test_truth_guard_blocks_sum_that_cannot_be_reconstructed():
    records = _synthetic_records(with_sums=True, two_candidates=False)
    for record in records:
        if record.info.role in {"L sum", "R sum"}:
            record.spl_db = record.spl_db + 4.0
    _selected, report = _phase_truth_guard(
        records,
        ["L main", "L sub", "R main", "R sub"],
    )
    assert report["status"] == "BLOCKED"
    assert report["phase_metrics_allowed"] is False


def test_bundled_legacy_mdat_is_blocked_instead_of_using_first_phase_array(private_legacy_mdat):
    path = private_legacy_mdat
    project = import_mdat_measurements(path)
    assert project.phase_truth_status == "BLOCKED"
    assert project.phase_metrics_allowed is False
    sides = project.phase_truth_diagnostics["sides"]
    assert {side["status"] for side in sides} == {"AMBIGUOUS"}
    assert all(side["sum_count"] == 0 for side in sides)
