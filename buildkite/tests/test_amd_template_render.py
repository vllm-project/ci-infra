import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import amd


REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "buildkite" / "test-template-amd.j2"
AMD_BOOTSTRAP = REPO_ROOT / "buildkite" / "bootstrap-amd.sh"
MINIJINJA = shutil.which("minijinja-cli")
pytestmark = pytest.mark.skipif(
    MINIJINJA is None,
    reason="minijinja-cli is required to render the AMD pipeline template",
)


def _render_template(
    *, nightly: str, torch_nightly: str, hf_hub_mode: str | None = None
):
    context = {
        "steps": [
            {
                "label": "Monitored wrapper",
                "mirror_hardwares": ["amdproduction"],
                "agent_pool": "mi300_1",
                "num_gpus": 1,
                "dind": True,
                "working_dir": "/vllm-workspace/tests",
                "commands": ["pytest tests/monitored.py"],
            },
            {
                "label": "Direct command",
                "mirror_hardwares": ["amdproduction"],
                "agent_pool": "mi300_1",
                "num_gpus": 1,
                "dind": True,
                "no_plugin": True,
                "command": "echo direct",
            },
        ],
        "branch": "feature",
        "list_file_diff": "",
        "run_all": "1",
        "nightly": nightly,
        "torch_nightly": torch_nightly,
        "mirror_hw": "amdproduction",
    }
    if hf_hub_mode is not None:
        context["hf_hub_mode"] = hf_hub_mode
    rendered = subprocess.check_output(
        [MINIJINJA, "--format", "yaml", str(TEMPLATE), "-"],
        cwd=REPO_ROOT,
        input=yaml.safe_dump(context),
        text=True,
    )
    pipeline = yaml.safe_load(rendered)
    amd_group = next(group for group in pipeline if group.get("group") == "AMD Tests")
    return {step["label"]: step for step in amd_group["steps"]}


@pytest.mark.parametrize(
    ("nightly", "torch_nightly", "expected_mode"),
    [
        ("0", "0", "offline-first"),
        ("1", "0", "online"),
        ("0", "1", "online"),
    ],
)
def test_amd_template_selects_hf_hub_mode(nightly, torch_nightly, expected_mode):
    steps_by_label = _render_template(
        nightly=nightly,
        torch_nightly=torch_nightly,
        hf_hub_mode="offline-first",
    )

    monitored = steps_by_label["mi300_1: Monitored wrapper"]
    assert monitored["env"]["VLLM_CI_HF_HUB_MODE"] == expected_mode


@pytest.mark.parametrize(
    ("nightly", "torch_nightly"),
    [("0", "0"), ("1", "0"), ("0", "1")],
)
def test_amd_template_preserves_existing_data_without_hf_hub_mode(
    nightly, torch_nightly
):
    without_mode = _render_template(
        nightly=nightly,
        torch_nightly=torch_nightly,
    )
    with_empty_mode = _render_template(
        nightly=nightly,
        torch_nightly=torch_nightly,
        hf_hub_mode="",
    )
    assert with_empty_mode == without_mode

    monitored = without_mode["mi300_1: Monitored wrapper"]
    direct = without_mode["mi300_1: Direct command"]

    assert "VLLM_CI_HF_HUB_MODE" not in monitored["env"]
    assert monitored["retry"] == direct["retry"]
    assert monitored["retry"]["automatic"] == [
        {"signal_reason": "stack_error", "limit": 1},
        {"signal_reason": "agent_stop", "limit": 1},
        {"signal_reason": "agent_refused", "limit": 1},
        {"exit_status": -1, "signal_reason": "none", "limit": 1},
    ]


def test_amd_bootstrap_passes_hf_hub_mode_to_template():
    assert '-D hf_hub_mode="${VLLM_CI_HF_HUB_MODE:-}"' in AMD_BOOTSTRAP.read_text()


def test_amd_template_scopes_hf_retry_to_wrapped_steps():
    steps_by_label = _render_template(
        nightly="0",
        torch_nightly="0",
        hf_hub_mode="offline-first",
    )

    monitored = steps_by_label["mi300_1: Monitored wrapper"]
    direct = steps_by_label["mi300_1: Direct command"]
    hf_retry = {
        "exit_status": amd.AMD_HF_HUB_RETRY_EXIT_STATUS,
        "limit": 1,
    }

    assert monitored["command"] == (
        "bash .buildkite/scripts/hardware_ci/run-amd-test.sh"
    )
    assert monitored["retry"]["automatic"] == [
        *direct["retry"]["automatic"],
        hf_retry,
    ]

    assert direct["command"] == "echo direct"
    steps_with_hf_mode = {
        label
        for label, step in steps_by_label.items()
        if "VLLM_CI_HF_HUB_MODE" in step.get("env", {})
    }
    steps_with_hf_retry = {
        label
        for label, step in steps_by_label.items()
        if hf_retry in step.get("retry", {}).get("automatic", [])
    }
    assert steps_with_hf_mode == {"mi300_1: Monitored wrapper"}
    assert steps_with_hf_retry == {"mi300_1: Monitored wrapper"}
