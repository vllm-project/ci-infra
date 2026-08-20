import base64
import json
import re
import subprocess

import pytest
import yaml

import buildkite_step
from buildkite_step import convert_group_step_to_buildkite_step
from constants import DeviceType
import pipeline_generator as pipeline_module
from pipeline_generator import (
    PipelineGenerator,
    REPUBLISH_INVENTORY_ENV,
    REPUBLISH_SOURCE_BUILD_ENV,
    REPUBLISH_SOURCE_BUILD_ID_ENV,
    REPUBLISH_TRIALS_ENV,
    configure_test_tracing,
    create_snapshot_republish_group_step,
    create_snapshot_group_step,
    finalize_trace_inventory,
    select_steps_and_dependencies,
    should_trace_nightly,
)
from step import Step, group_steps


pytestmark = pytest.mark.usefixtures("fake_global_config")


def _republish_config(fake_global_config):
    return {
        **fake_global_config,
        "branch": "main",
        "commit": "a" * 40,
        "nightly": "1",
        "trace_s3_bucket": "vllm-ci-test-selection",
        "trace_s3_prefix": "test-selection/vllm",
    }


def _set_republish_env(monkeypatch):
    inventory = {
        "always_run": [{"key": "plain-job", "reason": "policy"}],
        "ci_infra_revision": "b" * 40,
        "collector_sha256": "c" * 64,
        "jobs": [
            {
                "expected_shards": 1,
                "key": "traced-job",
                "mode": "python-only",
            }
        ],
        "repository_sha": "a" * 40,
        "schema_version": 1,
        "wait_results": {
            "traced-job": {
                "outcome": "passed",
                "state": "finished",
                "status": "terminal",
            }
        },
    }
    raw = (json.dumps(inventory, sort_keys=True, separators=(",", ":")) + "\n").encode()
    monkeypatch.setenv(REPUBLISH_INVENTORY_ENV, base64.b64encode(raw).decode())
    monkeypatch.setenv(REPUBLISH_SOURCE_BUILD_ENV, "84585")
    monkeypatch.setenv(
        REPUBLISH_SOURCE_BUILD_ID_ENV,
        "01a0199a-ca8a-44c4-89cc-f387790befd5",
    )
    monkeypatch.setenv(
        REPUBLISH_TRIALS_ENV,
        json.dumps(
            [
                {"head": "d" * 40, "pull_request": 50227},
                {"head": "e" * 40, "pull_request": 48939},
            ]
        ),
    )
    return raw


def test_only_trusted_vllm_main_nightly_traces(fake_global_config):
    config = {
        **fake_global_config,
        "branch": "main",
        "nightly": "1",
        "trace_s3_bucket": "vllm-ci-test-selection",
    }
    assert should_trace_nightly(config)
    assert should_trace_nightly(
        {
            **config,
            "branch": "ci-tsel-main-mirror",
            "trace_canary_branch": "ci-tsel-main-mirror",
            "trace_canary_commit": "a" * 40,
        }
    )

    for field, value in (
        ("branch", "feature"),
        ("branch", None),
        ("nightly", "0"),
        ("pull_request", "123"),
        ("trace_s3_bucket", None),
    ):
        assert not should_trace_nightly({**config, field: value})


def test_targeted_nightly_inventory_matches_dependency_closure():
    steps = [
        Step(label="Image", key="image-build", commands=["bash image.sh"]),
        Step(
            label="First",
            key="first-tests",
            commands=["pytest tests/first"],
            depends_on=["image-build"],
        ),
        Step(
            label="Second",
            key="second-tests",
            commands=["pytest tests/second"],
            depends_on=["image-build"],
        ),
        Step(label="Other", key="other-tests", commands=["pytest tests/other"]),
    ]
    selected, selected_keys = select_steps_and_dependencies(
        steps, frozenset({"first-tests", "second-tests"})
    )
    selected_test_area_keys = {
        "first-tests",
        "second-tests",
        "other-tests",
    } & set(selected_keys or ())

    selected, inventory = configure_test_tracing(
        selected,
        selected_test_area_keys,
        "a" * 40,
        "b" * 40,
        "c" * 64,
    )
    rendered = convert_group_step_to_buildkite_step(group_steps(selected))
    finalize_trace_inventory(inventory, rendered)

    assert selected_keys == frozenset({"first-tests", "second-tests", "image-build"})
    assert [row["key"] for row in inventory["jobs"]] == [
        "first-tests",
        "second-tests",
    ]
    assert inventory["always_run"] == [
        {"key": "image-build", "reason": "rendered_uninstrumented_step"}
    ]


def test_mirror_branch_snapshot_has_loud_canary_identity(fake_global_config):
    inventory = {
        "ci_infra_revision": "b" * 40,
        "jobs": [{"key": "tests"}],
    }
    config = {
        **fake_global_config,
        "trace_canary_branch": "ci-tsel-main-mirror",
        "trace_canary_commit": "a" * 40,
        "trace_s3_bucket": "vllm-ci-test-selection",
        "trace_s3_prefix": "test-selection/vllm/canary/retry",
    }

    group = create_snapshot_group_step(inventory, config)

    identity = "ci-tsel-main-mirror@" + "a" * 40
    assert identity in group.group
    assert identity in group.steps[0].label
    assert "TEST-SELECTION CANARY authorized" in group.steps[0].commands[0]


def test_republish_renders_exactly_one_pinned_step(
    fake_global_config, monkeypatch, tmp_path
):
    config = _republish_config(fake_global_config)
    raw = _set_republish_env(monkeypatch)
    monkeypatch.setattr(pipeline_module, "_ci_infra_revision", lambda: "f" * 40)
    monkeypatch.setattr(pipeline_module, "get_global_config", lambda: config)
    output = tmp_path / "pipeline.yaml"
    generator = PipelineGenerator.__new__(PipelineGenerator)
    generator.output_file_path = str(output)

    generator.generate()

    document = yaml.safe_load(output.read_text())
    assert len(document["steps"]) == 1
    group = document["steps"][0]
    assert group["group"] == "Test selection snapshot republish"
    assert len(group["steps"]) == 1
    step = group["steps"][0]
    assert step["agents"] == {"queue": "cpu_queue_postmerge_us_east_1"}
    assert step["timeout_in_minutes"] == 180
    command = step["commands"][0]
    assert "wait-for-steps" not in command
    assert "--build 01a0199a-ca8a-44c4-89cc-f387790befd5" in command
    assert "--build 84585" not in command
    assert "source-build.txt" in command
    assert "source-build-id.txt" in command
    assert "@" + "f" * 40 in command
    assert base64.b64encode(raw).decode() in command
    assert "refs/pull/50227/head" in command
    assert "refs/pull/48939/head" in command
    subprocess.run(
        ["bash", "-n"],
        input=command.replace("$$", "$"),
        check=True,
        text=True,
    )


def test_mirror_branch_republish_has_loud_canary_identity(
    fake_global_config, monkeypatch
):
    _set_republish_env(monkeypatch)
    config = {
        **_republish_config(fake_global_config),
        "branch": "ci-tsel-main-mirror",
        "trace_canary_branch": "ci-tsel-main-mirror",
        "trace_canary_commit": "a" * 40,
    }
    monkeypatch.setattr(pipeline_module, "_ci_infra_revision", lambda: "f" * 40)

    group = create_snapshot_republish_group_step(config)

    identity = "ci-tsel-main-mirror@" + "a" * 40
    assert identity in group.group
    assert identity in group.steps[0].label
    assert "SNAPSHOT REPUBLISH CANARY authorized" in group.steps[0].commands[0]


def test_republish_rejects_malformed_or_untrusted_inputs(
    fake_global_config, monkeypatch
):
    config = _republish_config(fake_global_config)
    _set_republish_env(monkeypatch)
    monkeypatch.setenv(REPUBLISH_INVENTORY_ENV, "not-base64")
    with pytest.raises(ValueError, match="INVENTORY.*invalid"):
        create_snapshot_republish_group_step(config)

    _set_republish_env(monkeypatch)
    with pytest.raises(ValueError, match="trusted vLLM nightly or"):
        create_snapshot_republish_group_step({**config, "branch": "feature"})

    _set_republish_env(monkeypatch)
    monkeypatch.setenv(REPUBLISH_SOURCE_BUILD_ID_ENV, "84585")
    with pytest.raises(ValueError, match="SOURCE_BUILD_ID.*build UUID"):
        create_snapshot_republish_group_step(config)


def test_republish_rejects_incomplete_wait_accounting(fake_global_config, monkeypatch):
    config = _republish_config(fake_global_config)
    raw = _set_republish_env(monkeypatch)
    inventory = json.loads(raw)
    inventory["wait_results"] = {}
    invalid = (
        json.dumps(inventory, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    monkeypatch.setenv(REPUBLISH_INVENTORY_ENV, base64.b64encode(invalid).decode())

    with pytest.raises(ValueError, match="wait results are incomplete"):
        create_snapshot_republish_group_step(config)


def test_test_area_pytest_jobs_are_enrolled_without_yaml_trace_policy():
    steps = [
        Step(
            label="CPU",
            key="cpu-tests",
            commands=["export FOO=bar", "pytest tests/unit"],
        ),
        Step(
            label="GPU",
            key="gpu-tests",
            commands=["pytest tests/kernels"],
            device=DeviceType.H100,
        ),
        Step(label="Script", key="script", commands=["bash smoke.sh"]),
    ]

    _steps, inventory = configure_test_tracing(
        steps,
        {"cpu-tests", "gpu-tests", "script"},
        "a" * 40,
        "b" * 40,
        "c" * 64,
    )

    assert [(row["key"], row["mode"]) for row in inventory["jobs"]] == [
        ("cpu-tests", "python-only"),
        ("gpu-tests", "kernel-set"),
    ]
    assert inventory["always_run"] == [
        {"key": "script", "reason": "not_plain_pytest_commands"}
    ]
    assert steps[0].trace_represented_job_key == "cpu-tests"
    assert steps[1].trace_gpu is True


def test_automatic_nightly_preserves_evidence_based_fleet_carve_outs():
    steps = [
        Step(
            label="Previously collector incompatible",
            key="kernels-fp8-moe-test-2xh100",
            commands=["pytest tests/kernels"],
            device=DeviceType.H100,
        ),
        Step(
            label="Automatically enrolled forked-CUDA incompatible",
            key="multi-modal-models-extended-generation-2",
            commands=["pytest tests/models"],
            device=DeviceType.H200,
        ),
        Step(
            label="Coverage overhead",
            key="engine-1-gpu",
            commands=["pytest tests/engine"],
            device=DeviceType.H100,
        ),
        Step(
            label="Traceable",
            key="gpu-tests",
            commands=["pytest tests/kernels"],
            device=DeviceType.H100,
        ),
    ]

    _steps, inventory = configure_test_tracing(
        steps,
        {step.key for step in steps},
        "a" * 40,
        "b" * 40,
        "c" * 64,
    )

    assert inventory["jobs"] == [
        {"expected_shards": 1, "key": "gpu-tests", "mode": "kernel-set"}
    ]
    assert inventory["always_run"] == [
        {"key": "engine-1-gpu", "reason": "cpu_overhead_policy"},
        {
            "key": "kernels-fp8-moe-test-2xh100",
            "reason": "collector_compatibility_policy",
        },
        {
            "key": "multi-modal-models-extended-generation-2",
            "reason": "collector_compatibility_policy",
        },
    ]
    assert steps[0].trace_represented_job_key is None
    assert steps[1].trace_represented_job_key is None


def test_trace_wrapper_preserves_the_original_command_list_as_one_script(
    fake_global_config,
):
    step = Step(
        label="Tests",
        key="tests",
        commands=["export FOO=bar", 'test "$FOO" = bar && pytest tests/unit'],
        trace_represented_job_key="tests",
        trace_collector_sha256="c" * 64,
    )

    rendered = buildkite_step._prepare_commands(step, {})
    wrapper = rendered[-1]
    assert "trace-output/tests/attempt-$${BUILDKITE_RETRY_COUNT:-0}" in wrapper
    match = re.search(r"--commands-base64 ([A-Za-z0-9+/=]+)", wrapper)
    assert match is not None
    commands = json.loads(base64.b64decode(match.group(1)))
    assert commands == [
        'set -e\nexport FOO=bar\ntest "$FOO" = bar && pytest tests/unit'
    ]
