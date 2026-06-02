from pathlib import Path
import sys


PIPELINE_GENERATOR_DIR = Path(__file__).resolve().parents[2] / "pipeline_generator"

if str(PIPELINE_GENERATOR_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_GENERATOR_DIR))

from constants import AgentQueue
import buildkite_step as buildkite_step_module
from step import Step


def _make_global_config(diff_files, run_all=False, nightly="0"):
    return {
        "name": "vllm_ci",
        "registries": "public.ecr.aws/q9t5s3a7",
        "repositories": {
            "main": "vllm-ci-postmerge-repo",
            "premerge": "vllm-ci-test-repo",
        },
        "branch": "feature/test-amd",
        "list_file_diff": diff_files,
        "nightly": nightly,
        "run_all": run_all,
    }


def _make_multimodal_step():
    return Step(
        label="Multi-Modal Models (Standard) 3: llava + qwen2_vl",
        working_dir="/vllm-workspace/tests",
        commands=[
            "pip install git+https://github.com/TIGER-AI-Lab/Mantis.git",
            'pytest -v -s models/multimodal/generation/test_common.py -m core_model -k "not qwen2 and not qwen3 and not gemma"',
            "pytest -v -s models/multimodal/generation/test_qwen2_vl.py -m core_model",
        ],
        source_file_dependencies=["vllm/", "tests/models/multimodal"],
        mirror={"amd": {"device": "mi300_1"}},
        parallelism=2,
    )


def _patch_buildkite_module(monkeypatch, diff_files, run_all=False, nightly="0"):
    monkeypatch.setattr(
        buildkite_step_module,
        "get_global_config",
        lambda: _make_global_config(diff_files, run_all=run_all, nightly=nightly),
    )
    monkeypatch.setattr(
        buildkite_step_module,
        "get_ecr_cache_registry",
        lambda: ("cache-from", "cache-to"),
    )
    monkeypatch.setattr(
        buildkite_step_module,
        "get_image",
        lambda *args, **kwargs: "test-image",
    )
    monkeypatch.setattr(
        buildkite_step_module,
        "get_torch_nightly_image",
        lambda: "torch-nightly-image",
    )


def _get_amd_group(groups):
    return next(group for group in groups if group.group == "Hardware-AMD Tests")


def test_prepare_commands_uses_explicit_setup_profiles():
    step = Step(
        label="Profile Check",
        working_dir="/vllm-workspace/tests",
        commands=["pytest -q tests/test_profiles.py"],
    )

    nvidia_commands = buildkite_step_module._prepare_commands(
        step,
        {},
        setup_profile="nvidia",
    )
    amd_commands = buildkite_step_module._prepare_commands(
        step,
        {},
        setup_profile="amd",
    )
    no_setup_commands = buildkite_step_module._prepare_commands(
        step,
        {},
        setup_profile="none",
    )

    assert nvidia_commands[:4] == [
        "cd /vllm-workspace/tests",
        'echo "--- :nvidia: GPU Info"',
        "(command nvidia-smi || true)",
        'echo "--- :gear: CUDA Coredump Setup"',
    ]
    assert any("CUDA_ENABLE_COREDUMP_ON_EXCEPTION" in command for command in nvidia_commands)

    assert amd_commands[:3] == [
        "cd /vllm-workspace/tests",
        'echo "--- :amd: GPU Info"',
        "(command amd-smi || true)",
    ]
    assert not any("CUDA_ENABLE_COREDUMP_ON_EXCEPTION" in command for command in amd_commands)
    assert not any("nvidia-smi" in command for command in amd_commands)

    assert no_setup_commands == [
        "cd /vllm-workspace/tests",
        'echo "+++ :test_tube: Command (1/1): pytest -q tests/test_profiles.py"',
        "pytest -q tests/test_profiles.py",
    ]


def test_create_amd_mirror_step_uses_rocm_safe_command_wrapper():
    step = _make_multimodal_step()

    amd_step = buildkite_step_module._create_amd_mirror_step(
        step,
        {"$BUILDKITE_COMMIT": "$$BUILDKITE_COMMIT"},
        {"device": "mi300_1"},
    )

    mirrored_commands = amd_step.env["VLLM_TEST_COMMANDS"]

    assert amd_step.label == "AMD: Multi-Modal Models (Standard) 3: llava + qwen2_vl (mi300_1)"
    assert amd_step.commands == ["bash .buildkite/scripts/hardware_ci/run-amd-test.sh"]
    assert amd_step.depends_on == ["image-build-amd"]
    assert amd_step.agents["queue"] == AgentQueue.AMD_MI300_1
    assert amd_step.parallelism == 2
    assert amd_step.priority == 200
    assert amd_step.env["DOCKER_BUILDKIT"] == "1"

    assert mirrored_commands.startswith("cd /vllm-workspace/tests &&")
    assert "VLLM_TEST_COMMANDS" not in mirrored_commands
    assert "amd-smi" in mirrored_commands
    assert "nvidia-smi" not in mirrored_commands
    assert "CUDA_ENABLE_COREDUMP_ON_EXCEPTION" not in mirrored_commands
    assert "CUDA_COREDUMP_SHOW_PROGRESS" not in mirrored_commands
    assert "CUDA_COREDUMP_GENERATION_FLAGS" not in mirrored_commands
    assert "skip_nonrelocated_elf_images" not in mirrored_commands
    assert 'pytest -v -s models/multimodal/generation/test_common.py -m core_model -k "not qwen2 and not qwen3 and not gemma"' in mirrored_commands
    assert "pytest -v -s models/multimodal/generation/test_qwen2_vl.py -m core_model" in mirrored_commands
    assert 'echo "+++ :test_tube: Command (2/3): pytest -v -s models/multimodal/generation/test_common.py -m core_model -k not q"' in mirrored_commands


def test_convert_group_step_to_buildkite_step_gates_amd_mirror_by_prefix_match(
    monkeypatch,
):
    step = _make_multimodal_step()

    monkeypatch.setattr(
        buildkite_step_module,
        "get_global_config",
        lambda: _make_global_config(["vllm/v1/worker/gpu_worker.py"]),
    )
    monkeypatch.setattr(
        buildkite_step_module,
        "get_ecr_cache_registry",
        lambda: ("cache-from", "cache-to"),
    )
    monkeypatch.setattr(
        buildkite_step_module,
        "get_image",
        lambda *args, **kwargs: "test-image",
    )
    monkeypatch.setattr(
        buildkite_step_module,
        "get_torch_nightly_image",
        lambda: "torch-nightly-image",
    )

    groups = buildkite_step_module.convert_group_step_to_buildkite_step(
        {"Models - Multimodal": [step]}
    )
    amd_group = next(group for group in groups if group.group == "Hardware-AMD Tests")

    assert len(amd_group.steps) == 1
    amd_step = amd_group.steps[0]
    assert amd_step.label.startswith("AMD: Multi-Modal Models")
    assert amd_step.depends_on == ["image-build-amd"]

    monkeypatch.setattr(
        buildkite_step_module,
        "get_global_config",
        lambda: _make_global_config(["notvllm/models_multimodal.py"]),
    )

    blocked_groups = buildkite_step_module.convert_group_step_to_buildkite_step(
        {"Models - Multimodal": [step]}
    )
    blocked_amd_group = next(
        group for group in blocked_groups if group.group == "Hardware-AMD Tests"
    )

    assert len(blocked_amd_group.steps) == 2
    block_step, blocked_amd_step = blocked_amd_group.steps
    assert block_step.block == "Run AMD: Multi-Modal Models (Standard) 3: llava + qwen2_vl"
    assert block_step.depends_on == ["image-build-amd"]
    assert blocked_amd_step.depends_on == [
        "image-build-amd",
        "block-amd-multi-modal-models-standard-3--llava---qwen2_vl",
    ]


def test_convert_group_step_to_buildkite_step_runs_amd_mirror_for_rocm_dependency(
    monkeypatch,
):
    step = _make_multimodal_step()
    _patch_buildkite_module(
        monkeypatch, [".buildkite/scripts/hardware_ci/run-amd-test.sh"]
    )

    groups = buildkite_step_module.convert_group_step_to_buildkite_step(
        {"Models - Multimodal": [step]}
    )
    amd_group = _get_amd_group(groups)

    assert len(amd_group.steps) == 1
    assert amd_group.steps[0].label.startswith("AMD: Multi-Modal Models")
    assert amd_group.steps[0].depends_on == ["image-build-amd"]


def test_convert_group_step_to_buildkite_step_runs_all_amd_mirrors_for_rocm_dependency(
    monkeypatch,
):
    first_step = _make_multimodal_step().model_copy(
        update={"label": "AMD Mirror A"}
    )
    optional_step = _make_multimodal_step().model_copy(
        update={"label": "AMD Mirror B", "optional": True}
    )
    _patch_buildkite_module(
        monkeypatch, [".buildkite/scripts/hardware_ci/run-amd-test.sh"]
    )

    groups = buildkite_step_module.convert_group_step_to_buildkite_step(
        {"Models - Multimodal": [first_step, optional_step]}
    )
    amd_group = _get_amd_group(groups)

    assert [step.label for step in amd_group.steps] == [
        "AMD: AMD Mirror A (mi300_1)",
        "AMD: AMD Mirror B (mi300_1)",
    ]
    assert [step.depends_on for step in amd_group.steps] == [
        ["image-build-amd"],
        ["image-build-amd"],
    ]


def test_convert_group_step_to_buildkite_step_runs_amd_mirror_for_amd_specific_dependency(
    monkeypatch,
):
    step = _make_multimodal_step().model_copy(
        update={
            "source_file_dependencies": ["tests/models/multimodal"],
            "mirror": {
                "amd": {
                    "device": "mi300_1",
                    "source_file_dependencies": ["csrc/quantization/"],
                }
            },
        }
    )
    _patch_buildkite_module(monkeypatch, ["csrc/quantization/fp8/foo.cu"])

    groups = buildkite_step_module.convert_group_step_to_buildkite_step(
        {"Models - Multimodal": [step]}
    )
    amd_group = _get_amd_group(groups)

    assert len(amd_group.steps) == 1
    assert amd_group.steps[0].label.startswith("AMD: Multi-Modal Models")
    assert amd_group.steps[0].depends_on == ["image-build-amd"]


def test_amd_source_file_dependencies_deduplicates_preserving_order():
    source_file_dependencies = buildkite_step_module._amd_source_file_dependencies(
        {
            "source_file_dependencies": [
                "vllm/platforms/rocm.py",
                "vllm/platforms/rocm.py",
                "csrc/rocm",
                "custom/path",
                "custom/path/",
            ],
        }
    )

    assert source_file_dependencies.count("vllm/platforms/rocm.py") == 1
    assert source_file_dependencies.count("csrc/rocm/") == 1
    assert "csrc/rocm" not in source_file_dependencies
    assert source_file_dependencies[-1] == "custom/path"
    assert "custom/path/" not in source_file_dependencies


def test_convert_group_step_to_buildkite_step_keeps_optional_amd_mirror_blocked(
    monkeypatch,
):
    step = _make_multimodal_step().model_copy(update={"optional": True})
    _patch_buildkite_module(monkeypatch, ["vllm/v1/worker/gpu_worker.py"])

    groups = buildkite_step_module.convert_group_step_to_buildkite_step(
        {"Models - Multimodal": [step]}
    )
    amd_group = _get_amd_group(groups)

    assert len(amd_group.steps) == 2
    block_step, blocked_amd_step = amd_group.steps
    assert block_step.block == "Run AMD: Multi-Modal Models (Standard) 3: llava + qwen2_vl"
    assert block_step.depends_on == ["image-build-amd"]
    assert blocked_amd_step.depends_on == [
        "image-build-amd",
        "block-amd-multi-modal-models-standard-3--llava---qwen2_vl",
    ]


def test_create_amd_mirror_step_respects_custom_amd_commands_and_working_dir():
    step = _make_multimodal_step()

    amd_step = buildkite_step_module._create_amd_mirror_step(
        step,
        {},
        {
            "device": "mi325_1",
            "working_dir": "/vllm-workspace/examples",
            "commands": [
                "python3 offline_inference/vision_language.py --model-type qwen2_5_vl"
            ],
        },
    )

    mirrored_commands = amd_step.env["VLLM_TEST_COMMANDS"]
    assert amd_step.agents["queue"] == AgentQueue.AMD_MI325_1
    assert mirrored_commands.startswith("cd /vllm-workspace/examples &&")
    assert "offline_inference/vision_language.py --model-type qwen2_5_vl" in mirrored_commands
    assert "test_qwen2_vl.py" not in mirrored_commands
    assert "amd-smi" in mirrored_commands
    assert "nvidia-smi" not in mirrored_commands
    assert "CUDA_ENABLE_COREDUMP_ON_EXCEPTION" not in mirrored_commands
