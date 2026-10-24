from types import SimpleNamespace
import numpy as np

from rew_api_parity import compare_projects
from rew_common import Measurement


def project(delta_spl=0.0, delta_phase=0.0):
    f = np.linspace(20.0, 250.0, 512)
    roles = {}
    for i, role in enumerate(("L main", "L sub", "R main", "R sub")):
        spl = 70 + i + np.sin(f / 20) + delta_spl
        phase = -f / 3 + i + delta_phase
        roles[role] = Measurement(role, f, spl, phase)
    return SimpleNamespace(measurements_by_role=roles)


def test_parity_pass_for_identical_data():
    result = compare_projects(project(), project())
    assert result["status"] == "PASS"


def test_parity_fail_for_smoothed_or_shifted_data():
    result = compare_projects(project(), project(delta_spl=0.3, delta_phase=3.0))
    assert result["status"] == "FAIL"
