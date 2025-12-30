from enum import Enum


class DeviceType(str, Enum):
    H100 = "h100"
    H200 = "h200"
    B200 = "b200"
    A100 = "a100"
    CPU = "cpu"
    INTEL_CPU = "intel_cpu"
    INTEL_HPU = "intel_hpu"
    INTEL_GPU = "intel_gpu"
    ARM_CPU = "arm_cpu"
    GH200 = "gh200"
    ASCEND = "ascend_npu"


class AgentQueue(str, Enum):
    CPU_PREMERGE = "cpu_queue_premerge"
    CPU_POSTMERGE = "cpu_queue_postmerge"
    GPU_1 = "gpu_1_queue"
    GPU_4 = "gpu_4_queue"
    MITHRIL_H100 = "mithril-h100-pool"
    SKYLAB_H200 = "skylab-h200"
    B200 = "B200"
    SMALL_CPU_PREMERGE = "small_cpu_queue_premerge"
    A100 = "a100_queue"
    CPU_PREMERGE_US_EAST_1 = "cpu_queue_premerge_us_east_1"
    CPU_POSTMERGE_US_EAST_1 = "cpu_queue_postmerge_us_east_1"
    INTEL_CPU = "intel-cpu"
    INTEL_HPU = "intel-hpu"
    INTEL_GPU = "intel-gpu"
    ARM_CPU = "arm-cpu"
    GH200 = "gh200_queue"
    ASCEND = "ascend"
