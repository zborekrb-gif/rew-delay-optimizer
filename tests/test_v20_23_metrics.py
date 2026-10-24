from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from rew_acoustic_crossover_designer import _score_side
from rew_metric_registry import (
    metric,
    same_scale,
    side_integration_from_construct_dip,
    write_metric_registry,
)


class UnifiedMetricTests(unittest.TestCase):
    def test_side_integration_reference_points(self) -> None:
        excellent = side_integration_from_construct_dip(2.5, 2.5, 4.0, 4.0)
        poor = side_integration_from_construct_dip(-1.5, -1.5, 10.0, 10.0)
        self.assertAlmostEqual(float(excellent["l_score"]), 100.0, places=9)
        self.assertAlmostEqual(float(excellent["r_score"]), 100.0, places=9)
        self.assertAlmostEqual(float(poor["l_score"]), 0.0, places=9)
        self.assertFalse(bool(poor["gate_pass"]))

    def test_mode8_side_score_uses_common_formula(self) -> None:
        f = np.geomspace(40.0, 160.0, 400)
        main = np.ones_like(f, dtype=complex)
        sub = np.ones_like(f, dtype=complex)
        summed = main + sub
        score, construct, dip = _score_side(summed, main, sub, f, 90.0)
        expected = side_integration_from_construct_dip(construct, construct, dip, dip)
        self.assertAlmostEqual(score, float(expected["l_score"]), places=9)
        self.assertGreater(score, 95.0)

    def test_comparison_groups_are_explicit(self) -> None:
        self.assertTrue(same_scale("side_integration_raw", "side_integration_project"))
        self.assertFalse(same_scale("body_support_raw", "body_continuity_project"))
        self.assertFalse(same_scale("spatial_robustness_raw", "parameter_robustness_project"))
        self.assertEqual(metric("selector_score_project").stage, "SYMULACJA PROJEKTU")

    def test_registry_files_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            write_metric_registry(td)
            md = Path(td) / "METRYKI_I_SKALY.md"
            csv = Path(td) / "METRYKI_I_SKALY.csv"
            self.assertTrue(md.exists())
            self.assertTrue(csv.exists())
            text = md.read_text(encoding="utf-8")
            self.assertIn("Sklejenie stron", text)
            self.assertIn("POMIAR POST-DIRAC", text)


if __name__ == "__main__":
    unittest.main()
