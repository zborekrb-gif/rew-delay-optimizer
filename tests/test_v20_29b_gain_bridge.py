from __future__ import annotations

import numpy as np

from rew_common import MdatMeasurementInfo
from rew_gain_bridge import _estimate_side_gain
from rew_mdat import _MdatNumericRecord


def _record(name: str, role: str, freq: np.ndarray, amp: np.ndarray) -> _MdatNumericRecord:
    return _MdatNumericRecord(
        info=MdatMeasurementInfo(1, name, role, 100),
        freq=freq.copy(),
        spl_db=20.0 * np.log10(np.maximum(amp, 1e-15)),
        phase_candidates=[],
    )


def test_amplitude_only_gain_bridge_recovers_six_db_without_phase_or_lr_sum():
    freq = np.linspace(20.0, 250.0, 900)
    main = 0.35 + 0.95 / np.sqrt(1.0 + np.power(90.0 / freq, 8.0))
    sub = 1.10 / np.sqrt(1.0 + np.power(freq / 105.0, 8.0))
    relative_phase = np.deg2rad(-75.0 + 0.18 * (freq - 90.0))
    raw_sum = np.sqrt(np.square(main) + np.square(sub) + 2.0 * main * sub * np.cos(relative_phase))
    gain = np.power(10.0, 6.0 / 20.0)
    bridge_sum = np.sqrt(np.square(main) + np.square(gain * sub) + 2.0 * main * gain * sub * np.cos(relative_phase))
    raw_sums = [
        _record(f"L_SUM{i}", "L sum", freq, raw_sum * np.power(10.0, jitter / 20.0))
        for i, jitter in enumerate((0.0, 0.03, -0.02), start=1)
    ]
    result = _estimate_side_gain(
        "L",
        _record("L_MAIN", "L main", freq, main),
        _record("L_SUB", "L sub", freq, sub),
        raw_sums,
        _record("L_SUM_SUB0", "L sum", freq, bridge_sum),
    )
    assert abs(result.gain_db - 6.0) < 0.08
    assert result.valid_points > 200
