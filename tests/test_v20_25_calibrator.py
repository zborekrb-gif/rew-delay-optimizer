from __future__ import annotations

import unittest

from rew_mdat import infer_mdat_measurement_role
from rew_prediction_measurement_calibrator import _aggregate


class CalibratorTests(unittest.TestCase):
    def test_full_pre_dirac_roles_are_recognized(self) -> None:
        self.assertEqual(infer_mdat_measurement_role("C_ABD92_L_STRONA"), "L strona")
        self.assertEqual(infer_mdat_measurement_role("C_ABD92_R_STRONA"), "R strona")
        self.assertEqual(infer_mdat_measurement_role("C_ABD92_LR"), "L+R")
        self.assertEqual(infer_mdat_measurement_role("ABD92_LR_DIRAC_ON"), "L+R Dirac ON")

    def test_same_topology_multiple_states_count_once(self) -> None:
        records = [
            {"topology_id": "A", "bias_side_l": -1, "bias_side_r": -2, "bias_technical": -1},
            {"topology_id": "A", "bias_side_l": -3, "bias_side_r": -4, "bias_technical": -2},
            {"topology_id": "B", "bias_side_l": -2, "bias_side_r": -1, "bias_technical": -3},
        ]
        a = _aggregate(records)
        self.assertEqual(a["validation_state_count"], 3)
        self.assertEqual(a["independent_topology_count"], 2)
        self.assertAlmostEqual(a["side_bias_l"], -2.0)
        self.assertGreater(a["side_sigma_l"], 0.0)


if __name__ == "__main__":
    unittest.main()
