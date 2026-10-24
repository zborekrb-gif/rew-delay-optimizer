from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from REWDO_GUI import MODES, build_command


class GuiFlagTests(unittest.TestCase):
    def test_score_calibration_and_delay_refinement_flags_are_independent(self) -> None:
        mode = next(m for m in MODES if m.key == "acoustic_biquad")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cmd = build_command(
                mode=mode,
                mdat_path=root / "raw.mdat",
                out_dir=root / "out",
                reference="90_BW48_slot2",
                user_reference_path=Path(__file__).resolve().parents[1] / "USER_REFERENCE_90BW48.yaml",
                workers=1,
                extra_args="",
                verify_values={},
                bridge_path=root / "bridge.mdat",
                empirical_calibration_enabled=False,
                delay_refinement_enabled=True,
            )
            self.assertNotIn("--enable-empirical-calibration", cmd)
            self.assertNotIn("--no-delay-refinement", cmd)
            self.assertIn("--gain-bridge", cmd)

            cmd2 = build_command(
                mode=mode,
                mdat_path=root / "raw.mdat",
                out_dir=root / "out2",
                reference="90_BW48_slot2",
                user_reference_path=Path(__file__).resolve().parents[1] / "USER_REFERENCE_90BW48.yaml",
                workers=1,
                extra_args="",
                verify_values={},
                bridge_path=root / "bridge.mdat",
                empirical_calibration_enabled=True,
                delay_refinement_enabled=False,
            )
            self.assertIn("--enable-empirical-calibration", cmd2)
            self.assertIn("--no-delay-refinement", cmd2)


if __name__ == "__main__":
    unittest.main()


def test_time_domain_mode_builds_snapshot_command():
    mode = next(m for m in MODES if m.key == "time_domain")
    root = Path(__file__).resolve().parents[1]
    cmd = build_command(
        mode=mode,
        mdat_path=root / "test.rewapi.json",
        out_dir=root / "time_out",
        reference="90BW48_SLOT2_BASELINE",
        user_reference_path=root / "USER_REFERENCE_90BW48.yaml",
        workers=1,
        extra_args="",
        verify_values={},
    )
    assert "rew_time_domain_analyzer.py" in " ".join(cmd) or "time_domain" in cmd
    assert "--snapshot" in cmd
    assert "90BW48_SLOT2_BASELINE" in cmd


def test_gain_bridge_and_slot4max_are_available_in_gui():
    root = Path(__file__).resolve().parents[1]
    for key, expected_script in (
        ("gain_bridge", "rew_gain_bridge.py"),
        ("slot4max", "rew_slot4max_project.py"),
    ):
        mode = next(m for m in MODES if m.key == key)
        assert mode.needs_bridge is True
        cmd = build_command(
            mode=mode,
            mdat_path=root / "raw.mdat",
            bridge_path=root / "bridge.mdat",
            out_dir=root / "out",
            reference="90_BW48_slot2",
            user_reference_path=root / "USER_REFERENCE_90BW48.yaml",
            workers=1,
            extra_args="",
            verify_values={},
        )
        assert expected_script in " ".join(cmd) or key in cmd
        assert cmd[cmd.index("--raw") + 1].endswith("raw.mdat")
        assert cmd[cmd.index("--bridge") + 1].endswith("bridge.mdat")
