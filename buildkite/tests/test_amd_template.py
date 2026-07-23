import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


MINIJINJA = shutil.which("minijinja-cli")
TEMPLATE = Path(__file__).parents[1] / "test-template-amd.j2"
FLAG_PATTERN = re.compile(r'VLLM_CI_HF_OFFLINE_RETRY: "([01])"')


def test_hf_offline_retry_policy_keeps_fail_closed_guards():
    template = TEMPLATE.read_text()

    assert "hf_offline_retry is boolean" in template
    assert "step_hf_offline_retry is boolean" in template
    assert "step_hf_offline_retry is undefined" in template


@pytest.mark.skipif(MINIJINJA is None, reason="minijinja-cli is not installed")
@pytest.mark.parametrize(
    ("default", "step_value", "expected"),
    [
        pytest.param(True, None, "1", id="enabled-default"),
        pytest.param(False, None, "0", id="disabled-default"),
        pytest.param("true", None, "0", id="non-boolean-default-fails-closed"),
        pytest.param(True, False, "0", id="step-opt-out"),
        pytest.param(True, "false", "0", id="non-boolean-step-fails-closed"),
        pytest.param(False, True, "1", id="step-opt-in"),
    ],
)
def test_hf_offline_retry_policy(default, step_value, expected):
    step = {
        "label": "Minimal AMD test",
        "mirror_hardwares": ["amdexperimental"],
        "agent_pool": "mi300_1",
        "commands": ["pytest tests/example.py"],
    }
    if step_value is not None:
        step["hf_offline_retry"] = step_value

    result = subprocess.run(
        [
            MINIJINJA,
            str(TEMPLATE),
            "-",
            "--format",
            "yaml",
            "-D",
            "branch=main",
            "-D",
            "list_file_diff=",
            "-D",
            "run_all=1",
            "-D",
            "nightly=1",
            "-D",
            "torch_nightly=0",
            "-D",
            "mirror_hw=amdexperimental",
            "-D",
            "fail_fast=0",
            "-D",
            "vllm_use_precompiled=0",
            "-D",
            "vllm_merge_base_commit=HEAD",
            "-D",
            "cov_enabled=0",
            "-D",
            "vllm_ci_branch=main",
        ],
        input=yaml.safe_dump(
            {
                "hf_offline_retry": default,
                "steps": [step],
            }
        ),
        check=True,
        capture_output=True,
        text=True,
    )

    assert FLAG_PATTERN.findall(result.stdout) == [expected]
