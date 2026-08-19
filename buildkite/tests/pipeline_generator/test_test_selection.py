import base64
import json
import re

import pytest

import buildkite_step
from constants import DeviceType
from pipeline_generator import configure_test_tracing, should_trace_nightly
from step import Step


pytestmark = pytest.mark.usefixtures("fake_global_config")


def test_only_trusted_vllm_main_nightly_traces(fake_global_config):
    config = {
        **fake_global_config,
        "branch": "main",
        "nightly": "1",
        "trace_s3_bucket": "vllm-ci-test-selection",
    }
    assert should_trace_nightly(config)

    for field, value in (
        ("branch", "feature"),
        ("nightly", "0"),
        ("pull_request", "123"),
        ("trace_s3_bucket", None),
    ):
        assert not should_trace_nightly({**config, field: value})


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
    match = re.search(r"--commands-base64 ([A-Za-z0-9+/=]+)", wrapper)
    assert match is not None
    commands = json.loads(base64.b64decode(match.group(1)))
    assert commands == [
        'set -e\nexport FOO=bar\ntest "$FOO" = bar && pytest tests/unit'
    ]
