from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

import numpy as np

from rew_acoustic_biquad_designer import (
    AllPassSpec,
    _allpass_peak_group_delay_ms,
    _allpass_response,
    _main_sub_phase_score,
)
from rew_cut_only import evaluate_cut_only


class CoreTests(unittest.TestCase):
    def test_allpass_has_unity_magnitude(self) -> None:
        f = np.geomspace(20.0, 500.0, 1000)
        h = _allpass_response(AllPassSpec(True, 90.0, 0.7), 96000.0, f)
        err_db = np.max(np.abs(20.0 * np.log10(np.maximum(np.abs(h), 1e-30))))
        self.assertLess(err_db, 1e-6)
        self.assertLess(_allpass_peak_group_delay_ms(AllPassSpec(True, 90.0, 0.7), 96000.0), 9.0)

    def test_direct_main_sub_phase_score(self) -> None:
        f = np.geomspace(40.0, 160.0, 300)
        main = np.ones_like(f, dtype=complex)
        sub_good = np.ones_like(f, dtype=complex)
        sub_bad = np.exp(1j * np.deg2rad(150.0)) * np.ones_like(f, dtype=complex)
        good, good_deg = _main_sub_phase_score(main, sub_good, f, 90.0)
        bad, bad_deg = _main_sub_phase_score(main, sub_bad, f, 90.0)
        self.assertGreater(good, 95.0)
        self.assertLess(good_deg, 1.0)
        self.assertLess(bad, 35.0)
        self.assertGreater(bad_deg, 140.0)

    def test_cut_only_does_not_copy_reference_bass(self) -> None:
        f = np.linspace(20.0, 250.0, 1000)
        candidate = -2.0 * np.log2(np.maximum(f, 40.0) / 180.0)
        ref_a = np.zeros_like(f)
        ref_b = 15.0 * np.sin(np.log(f) * 3.0)
        a = evaluate_cut_only(candidate, ref_a, f)
        b = evaluate_cut_only(candidate, ref_b, f)
        self.assertAlmostEqual(a.score, b.score, places=9)
        self.assertAlmostEqual(a.boost_p95_db, b.boost_p95_db, places=9)

    def test_windows_launchers_use_crlf(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for path in list(root.glob("*.bat")) + list(root.glob("*.cmd")):
            data = path.read_bytes()
            self.assertNotIn(b"\n", data.replace(b"\r\n", b""), msg=path.name)


if __name__ == "__main__":
    unittest.main()
