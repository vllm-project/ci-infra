from enum import Enum
class GPUType(Enum):
    H100 = "h100"
    H200 = "h200"
    B200 = "b200"
    A100 = "a100"

class AgentQueue(Enum):
    CPU_QUEUE_PREMERGE = "cpu_queue_premerge"
    GPU_1_QUEUE = "gpu_1_queue"
    GPU_4_QUEUE = "gpu_4_queue"
    MITHRIL_H100_POOL = "mithril-h100-pool"
    SKYLAB_H200 = "skylab-h200"
    B200 = "B200"
    SMALL_CPU_QUEUE_PREMERGE = "small_cpu_queue_premerge"
    A100_QUEUE = "a100_queue"
    CPU_QUEUE_PREMERGE_US_EAST_1 = "cpu_queue_premerge_us_east_1"
