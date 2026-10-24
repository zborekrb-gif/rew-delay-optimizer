"""Synthetic sine/exponential impulses and constant arrays; no measured report data."""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pytest

from rew_api_client import encode_float32_base64
from rew_time_domain_analyzer import analyse_variants, load_time_measurements, write_reports


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


def _record(title: str, role: str, spl: np.ndarray, tau: float, gd_ms: float = 10.0):
    freq = np.arange(20.0, 301.0, 1.0)
    phase = np.zeros_like(freq)
    return {
        "summary": {"uuid": title, "title": title},
        "role": role,
        "frequencyResponse": _fr_payload(freq, spl, phase),
        "groupDelay": _gd_payload(freq, np.full(freq.size, gd_ms / 1000.0)),
        "impulseResponse": _ir_payload(_decaying_ir(8000, tau), 8000),
    }


def _snapshot(records):
    return {
        "format": "REWDO_REW_API_SNAPSHOT",
        "formatVersion": 3,
        "createdUtc": "2026-07-27T00:00:00+00:00",
        "apiBaseUrl": "http://127.0.0.1:4735",
        "measurementState": {"timingReferenceConsistent": True, "microphoneStationary": True},
        "acquisition": {
            "mode": "native_unsmoothed",
            "nativeAxisRequested": True,
            "noSmoothingRequested": True,
            "smoothingRequested": "None",
            "ppoRequested": None,
            "impulseRequested": True,
            "groupDelayRequested": True,
        },
        "responseIntegrity": {"status": "PASS", "problems": []},
        "measurements": records,
    }


def _full_dataset(*, include_fast=True, side_bad=False, thin=False):
    freq = np.arange(20.0, 301.0, 1.0)
    base = np.full_like(freq, 80.0)
    records = []
    roles = [("L_STRONA", "L Dirac ON"), ("R_STRONA", "R Dirac ON"), ("L+R", "L+R Dirac ON")]
    seq = 1
    for repeat, tag, tau_shift in [(1, "SLOT2", 0.000), (2, "REPEAT2", 0.003), (3, "REPEAT3", -0.003)]:
        for suffix, role in roles:
            title = f"{seq:02d}_REF_{tag}_90BW48_G0_DIRAC_ON_{suffix}"
            records.append(_record(title, role, base, 0.180 + tau_shift, 10.0))
            seq += 1
    if include_fast:
        for suffix, role in roles:
            tau = 0.070
            if side_bad and suffix == "R_STRONA":
                tau = 0.500
            spl = base.copy()
            if thin:
                spl[(freq >= 125) & (freq <= 220)] -= 1.5
            title = f"{seq:02d}_FAST_TEST_90BW48_G0_DIRAC_ON_{suffix}"
            records.append(_record(title, role, spl, tau, 10.0))
            seq += 1
    return records


def _load(records):
    td = TemporaryDirectory()
    path = Path(td.name) / "time.rewapi.json"
    path.write_text(json.dumps(_snapshot(records)), encoding="utf-8")
    payload, items, warnings = load_time_measurements(path, 500.0)
    return td, path, payload, items, warnings


def test_reference_repeats_are_not_classified_as_better_candidates():
    td, path, payload, items, warnings = _load(_full_dataset(include_fast=False))
    try:
        profiles, variants, family, extra, refs = analyse_variants(items, "REF_SLOT2_90BW48_G0")
        assert family == "90bw48 g0 dirac on"
        assert len(refs) == 9
        assert variants == []
        assert all(profiles[role].count == 3 for role in ("L", "R", "L+R"))
        assert all(profiles[role].reliable for role in profiles)
    finally:
        td.cleanup()


def test_complete_fast_variant_must_improve_lr_and_a_side():
    td, path, payload, items, warnings = _load(_full_dataset())
    try:
        profiles, variants, _, _, _ = analyse_variants(items, "REF_SLOT2_90BW48_G0")
        assert len(variants) == 1
        assert variants[0].verdict == "POTENCJALNIE LEPSZY"
    finally:
        td.cleanup()


def test_lr_improvement_with_bad_right_side_is_rejected():
    td, path, payload, items, warnings = _load(_full_dataset(side_bad=True))
    try:
        _, variants, _, _, _ = analyse_variants(items, "REF_SLOT2_90BW48_G0")
        assert len(variants) == 1
        assert variants[0].verdict in {"ODRZUĆ — NIESPÓJNOŚĆ L/R", "GORSZY CZASOWO"}
    finally:
        td.cleanup()


def test_body_loss_overrides_fast_decay():
    td, path, payload, items, warnings = _load(_full_dataset(thin=True))
    try:
        _, variants, _, _, _ = analyse_variants(items, "REF_SLOT2_90BW48_G0")
        assert variants[0].verdict == "ODRZUĆ — UTRATA BODY"
    finally:
        td.cleanup()


def test_old_nonliteral_query_is_explicitly_inferred_and_reported():
    td, path, payload, items, warnings = _load(_full_dataset(include_fast=False))
    try:
        _, _, family, extra, _ = analyse_variants(items, "90BW48_SLOT2_BASELINE")
        assert family == "90bw48 g0 dirac on"
        assert any("nie wystąpił literalnie" in message for message in extra)
    finally:
        td.cleanup()


def test_report_is_variant_level_and_contains_repeatability_table():
    td, path, payload, items, warnings = _load(_full_dataset())
    try:
        out = Path(td.name) / "out"
        write_reports(out, path, payload, items, "REF_SLOT2_90BW48_G0", warnings)
        text = (out / "01_WERDYKT_90BW48_FAST.md").read_text(encoding="utf-8")
        assert "jeden werdykt dla L/R/L+R" in text
        assert "Powtarzalność referencji" in text
        assert "POTENCJALNIE LEPSZY" in text
        assert (out / "02_POWTARZALNOSC_REFERENCJI.csv").exists()
        assert (out / "03_WARIANTY_ZBIORCZO.csv").exists()
    finally:
        td.cleanup()
