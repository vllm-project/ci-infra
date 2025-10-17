"""Script paths and file locations."""

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

