import sys
from pathlib import Path

import pytest

PIPELINE_GENERATOR_DIR = Path(__file__).resolve().parents[2] / "pipeline_generator"
sys.path.insert(0, str(PIPELINE_GENERATOR_DIR))

import buildkite_step
import step as step_module
from constants import AgentQueue
from step import Step, group_steps, read_steps_from_job_dir

TEST_JOB_DIR = Path(__file__).resolve().parent / "test_files" / "test_jobs"


@pytest.fixture(autouse=True)
def fake_global_config(monkeypatch):
    config = {
        "name": "vllm_ci",
        "github_repo_name": "vllm-project/vllm",
        "job_dirs": [],
        "registries": "example.com/vllm",
        "repositories": {
            "main": "vllm-ci-postmerge-repo",
            "premerge": "vllm-ci-test-repo",
        },
        "branch": "test-branch",
        "commit": "abc123",
        "pull_request": "false",
        "docs_only_disable": "1",
        "nightly": "0",
        "run_all": False,
        "list_file_diff": [],
        "fail_fast": False,
    }
    monkeypatch.setattr(step_module, "get_global_config", lambda: config)
    monkeypatch.setattr(buildkite_step, "get_global_config", lambda: config)
    monkeypatch.setattr(
        buildkite_step,
        "get_ecr_cache_registry",
        lambda: ("cache-from", "cache-to"),
    )
    monkeypatch.setattr(
        buildkite_step,
        "get_image",
        lambda cpu=False, arm64=False: "test-image",
    )
    monkeypatch.setattr(
        buildkite_step,
        "get_torch_nightly_image",
        lambda: "torch-nightly-image",
    )


def _render_single_step(step):
    return buildkite_step.convert_group_step_to_buildkite_step({
        step.group: [step],
    })[0]


def test_read_steps_from_job_dir():
    steps = read_steps_from_job_dir(str(TEST_JOB_DIR))
    steps_by_label = {step.label: step for step in steps}

    assert len(steps) == 8
    assert steps_by_label["Test A"].group == "bb"
    assert steps_by_label["Test A"].commands == [
        'echo "Test A"',
        'echo "Test A.B"',
    ]
    assert steps_by_label["Test D"].num_nodes == 2
    assert steps_by_label["Test D"].num_devices == 4
    assert steps_by_label["Test E"].group == "a"


def test_group_steps_sorts_steps_within_each_group():
    steps = read_steps_from_job_dir(str(TEST_JOB_DIR))
    grouped_steps = group_steps(steps)

    assert set(grouped_steps) == {"a", "bb"}
    assert [step.label for step in grouped_steps["a"]] == [
        "Test E",
        "Test F",
        "Test G",
        "Test H",
    ]
    assert [step.label for step in grouped_steps["bb"]] == [
        "Test A",
        "Test B",
        "Test C",
        "Test D",
    ]


@pytest.mark.parametrize(
    ("device", "queue"),
    [
        ("mi300_4", AgentQueue.AMD_MI300_4),
        ("mi300_8", AgentQueue.AMD_MI300_8),
    ],
)
def test_direct_amd_gpu_steps_use_amd_ci_path(device, queue):
    step = Step(
        label="AMD direct test",
        group="Direct AMD",
        key=f"amd-direct-{device}",
        depends_on=["image-build"],
        device=device,
        optional=True,
        soft_fail=True,
        working_dir="/vllm-workspace/tests",
        commands=["pytest tests/foo.py"],
    )

    group_step = _render_single_step(step)
    block_step, command_step = group_step.steps

    assert group_step.group == "Hardware-AMD Tests"
    assert block_step.block == f"Run AMD: AMD direct test ({device})"
    assert block_step.depends_on == ["image-build-amd"]
    assert command_step.label == f"AMD: AMD direct test ({device})"
    assert command_step.depends_on == ["image-build-amd", block_step.key]
    assert command_step.agents == {"queue": queue}
    assert command_step.commands == [
        "bash .buildkite/scripts/hardware_ci/run-amd-test.sh",
    ]
    assert command_step.plugins is None
    assert command_step.retry == buildkite_step.AMD_RETRY
    assert len(command_step.retry["automatic"]) == 1

    test_commands = command_step.env["VLLM_TEST_COMMANDS"]
    assert test_commands.startswith(f"export VLLM_TEST_GROUP_NAME={step.key}")
    assert "(command amd-smi || true)" in test_commands
    assert "cd /vllm-workspace/tests" in test_commands
    assert "pytest tests/foo.py" in test_commands
    assert "nvidia-smi" not in test_commands
    assert "CUDA_ENABLE_COREDUMP_ON_EXCEPTION" not in test_commands


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
