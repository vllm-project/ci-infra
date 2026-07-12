import shutil
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest
import yaml


TEMPLATE_PATH = Path(__file__).parents[1] / "test-template-amd.j2"


def _step_by_label(steps, label):
    return next(step for step in steps if step.get("label") == label)


def test_amd_template_routes_native_and_legacy_steps(tmp_path):
    minijinja_cli = shutil.which("minijinja-cli")
    if minijinja_cli is None:
        pytest.skip("minijinja-cli is not installed")

    config_path = tmp_path / "test-amd.yaml"
    config_path.write_text(
        dedent(
            """\
            steps:
              - label: Native GPU
                timeout_in_minutes: 47
                mirror_hardwares: [amdproduction]
                native_ci: true
                agent_pool: mi300_2
                num_gpus: 2
                working_dir: /vllm-workspace/tests
                commands:
                  - pytest -v -s tests/native_gpu.py

              - label: Native CPU only
                timeout_in_minutes: 31
                mirror_hardwares: [amdproduction]
                native_ci: true
                agent_pool: mi300_1
                no_gpu: true
                working_dir: /vllm-workspace/tests
                commands:
                  - pytest -v -s -m cpu_test tests/native_cpu.py

              - label: Legacy GPU
                timeout_in_minutes: 63
                mirror_hardwares: [amdproduction]
                agent_pool: mi325_1
                grade: Blocking
                working_dir: /vllm-workspace/tests
                commands:
                  - pytest -v -s tests/legacy_gpu.py
            """
        )
    )

    result = subprocess.run(
        [
            minijinja_cli,
            str(TEMPLATE_PATH),
            str(config_path),
            "-D",
            "branch=feature",
            "-D",
            "list_file_diff=",
            "-D",
            "run_all=1",
            "-D",
            "nightly=0",
            "-D",
            "torch_nightly=0",
            "-D",
            "mirror_hw=amdproduction",
            "-D",
            "fail_fast=true",
            "-D",
            "vllm_use_precompiled=false",
            "-D",
            "vllm_merge_base_commit=deadbeef",
            "-D",
            "cov_enabled=0",
            "-D",
            "vllm_ci_branch=feature",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    pipeline = yaml.safe_load(result.stdout)
    assert isinstance(pipeline, list)
    assert len(pipeline) == 1
    steps = pipeline[0]["steps"]

    native_gpu = _step_by_label(steps, "mi300_2: Native GPU")
    assert native_gpu["agents"] == {"queue": "amd_mi300_2"}
    assert native_gpu["depends_on"] == "image-build-amd"
    assert native_gpu["command"] == (
        "bash .buildkite/scripts/hardware_ci/run-amd-test.sh"
    )
    assert native_gpu["timeout_in_minutes"] == 47
    assert native_gpu["soft_fail"] is True

    native_env = native_gpu["env"]
    assert "DOCKER_IMAGE_NAME" not in native_env
    assert "DOCKER_BUILDKIT" not in native_env
    assert "VLLM_CI_FALLBACK_IMAGE" not in native_env
    assert native_env["VLLM_CI_BASE_IMAGE"] == (
        "rocm/vllm-dev:ci_base-$BUILDKITE_COMMIT"
    )
    assert native_env["AMD_CI_RUNTIME"] == "native"
    assert native_env["NATIVE_CI"] == "true"
    assert native_env["VLLM_CI_DOCKER_DISABLED"] == "1"
    assert native_env["VLLM_CI_EXPECTED_GPU_COUNT"] == "2"
    assert native_env["VLLM_CI_USE_ARTIFACTS"] == "1"
    assert native_env["VLLM_CI_ARTIFACT_GLOB"] == (
        "artifacts/vllm-rocm-install/vllm-rocm-install.tar.gz"
    )
    assert native_env["VLLM_CI_ARTIFACT_CHECKSUM_GLOB"] == (
        "artifacts/vllm-rocm-install/vllm-rocm-install.tar.gz.sha256"
    )
    assert native_env["VLLM_CI_ARTIFACT_STEP"] == "image-build-amd"
    assert native_env["VLLM_CI_REQUIRE_PERSISTENT_HF_CACHE"] == "1"
    assert native_env["VLLM_CI_WORKSPACE"] == "/vllm-workspace"
    assert native_env["VLLM_CI_REQUIRE_WORKSPACE_MOUNT"] == "1"
    assert native_env["HF_HOME"] == "/home/buildkite-agent/huggingface"
    assert native_env["PYTORCH_ROCM_ARCH"] == ""
    assert native_env["CONTAINER_TIMEOUT_S"] == "2760"
    assert "cd /vllm-workspace/tests" in native_env["VLLM_TEST_COMMANDS"]
    assert "pytest -v -s tests/native_gpu.py" in native_env["VLLM_TEST_COMMANDS"]

    pod_patch = native_gpu["plugins"][0]["kubernetes"]["podSpecPatch"]
    assert pod_patch["automountServiceAccountToken"] is False
    assert pod_patch["securityContext"] == {
        "seccompProfile": {"type": "RuntimeDefault"}
    }
    assert pod_patch["imagePullSecrets"] == [{"name": "docker-config"}]
    assert pod_patch["volumes"] == [
        {
            "name": "devshm",
            "emptyDir": {"medium": "Memory", "sizeLimit": "16Gi"},
        },
        {"name": "vllm-workspace", "emptyDir": {}},
    ]

    container = pod_patch["containers"][0]
    assert container["name"] == "container-0"
    assert container["image"] == "rocm/vllm-dev:ci_base-$BUILDKITE_COMMIT"
    assert container["imagePullPolicy"] == "Always"
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"add": ["IPC_LOCK"]},
    }
    assert container["resources"] == {
        "limits": {"amd.com/gpu": "2"},
        "requests": {"amd.com/gpu": "2"},
    }
    assert container["volumeMounts"] == [
        {"name": "devshm", "mountPath": "/dev/shm"},
        {"name": "vllm-workspace", "mountPath": "/vllm-workspace"},
    ]
    assert {
        "name": "VLLM_CI_EXPECTED_GPU_COUNT",
        "value": "2",
    } in container["env"]
    assert {
        "name": "VLLM_CI_WORKSPACE",
        "value": "/vllm-workspace",
    } in container["env"]
    assert {
        "name": "VLLM_CI_REQUIRE_WORKSPACE_MOUNT",
        "value": "1",
    } in container["env"]

    native_cpu = _step_by_label(steps, "mi300_1: Native CPU only")
    assert native_cpu["timeout_in_minutes"] == 31
    assert native_cpu["soft_fail"] is True
    assert native_cpu["env"]["VLLM_CI_EXPECTED_GPU_COUNT"] == "0"
    cpu_container = native_cpu["plugins"][0]["kubernetes"]["podSpecPatch"][
        "containers"
    ][0]
    assert cpu_container["resources"] == {
        "limits": {"amd.com/gpu": "0"},
        "requests": {"amd.com/gpu": "0"},
    }
    assert {
        "name": "VLLM_CI_EXPECTED_GPU_COUNT",
        "value": "0",
    } in cpu_container["env"]

    legacy = _step_by_label(steps, "mi325_1: Legacy GPU")
    assert legacy["agents"] == {"queue": "amd_mi325_1"}
    assert "plugins" not in legacy
    assert legacy["timeout_in_minutes"] == 63
    # AMD mirrors remain soft-fail even if the source test is blocking.
    assert legacy["soft_fail"] is True
    assert legacy["env"]["DOCKER_BUILDKIT"] == "1"
    assert legacy["env"]["DOCKER_IMAGE_NAME"] == "rocm/vllm-dev:ci_base"
    assert legacy["env"]["VLLM_CI_BASE_IMAGE"] == "rocm/vllm-dev:ci_base"
    assert legacy["env"]["VLLM_CI_FALLBACK_IMAGE"] == ("rocm/vllm-ci:$BUILDKITE_COMMIT")
    assert "AMD_CI_RUNTIME" not in legacy["env"]
    assert "NATIVE_CI" not in legacy["env"]
    assert "VLLM_CI_DOCKER_DISABLED" not in legacy["env"]
