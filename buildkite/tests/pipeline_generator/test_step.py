import base64
import subprocess
import sys
from pathlib import Path

import pytest

import buildkite_step
from pipeline_generator import select_steps_and_dependencies
from step import Step, group_steps, read_steps_from_job_dir

pytestmark = pytest.mark.usefixtures("fake_global_config")

TEST_JOB_DIR = Path(__file__).resolve().parent / "test_files" / "test_jobs"


def _render_single_step(step):
    return buildkite_step.convert_group_step_to_buildkite_step(
        {
            step.group: [step],
        }
    )[0]


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


def test_selected_steps_include_transitive_dependencies():
    steps = [
        Step(label="Image", key="image-build", commands=["build"]),
        Step(
            label="Prepare",
            key="prepare",
            depends_on=["image-build"],
            commands=["prepare"],
        ),
        Step(
            label="Test",
            key="test",
            depends_on=["prepare"],
            commands=["test"],
        ),
        Step(label="Other", key="other", commands=["other"]),
    ]

    selected, selected_keys = select_steps_and_dependencies(steps, frozenset({"test"}))

    assert [step.key for step in selected] == [
        "image-build",
        "prepare",
        "test",
    ]
    assert selected_keys == frozenset({"image-build", "prepare", "test"})


def test_selected_steps_reject_unknown_key():
    with pytest.raises(ValueError, match="Unknown CI step key.*missing"):
        select_steps_and_dependencies(
            [Step(label="Test", key="test", commands=["test"])],
            frozenset({"missing"}),
        )


def test_selected_steps_reject_unknown_dependency():
    step = Step(
        label="Test",
        key="test",
        depends_on=["missing"],
        commands=["test"],
    )

    with pytest.raises(
        ValueError, match="CI step test depends on unknown step missing"
    ):
        select_steps_and_dependencies([step], frozenset({"test"}))


def test_selected_step_runs_without_source_match(fake_global_config):
    fake_global_config["only_step_keys"] = frozenset({"selected"})
    step = Step(
        label="Selected",
        key="selected",
        source_file_dependencies=["unrelated.py"],
        commands=["test"],
    )

    assert buildkite_step._step_should_run(step, ["changed.py"])


def test_every_generated_job_has_a_unique_key():
    steps = read_steps_from_job_dir(str(TEST_JOB_DIR))

    buildkite_groups = buildkite_step.convert_group_step_to_buildkite_step(
        group_steps(steps)
    )
    jobs = [job for group in buildkite_groups for job in group.steps]
    keys = [job.key for job in jobs]

    assert all(keys)
    assert len(keys) == len(set(keys))
    assert "test-a" in keys
    assert "block-test-a" in keys


def test_explicit_step_key_is_preserved():
    step = Step(
        label="Label-derived key should not win",
        group="Keys",
        key="configured-key",
        commands=["true"],
    )

    command_step = next(
        job
        for job in _render_single_step(step).steps
        if isinstance(job, buildkite_step.BuildkiteCommandStep)
    )

    assert command_step.key == "configured-key"


def test_continue_on_failure_exits_nonzero_after_command_failure(monkeypatch):
    monkeypatch.setenv("CONTINUE_ON_FAILURE", "1")
    step = Step(
        label="Continue on failure",
        group="Failure handling",
        commands=[
            "echo before",
            "false",
            "echo after",
        ],
    )

    commands = buildkite_step._prepare_commands(
        step,
        variables_to_inject={},
        setup_profile="none",
    )
    script = " && ".join(commands).replace("$$CI_OVERALL_STATUS", "$CI_OVERALL_STATUS")

    result = subprocess.run(
        ["bash", "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert "__CI_OVERALL_STATUS" not in script
    assert "CI_OVERALL_STATUS=1" in script
    assert "after" in result.stdout
    assert result.returncode == 1


def test_otel_trace_wraps_commands_only_on_trusted_main(fake_global_config):
    fake_global_config["branch"] = "main"
    step = Step(
        label="Traced",
        group="Tracing",
        commands=["export VALUE=ready", 'test "$VALUE" = ready'],
        otel_trace=True,
    )

    commands = buildkite_step._prepare_commands(
        step,
        variables_to_inject={},
        setup_profile="none",
    )

    assert commands[0] == "source /vllm-workspace/.buildkite/scripts/ci_otel.sh"
    assert commands[2].startswith("ci_otel_run 1 ")
    assert commands[4].startswith("ci_otel_run 2 ")
    _, _, encoded_label, encoded_command = commands[4].split(" ")
    assert base64.b64decode(encoded_label).decode() == "test VALUE = ready"
    assert base64.b64decode(encoded_command).decode() == 'test "$VALUE" = ready'
    assert "'" not in commands[4]


def test_otel_trace_is_disabled_for_amd_mirror(fake_global_config):
    fake_global_config["branch"] = "main"
    step = Step(label="AMD mirror", commands=["pytest tests"], otel_trace=True)

    commands = buildkite_step._prepare_commands(
        step,
        variables_to_inject={},
        setup_profile="amd",
    )

    assert not any("ci_otel" in command for command in commands)


def test_otel_trace_is_disabled_for_pull_requests(fake_global_config):
    fake_global_config["branch"] = "main"
    fake_global_config["pull_request"] = "123"
    step = Step(label="Untrusted", commands=["pytest tests"], otel_trace=True)

    commands = buildkite_step._prepare_commands(
        step,
        variables_to_inject={},
        setup_profile="none",
    )

    assert not any("ci_otel" in command for command in commands)


def test_generated_steps_retry_when_the_agent_is_lost():
    step = Step(
        label="Agent retry",
        group="Failure handling",
        key="image-build-agent-retry",
        device="h100",
        commands=["pytest tests/basic.py"],
    )

    command_step = _render_single_step(step).steps[0]

    assert command_step.retry == {
        "automatic": [{"exit_status": -1, "limit": 1}],
    }


def test_agent_lost_retry_preserves_step_retry_conditions():
    step = Step(
        label="Agent retry with policy",
        group="Failure handling",
        key="image-build-agent-retry-with-policy",
        device="h100",
        commands=["pytest tests/basic.py"],
        retry={"automatic": {"exit_status": 143, "limit": 2}},
    )

    command_step = _render_single_step(step).steps[0]

    assert command_step.retry == {
        "automatic": [
            {"exit_status": -1, "limit": 1},
            {"exit_status": 143, "limit": 2},
        ],
    }


def test_multi_gpu_step_dumps_nvidia_topology():
    step = Step(
        label="Distributed Comm Ops Test",
        group="Distributed",
        key="distributed-comm-ops",
        depends_on=["image-build"],
        device="h100",
        num_devices=2,
        working_dir="/vllm-workspace/tests",
        commands=["pytest tests/distributed/test_comm_ops.py"],
    )

    commands = buildkite_step._prepare_commands(step, variables_to_inject={})

    assert "(command nvidia-smi topo -m || true)" in commands
    # Topology dump comes after the base GPU info and before coredump setup.
    topo_index = commands.index("(command nvidia-smi topo -m || true)")
    smi_index = commands.index("(command nvidia-smi || true)")
    assert smi_index < topo_index


def test_multi_node_step_dumps_nvidia_topology():
    step = Step(
        label="Multi-node Test",
        group="Distributed",
        key="multi-node",
        depends_on=["image-build"],
        device="h100",
        num_nodes=2,
        num_devices=4,
        working_dir="/vllm-workspace/tests",
        commands=["pytest tests/distributed/test_multi_node.py"],
    )

    commands = buildkite_step._prepare_commands(step, variables_to_inject={})

    assert "(command nvidia-smi topo -m || true)" in commands


def test_single_gpu_step_skips_nvidia_topology():
    step = Step(
        label="Single GPU Test",
        group="Single",
        key="single-gpu",
        depends_on=["image-build"],
        device="h100",
        num_devices=1,
        working_dir="/vllm-workspace/tests",
        commands=["pytest tests/basic.py"],
    )

    commands = buildkite_step._prepare_commands(step, variables_to_inject={})

    # Base GPU info is still emitted, but the topology dump is multi-GPU only.
    assert "(command nvidia-smi || true)" in commands
    assert "(command nvidia-smi topo -m || true)" not in commands


def test_torch_nightly_flag_no_separate_group(fake_global_config):
    # TORCH_NIGHTLY=1 now runs the entire existing pipeline against the nightly
    # base image (built by image_build.sh when TORCH_NIGHTLY=1, CUDA/GPU lane).
    # It must NOT synthesize a separate "vLLM Against PyTorch Nightly" group.
    fake_global_config["torch_nightly"] = "1"
    step = Step(
        label="Untagged test",
        group="Some Group",
        key="untagged-test",
        depends_on=["image-build"],
        working_dir="/vllm-workspace/tests",
        commands=["pytest tests/untagged.py"],
        source_file_dependencies=["tests/untagged.py"],
        device="h200_18gb",
    )

    group_steps = buildkite_step.convert_group_step_to_buildkite_step(
        {
            step.group: [step],
        }
    )

    # No dedicated torch-nightly group is synthesized anymore.
    assert not any(g.group == "vLLM Against PyTorch Nightly" for g in group_steps)

    # The step stays in its normal group and is built once (no nightly duplicate).
    normal_group = next(g for g in group_steps if g.group == "Some Group")
    labels = [
        s.label
        for s in normal_group.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    ]
    assert "Untagged test" in labels
    assert not any(lbl.startswith("Torch Nightly ") for lbl in labels)


def test_image_tag_matches_get_image_and_latest_suppressed_on_nightly(
    fake_global_config,
):
    fake_global_config["branch"] = "main"
    # Build target ($IMAGE_TAG) mirrors what test steps pull (get_image()).
    vars_ = buildkite_step._get_variables_to_inject()
    assert vars_["$IMAGE_TAG"] == buildkite_step.get_image()
    assert (
        vars_["$IMAGE_TAG_LATEST"] == "example.com/vllm/vllm-ci-postmerge-repo:latest"
    )

    # A nightly run must not publish :latest (and still mirrors get_image()).
    fake_global_config["torch_nightly"] = "1"
    vars_ = buildkite_step._get_variables_to_inject()
    assert vars_["$IMAGE_TAG"] == buildkite_step.get_image()
    assert vars_["$IMAGE_TAG_LATEST"] is None


def test_timeout_in_minutes_propagates_to_command_step():
    step = Step(
        label="Timed test",
        group="Timing",
        key="timed-test",
        depends_on=["image-build"],
        working_dir="/vllm-workspace/tests",
        commands=["pytest tests/timed.py"],
        device="h200_18gb",
        timeout_in_minutes=42,
    )

    group_step = _render_single_step(step)
    command_step = next(
        s
        for s in group_step.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )

    assert command_step.timeout_in_minutes == 42


def test_skip_timeout_omits_timeout_from_command_step(monkeypatch):
    monkeypatch.setenv(buildkite_step.SKIP_TIMEOUT_ENV_VAR, "1")
    step = Step(
        label="Skipped timeout",
        group="Timing",
        commands=["pytest tests/timed.py"],
        device="h200_18gb",
        timeout_in_minutes=42,
    )

    group_step = _render_single_step(step)
    command_step = next(
        s
        for s in group_step.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )

    assert command_step.timeout_in_minutes is None
    assert "timeout_in_minutes" not in command_step.model_dump(exclude_none=True)


def test_missing_timeout_in_minutes_is_omitted_from_pipeline():
    step = Step(
        label="Untimed test",
        group="Timing",
        key="untimed-test",
        depends_on=["image-build"],
        working_dir="/vllm-workspace/tests",
        commands=["pytest tests/untimed.py"],
        device="h200_18gb",
    )

    group_step = _render_single_step(step)
    command_step = next(
        s
        for s in group_step.steps
        if isinstance(s, buildkite_step.BuildkiteCommandStep)
    )

    assert command_step.timeout_in_minutes is None
    # exclude_none is used when dumping the pipeline, so an unset timeout must
    # not surface as a key at all.
    assert "timeout_in_minutes" not in command_step.model_dump(exclude_none=True)


def test_source_file_dependencies_match_without_exclusions():
    deps = ["vllm/", "tests/models/multimodal"]
    assert buildkite_step._source_file_dependencies_match(
        deps, ["vllm/model_executor/models/llama.py"]
    )
    assert not buildkite_step._source_file_dependencies_match(deps, ["docs/foo.md"])


def test_exclusion_carves_out_subtree_from_broad_include():
    deps = ["vllm/", "!vllm/distributed/kv_transfer/"]
    # A change confined to the excluded subtree does not select the step.
    assert not buildkite_step._source_file_dependencies_match(
        deps, ["vllm/distributed/kv_transfer/kv_connector/v1/nixl/worker.py"]
    )
    # A change elsewhere under the broad include still selects it.
    assert buildkite_step._source_file_dependencies_match(
        deps, ["vllm/model_executor/models/llama.py"]
    )
    # Sibling distributed code (not under the exclusion) still selects it.
    assert buildkite_step._source_file_dependencies_match(
        deps, ["vllm/distributed/parallel_state.py"]
    )


def test_exclusion_only_applies_per_file():
    # A diff touching both an excluded file and an included file still matches:
    # the included file is enough on its own.
    deps = ["vllm/", "!vllm/distributed/kv_transfer/"]
    assert buildkite_step._source_file_dependencies_match(
        deps,
        [
            "vllm/distributed/kv_transfer/kv_connector/v1/nixl/worker.py",
            "vllm/config/__init__.py",
        ],
    )


def test_step_explicitly_listing_excluded_subtree_still_matches():
    # A dedicated step that includes the subtree directly is unaffected.
    deps = ["vllm/distributed/kv_transfer/kv_connector/v1/nixl/"]
    assert buildkite_step._source_file_dependencies_match(
        deps, ["vllm/distributed/kv_transfer/kv_connector/v1/nixl/worker.py"]
    )


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
