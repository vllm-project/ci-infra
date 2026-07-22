import base64
import os
import re
import shlex
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


def _make_pytest_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "pytest-bin"
    bin_dir.mkdir()
    pytest_executable = bin_dir / "pytest"
    pytest_executable.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m pytest "$@"\n',
        encoding="utf-8",
    )
    pytest_executable.chmod(0o755)
    return bin_dir


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


def test_otel_trace_wraps_every_command_on_trusted_main(fake_global_config):
    fake_global_config["branch"] = "main"
    step = Step(
        label="Traced",
        group="Tracing",
        commands=["export VALUE=ready", 'test "$VALUE" = ready'],
    )

    commands = buildkite_step._prepare_commands(
        step,
        variables_to_inject={},
        setup_profile="none",
    )

    assert "CI_INFRA_OTEL_DIR=$$(mktemp -d 2>/dev/null)" in commands[0]
    assert commands[0].endswith("fi; :")
    assert '. "$$CI_INFRA_OTEL_DIR/ci_otel.sh"' in commands[0]
    assert "ci_otel_start 1 " in commands[2]
    assert "export VALUE=ready" in commands[2]
    assert "ci_otel_finish" in commands[2]
    assert "ci_otel_start 2 " in commands[4]
    assert 'test "$VALUE" = ready' in commands[4]
    match = re.search(r"ci_otel_start 2 (\S+)", commands[4])
    assert match is not None
    (encoded_label,) = match.groups()
    assert base64.b64decode(encoded_label).decode() == "test VALUE = ready"
    assert "'" not in commands[4]


def test_otel_trace_allows_exact_api_treatment_branch(
    fake_global_config, monkeypatch
):
    fake_global_config["branch"] = "khluu/otel"
    monkeypatch.setenv("BUILDKITE_SOURCE", "api")
    monkeypatch.setenv("CI_INFRA_OTEL_TREATMENT_BRANCH", "khluu/otel")
    step = Step(label="Treatment", commands=["pytest tests"])

    commands = buildkite_step._prepare_commands(
        step,
        variables_to_inject={},
        setup_profile="none",
    )

    assert any("ci_otel_start" in command for command in commands)


def test_otel_trace_rejects_non_api_treatment_branch(
    fake_global_config, monkeypatch
):
    fake_global_config["branch"] = "khluu/otel"
    monkeypatch.setenv("BUILDKITE_SOURCE", "webhook")
    monkeypatch.setenv("CI_INFRA_OTEL_TREATMENT_BRANCH", "khluu/otel")
    step = Step(label="Untrusted treatment", commands=["pytest tests"])

    commands = buildkite_step._prepare_commands(
        step,
        variables_to_inject={},
        setup_profile="none",
    )

    assert not any("ci_otel" in command for command in commands)


def test_otel_trace_preserves_generator_variable_injection(fake_global_config):
    fake_global_config["branch"] = "main"
    step = Step(
        label="Traced image build",
        group="Tracing",
        commands=["build $REGISTRY $REPO $BUILDKITE_COMMIT"],
    )

    commands = buildkite_step._prepare_commands(
        step,
        variables_to_inject={
            "$REGISTRY": "registry.example.com",
            "$REPO": "vllm-ci",
            "$BUILDKITE_COMMIT": "$$BUILDKITE_COMMIT",
        },
        setup_profile="none",
    )

    assert "build registry.example.com vllm-ci $$BUILDKITE_COMMIT" in commands[2]


def test_otel_helper_bundle_installs_and_sources():
    command = buildkite_step._otel_setup_command().replace("$$", "$")
    result = subprocess.run(
        [
            "bash",
            "-c",
            command
            + ' && test -f "$CI_INFRA_OTEL_DIR/ci_otel.py"'
            + ' && test -f "$CI_INFRA_OTEL_DIR/ci_pytest.sh"'
            + ' && test -f "$CI_INFRA_OTEL_DIR/ci_pytest_otel.py"'
            + ' && test "$CI_INFRA_OTEL_READY" = 1'
            + ' && test "$PYTHONPATH" = original-pythonpath'
            + ' && test "$PYTEST_ADDOPTS" = original-pytest-options'
            + " && type ci_otel_start"
            + " && type ci_otel_finish",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": "original-pythonpath",
            "PYTEST_ADDOPTS": "original-pytest-options",
        },
    )

    assert result.returncode == 0, result.stderr


def test_pytest_shim_traces_with_command_level_pythonpath_override(
    fake_global_config, tmp_path
):
    fake_global_config["branch"] = "main"
    test_file = tmp_path / "test_sample.py"
    test_file.write_text("def test_sample():\n    assert True\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pytest_bin = _make_pytest_bin(tmp_path)
    step = Step(
        label="PYTHONPATH override",
        group="Tracing",
        commands=[
            f"PYTHONPATH={shlex.quote(str(workspace))} "
            f"pytest -q {shlex.quote(str(test_file))}"
        ],
    )
    commands = buildkite_step._prepare_commands(
        step,
        variables_to_inject={},
        setup_profile="none",
    )

    script = (
        "\n".join(commands).replace("$$", "$")
        + '\ngrep -q \'"name":"pytest.test"\' '
        + '"$CI_INFRA_OTEL_SPOOL_DIR"/spans-*.jsonl'
    )
    result = subprocess.run(
        ["/bin/sh", "-e", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{pytest_bin}:{os.environ['PATH']}",
            "PYTEST_ADDOPTS": "",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "1 passed" in result.stdout
    assert "No module named 'ci_pytest_otel'" not in result.stderr


def test_pytest_shim_falls_back_when_plugin_disappears(tmp_path):
    test_file = tmp_path / "test_sample.py"
    test_file.write_text("def test_sample():\n    assert True\n", encoding="utf-8")
    pytest_bin = _make_pytest_bin(tmp_path)
    missing_helpers = tmp_path / "missing-helpers"
    missing_helpers.mkdir()
    command = buildkite_step._otel_setup_command().replace("$$", "$")
    shell = (
        command
        + "\nci_otel_start 1 dGVzdA=="
        + f"\nCI_INFRA_OTEL_DIR={shlex.quote(str(missing_helpers))}"
        + "\nexport CI_INFRA_OTEL_DIR"
        + f"\npytest -q {shlex.quote(str(test_file))}"
    )

    result = subprocess.run(
        ["/bin/sh", "-e", "-c", shell],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{pytest_bin}:{os.environ['PATH']}",
            "PYTEST_ADDOPTS": "",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "1 passed" in result.stdout
    assert "pytest tracing skipped; running pytest normally" in result.stderr


def test_otel_setup_failure_is_soft_and_direct_command_runs(
    fake_global_config, tmp_path
):
    fake_global_config["branch"] = "main"
    output = tmp_path / "command-ran"
    step = Step(
        label="Traced",
        group="Tracing",
        commands=['printf ran > "$OUTPUT_FILE"'],
    )
    commands = buildkite_step._prepare_commands(
        step,
        variables_to_inject={},
        setup_profile="none",
    )
    script = "mktemp() { return 1; };\n" + "\n".join(commands).replace("$$", "$")

    result = subprocess.run(
        ["/bin/sh", "-e", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "OUTPUT_FILE": str(output)},
    )

    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == "ran"
    assert "tracing setup skipped" in result.stderr


def test_otel_import_failure_leaves_test_environment_untouched(
    fake_global_config, tmp_path
):
    fake_global_config["branch"] = "main"
    output = tmp_path / "command-ran"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_python.chmod(0o755)
    step = Step(
        label="Traced",
        group="Tracing",
        commands=['printf ran > "$OUTPUT_FILE"'],
    )
    commands = buildkite_step._prepare_commands(
        step,
        variables_to_inject={},
        setup_profile="none",
    )
    script = (
        "\n".join(commands).replace("$$", "$")
        + '\ntest "$CI_INFRA_OTEL_READY" = 0'
        + '\ntest "$PYTHONPATH" = original-pythonpath'
        + '\ntest "$PYTEST_ADDOPTS" = original-pytest-options'
    )

    result = subprocess.run(
        ["/bin/sh", "-e", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "OUTPUT_FILE": str(output),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "PYTHONPATH": "original-pythonpath",
            "PYTEST_ADDOPTS": "original-pytest-options",
        },
    )

    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == "ran"
    assert "tracing disabled" in result.stderr


def test_otel_does_not_change_errexit_behavior(fake_global_config, tmp_path):
    fake_global_config["branch"] = "main"
    output = tmp_path / "must-not-run"
    step = Step(
        label="Traced failure",
        group="Tracing",
        commands=['false\nprintf bad > "$OUTPUT_FILE"'],
    )
    commands = buildkite_step._prepare_commands(
        step,
        variables_to_inject={},
        setup_profile="none",
    )
    script = "\n".join(commands).replace("$$", "$")

    result = subprocess.run(
        ["/bin/sh", "-e", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "OUTPUT_FILE": str(output)},
    )

    assert result.returncode == 1
    assert not output.exists()


def test_otel_helper_bundle_runs_under_posix_shell():
    command = buildkite_step._otel_setup_command().replace("$$", "$")
    result = subprocess.run(
        [
            "/bin/sh",
            "-c",
            command
            + " && ci_otel_start 1 dHJ1ZQ=="
            + " && true && ci_otel_finish 0"
            + ' && test -n "$(find "$CI_INFRA_OTEL_SPOOL_DIR" -name spans-\\*.jsonl -size +0c -print -quit)"',
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_otel_trace_is_disabled_for_amd_mirror(fake_global_config):
    fake_global_config["branch"] = "main"
    step = Step(label="AMD mirror", commands=["pytest tests"])

    commands = buildkite_step._prepare_commands(
        step,
        variables_to_inject={},
        setup_profile="amd",
    )

    assert not any("ci_otel" in command for command in commands)


def test_otel_trace_is_disabled_for_pull_requests(fake_global_config):
    fake_global_config["branch"] = "main"
    fake_global_config["pull_request"] = "123"
    step = Step(label="Untrusted", commands=["pytest tests"])

    commands = buildkite_step._prepare_commands(
        step,
        variables_to_inject={},
        setup_profile="none",
    )

    assert not any("ci_otel" in command for command in commands)


def test_otel_trace_is_disabled_for_non_vllm_pipelines(fake_global_config):
    fake_global_config["branch"] = "main"
    fake_global_config["github_repo_name"] = "vllm-project/other"
    step = Step(label="Other repository", commands=["pytest tests"])

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


def test_variable_injection_omits_cache_tags_owned_by_image_build():
    vars_ = buildkite_step._get_variables_to_inject()

    assert "$CACHE_FROM" not in vars_
    assert "$CACHE_TO" not in vars_


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
