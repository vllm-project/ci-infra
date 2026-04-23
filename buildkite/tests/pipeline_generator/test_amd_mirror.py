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
    assert "nvidia-smi" not in mirrored_commands
    assert "CUDA_ENABLE_COREDUMP_ON_EXCEPTION" not in mirrored_commands
