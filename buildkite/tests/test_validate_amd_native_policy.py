import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "validate-amd-native-policy.py"
SPEC = importlib.util.spec_from_file_location("validate_amd_native_policy", MODULE_PATH)
assert SPEC and SPEC.loader
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


def _config(**overrides):
    step = {
        "label": "AMD policy test",
        "mirror_hardwares": ["amdproduction"],
        "agent_pool": "mi300_1",
        "native_ci": True,
    }
    step.update(overrides)
    return {"steps": [step]}


@pytest.mark.parametrize("gpu_count", [1, 2, 4, 8])
def test_accepts_native_mi300_gpu_counts(gpu_count):
    config = _config(
        agent_pool=f"mi300_{gpu_count}",
        num_gpus=gpu_count,
    )

    assert VALIDATOR.validate_config(config) == []


@pytest.mark.parametrize("family", ["mi250", "mi325", "mi355"])
def test_accepts_legacy_non_mi300(family):
    config = _config(
        agent_pool=f"{family}_1",
        native_ci=False,
    )

    assert VALIDATOR.validate_config(config) == []


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"native_ci": False}, "mi300_1 must use native"),
        ({"agent_pool": "mi325_1"}, "mi325_1 must use legacy DinD"),
        ({"native_ci": "true"}, "native_ci must be a boolean"),
        ({"num_nodes": 2}, "cannot be multi-node"),
        ({"no_plugin": True}, "cannot use no_plugin"),
        ({"agent_pool": "mi300_4", "num_gpus": 2}, "does not match mi300_4"),
        ({"agent_pool": None}, "invalid or missing AMD agent_pool"),
    ],
)
def test_rejects_invalid_policy(overrides, message):
    assert any(
        message in error for error in VALIDATOR.validate_config(_config(**overrides))
    )
