from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from rew_empirical_calibration import load_empirical_calibration
from rew_acoustic_biquad_designer import _delay_refinement_objective


class EmpiricalCalibrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.cal = load_empirical_calibration(self.root / "tests/fixtures/calibration_two_states.synthetic.json")

    def test_calibration_counts_topologies_not_delay_states(self) -> None:
        # v20.31: jedna niezależna topologia nie może automatycznie włączać
        # korekty punktowej. Rekordy pozostają dostępne wyłącznie do jawnego A/B.
        self.assertFalse(self.cal.enabled)
        self.assertEqual(self.cal.custom_validation_state_count, 2)
        self.assertEqual(self.cal.custom_independent_topology_count, 1)
        self.assertEqual(self.cal.confidence_label, "WYŁĄCZONA")
        self.assertEqual(self.cal.uncertainty_penalty, 0.0)
        self.assertLessEqual(self.cal.weight, 0.50)
        self.assertLess(self.cal.custom_side_bias_r, self.cal.custom_side_bias_l)

    def test_disabling_score_calibration_keeps_delay_settings(self) -> None:
        disabled = self.cal.with_score_calibration_enabled(False)
        self.assertFalse(disabled.enabled)
        self.assertTrue(disabled.delay_refinement_enabled)
        self.assertEqual(disabled.delay_coarse_radius_ms, self.cal.delay_coarse_radius_ms)

    def test_delay_refinement_has_wide_enough_range(self) -> None:
        self.assertGreaterEqual(self.cal.delay_coarse_radius_ms, 1.0)
        self.assertLessEqual(self.cal.delay_fine_step_ms, 0.05)
        self.assertGreaterEqual(self.cal.delay_max_iterations, 4)

    def test_delay_objective_protects_worst_side(self) -> None:
        common = dict(
            nominal_scores={"REFERENCE_SAFE": 50.0, "KICK_CONTROL": 45.0, "CLEAN_BODY": 48.0},
            technical_score=60.0, lr_sum_score=55.0, main_sub_phase_min=70.0,
        )
        weak = SimpleNamespace(side_min=45.0, **common)
        balanced = SimpleNamespace(side_min=58.0, **common)
        self.assertGreater(_delay_refinement_objective(balanced), _delay_refinement_objective(weak))

    def test_single_synthetic_topology_has_low_confidence_when_enabled(self) -> None:
        self.assertEqual(self.cal.with_score_calibration_enabled(True).confidence_label, "NISKA")
        self.assertEqual(self.cal.custom_independent_topology_count, 1)


if __name__ == "__main__":
    unittest.main()
