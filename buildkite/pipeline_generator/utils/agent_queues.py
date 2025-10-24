"""Agent queue selection logic."""

from typing import Optional

from ..utils.constants import TestLabels
from .constants import AgentQueue, GPUType


def get_agent_queue(
    no_gpu: Optional[bool],
    gpu_type: Optional[str],
    num_gpus: Optional[int],
    label: Optional[str] = None,
) -> str:
    """Determine the agent queue based on GPU requirements."""
    # Documentation Build gets special queue
    if label == TestLabels.DOCUMENTATION_BUILD:
        return AgentQueue.SMALL_CPU_QUEUE_PREMERGE

    if no_gpu:
        return AgentQueue.CPU_QUEUE_PREMERGE_US_EAST_1

    # Map GPU types to queues
    gpu_queue_map = {
        GPUType.A100.value: AgentQueue.A100_QUEUE,
        GPUType.H100.value: AgentQueue.MITHRIL_H100_POOL,
        GPUType.H200.value: AgentQueue.SKYLAB_H200,
        GPUType.B200.value: AgentQueue.B200,
    }

    if gpu_type and gpu_type in gpu_queue_map:
        return gpu_queue_map[gpu_type]

    # Default CUDA GPUs (L4)
    return AgentQueue.GPU_1_QUEUE if not num_gpus or num_gpus == 1 else AgentQueue.GPU_4_QUEUE
