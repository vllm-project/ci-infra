"""Constants and enums for pipeline generation."""

import enum
from typing import List


class PipelineMode(str, enum.Enum):
    """Pipeline generation mode."""

    CI = "ci"  # Full CI pipeline (default)
    FASTCHECK = "fastcheck"  # Fast pre-merge checks only
    AMD = "amd"  # AMD-only pipeline


class BuildStepKeys:
    """Keys for build step dependencies."""

    MAIN_IMAGE = "image-build"
    CPU_IMAGE = "image-build-cpu"
    AMD_BUILD = "amd-build"
    TORCH_NIGHTLY_IMAGE = "image-build-torch-nightly"
    CU118_IMAGE = "image-build-cu118"


class EnvironmentVariables:
    """Common environment variable names."""

    DOCKER_BUILDKIT = "DOCKER_BUILDKIT"
    VLLM_USAGE_SOURCE = "VLLM_USAGE_SOURCE"
    VLLM_ALLOW_DEPRECATED_BEAM_SEARCH = "VLLM_ALLOW_DEPRECATED_BEAM_SEARCH"
    VLLM_ATTENTION_BACKEND = "VLLM_ATTENTION_BACKEND"
    HF_HOME_VAR = "HF_HOME"
    HF_TOKEN = "HF_TOKEN"
    CODECOV_TOKEN = "CODECOV_TOKEN"
    BUILDKITE_ANALYTICS_TOKEN = "BUILDKITE_ANALYTICS_TOKEN"
    NCCL_CUMEM_HOST_ENABLE = "NCCL_CUMEM_HOST_ENABLE"


class EnvironmentValues:
    """Common environment variable values."""

    VLLM_USAGE_CI_TEST = "ci-test"
    ATTENTION_BACKEND_XFORMERS = "XFORMERS"


# Shell Commands
class ShellCommands:
    """Common shell command snippets."""

    CHECK_NVIDIA_GPU = "(command nvidia-smi || true)"
    CHECK_AMD_GPU = "(command rocm-smi || true)"
    SETUP_DEPRECATED_BEAM_SEARCH = "export VLLM_ALLOW_DEPRECATED_BEAM_SEARCH=1"


# Constants
HF_HOME = "/root/.cache/huggingface"
DEFAULT_WORKING_DIR = "/vllm-workspace/tests"
VLLM_ECR_URL = "public.ecr.aws/q9t5s3a7"
VLLM_ECR_REPO = f"{VLLM_ECR_URL}/vllm-ci-test-repo"
AMD_REPO = "rocm/vllm-ci"


# Test Scripts
class Scripts:
    """Buildkite script paths."""

    # Multi-node
    RUN_MULTI_NODE_TEST = "./.buildkite/scripts/run-multi-node-test.sh"

    # Hardware CI scripts
    RUN_NEURON_TEST = ".buildkite/scripts/hardware_ci/run-neuron-test.sh"
    RUN_AMD_TEST = ".buildkite/scripts/hardware_ci/run-amd-test.sh"
    RUN_INTEL_CPU_TEST = ".buildkite/scripts/hardware_ci/run-cpu-test.sh"
    RUN_INTEL_HPU_TEST = ".buildkite/scripts/hardware_ci/run-intel-hpu-test.sh"
    RUN_INTEL_GPU_TEST = ".buildkite/scripts/hardware_ci/run-intel-gpu-test.sh"
    RUN_TPU_TEST = ".buildkite/scripts/hardware_ci/run-tpu-test.sh"
    RUN_TPU_V1_TEST = ".buildkite/scripts/hardware_ci/run-tpu-v1-test.sh"
    RUN_GH200_TEST = ".buildkite/scripts/hardware_ci/run-gh200-test.sh"
    RUN_CPU_TEST_PPC64LE = ".buildkite/scripts/hardware_ci/run-cpu-test-ppc64le.sh"
    RUN_CPU_TEST_S390X = ".buildkite/scripts/hardware_ci/run-cpu-test-s390x.sh"
    RUN_ASCEND_TEST = ".buildkite/scripts/hardware_ci/run-ascend-test.sh"

    # TPU scripts
    TPU_CLEANUP_DOCKER = "bash .buildkite/scripts/tpu/cleanup_docker.sh"
    TPU_DOCKER_RUN_BM = "bash .buildkite/scripts/tpu/docker_run_bm.sh"

    # Coverage
    UPLOAD_CODECOV = "https://raw.githubusercontent.com/vllm-project/ci-infra/{}/buildkite/scripts/upload_codecov.sh"


# Build Files
class BuildFiles:
    """Dockerfile and build-related paths."""

    DOCKERFILE = "docker/Dockerfile"
    DOCKERFILE_ROCM = "docker/Dockerfile.rocm"


# Configuration Files
class ConfigFiles:
    """Configuration file paths."""

    TEST_PIPELINE_YAML = ".buildkite/test-pipeline.yaml"
    OUTPUT_PIPELINE_YAML = ".buildkite/pipeline.yaml"


# Legacy constants (keep for compatibility)
TEST_PATH = ".buildkite/test-pipeline.yaml"
EXTERNAL_HARDWARE_TEST_PATH = ".buildkite/external-tests.yaml"
PIPELINE_FILE_PATH = ".buildkite/pipeline.yaml"
MULTI_NODE_TEST_SCRIPT = ".buildkite/run-multi-node-test.sh"

TEST_DEFAULT_COMMANDS = [
    ShellCommands.CHECK_NVIDIA_GPU,  # Sanity check for Nvidia GPU setup
    "export VLLM_LOGGING_LEVEL=DEBUG",
    ShellCommands.SETUP_DEPRECATED_BEAM_SEARCH,
]

STEPS_TO_BLOCK: List[str] = []


# ==============================================================================
# JOB LABELS - Centralized constants to avoid magic strings
# ==============================================================================


class TestLabels:
    """Test job labels that require special handling."""

    DOCUMENTATION_BUILD = "Documentation Build"
    BENCHMARKS = "Benchmarks"
    BASIC_CORRECTNESS_TEST = "Basic Correctness Test"
    SPECULATIVE_DECODING_TESTS = "Speculative decoding tests"

    # AMD queue determination labels
    KERNELS_ATTENTION_TEST = "Kernels Attention Test %N"
    LORA_TEST = "LoRA Test %N"
    KERNELS_QUANTIZATION_TEST = "Kernels Quantization Test %N"

    DISTRIBUTED_TESTS_4_GPU = "Distributed Tests (4 GPUs)"
    TWO_NODE_TESTS_4_GPU = "2 Node Tests (4 GPUs in total)"
    MULTI_STEP_TESTS_4_GPU = "Multi-step Tests (4 GPUs)"
    PIPELINE_PARALLELISM_TEST = "Pipeline Parallelism Test"
    LORA_TP_TEST_DISTRIBUTED = "LoRA TP Test (Distributed)"

    DISTRIBUTED_COMM_OPS_TEST = "Distributed Comm Ops Test"
    DISTRIBUTED_TESTS_2_GPU = "Distributed Tests (2 GPUs)"
    PLUGIN_TESTS_2_GPU = "Plugin Tests (2 GPUs)"
    WEIGHT_LOADING_MULTIPLE_GPU_TEST = "Weight Loading Multiple GPU Test"
    WEIGHT_LOADING_MULTIPLE_GPU_TEST_LARGE = "Weight Loading Multiple GPU Test - Large Models"


class HardwareLabels:
    """Hardware test labels."""

    NEURON_TEST = "Neuron Test"
    INTEL_CPU_TEST = "Intel CPU Test"
    INTEL_GPU_TEST = "Intel GPU Test"
    INTEL_HPU_TEST = "Intel HPU Test"
    TPU_V0_TEST = "TPU V0 Test"
    TPU_V0_TEST_NOTIFICATION = "TPU V0 Test Notification"
    TPU_V1_TEST = "TPU V1 Test"
    TPU_V1_TEST_PART2 = "TPU V1 Test Part2"
    TPU_V1_BENCHMARK = "TPU V1 Benchmark Test"
    TPU_V1_TEST_NOTIFICATION = "TPU V1 Test Notification"
    GH200_TEST = "GH200 Test"
    IBM_POWER_TEST = "IBM Power(ppc64le) CPU Test"
    IBM_Z_TEST = "IBM Z (s390x) CPU Test"
    ASCEND_TEST = "Ascend NPU Test"


class AMDQueueLabels:
    """Labels used for AMD queue determination."""

    # 8 GPU tests
    AMD_MI325_8_LABELS = [
        TestLabels.BENCHMARKS,
        TestLabels.KERNELS_ATTENTION_TEST,
        TestLabels.LORA_TEST,
        TestLabels.KERNELS_QUANTIZATION_TEST,
    ]

    # 4 GPU tests
    AMD_MI325_4_LABELS = [
        TestLabels.DISTRIBUTED_TESTS_4_GPU,
        TestLabels.TWO_NODE_TESTS_4_GPU,
        TestLabels.MULTI_STEP_TESTS_4_GPU,
        TestLabels.PIPELINE_PARALLELISM_TEST,
        TestLabels.LORA_TP_TEST_DISTRIBUTED,
    ]

    # 2 GPU tests
    AMD_MI325_2_LABELS = [
        TestLabels.DISTRIBUTED_COMM_OPS_TEST,
        TestLabels.DISTRIBUTED_TESTS_2_GPU,
        TestLabels.PLUGIN_TESTS_2_GPU,
        TestLabels.WEIGHT_LOADING_MULTIPLE_GPU_TEST,
        TestLabels.WEIGHT_LOADING_MULTIPLE_GPU_TEST_LARGE,
    ]


class BlockLabels:
    """Block step labels."""

    RUN_NEURON_TEST = "Run Neuron Test"
    RUN_A100_TESTS = "Run A100 tests"
    RUN_TPU_V0_TEST = "Run TPU V0 Test"
    RUN_TPU_V1_TEST = "Run TPU V1 Test"
    RUN_GH200_TEST = "Run GH200 Test"
    RUN_INTEL_CPU_TEST = "Run Intel CPU test"
    RUN_INTEL_GPU_TEST = "Run Intel GPU test"
    BUILD_TORCH_NIGHTLY_IMAGE = "Build torch nightly image"
    BUILD_CUDA_118_IMAGE = "Build CUDA 11.8 image"


class BuildLabels:
    """Build step labels."""

    BUILD_IMAGE = ":docker: build image"
    BUILD_IMAGE_CPU = ":docker: build image CPU"
    BUILD_IMAGE_CUDA_118 = ":docker: build image CUDA 11.8"
    BUILD_TORCH_NIGHTLY_IMAGE = ":docker: build torch nightly image"
    AMD_BUILD_IMAGE = "AMD: :docker: build image"

    @staticmethod
    def amd_build_with_mirror(mirror_hw: str) -> str:
        """Generate AMD build label with mirror hardware."""
        return f"AMD: :docker: build image with {mirror_hw}"


class GroupLabels:
    """Group labels."""

    AMD_TESTS = "AMD Tests"
    TORCH_NIGHTLY = "Torch Nightly"


class AMDLabelPrefixes:
    """AMD test label prefixes."""

    AMD_MI300 = "AMD MI300"

    @staticmethod
    def with_test(test_name: str) -> str:
        """Generate AMD MI300 test label."""
        return f"AMD MI300: {test_name}"

    @staticmethod
    def with_test_and_mirror(test_name: str, mirror_hw: str) -> str:
        """Generate AMD MI300 test label with mirror."""
        return f"AMD MI300: {test_name} with {mirror_hw}"


# ==============================================================================
# GPU AND AGENT TYPES
# ==============================================================================


class GPUType(str, enum.Enum):
    A100 = "a100"
    H100 = "h100"
    H200 = "h200"
    B200 = "b200"


class AgentQueue(str, enum.Enum):
    AWS_CPU = "cpu_queue"
    AWS_SMALL_CPU = "small_cpu_queue"
    AWS_CPU_PREMERGE = "cpu_queue_premerge_us_east_1"
    AWS_CPU_PREMERGE_SIMPLE = "cpu_queue_premerge"  # For fastcheck
    AWS_CPU_POSTMERGE = "cpu_queue_postmerge_us_east_1"
    AWS_1xL4 = "gpu_1_queue"
    AWS_4xL4 = "gpu_4_queue"
    A100 = "a100_queue"
    H100 = "mithril-h100-pool"
    H200 = "skylab-h200"
    B200 = "B200"
    AMD_GPU = "amd"
    AMD_CPU = "amd-cpu"
    AMD_MI325_1 = "amd_mi325_1"
    AMD_MI325_2 = "amd_mi325_2"
    AMD_MI325_4 = "amd_mi325_4"
    AMD_MI325_8 = "amd_mi325_8"
    NEURON = "neuron"
    INTEL_CPU = "intel-cpu"
    INTEL_GPU = "intel-gpu"
    INTEL_HPU = "intel-hpu"
    TPU = "tpu_v6e_queue"
    GH200 = "gh200_queue"
    IBM_PPC64LE = "ibm-ppc64le"
    IBM_S390X = "ibm_s390x"
    ASCEND = "ascend"
    SMALL_CPU_PREMERGE = "small_cpu_queue_premerge"
