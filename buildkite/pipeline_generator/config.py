"""Configuration and constants for pipeline generation."""

import enum
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ==============================================================================
# ENUMS AND MODES
# ==============================================================================


class PipelineMode(str, enum.Enum):
    """Pipeline generation mode."""
    CI = "ci"
    FASTCHECK = "fastcheck"
    AMD = "amd"


class GPUType(str, enum.Enum):
    """GPU types."""
    A100 = "a100"
    H100 = "h100"
    H200 = "h200"
    B200 = "b200"


# ==============================================================================
# CONFIGURATION CLASS
# ==============================================================================


class PipelineGeneratorConfig:
    """Configuration for the pipeline generator."""

    def __init__(
        self,
        container_registry: str,
        container_registry_repo: str,
        commit: str,
        branch: str,
        list_file_diff: list,
        run_all: bool = False,
        nightly: bool = False,
        mirror_hw: str = "amdexperimental",
        fail_fast: bool = False,
        vllm_use_precompiled: str = "0",
        cov_enabled: bool = False,
        vllm_ci_branch: str = "main",
        pipeline_mode: PipelineMode = PipelineMode.CI,
    ):
        self.run_all = run_all
        self.nightly = nightly
        self.list_file_diff = list_file_diff
        self.container_registry = container_registry
        self.container_registry_repo = container_registry_repo
        self.commit = commit
        self.branch = branch
        self.mirror_hw = mirror_hw
        self.fail_fast = fail_fast
        self.vllm_use_precompiled = vllm_use_precompiled
        self.cov_enabled = cov_enabled
        self.vllm_ci_branch = vllm_ci_branch
        self.pipeline_mode = pipeline_mode

    def _get_repo_suffix(self) -> str:
        """Get repository suffix based on branch (postmerge for main, test otherwise)."""
        return "postmerge" if self.branch == "main" else "test"

    @property
    def container_image(self):
        """Get the main CUDA container image."""
        if self.pipeline_mode in [PipelineMode.FASTCHECK, PipelineMode.AMD]:
            return "public.ecr.aws/q9t5s3a7/vllm-ci-test-repo:$BUILDKITE_COMMIT"
        return f"public.ecr.aws/q9t5s3a7/vllm-ci-{self._get_repo_suffix()}-repo:$BUILDKITE_COMMIT"

    @property
    def container_image_torch_nightly(self):
        """Get the torch nightly container image."""
        return f"public.ecr.aws/q9t5s3a7/vllm-ci-{self._get_repo_suffix()}-repo:$BUILDKITE_COMMIT-torch-nightly"

    @property
    def container_image_cu118(self):
        """Get the CUDA 11.8 container image."""
        return f"public.ecr.aws/q9t5s3a7/vllm-ci-{self._get_repo_suffix()}-repo:$BUILDKITE_COMMIT-cu118"

    @property
    def container_image_cpu(self):
        """Get the CPU container image."""
        return f"public.ecr.aws/q9t5s3a7/vllm-ci-{self._get_repo_suffix()}-repo:$BUILDKITE_COMMIT-cpu"

    @property
    def container_image_amd(self):
        """Get the AMD container image."""
        return "rocm/vllm-ci:$BUILDKITE_COMMIT"

    def validate(self):
        """Validate the configuration."""
        pattern = r"^[0-9a-f]{40}$"
        if not re.match(pattern, self.commit):
            raise ValueError(f"Commit {self.commit} is not a valid Git commit hash")


# ==============================================================================
# CONSTANTS
# ==============================================================================

# ECR and Images
VLLM_ECR_URL = "public.ecr.aws/q9t5s3a7"
VLLM_ECR_REPO = f"{VLLM_ECR_URL}/vllm-ci-test-repo"
AMD_REPO = "rocm/vllm-ci"

# Paths
HF_HOME = "/root/.cache/huggingface"
HF_HOME_FSX = "/fsx/hf_cache"
DEFAULT_WORKING_DIR = "/vllm-workspace/tests"

# Build Step Keys
BUILD_KEY_MAIN = "image-build"
BUILD_KEY_CPU = "image-build-cpu"
BUILD_KEY_CU118 = "image-build-cu118"
BUILD_KEY_AMD = "amd-build"
BUILD_KEY_TORCH_NIGHTLY = "image-build-torch-nightly"

# Agent Queues
QUEUE_CPU = "cpu_queue"
QUEUE_CPU_PREMERGE = "cpu_queue_premerge"
QUEUE_CPU_PREMERGE_US_EAST_1 = "cpu_queue_premerge_us_east_1"
QUEUE_CPU_POSTMERGE_US_EAST_1 = "cpu_queue_postmerge_us_east_1"
QUEUE_SMALL_CPU = "small_cpu_queue"
QUEUE_SMALL_CPU_PREMERGE = "small_cpu_queue_premerge"
QUEUE_GPU_1 = "gpu_1_queue"
QUEUE_GPU_4 = "gpu_4_queue"
QUEUE_A100 = "a100_queue"
QUEUE_H100 = "mithril-h100-pool"
QUEUE_H200 = "skylab-h200"
QUEUE_B200 = "B200"
QUEUE_AMD = "amd"
QUEUE_AMD_CPU = "amd-cpu"
QUEUE_AMD_MI300_1 = "amd_mi300_1"
QUEUE_AMD_MI325_1 = "amd_mi325_1"
QUEUE_AMD_MI325_2 = "amd_mi325_2"
QUEUE_AMD_MI325_4 = "amd_mi325_4"
QUEUE_AMD_MI325_8 = "amd_mi325_8"
QUEUE_NEURON = "neuron"
QUEUE_INTEL_CPU = "intel-cpu"
QUEUE_INTEL_GPU = "intel-gpu"
QUEUE_INTEL_HPU = "intel-hpu"
QUEUE_TPU_V5 = "tpu_v5_queue"
QUEUE_TPU_V6E = "tpu_v6e_queue"
QUEUE_GH200 = "gh200_queue"
QUEUE_IBM_PPC64LE = "ibm-ppc64le"
QUEUE_IBM_S390X = "ibm_s390x"
QUEUE_ASCEND = "ascend"

# Docker Plugin
DOCKER_PLUGIN = "docker#v5.2.0"

# Retry Configuration
RETRY_EXIT_STATUS_AGENT_LOST = -1
RETRY_EXIT_STATUS_AGENT_TERMINATED = -10

# Priority Values
PRIORITY_AMD = 100
PRIORITY_A100 = 10000

# Scripts
SCRIPT_RUN_MULTI_NODE = "./.buildkite/scripts/run-multi-node-test.sh"
SCRIPT_RUN_NEURON = ".buildkite/scripts/hardware_ci/run-neuron-test.sh"
SCRIPT_RUN_AMD = ".buildkite/scripts/hardware_ci/run-amd-test.sh"
SCRIPT_RUN_INTEL_CPU = ".buildkite/scripts/hardware_ci/run-cpu-test.sh"
SCRIPT_RUN_INTEL_GPU = ".buildkite/scripts/hardware_ci/run-xpu-test.sh"
SCRIPT_RUN_INTEL_HPU = ".buildkite/scripts/hardware_ci/run-hpu-test.sh"
SCRIPT_RUN_TPU = ".buildkite/scripts/hardware_ci/run-tpu-test.sh"
SCRIPT_RUN_TPU_V1 = ".buildkite/scripts/hardware_ci/run-tpu-v1-test.sh"
SCRIPT_RUN_TPU_V1_PART2 = ".buildkite/scripts/hardware_ci/run-tpu-v1-test-part2.sh"
SCRIPT_RUN_GH200 = ".buildkite/scripts/hardware_ci/run-gh200-test.sh"
SCRIPT_RUN_IBM_POWER = ".buildkite/scripts/hardware_ci/run-cpu-test-ppc64le.sh"
SCRIPT_RUN_IBM_S390X = ".buildkite/scripts/hardware_ci/run-cpu-test-s390x.sh"
SCRIPT_RUN_ASCEND = ".buildkite/scripts/hardware_ci/run-ascend-test.sh"
SCRIPT_TPU_CLEANUP = "bash .buildkite/scripts/tpu/cleanup_docker.sh"
SCRIPT_TPU_DOCKER_RUN_BM = "bash .buildkite/scripts/tpu/docker_run_bm.sh"

# Dockerfiles
DOCKERFILE = "docker/Dockerfile"
DOCKERFILE_ROCM = "docker/Dockerfile.rocm"

# Test Labels
LABEL_DOC_BUILD = "Documentation Build"
LABEL_BENCHMARKS = "Benchmarks"
LABEL_BASIC_CORRECTNESS = "Basic Correctness Test"
LABEL_SPEC_DECODE = "Speculative decoding tests"

# Kubernetes
K8S_NVIDIA_GPU_RESOURCE = "nvidia.com/gpu"
K8S_NVIDIA_GPU_PRODUCT = "nvidia.com/gpu.product"
K8S_NVIDIA_A100_PRODUCT = "NVIDIA-A100-SXM4-80GB"
K8S_HF_TOKEN_SECRET = "hf-token-secret"
K8S_HF_TOKEN_KEY = "token"
K8S_PRIORITY_CLASS = "ci"
K8S_DEVSHM_VOLUME = "devshm"
K8S_HF_CACHE_VOLUME = "hf-cache"
K8S_DEV_SHM_PATH = "/dev/shm"


# ==============================================================================
# HARDWARE TEST DATACLASSES
# ==============================================================================


@dataclass
class HardwareTestConfig:
    """Configuration for a hardware-specific test."""
    label: str
    queue: str
    script_path: str
    depends_on: Optional[str] = None
    soft_fail: bool = True
    timeout_in_minutes: Optional[int] = None
    extra_commands: List[str] = field(default_factory=list)
    env: Optional[Dict[str, str]] = None
    key: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to Buildkite step dictionary."""
        step: Dict[str, Any] = {
            "label": self.label,
            "agents": {"queue": self.queue},
            "soft_fail": self.soft_fail,
        }

        if self.depends_on is not None:
            step["depends_on"] = self.depends_on

        if self.timeout_in_minutes:
            step["timeout_in_minutes"] = self.timeout_in_minutes

        if self.key:
            step["key"] = self.key

        # Build command
        if self.label == "GH200 Test":
            step["command"] = f"nvidia-smi && bash {self.script_path}"
        elif self.extra_commands:
            step["commands"] = self.extra_commands + [f"bash {self.script_path}"]
        else:
            step["command"] = f"bash {self.script_path}"

        if self.env:
            step["env"] = self.env

        return step


@dataclass
class TPUTestConfig:
    """Configuration for TPU tests."""
    label: str
    key: str
    timeout_in_minutes: int
    script_path: Optional[str] = None
    extra_docker_build: Optional[str] = None
    extra_scripts: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to Buildkite step."""
        commands = [SCRIPT_TPU_CLEANUP]

        if self.extra_docker_build:
            commands.append(self.extra_docker_build)

        if self.script_path:
            commands.append(f'if [[ -f "{self.script_path}" ]]; then bash {self.script_path}; fi')

        commands.extend(self.extra_scripts)

        return {
            "label": self.label,
            "soft_fail": True,
            "depends_on": None,
            "key": self.key,
            "timeout_in_minutes": self.timeout_in_minutes,
            "agents": {"queue": QUEUE_TPU_V6E},
            "commands": commands,
        }


# ==============================================================================
# HARDWARE TEST DEFINITIONS
# ==============================================================================

# Simple hardware tests
NEURON_TEST = HardwareTestConfig(
    label="Neuron Test",
    queue=QUEUE_NEURON,
    script_path=SCRIPT_RUN_NEURON,
)

INTEL_HPU_TEST = HardwareTestConfig(
    label="Intel HPU Test",
    queue=QUEUE_INTEL_HPU,
    script_path=SCRIPT_RUN_INTEL_HPU,
)

INTEL_GPU_TEST = HardwareTestConfig(
    label="Intel GPU Test",
    queue=QUEUE_INTEL_GPU,
    script_path=SCRIPT_RUN_INTEL_GPU,
)

ASCEND_TEST = HardwareTestConfig(
    label="Ascend NPU Test",
    queue=QUEUE_ASCEND,
    script_path=".buildkite/scripts/hardware_ci/run-npu-test.sh",
    timeout_in_minutes=20,
)

GH200_TEST = HardwareTestConfig(
    label="GH200 Test",
    queue=QUEUE_GH200,
    script_path=SCRIPT_RUN_GH200,
    extra_commands=[],
)

# TPU tests
TPU_V1_TEST = TPUTestConfig(
    label="TPU V1 Test",
    key="run-tpu-v1-test",
    timeout_in_minutes=180,
    script_path=SCRIPT_RUN_TPU_V1,
)

TPU_V1_TEST_PART2 = TPUTestConfig(
    label="TPU V1 Test Part2",
    key="run-tpu-v1-test-part2",
    timeout_in_minutes=90,
    script_path=SCRIPT_RUN_TPU_V1_PART2,
)

TPU_V1_BENCHMARK = TPUTestConfig(
    label="TPU V1 Benchmark Test",
    key="run-tpu-v1-benchmark-test",
    timeout_in_minutes=60,
    extra_docker_build=(
        "DOCKER_BUILDKIT=1 docker build --build-arg max_jobs=16 --build-arg USE_SCCACHE=1 "
        "--build-arg GIT_REPO_CHECK=0 --tag vllm/vllm-tpu-bm --progress plain -f docker/Dockerfile.tpu ."
    ),
    extra_scripts=[
        "bash .buildkite/scripts/tpu/docker_run_bm.sh .buildkite/scripts/tpu/config_v6e_1.env",
        "bash .buildkite/scripts/tpu/docker_run_bm.sh .buildkite/scripts/tpu/quantized_v6e_1.env",
    ],
)


# AMD Queue Label Lists
AMD_MI325_8_LABELS = [
    LABEL_BENCHMARKS,
    "Kernels Attention Test %N",
    "LoRA Test %N",
    "Kernels Quantization Test %N",
]

AMD_MI325_4_LABELS = [
    "Distributed Tests (4 GPUs)",
    "2 Node Tests (4 GPUs in total)",
    "Multi-step Tests (4 GPUs)",
    "Pipeline Parallelism Test",
    "LoRA TP Test (Distributed)",
]

AMD_MI325_2_LABELS = [
    "Distributed Comm Ops Test",
    "Distributed Tests (2 GPUs)",
    "Plugin Tests (2 GPUs)",
    "Weight Loading Multiple GPU Test",
    "Weight Loading Multiple GPU Test - Large Models",
]

