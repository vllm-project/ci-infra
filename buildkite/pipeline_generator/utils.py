from enum import Enum
from step import Step
import os
from typing import List, Dict, Tuple
import re
import subprocess
import requests
from global_config import get_global_config

class GPUType(str, Enum):
    H100 = "h100"
    H200 = "h200"
    B200 = "b200"
    A100 = "a100"

class AgentQueue(str, Enum):
    CPU_QUEUE_PREMERGE = "cpu_queue_premerge"
    CPU_QUEUE_POSTMERGE = "cpu_queue_postmerge"
    GPU_1_QUEUE = "gpu_1_queue"
    GPU_4_QUEUE = "gpu_4_queue"
    MITHRIL_H100_POOL = "mithril-h100-pool"
    SKYLAB_H200 = "skylab-h200"
    B200 = "B200"
    SMALL_CPU_QUEUE_PREMERGE = "small_cpu_queue_premerge"
    A100_QUEUE = "a100_queue"
    CPU_QUEUE_PREMERGE_US_EAST_1 = "cpu_queue_premerge_us_east_1"
    CPU_QUEUE_POSTMERGE_US_EAST_1 = "cpu_queue_postmerge_us_east_1"

def get_agent_queue(step: Step):
    branch = get_global_config()["branch"]
    if step.label.startswith(":docker:"):
        if branch == "main":
            return AgentQueue.CPU_QUEUE_POSTMERGE_US_EAST_1
        else:
            return AgentQueue.CPU_QUEUE_PREMERGE_US_EAST_1
    elif step.label == "Documentation Build":
        return AgentQueue.SMALL_CPU_QUEUE_PREMERGE
    elif step.no_gpu:
        return AgentQueue.CPU_QUEUE_PREMERGE_US_EAST_1
    elif step.gpu == GPUType.A100:
        return AgentQueue.A100_QUEUE
    elif step.gpu == GPUType.H100:
        return AgentQueue.MITHRIL_H100_POOL
    elif step.gpu == GPUType.H200:
        return AgentQueue.SKYLAB_H200
    elif step.gpu == GPUType.B200:
        return AgentQueue.B200
    elif step.num_gpus == 2 or step.num_gpus == 4:
        return AgentQueue.GPU_4_QUEUE
    else:
        return AgentQueue.GPU_1_QUEUE

def is_docs_only_change(list_file_diff: List[str]) -> bool:
    for file_path in list_file_diff:
        if not file_path:
            continue
        if file_path.startswith("docs/"):
            continue
        if file_path.endswith(".md"):
            continue
        if file_path == "mkdocs.yaml":
            continue
        return False
    return True

def get_pr_labels(pull_request: str) -> List[str]:
    if not pull_request or pull_request == "false":
        return []
    request_url = f"https://api.github.com/repos/vllm-project/vllm/pulls/{pull_request}"
    response = requests.get(request_url)
    response.raise_for_status()
    return [label["name"] for label in response.json()["labels"]]

def should_use_precompiled() -> bool:
    global_config = get_global_config()
    if os.getenv("VLLM_USE_PRECOMPILED") == "1":
        return True
    if global_config["run_all"] == "1":
        return False
    wheel_metadata_url = f"https://wheels.vllm.ai/{global_config['merge_base_commit']}/vllm/metadata.json"
    response = requests.get(wheel_metadata_url)
    response.raise_for_status()
    if response.headers:
        return True
    else:
        return False

def get_image(cpu: bool = False) -> str:
    global_config = get_global_config()
    commit = global_config["commit"]
    branch = global_config["branch"]
    registries = global_config["registries"]
    repositories = global_config["repositories"]
    image = None
    if branch == "main":
        image = f"{registries}/{repositories['main']}:{commit}"
    else:
        image = f"{registries}/{repositories['premerge']}:{commit}"
    if cpu:
        image = f"{image}-cpu"
    return image