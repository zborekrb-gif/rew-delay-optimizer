"""Synthetic arrays and explicit dummy file bytes. Reference settings 12/4/5/6 ms are fabricated examples; a repeated-letter digest is dummy metadata, not measurement evidence."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from rew_api_client import encode_float32_base64, verify_snapshot_response_integrity
from rew_candidate_safety import (
    NoSafeCandidateError,
    combine_required_state_safety,
    evaluate_candidate_safety,
    write_quarantine,
)
from rew_common import MdatMeasurementInfo, Measurement
from rew_gain_bridge import GainBridgeAudit, GainBridgeSide, _estimate_side_gain, write_gain_bridge_report
from rew_gain_state import build_gain_state
from rew_mdat import _MdatNumericRecord
from rew_measurement_source import RewApiMeasurementSource
from rew_measurement_validation import (
    MeasurementValidationError,
    validate_measurement_project,
    validate_numeric_response_arrays,
)
from rew_post_dirac_ab import analyze_post_dirac_ab
from rew_reference_registry import find_reference_conflicts, load_reference_record, reference_relative_delays
from rew_slot4max_project import _reference_registry_status
from rew_verify_user_slot import _write_user_reference_yaml


def _record(name: str, role: str, freq: np.ndarray, amp: np.ndarray) -> _MdatNumericRecord:
    return _MdatNumericRecord(
        info=MdatMeasurementInfo(1, name, role, 100),
        freq=freq.copy(),
        spl_db=20.0 * np.log10(np.maximum(amp, 1e-15)),
        phase_candidates=[],
    )


def _snapshot(records: list[dict]) -> dict:
    return {
        "format": "REWDO_REW_API_SNAPSHOT",
        "formatVersion": 3,
        "acquisition": {
            "nativeAxisRequested": True,
            "ppoRequested": None,
            "noSmoothingRequested": True,
            "smoothingRequested": "None",
        },
        "measurements": records,
    }


def _response(magnitudes: np.ndarray, phases: np.ndarray, *, start: float = 20.0, step: float = 1.0) -> dict:
    return {
        "smoothing": "None",
        "startFreq": start,
        "freqStep": step,
        "magnitudes": encode_float32_base64(magnitudes),
        "phases": encode_float32_base64(phases),
    }


def test_gain_bridge_two_positive_roots_is_blocked_as_ambiguous() -> None:
    freq = np.linspace(35.0, 140.0, 400)
    main = np.full_like(freq, 2.0)
    sub = np.ones_like(freq)
    cosine = -0.7
    actual_gain = 0.8
    raw_sum = np.sqrt(main**2 + sub**2 + 2.0 * main * sub * cosine)
    bridge_sum = np.sqrt(main**2 + (actual_gain * sub) ** 2 + 2.0 * main * actual_gain * sub * cosine)
    side = _estimate_side_gain(
        "L",
        _record("L_MAIN", "L main", freq, main),
        _record("L_SUB", "L sub", freq, sub),
        [_record(f"L_SUM{i}", "L sum", freq, raw_sum) for i in range(3)],
        _record("L_SUM_SUB0", "L sum", freq, bridge_sum),
    )
    assert side.classification == "AMBIGUOUS"
    assert side.ambiguous_fraction > 0.99


def test_api_integrity_rejects_nan_and_inf_values() -> None:
    magnitudes = np.linspace(70.0, 72.0, 32)
    phases = np.linspace(-80.0, 20.0, 32)
    magnitudes[7] = np.nan
    phases[11] = np.inf
    payload = _snapshot([{
        "summary": {"title": "BAD_NUMBERS"},
        "frequencyResponse": _response(magnitudes, phases),
        "decodedInfo": {"ppoRequested": None},
    }])
    ok, problems = verify_snapshot_response_integrity(payload)
    assert ok is False
    assert any("NaN/Inf" in problem for problem in problems)


def test_api_integrity_rejects_broken_base64() -> None:
    response = _response(np.linspace(70.0, 72.0, 32), np.linspace(-80.0, 20.0, 32))
    response["magnitudes"] = "%%%broken%%%"
    payload = _snapshot([{
        "summary": {"title": "BAD_BASE64"},
        "frequencyResponse": response,
        "decodedInfo": {"ppoRequested": None},
    }])
    ok, problems = verify_snapshot_response_integrity(payload)
    assert ok is False
    assert any("Base64" in problem for problem in problems)


@pytest.mark.parametrize("step", [-1.0, 0.0])
def test_api_integrity_rejects_descending_or_duplicate_axis(step: float) -> None:
    payload = _snapshot([{
        "summary": {"title": "BAD_AXIS"},
        "frequencyResponse": _response(np.linspace(70.0, 72.0, 32), np.linspace(-80.0, 20.0, 32), step=step),
        "decodedInfo": {"ppoRequested": None},
    }])
    ok, problems = verify_snapshot_response_integrity(payload)
    assert ok is False
    assert problems and any("uszkodzone dane" in problem for problem in problems)


def test_post_dirac_accepts_api_snapshot_end_to_end(tmp_path: Path) -> None:
    records = []
    freq_points = 96
    for index, title in enumerate((
        "90_BW48_SLOT2_L", "90_BW48_SLOT2_R", "90_BW48_SLOT2_L+R",
        "94_BW48_SAFE_L", "94_BW48_SAFE_R", "94_BW48_SAFE_L+R",
    )):
        response = _response(
            np.linspace(70.0 + index * 0.02, 73.0 + index * 0.02, freq_points),
            np.linspace(-90.0 + index, 30.0 + index, freq_points),
            start=20.0,
            step=3.0,
        )
        records.append({
            "summary": {"title": title},
            "frequencyResponse": response,
            "decodedInfo": {"ppoRequested": None},
        })
    source = tmp_path / "post.rewapi.json"
    source.write_text(json.dumps(_snapshot(records)), encoding="utf-8")
    out = tmp_path / "out"
    rows = analyze_post_dirac_ab(source, out, reference="90_BW48_SLOT2")
    assert rows
    assert (out / "FINAL_DECISION_REPORT.md").exists()


def test_slot4_report_does_not_claim_uncomputed_rejections() -> None:
    source = (Path(__file__).resolve().parents[1] / "rew_slot4max_project.py").read_text(encoding="utf-8")
    assert "LR48 i LR24: nie wygrały" not in source
    assert "96/96 BW48: poprawiał" not in source
    assert "All-pass: warianty" not in source


@pytest.mark.parametrize(
    ("freq", "spl", "phase", "expected_code"),
    [
        (np.array([20.0, 21.0, 20.5] + list(range(22, 36))), np.zeros(17), np.zeros(17), "NON_MONOTONIC_AXIS"),
        (np.array([20.0, 21.0, 21.0] + list(range(22, 36))), np.zeros(17), np.zeros(17), "DUPLICATE_FREQUENCY"),
        (np.arange(17.0), np.zeros(17), np.zeros(17), "NON_POSITIVE_FREQUENCY"),
        (np.arange(20.0, 37.0), np.zeros(16), np.zeros(17), "LENGTH_MISMATCH"),
    ],
)
def test_central_validator_rejects_structural_numeric_errors(
    freq: np.ndarray, spl: np.ndarray, phase: np.ndarray, expected_code: str,
) -> None:
    result = validate_numeric_response_arrays(freq, spl, phase, minimum_points=16)
    assert result.ok is False
    assert expected_code in {issue.code for issue in result.issues}


def test_central_validator_rejects_too_few_points_and_missing_range() -> None:
    freq = np.linspace(30.0, 100.0, 12)
    result = validate_numeric_response_arrays(
        freq, np.zeros(12), np.zeros(12), minimum_points=16, required_range_hz=(20.0, 200.0),
    )
    codes = {issue.code for issue in result.issues}
    assert {"TOO_FEW_POINTS", "MISSING_REQUIRED_RANGE"}.issubset(codes)


def test_api_source_preserves_each_measurements_native_axis(tmp_path: Path) -> None:
    records = []
    for title, start, step in (("A_L", 20.0, 1.0), ("B_R", 31.0, 1.7)):
        records.append({
            "summary": {"title": title},
            "frequencyResponse": _response(np.linspace(70, 72, 32), np.linspace(-80, 10, 32), start=start, step=step),
            "decodedInfo": {"ppoRequested": None},
        })
    path = tmp_path / "axes.rewapi.json"
    path.write_text(json.dumps(_snapshot(records)), encoding="utf-8")
    loaded = RewApiMeasurementSource(path).load_all()
    assert loaded[0].measurement.freq[0] == pytest.approx(20.0)
    assert loaded[1].measurement.freq[0] == pytest.approx(31.0)
    assert np.diff(loaded[0].measurement.freq).mean() == pytest.approx(1.0)
    assert np.diff(loaded[1].measurement.freq).mean() == pytest.approx(1.7)


def _safe_candidate(**changes: float | str) -> SimpleNamespace:
    values: dict[str, float | str] = {
        "decision_class": "MOCNY KANDYDAT",
        "side_min": 82.0,
        "body_score": 75.0,
        "lr_sum_score": 78.0,
        "effective_xo_hz": 92.0,
        "acoustic_score": 84.0,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_effective_xo_116_3_is_a_hard_export_block() -> None:
    result = evaluate_candidate_safety(
        _safe_candidate(effective_xo_hz=116.3),
        input_integrity_ok=True,
        phase_integrity_ok=True,
        gain_state_verified=True,
        gain_state_complete=True,
    )
    assert result.candidate_safe_to_export is False
    assert any("116.3" in reason and "70.0–105.0" in reason for reason in result.reasons)


def test_candidate_pass_at_dirac_but_fail_at_listening_is_blocked() -> None:
    base = evaluate_candidate_safety(
        _safe_candidate(), input_integrity_ok=True, phase_integrity_ok=True,
        gain_state_verified=True, gain_state_complete=True,
    )
    dirac = evaluate_candidate_safety(
        _safe_candidate(), input_integrity_ok=True, phase_integrity_ok=True,
        gain_state_verified=True, gain_state_complete=True,
    )
    listening = evaluate_candidate_safety(
        _safe_candidate(side_min=38.0), input_integrity_ok=True, phase_integrity_ok=True,
        gain_state_verified=True, gain_state_complete=True,
    )
    combined = combine_required_state_safety(base, {"dirac": dirac, "listening": listening})
    assert dirac.candidate_safe_to_export is True
    assert listening.candidate_safe_to_export is False
    assert combined.candidate_safe_to_export is False
    assert any(reason.startswith("listening:") for reason in combined.reasons)


def test_gain_state_scales_complex_subs_without_changing_phase(tmp_path: Path) -> None:
    bridge = tmp_path / "bridge.mdat"
    bridge.write_bytes(b"verified bridge identity")
    state = build_gain_state(
        raw_db=-6.0, dirac_db=-4.0, listening_db=0.0,
        source="GAIN_BRIDGE", verified=True, bridge_path=bridge,
        bridge_left_delta_db=5.8, bridge_right_delta_db=6.2,
    )
    left = np.array([1 + 1j, -2 + 0.5j])
    right = np.array([0.2 - 0.8j, 3 + 4j])
    scaled_l, scaled_r = state.scale_complex_subs(left, right, "listening")
    assert np.angle(scaled_l) == pytest.approx(np.angle(left))
    assert np.angle(scaled_r) == pytest.approx(np.angle(right))
    assert np.abs(scaled_l / left) == pytest.approx(10 ** (5.8 / 20.0))
    assert np.abs(scaled_r / right) == pytest.approx(10 ** (6.2 / 20.0))


def test_all_diagnostic_candidates_create_quarantine_without_biquad_txt(tmp_path: Path) -> None:
    candidates = [SimpleNamespace(code="DIAG_1", decision_class="DIAGNOSTYKA", side_min=20.0, body_score=20.0, lr_sum_score=20.0, effective_xo_hz=116.3)]
    results = [evaluate_candidate_safety(
        candidates[0], input_integrity_ok=True, phase_integrity_ok=False,
        gain_state_verified=False, gain_state_complete=True,
    )]
    write_quarantine(tmp_path, candidates, results, mode="TEST")
    assert (tmp_path / "QUARANTINE_DIAGNOSTIC.json").exists()
    assert (tmp_path / "QUARANTINE_DIAGNOSTIC.csv").exists()
    assert (tmp_path / "NIE_WGRYWAC_DO_MINIDSP.txt").exists()
    assert not list(tmp_path.rglob("*_MINIDSP_BIQUADS.txt"))
    row = json.loads((tmp_path / "QUARANTINE_DIAGNOSTIC.json").read_text(encoding="utf-8"))["candidates"][0]
    assert {"global_gain_l_db", "local_gain_l_db", "final_gain_l_db"}.issubset(row)


def _reference_yaml(r_sub_ms: float) -> str:
    return "\n".join([
        "name: REF_90", "crossover_hz: 90", "filter: BW48",
        "l_main_ms: 12", "r_main_ms: 12", "l_sub_ms: 4", f"r_sub_ms: {r_sub_ms}",
        "verified: true", "verification_status: MEASURED",
        "verified_by_measurement_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "verified_at: 2026-08-17T00:00:00Z", "",
    ])


def test_synthetic_reference_delay_conflict_5_vs_6_blocks_anchor(tmp_path: Path) -> None:
    first = tmp_path / "USER_REFERENCE_90BW48.yaml"
    second = tmp_path / "USER_REFERENCE_SLOT4MAX_90BW48.yaml"
    first.write_text(_reference_yaml(5.0), encoding="utf-8")
    second.write_text(_reference_yaml(6.0), encoding="utf-8")
    conflicts = find_reference_conflicts([first, second])
    assert conflicts and "r_sub_ms" in conflicts[0].differing_fields
    assert reference_relative_delays(first, companion_paths=[second]) is None
    ok, status = _reference_registry_status(first)
    assert ok is False and "Konflikt" in status
    second.write_text(_reference_yaml(5.0), encoding="utf-8")
    first.write_text(_reference_yaml(5.0), encoding="utf-8")
    ok, status = _reference_registry_status(first)
    # v20.32: removing a conflict does not prove a configuration or its sums.
    assert ok is False


def test_cli_maps_no_safe_candidate_to_exit_code_3(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import rew_acoustic_crossover_designer as module

    def blocked(*_args, **_kwargs):
        raise NoSafeCandidateError("brak")

    monkeypatch.setattr(module, "design_acoustic_crossovers", blocked)
    code = module.main(["--mdat", str(tmp_path / "raw.mdat"), "--out", str(tmp_path / "out")])
    assert code == 3


def test_validator_project_paths_and_error_object() -> None:
    freq = np.linspace(20.0, 200.0, 32)
    good = Measurement("GOOD", freq, np.zeros(32), np.zeros(32))
    result = validate_measurement_project({"good": good}, required_range_hz=(20.0, 200.0))
    assert result.ok
    empty = validate_measurement_project([], raise_on_error=False)
    assert empty.ok is False and empty.issues[0].code == "EMPTY_PROJECT"
    invalid = validate_measurement_project({"bad": object()}, raise_on_error=False)
    assert invalid.ok is False and invalid.issues[0].code == "INVALID_MEASUREMENT"
    with pytest.raises(MeasurementValidationError, match="projekt nie zawiera"):
        validate_measurement_project([])


def test_gain_bridge_report_exposes_ambiguity_and_monotonicity(tmp_path: Path) -> None:
    def side(name: str) -> GainBridgeSide:
        return GainBridgeSide(
            side=name, gain_db=6.0, p10_db=5.8, p90_db=6.2,
            valid_points=80, total_points=100, raw_sum_count=3,
            raw_repeat_spread_median_db=0.1, raw_repeat_spread_p90_db=0.2,
            total_sum_delta_median_db=3.1, classification="AMBIGUOUS",
            unique_points=80, ambiguous_points=20, invalid_points=0,
            ambiguous_fraction=0.2, monotonic_fraction=0.9,
            ambiguous_low_median_db=-1.94, ambiguous_high_median_db=6.02,
        )

    audit = GainBridgeAudit("BLOCKED", Path("raw.mdat"), Path("bridge.mdat"), side("L"), side("R"), "niejednoznaczny")
    report = write_gain_bridge_report(audit, tmp_path)
    text = report.read_text(encoding="utf-8")
    assert "AMBIGUOUS" in text
    assert "Niejednoznaczność" in text
    assert "Monotoniczność" in text
    assert "-1.94 / +6.02" in text


def test_gain_bridge_record_loader_and_cli_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import rew_gain_bridge as module

    freq = np.linspace(35.0, 140.0, 64)
    record = _record("X", "L main", freq, np.ones_like(freq))
    info = MdatMeasurementInfo(1, "X", "L main", 10)
    source = tmp_path / "source.mdat"
    source.write_bytes(b"serialized")
    monkeypatch.setattr(module, "scan_mdat_project", lambda _path: {"measurements": [info]})
    monkeypatch.setattr(module, "_scan_java_serialized_float_arrays", lambda _data: [])
    monkeypatch.setattr(module, "_decode_mdat_numeric_records", lambda *_args: ([record], ["OTHER: harmless warning"]))
    assert module._records(source, {"L main"}) == [record]
    monkeypatch.setattr(module, "_decode_mdat_numeric_records", lambda *_args: ([record], ["X: phase invalid"]))
    with pytest.raises(ValueError, match="phase invalid"):
        module._records(source, {"L main"})

    def side(name: str) -> GainBridgeSide:
        return GainBridgeSide(
            side=name, gain_db=6.0, p10_db=5.9, p90_db=6.1,
            valid_points=80, total_points=90, raw_sum_count=3,
            raw_repeat_spread_median_db=0.1, raw_repeat_spread_p90_db=0.2,
            total_sum_delta_median_db=3.0, classification="UNIQUE",
            unique_points=80, ambiguous_points=0, invalid_points=10,
            ambiguous_fraction=0.0, monotonic_fraction=1.0,
            ambiguous_low_median_db=float("nan"), ambiguous_high_median_db=float("nan"),
        )

    verified = GainBridgeAudit("VERIFIED", Path("raw.mdat"), Path("bridge.mdat"), side("L"), side("R"), "OK")
    monkeypatch.setattr(module, "analyze_gain_bridge", lambda *_args: verified)
    assert module.main(["--raw", "raw.mdat", "--bridge", "bridge.mdat", "--out", str(tmp_path / "cli")]) == 0
    verified.status = "BLOCKED"
    assert module.main(["--raw", "raw.mdat", "--bridge", "bridge.mdat", "--out", str(tmp_path / "blocked")]) == 2
    monkeypatch.setattr(module, "analyze_gain_bridge", lambda *_args: (_ for _ in ()).throw(ValueError("bad bridge")))
    assert module.main(["--raw", "raw.mdat", "--bridge", "bridge.mdat", "--out", str(tmp_path / "error")]) == 2


def test_candidate_gate_covers_all_integrity_and_headroom_blocks(tmp_path: Path) -> None:
    candidate = SimpleNamespace(
        code="BAD", decision_class="", side_min="bad", body_score=float("nan"),
        lr_sum_score=10.0, effective_xo_hz=float("inf"), acoustic_score=float("nan"),
        l_sub_gain_db=13.5, r_sub_gain_db=-13.5,
    )
    result = evaluate_candidate_safety(
        candidate, input_integrity_ok=False, phase_integrity_ok=False,
        shd_representable=False, gain_state_verified=False, gain_state_complete=False,
        reference_anchor_verified=False,
    )
    assert not result.candidate_safe_to_export
    text = " | ".join(result.reasons)
    assert "Input Integrity" in text and "reprezentowalny" in text and "headroomu" in text and "referencja" in text
    combined = combine_required_state_safety(result, {})
    assert any("brak oceny wymaganego stanu" in reason for reason in combined.reasons)
    write_quarantine(tmp_path / "empty", [], [], mode="EMPTY")
    assert (tmp_path / "empty" / "QUARANTINE_DIAGNOSTIC.csv").exists()


def test_validator_decode_dimension_and_invalid_range_paths() -> None:
    decoded = validate_numeric_response_arrays(["not-a-number"], [0.0], [0.0])
    assert decoded.issues[0].code == "ARRAY_DECODE"
    dimensional = validate_numeric_response_arrays(
        np.ones((4, 4)), np.ones((4, 4)), np.ones((4, 4)), minimum_points=1,
    )
    assert dimensional.issues and all(issue.code == "ARRAY_DIMENSION" for issue in dimensional.issues)
    freq = np.linspace(20.0, 200.0, 32)
    invalid_range = validate_numeric_response_arrays(
        freq, np.zeros(32), np.zeros(32), required_range_hz=(200.0, 20.0),
    )
    assert any(issue.code == "INVALID_REQUIRED_RANGE" for issue in invalid_range.issues)


def test_reference_writer_requires_measurement_identity_for_verified_anchor(tmp_path: Path) -> None:
    unverified_path = _write_user_reference_yaml(
        tmp_path / "unverified.yaml",
        l_main=12.0, r_main=12.0, l_sub=4.0, r_sub=6.0,
        l_pol="OFF", r_pol="OFF", xo=90, filt="BW48", verification_source=None,
    )
    unverified = load_reference_record(unverified_path)
    assert unverified.verified is False
    assert unverified.verification_status == "USER_ENTERED_UNVERIFIED"
    assert unverified.anchor_allowed is False

    evidence = tmp_path / "measurement.mdat"
    evidence.write_bytes(b"measurement identity")
    # Regression changed deliberately: arbitrary bytes are not measurement proof.
    with pytest.raises(ValueError):
        _write_user_reference_yaml(
            tmp_path / "verified.yaml", l_main=12.0, r_main=12.0, l_sub=4.0, r_sub=6.0,
            l_pol="OFF", r_pol="OFF", xo=90, filt="BW48", verification_source=evidence,
        )
