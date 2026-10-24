from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

import rew_gain_bridge
import rew_mdat
from rew_api_client import encode_float32_base64
from rew_common import MdatMeasurementInfo
from rew_filter_generator import Biquad, make_sections, write_minidsp_biquads
from rew_gain_bridge import analyze_gain_bridge
from rew_mdat import _decode_mdat_numeric_records, import_rew_api_snapshot, infer_mdat_profile_from_names
from rew_raw_measurement_check import main as raw_check_main


def _api_snapshot(*, smoothing: str = "None", native_axis: bool = True) -> dict:
    freq_points = 32
    response = {
        "smoothing": smoothing,
        "startFreq": 20.0,
        "freqStep": 1.0,
        "magnitudes": encode_float32_base64(np.linspace(70.0, 73.0, freq_points)),
        "phases": encode_float32_base64(np.linspace(-90.0, 20.0, freq_points)),
    }
    records = []
    for title, role in (
        ("RAW_L_MAIN", "L main"),
        ("RAW_L_SUB", "L sub"),
        ("RAW_R_MAIN", "R main"),
        ("RAW_R_SUB", "R sub"),
    ):
        records.append({
            "summary": {"title": title},
            "role": role,
            "frequencyResponse": dict(response),
            "decodedInfo": {"ppoRequested": None},
        })
    return {
        "format": "REWDO_REW_API_SNAPSHOT",
        "formatVersion": 3,
        "acquisition": {
            "nativeAxisRequested": native_axis,
            "ppoRequested": None,
            "noSmoothingRequested": smoothing == "None",
            "smoothingRequested": smoothing,
        },
        "measurements": records,
    }


def test_api_import_enforces_integrity_gate(tmp_path: Path) -> None:
    good_path = tmp_path / "good.rewapi.json"
    good_path.write_text(json.dumps(_api_snapshot()), encoding="utf-8")
    good = import_rew_api_snapshot(good_path)
    assert good.phase_truth_status == "API_INTEGRITY"
    assert good.phase_metrics_allowed is True

    bad_path = tmp_path / "bad.rewapi.json"
    bad_path.write_text(json.dumps(_api_snapshot(smoothing="1/12", native_axis=False)), encoding="utf-8")
    bad = import_rew_api_snapshot(bad_path)
    assert bad.phase_truth_status == "BLOCKED"
    assert bad.phase_metrics_allowed is False
    assert bad.phase_truth_diagnostics["problems"]


def test_each_mdat_measurement_keeps_its_own_frequency_axis(monkeypatch: pytest.MonkeyPatch) -> None:
    points = 64
    infos = [
        MdatMeasurementInfo(1, "L_MAIN", "L main", 10),
        MdatMeasurementInfo(2, "R_MAIN", "R main", 1000),
    ]
    arrays = [
        {"data_start": 100, "data_end": 356, "length": points, "values": np.linspace(70.0, 75.0, points)},
        {"data_start": 400, "data_end": 656, "length": points, "values": np.linspace(-1000.0, -500.0, points)},
        {"data_start": 1100, "data_end": 1356, "length": points, "values": np.linspace(71.0, 76.0, points)},
        {"data_start": 1400, "data_end": 1656, "length": points, "values": np.linspace(-900.0, -400.0, points)},
    ]

    def fake_axis(_data: bytes, after_pos: int, count: int):
        start = 20.0 if after_pos < 1000 else 30.0
        axis = np.linspace(start, start + count - 1, count)
        return axis, float(axis[0]), float(axis[-1]), 1.0

    monkeypatch.setattr(rew_mdat, "_infer_mdat_freq_axis", fake_axis)
    records, warnings = _decode_mdat_numeric_records(b"\0" * 2000, infos, arrays)
    assert warnings == []
    assert records[0].freq[0] == 20.0
    assert records[1].freq[0] == 30.0


def test_raw_check_returns_nonzero_for_phase_truth_block(tmp_path: Path, private_legacy_mdat) -> None:
    legacy = private_legacy_mdat
    assert raw_check_main(["--mdat", str(legacy), "--out", str(tmp_path)]) == 2
    assert (tmp_path / "RAW_MEASUREMENT_CHECK.md").exists()


def test_minidsp_export_refuses_truncation_and_invalid_sections(tmp_path: Path) -> None:
    valid = make_sections("HP", 96000.0, 90.0, "BW48")
    output = tmp_path / "valid.txt"
    write_minidsp_biquads(output, valid, "", slots=8)
    assert output.read_text(encoding="utf-8").count("biquad") == 8

    too_many = [
        Biquad(i, 1.0, 0.0, 0.0, 0.0, 0.0, "HP", 0.7)
        for i in range(1, 10)
    ]
    with pytest.raises(ValueError, match="zatrzymany zamiast cicho obciąć"):
        write_minidsp_biquads(tmp_path / "overflow.txt", too_many, "", slots=8)

    nonsequential = [Biquad(2, 1.0, 0.0, 0.0, 0.0, 0.0, "HP", 0.7)]
    with pytest.raises(ValueError, match="kolejne od 1"):
        write_minidsp_biquads(tmp_path / "numbering.txt", nonsequential, "", slots=8)

    unstable = [Biquad(1, 1.0, 0.0, 0.0, -2.1, 1.1, "HP", 0.7)]
    with pytest.raises(ValueError, match="niestabilna"):
        write_minidsp_biquads(tmp_path / "unstable.txt", unstable, "", slots=8)


def _bridge_record(name: str, role: str, freq: np.ndarray, amp: np.ndarray):
    return rew_mdat._MdatNumericRecord(
        info=MdatMeasurementInfo(1, name, role, 100),
        freq=freq.copy(),
        spl_db=20.0 * np.log10(np.maximum(amp, 1e-15)),
        phase_candidates=[],
    )


def _bridge_fixture(*, duplicate_main: bool = False, unstable_repeats: bool = False):
    freq = np.linspace(20.0, 250.0, 900)
    main = 0.35 + 0.95 / np.sqrt(1.0 + np.power(90.0 / freq, 8.0))
    sub = 1.10 / np.sqrt(1.0 + np.power(freq / 105.0, 8.0))
    rel_phase = np.deg2rad(-75.0 + 0.18 * (freq - 90.0))
    raw_sum = np.sqrt(np.square(main) + np.square(sub) + 2.0 * main * sub * np.cos(rel_phase))
    gain = np.power(10.0, 6.0 / 20.0)
    bridge_sum = np.sqrt(np.square(main) + np.square(gain * sub) + 2.0 * main * gain * sub * np.cos(rel_phase))
    jitters = (0.0, 4.0, -4.0) if unstable_repeats else (0.0, 0.03, -0.02)
    raw_records = []
    for side in ("L", "R"):
        raw_records.extend([
            _bridge_record(f"{side}_MAIN", f"{side} main", freq, main),
            _bridge_record(f"{side}_SUB", f"{side} sub", freq, sub),
        ])
        raw_records.extend(
            _bridge_record(f"{side}_SUM{i}", f"{side} sum", freq, raw_sum * np.power(10.0, jitter / 20.0))
            for i, jitter in enumerate(jitters, start=1)
        )
    if duplicate_main:
        raw_records.append(_bridge_record("L_MAIN_DUP", "L main", freq, main))
    bridge_records = [
        _bridge_record("L_SUM_SUB0", "L sum", freq, bridge_sum),
        _bridge_record("R_SUM_SUB0", "R sum", freq, bridge_sum),
    ]
    return raw_records, bridge_records


def test_gain_bridge_blocks_unstable_repeats_and_duplicate_components(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_records, bridge_records = _bridge_fixture(unstable_repeats=True)
    monkeypatch.setattr(
        rew_gain_bridge,
        "_records",
        lambda path, _roles: raw_records if Path(path).name == "raw.mdat" else bridge_records,
    )
    result = analyze_gain_bridge(Path("raw.mdat"), Path("bridge.mdat"))
    assert result.status == "BLOCKED"
    assert result.left.raw_repeat_spread_p90_db > 1.0

    duplicate_records, bridge_records = _bridge_fixture(duplicate_main=True)
    monkeypatch.setattr(
        rew_gain_bridge,
        "_records",
        lambda path, _roles: duplicate_records if Path(path).name == "raw.mdat" else bridge_records,
    )
    with pytest.raises(ValueError, match="dokładnie jednego pomiaru L main"):
        analyze_gain_bridge(Path("raw.mdat"), Path("bridge.mdat"))


def test_profile_parser_accepts_non_anchor_crossovers() -> None:
    assert infer_mdat_profile_from_names(["102_BW48_L_MAIN"])[0] == "102 Hz"
    assert infer_mdat_profile_from_names(["118-LR24-R_SUB"])[0] == "118 Hz"
