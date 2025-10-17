"""Agent queue selection logic."""
from typing import Optional

from .constants import AgentQueue, GPUType
from ..shared.job_labels import TestLabels


def get_agent_queue(no_gpu: Optional[bool], gpu_type: Optional[str], num_gpus: Optional[int], label: Optional[str] = None) -> AgentQueue:
    """Determine the agent queue based on GPU requirements."""
    # Documentation Build gets special queue
    if label == TestLabels.DOCUMENTATION_BUILD:
        return AgentQueue.SMALL_CPU_PREMERGE
    
    if no_gpu:
        return AgentQueue.AWS_CPU_PREMERGE
    
    # Special GPU types
    if gpu_type == GPUType.A100.value:
        return AgentQueue.A100
    if gpu_type == GPUType.H100.value:
        return AgentQueue.H100
    if gpu_type == GPUType.H200.value:
        return AgentQueue.H200
    if gpu_type == GPUType.B200.value:
        return AgentQueue.B200
    
    # Default CUDA GPUs (L4)
    return AgentQueue.AWS_1xL4 if not num_gpus or num_gpus == 1 else AgentQueue.AWS_4xL4

