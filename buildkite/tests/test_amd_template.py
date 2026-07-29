import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


MINIJINJA = shutil.which("minijinja-cli")
TEMPLATE = Path(__file__).parents[1] / "test-template-amd.j2"
BOOTSTRAP = Path(__file__).parents[1] / "bootstrap-amd.sh"
BASE_RETRY = [
    {"signal_reason": "stack_error", "limit": 1},
    {"signal_reason": "agent_stop", "limit": 1},
    {"signal_reason": "agent_refused", "limit": 1},
    {"exit_status": -1, "signal_reason": "none", "limit": 1},
]
ENABLED_RETRY = [
    BASE_RETRY[0],
    {"exit_status": 1, "limit": 1},
    *BASE_RETRY[1:],
]


def _render_amd_step(
    *,
    capability=None,
    step_request=None,
    no_plugin=False,
    num_nodes=None,
    nightly="0",
    torch_nightly="0",
    kill_switch="0",
):
    assert MINIJINJA is not None, "minijinja-cli is required for template tests"
    step = {
        "label": "Minimal AMD test",
        "mirror_hardwares": ["amdexperimental"],
        "agent_pool": "mi300_1",
        "commands": ["pytest tests/example.py"],
    }
    if step_request is not None:
        step["hf_offline_retry"] = step_request
    if no_plugin:
        step["no_plugin"] = True
    if num_nodes is not None:
        step["num_nodes"] = num_nodes

    context = {"steps": [step]}
    if capability is not None:
        context["amd_hf_offline_retry"] = capability

    definitions = {
        "branch": "main",
        "list_file_diff": "",
        "run_all": "1",
        "nightly": nightly,
        "torch_nightly": torch_nightly,
        "mirror_hw": "amdexperimental",
        "fail_fast": "0",
        "vllm_use_precompiled": "0",
        "vllm_merge_base_commit": "HEAD",
        "cov_enabled": "0",
        "vllm_ci_branch": "main",
        "vllm_ci_disable_hf_offline_retry": kill_switch,
        "rocm_base_refresh_skip": "0",
        "rocm_base_refresh_force": "0",
        "rocm_base_refresh_diff_unavailable": "0",
    }
    command = [MINIJINJA, str(TEMPLATE), "-", "--format", "yaml"]
    for name, value in definitions.items():
        command.extend(["-D", f"{name}={value}"])

    result = subprocess.run(
        command,
        input=yaml.safe_dump(context),
        check=True,
        capture_output=True,
        text=True,
    )
    pipeline = yaml.safe_load(result.stdout)
    return next(
        step
        for step in pipeline[0]["steps"]
        if step["label"] == "mi300_1: Minimal AMD test"
    )


HF_RETRY_CASES = {
    "explicit-cohort": ({"capability": True, "step_request": True}, "1"),
    "missing-capability": ({"step_request": True}, "0"),
    "disabled-capability": (
        {"capability": False, "step_request": True},
        "0",
    ),
    "missing-request": ({"capability": True}, "0"),
    "explicit-opt-out": ({"capability": True, "step_request": False}, "0"),
    "malformed-capability": (
        {"capability": "true", "step_request": True},
        "0",
    ),
    "malformed-request": (
        {"capability": True, "step_request": "true"},
        "0",
    ),
    "multi-node": (
        {"capability": True, "step_request": True, "num_nodes": 2},
        "0",
    ),
    "nightly": (
        {"capability": True, "step_request": True, "nightly": "1"},
        "0",
    ),
    "torch-nightly": (
        {"capability": True, "step_request": True, "torch_nightly": "1"},
        "0",
    ),
    "kill-switch": (
        {"capability": True, "step_request": True, "kill_switch": "1"},
        "0",
    ),
    "malformed-kill-switch": (
        {"capability": True, "step_request": True, "kill_switch": "true"},
        "0",
    ),
    "direct-command": (
        {"capability": True, "step_request": True, "no_plugin": True},
        None,
    ),
}


@pytest.mark.parametrize(
    ("overrides", "expected_flag"),
    [
        pytest.param(overrides, expected_flag, id=name)
        for name, (overrides, expected_flag) in HF_RETRY_CASES.items()
    ],
)
def test_hf_offline_retry_renders_exact_policy(overrides, expected_flag):
    step = _render_amd_step(**overrides)

    assert step.get("env", {}).get("VLLM_CI_HF_OFFLINE_RETRY") == expected_flag
    expected_retry = ENABLED_RETRY if expected_flag == "1" else BASE_RETRY
    assert step["retry"] == {"automatic": expected_retry}
    exit_statuses = {
        condition["exit_status"]
        for condition in step["retry"]["automatic"]
        if "exit_status" in condition
    }
    assert 2 not in exit_statuses
    assert 123 not in exit_statuses


def test_bootstrap_validates_and_passes_generation_kill_switch():
    bootstrap = BOOTSTRAP.read_text()

    assert '-z "${VLLM_CI_DISABLE_HF_OFFLINE_RETRY+x}"' in bootstrap
    assert 'case "$VLLM_CI_DISABLE_HF_OFFLINE_RETRY" in' in bootstrap
    assert "export VLLM_CI_DISABLE_HF_OFFLINE_RETRY" in bootstrap
    assert (
        "-D vllm_ci_disable_hf_offline_retry="
        '"$VLLM_CI_DISABLE_HF_OFFLINE_RETRY"' in bootstrap
    )


@pytest.mark.parametrize("value", ["", "true", "2"])
def test_bootstrap_rejects_invalid_generation_kill_switch(value):
    result = subprocess.run(
        ["bash", str(BOOTSTRAP)],
        env={**os.environ, "VLLM_CI_DISABLE_HF_OFFLINE_RETRY": value},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "must be exactly 0 or 1" in result.stderr
