from enum import Enum


class DeviceType(str, Enum):
    H100 = "h100"
    H200 = "h200"
    B200 = "b200"
    A100 = "a100"
    CPU = "cpu"
    CPU_SMALL = "cpu-small"
    CPU_MEDIUM = "cpu-medium"
    INTEL_CPU = "intel_cpu"
    INTEL_HPU = "intel_hpu"
    INTEL_GPU = "intel_gpu"
    ARM_CPU = "arm_cpu"
    GH200 = "gh200"
    ASCEND = "ascend_npu"
    AMD_CPU = "amd_cpu"
    AMD_MI250_1 = "mi250_1"
    AMD_MI250_2 = "mi250_2"
    AMD_MI250_4 = "mi250_4"
    AMD_MI250_8 = "mi250_8"
    AMD_MI325_1 = "mi325_1"
    AMD_MI325_2 = "mi325_2"
    AMD_MI325_4 = "mi325_4"
    AMD_MI325_8 = "mi325_8"
    AMD_MI355_1 = "mi355_1"
    AMD_MI355_2 = "mi355_2"
    AMD_MI355_4 = "mi355_4"
    AMD_MI355_8 = "mi355_8"


class AgentQueue(str, Enum):
    CPU_PREMERGE = "cpu_queue_premerge"
    CPU_POSTMERGE = "cpu_queue_postmerge"
    ARM64_CPU_PREMERGE = "arm64_cpu_queue_premerge"
    ARM64_CPU_POSTMERGE = "arm64_cpu_queue_postmerge"
    GPU_1 = "gpu_1_queue"
    GPU_4 = "gpu_4_queue"
    MITHRIL_H100 = "mithril-h100-pool"
    H200 = "H200"
    B200 = "B200"
    SMALL_CPU_PREMERGE = "small_cpu_queue_premerge"
    MEDIUM_CPU_PREMERGE = "medium_cpu_queue_premerge"
    A100 = "a100_queue"
    CPU_PREMERGE_US_EAST_1 = "cpu_queue_premerge_us_east_1"
    CPU_POSTMERGE_US_EAST_1 = "cpu_queue_postmerge_us_east_1"
    INTEL_CPU = "intel-cpu"
    INTEL_HPU = "intel-hpu"
    INTEL_GPU = "intel-gpu"
    ARM_CPU = "arm-cpu"
    GH200 = "gh200_queue"
    ASCEND = "ascend"
    AMD_CPU = "amd-cpu"
    AMD_MI250_1 = "amd_mi250_1"
    AMD_MI250_2 = "amd_mi250_2"
    AMD_MI250_4 = "amd_mi250_4"
    AMD_MI250_8 = "amd_mi250_8"
    AMD_MI325_1 = "amd_mi325_1"
    AMD_MI325_2 = "amd_mi325_2"
    AMD_MI325_4 = "amd_mi325_4"
    AMD_MI325_8 = "amd_mi325_8"
    AMD_MI355_1 = "amd_mi355_1"
    AMD_MI355_2 = "amd_mi355_2"
    AMD_MI355_4 = "amd_mi355_4"
    AMD_MI355_8 = "amd_mi355_8"


DEVICE_TO_QUEUE = {
    DeviceType.CPU_SMALL: AgentQueue.SMALL_CPU_PREMERGE,
    DeviceType.CPU_MEDIUM: AgentQueue.MEDIUM_CPU_PREMERGE,
    DeviceType.CPU: AgentQueue.CPU_PREMERGE_US_EAST_1,
    DeviceType.A100: AgentQueue.A100,
    DeviceType.H100: AgentQueue.MITHRIL_H100,
    DeviceType.H200: AgentQueue.H200,
    DeviceType.B200: AgentQueue.B200,
    DeviceType.INTEL_CPU: AgentQueue.INTEL_CPU,
    DeviceType.INTEL_HPU: AgentQueue.INTEL_HPU,
    DeviceType.INTEL_GPU: AgentQueue.INTEL_GPU,
    DeviceType.ARM_CPU: AgentQueue.ARM_CPU,
    DeviceType.AMD_CPU: AgentQueue.AMD_CPU,
    DeviceType.GH200: AgentQueue.GH200,
    DeviceType.ASCEND: AgentQueue.ASCEND,
    DeviceType.AMD_MI250_1: AgentQueue.AMD_MI250_1,
    DeviceType.AMD_MI250_2: AgentQueue.AMD_MI250_2,
    DeviceType.AMD_MI250_4: AgentQueue.AMD_MI250_4,
    DeviceType.AMD_MI250_8: AgentQueue.AMD_MI250_8,
    DeviceType.AMD_MI325_1: AgentQueue.AMD_MI325_1,
    DeviceType.AMD_MI325_2: AgentQueue.AMD_MI325_2,
    DeviceType.AMD_MI325_4: AgentQueue.AMD_MI325_4,
    DeviceType.AMD_MI325_8: AgentQueue.AMD_MI325_8,
    DeviceType.AMD_MI355_1: AgentQueue.AMD_MI355_1,
    DeviceType.AMD_MI355_2: AgentQueue.AMD_MI355_2,
    DeviceType.AMD_MI355_4: AgentQueue.AMD_MI355_4,
    DeviceType.AMD_MI355_8: AgentQueue.AMD_MI355_8,
}
