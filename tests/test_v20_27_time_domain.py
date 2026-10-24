from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from rew_api_client import (
    decode_group_delay,
    decode_impulse_response,
    encode_float32_base64,
)
from rew_mdat import infer_mdat_measurement_role
from rew_time_domain_analyzer import (
    TimeMeasurement,
    analyse_band_decay,
    classify_candidate,
    compare_spl,
    load_time_measurements,
    write_reports,
)


def _fr_payload(freq, spl, phase):
    return {
        "smoothing": "None",
        "startFreq": float(freq[0]),
        "freqStep": float(freq[1] - freq[0]),
        "magnitudes": encode_float32_base64(spl),
        "phases": encode_float32_base64(phase),
    }


def _gd_payload(freq, gd_s):
    return {
        "smoothing": "None",
        "startFreq": float(freq[0]),
        "freqStep": float(freq[1] - freq[0]),
        "magnitudes": encode_float32_base64(gd_s),
    }


def _ir_payload(samples, fs):
    return {
        "startTime": -0.05,
        "sampleInterval": 1.0 / fs,
        "sampleRate": fs,
        "timingReference": "Acoustic timing reference",
        "unit": "Percent",
        "data": encode_float32_base64(samples),
    }


def _decaying_ir(fs: int, tau: float) -> np.ndarray:
    n = fs
    t = np.arange(n) / fs
    peak = int(0.05 * fs)
    ir = np.zeros(n, dtype=float)
    x = t[: n - peak]
    ir[peak:] = np.sin(2 * np.pi * 60.0 * x) * np.exp(-x / tau)
    ir[peak] += 0.8
    return ir


def test_decode_time_payloads():
    freq = np.arange(20.0, 121.0, 1.0)
    gd = decode_group_delay(_gd_payload(freq, np.full(freq.size, 0.012)))
    assert gd["group_delay_ms"][0] == pytest.approx(12.0, abs=1e-4)
    ir = decode_impulse_response(_ir_payload([0.0, 1.0, 0.0], 48000))
    assert ir["sample_rate_hz"] == pytest.approx(48000.0)
    assert len(ir["samples"]) == 3


def test_sum_role_detection():
    assert infer_mdat_measurement_role("03_L_SUM_M30_S20") == "L sum"
    assert infer_mdat_measurement_role("06_R_SUM_M30_S19") == "R sum"
    assert infer_mdat_measurement_role("09_LR_SUM_BASELINE") == "L+R"


def test_decay_detects_faster_tail():
    fs = 8000
    slow = _decaying_ir(fs, 0.180)
    fast = _decaying_ir(fs, 0.080)
    time = -0.05 + np.arange(fs) / fs
    a = analyse_band_decay(slow, time, fs, 45.0, 65.0, max_time_ms=500)
    b = analyse_band_decay(fast, time, fs, 45.0, 65.0, max_time_ms=500)
    assert a.late_100_db is not None and b.late_100_db is not None
    assert b.late_100_db < a.late_100_db
    assert a.t20_ms is not None and b.t20_ms is not None
    assert b.t20_ms < a.t20_ms


def test_end_to_end_snapshot_and_reports():
    fs = 8000
    freq = np.arange(20.0, 301.0, 1.0)
    phase = np.zeros_like(freq)
    reference_spl = np.full_like(freq, 80.0)
    fast_spl = reference_spl.copy()
    thin_spl = reference_spl.copy()
    thin_spl[(freq >= 125) & (freq <= 220)] -= 1.5
    records = []
    for title, spl, tau in [
        ("90BW48_SLOT2_BASELINE_L_SUM", reference_spl, 0.180),
        ("90BW48_FAST_L_SUM", fast_spl, 0.070),
        ("THIN_FAST_L_SUM", thin_spl, 0.060),
    ]:
        records.append({
            "summary": {"uuid": title, "title": title},
            "role": "L sum",
            "frequencyResponse": _fr_payload(freq, spl, phase),
            "groupDelay": _gd_payload(freq, np.full(freq.size, 0.010)),
            "impulseResponse": _ir_payload(_decaying_ir(fs, tau), fs),
        })
    snapshot = {
        "format": "REWDO_REW_API_SNAPSHOT",
        "formatVersion": 3,
        "createdUtc": "2026-07-26T00:00:00+00:00",
        "apiBaseUrl": "http://127.0.0.1:4735",
        "acquisition": {
            "mode": "native_unsmoothed",
            "nativeAxisRequested": True,
            "noSmoothingRequested": True,
            "ppoRequested": None,
            "impulseRequested": True,
            "groupDelayRequested": True,
        },
        "responseIntegrity": {"status": "PASS", "problems": []},
        "measurements": records,
    }
    with TemporaryDirectory() as td:
        path = Path(td) / "time.rewapi.json"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        payload, items, warnings = load_time_measurements(path, 500.0)
        assert len(items) == 3
        ref = items[0]
        cmp_fast = compare_spl(items[1], ref)
        verdict_fast, _, _ = classify_candidate(items[1], ref, cmp_fast)
        assert verdict_fast == "POTENCJALNIE LEPSZY"
        cmp_thin = compare_spl(items[2], ref)
        verdict_thin, _, _ = classify_candidate(items[2], ref, cmp_thin)
        assert verdict_thin == "ODRZUĆ — UTRATA BODY"


# pytest import na końcu ogranicza zależność modułu produkcyjnego od pytest.
import pytest
