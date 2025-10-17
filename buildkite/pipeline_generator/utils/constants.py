"""Constants and enums for pipeline generation."""
import enum
from typing import List


class PipelineMode(str, enum.Enum):
    """Pipeline generation mode."""
    CI = "ci"              # Full CI pipeline (default)
    FASTCHECK = "fastcheck"  # Fast pre-merge checks only
    AMD = "amd"            # AMD-only pipeline


# Constants
HF_HOME = "/root/.cache/huggingface"
DEFAULT_WORKING_DIR = "/vllm-workspace/tests"
VLLM_ECR_URL = "public.ecr.aws/q9t5s3a7"
VLLM_ECR_REPO = f"{VLLM_ECR_URL}/vllm-ci-test-repo"
AMD_REPO = "rocm/vllm-ci"

# File paths
TEST_PATH = ".buildkite/test-pipeline.yaml"
EXTERNAL_HARDWARE_TEST_PATH = ".buildkite/external-tests.yaml"
PIPELINE_FILE_PATH = ".buildkite/pipeline.yaml"
MULTI_NODE_TEST_SCRIPT = ".buildkite/run-multi-node-test.sh"

TEST_DEFAULT_COMMANDS = [
    "(command nvidia-smi || true)",  # Sanity check for Nvidia GPU setup
    "export VLLM_LOGGING_LEVEL=DEBUG",
    "export VLLM_ALLOW_DEPRECATED_BEAM_SEARCH=1",
]

STEPS_TO_BLOCK: List[str] = []


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

