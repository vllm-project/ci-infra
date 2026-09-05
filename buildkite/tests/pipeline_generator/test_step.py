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


def test_selected_steps_support_label_generated_keys():
    # Steps without an explicit key are uploaded with a label-derived key, so
    # retry builds reference them by that generated key.
    steps = [
        Step(label="Image", key="image-build", commands=["build"]),
        Step(
            label=":nvidia: (H200) Rust Frontend OpenAI Coverage",
            depends_on=["image-build"],
            commands=["test"],
        ),
    ]

    selected, selected_keys = select_steps_and_dependencies(
        steps, frozenset({"-nvidia--h200-rust-frontend-openai-coverage"})
    )

    assert [step.key for step in selected] == [
        "image-build",
        "-nvidia--h200-rust-frontend-openai-coverage",
    ]
    assert selected_keys == frozenset(
        {"image-build", "-nvidia--h200-rust-frontend-openai-coverage"}
    )


def test_selected_steps_reject_duplicate_generated_key():
    steps = [
        Step(label="Test", key="test", commands=["a"]),
        Step(label="test", commands=["b"]),
    ]

    with pytest.raises(ValueError, match="Duplicate CI step key: test"):
        select_steps_and_dependencies(steps, frozenset({"test"}))


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

    assert ".buildkite/scripts/ci-otel" in commands[0]
    assert "base64 --decode" not in commands[0]
    assert len(commands[0]) < 500
    assert '. "$$CI_INFRA_OTEL_DIR/ci_otel.sh"' in commands[0]
    assert "ci_otel_start 1 " in commands[2]
    assert "export VALUE=ready" in commands[2]
    assert "ci_otel_finish" in commands[2]
    assert "ci_otel_start 2 " in commands[4]
    assert 'test "$VALUE" = ready' in commands[4]
    match = re.search(r'ci_otel_start 2 ("[^"]*"|\S+)', commands[4])
    assert match is not None
    (quoted_label,) = match.groups()
    assert shlex.split(quoted_label)[0] == "test VALUE = ready"
    assert "'" not in commands[4]


def test_otel_trace_allows_exact_api_treatment_branch(fake_global_config, monkeypatch):
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


def test_otel_trace_uses_ci_otel_run_for_simple_commands(fake_global_config):
    fake_global_config["branch"] = "main"
    step = Step(
        label="Traced",
        group="Tracing",
        commands=["pytest tests", "export VALUE=ready", "python script.py"],
    )

    commands = buildkite_step._prepare_commands(
        step,
        variables_to_inject={},
        setup_profile="none",
    )

    # Simple commands use ci_otel_run
    assert "ci_otel_run 1 " in commands[2]
    assert "pytest tests" in commands[2]
    # Shell builtins use the explicit start/finish pair
    assert "ci_otel_start 2 " in commands[4]
    assert "export VALUE=ready" in commands[4]
    assert "ci_otel_finish" in commands[4]
    # Simple commands use ci_otel_run
    assert "ci_otel_run 3 " in commands[6]
    assert "python script.py" in commands[6]


def test_otel_trace_keeps_assignment_prefixed_commands_in_shell(fake_global_config):
    # Leading POSIX assignment words must run in the shell so the variables
    # apply as the job author wrote them; env would scope them to the child.
    fake_global_config["branch"] = "main"
    step = Step(
        label="Traced",
        group="Tracing",
        commands=[
            "TP_SIZE=1 pytest tests/entrypoints",
            "TP_SIZE=1 DP_SIZE=2 pytest tests/distributed",
        ],
    )

    commands = buildkite_step._prepare_commands(
        step,
        variables_to_inject={},
        setup_profile="none",
    )

    for index, command_index in ((1, 2), (2, 4)):
        command = commands[command_index]
        assert f"ci_otel_start {index} " in command
        assert "ci_otel_finish" in command
        assert "ci_otel_run" not in command
    assert "TP_SIZE=1 pytest tests/entrypoints" in commands[2]
    assert "TP_SIZE=1 DP_SIZE=2 pytest tests/distributed" in commands[4]


def test_is_simple_command():
    assert buildkite_step._is_simple_command("pytest tests")
    assert buildkite_step._is_simple_command("python script.py")
    assert buildkite_step._is_simple_command("docker build .")
    assert not buildkite_step._is_simple_command("export FOO=bar")
    assert not buildkite_step._is_simple_command("cd /tmp")
    assert not buildkite_step._is_simple_command("source env.sh")
    assert not buildkite_step._is_simple_command(". env.sh")
    assert not buildkite_step._is_simple_command("pytest tests | grep pass")
    assert not buildkite_step._is_simple_command("pytest tests > out.txt")
    assert not buildkite_step._is_simple_command("pytest tests && echo done")
    assert not buildkite_step._is_simple_command("echo $HOME")
    assert not buildkite_step._is_simple_command("echo `date`")
    # Leading POSIX assignment words (single and multiple) stay in the shell
    assert not buildkite_step._is_simple_command("FOO=bar pytest tests")
    assert not buildkite_step._is_simple_command("FOO=bar BAZ=qux pytest tests")
    assert not buildkite_step._is_simple_command("FOO=bar")
    # Assignments after the program name are plain arguments, not assignments
    assert buildkite_step._is_simple_command("pytest tests FOO=bar")


def test_otel_trace_rejects_non_api_treatment_branch(fake_global_config, monkeypatch):
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


def test_otel_setup_has_no_unescaped_dollar_for_pipeline_interpolation():
    # Buildkite interpolates $VAR / ${VAR} when uploading pipeline.yaml; a bare
    # $@ or $? is a hard parse error that fails the whole pipeline upload.
    # Every literal $ in the generated setup must be escaped as $$.
    setup = buildkite_step._otel_setup_command()
    stripped = setup.replace("$$", "")
    assert not re.search(r"\$(?![A-Za-z_{])", stripped)


def test_otel_setup_sources_repo_helpers(tmp_path):
    helper = tmp_path / "ci_otel.sh"
    helper.write_text(
        "ci_otel_start() { return 11; }; ci_otel_finish() { return 12; }; :\n",
        encoding="utf-8",
    )
    command = buildkite_step._otel_setup_command().replace("$$", "$")
    result = subprocess.run(
        [
            "bash",
            "-c",
            command
            + ' && test "$PYTHONPATH" = original-pythonpath'
            + ' && test "$PYTEST_ADDOPTS" = original-pytest-options'
            + " && ci_otel_start; test $? = 11"
            + " && ci_otel_finish; test $? = 12",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CI_INFRA_OTEL_DIR": str(tmp_path),
            "PYTHONPATH": "original-pythonpath",
            "PYTEST_ADDOPTS": "original-pytest-options",
        },
    )

    assert result.returncode == 0, result.stderr


def test_otel_setup_stays_fail_open_without_git_checkout(tmp_path):
    # When CI_INFRA_OTEL_DIR is unset and the working directory is not a git
    # checkout (no .git baked into the image), the setup command must still
    # succeed under `sh -e` so the job runs untraced instead of failing.
    command = buildkite_step._otel_setup_command().replace("$$", "$")
    env = {
        key: value for key, value in os.environ.items() if key != "CI_INFRA_OTEL_DIR"
    }
    result = subprocess.run(
        ["/bin/sh", "-e", "-c", command + "\necho setup-survived"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "setup-survived" in result.stdout
    assert "tracing setup skipped" in result.stderr


def test_noop_ci_otel_run_handles_assignment_prefixed_commands(tmp_path):
    # With the helper directory missing, the generated setup leaves the no-op
    # ci_otel_run in place. It must route through env so assignment-prefixed
    # commands (FOO=bar cmd) do not try to execute the assignment as a program.
    command = buildkite_step._otel_setup_command().replace("$$", "$")
    script = command + "\nci_otel_run 1 label FOO=bar env"
    result = subprocess.run(
        ["/bin/sh", "-e", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "CI_INFRA_OTEL_DIR": str(tmp_path / "missing")},
    )

    assert result.returncode == 0, result.stderr
    assert "FOO=bar" in result.stdout.splitlines()
    assert "tracing setup skipped" in result.stderr


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
    script = "\n".join(commands).replace("$$", "$")

    result = subprocess.run(
        ["/bin/sh", "-e", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CI_INFRA_OTEL_DIR": str(tmp_path / "missing"),
            "OUTPUT_FILE": str(output),
        },
    )

    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == "ran"
    assert "tracing setup skipped" in result.stderr


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
        env={
            **os.environ,
            "CI_INFRA_OTEL_DIR": str(tmp_path / "missing"),
            "OUTPUT_FILE": str(output),
        },
    )

    assert result.returncode == 1
    assert not output.exists()


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


def _ascend_test_step():
    return Step(
        label="Ascend NPU Test",
        group="Hardware",
        key="ascend-npu-test",
        device="ascend_npu",
        no_plugin=True,
        commands=["bash .buildkite/scripts/hardware_ci/run-npu-test.sh"],
    )


def test_ascend_tests_disabled_by_default(monkeypatch):
    monkeypatch.delenv(buildkite_step.ENABLE_ASCEND_TESTS_ENV_VAR, raising=False)
    step = _ascend_test_step()

    group_steps = buildkite_step.convert_group_step_to_buildkite_step(
        {step.group: [step]}
    )

    command_steps = [
        command_step
        for group_step in group_steps
        for command_step in group_step.steps
        if isinstance(command_step, buildkite_step.BuildkiteCommandStep)
    ]
    assert command_steps == []


def test_enable_ascend_tests_env_var_restores_ascend_steps(monkeypatch):
    monkeypatch.setenv(buildkite_step.ENABLE_ASCEND_TESTS_ENV_VAR, "1")
    step = _ascend_test_step()

    group_steps = buildkite_step.convert_group_step_to_buildkite_step(
        {step.group: [step]}
    )

    command_steps = [
        command_step
        for group_step in group_steps
        for command_step in group_step.steps
        if isinstance(command_step, buildkite_step.BuildkiteCommandStep)
    ]
    assert [command_step.label for command_step in command_steps] == [
        "Ascend NPU Test"
    ]


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
