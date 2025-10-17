"""Centralized job label constants to avoid magic strings."""

# Test Job Labels (frequently referenced)
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


# Hardware Test Labels
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


# AMD Queue Selection Labels (for queue mapping)
class AMDQueueLabels:
    """Labels used for AMD queue determination."""
    # 8 GPU tests
    AMD_MI325_8_LABELS = [
        TestLabels.BENCHMARKS,
        TestLabels.KERNELS_ATTENTION_TEST,
        TestLabels.LORA_TEST,
        TestLabels.KERNELS_QUANTIZATION_TEST
    ]
    
    # 4 GPU tests
    AMD_MI325_4_LABELS = [
        TestLabels.DISTRIBUTED_TESTS_4_GPU,
        TestLabels.TWO_NODE_TESTS_4_GPU,
        TestLabels.MULTI_STEP_TESTS_4_GPU,
        TestLabels.PIPELINE_PARALLELISM_TEST,
        TestLabels.LORA_TP_TEST_DISTRIBUTED
    ]
    
    # 2 GPU tests
    AMD_MI325_2_LABELS = [
        TestLabels.DISTRIBUTED_COMM_OPS_TEST,
        TestLabels.DISTRIBUTED_TESTS_2_GPU,
        TestLabels.PLUGIN_TESTS_2_GPU,
        TestLabels.WEIGHT_LOADING_MULTIPLE_GPU_TEST,
        TestLabels.WEIGHT_LOADING_MULTIPLE_GPU_TEST_LARGE
    ]
    
    # Default: 1 GPU (amd_mi325_1)


# Block Labels
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


# Build Labels
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


# Group Labels
class GroupLabels:
    """Group labels."""
    AMD_TESTS = "AMD Tests"
    TORCH_NIGHTLY = "Torch Nightly"


# Label prefixes for AMD tests
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

