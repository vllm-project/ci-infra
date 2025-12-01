from enum import Enum
from step import Step
import os
from typing import List, Dict
import re
import subprocess
import requests

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

def get_agent_queue(step: Step):
    if step.label == "Documentation Build":
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

def should_run_all(pr_labels: List[str], list_file_diff: List[str], run_all_patterns: List[str], run_all_exclude_patterns: List[str]) -> bool:
    """Determine if the pipeline should run all tests."""
    if os.getenv("RUN_ALL") == "1":
        return True
    if "ready-run-all-tests" in pr_labels:
        return True
    pattern_matched = False
    for file in list_file_diff:
        for pattern in run_all_patterns:
            if re.match(pattern, file):
                pattern_matched = True
                break
        if pattern_matched:
            match_ignore = False
            for exclude_pattern in run_all_exclude_patterns:
                if re.match(exclude_pattern, file):
                    match_ignore = True
                    break
            if not match_ignore:
                return True
    return False

def get_list_file_diff(branch: str) -> List[str]:
    """Get list of file paths that get changed between current branch and origin/main."""
    try:
        subprocess.run(["git", "add", "."], check=True)
        if branch == "main":
            output = subprocess.check_output(
                ["git", "diff", "--name-only", "--diff-filter=ACMDR", "HEAD~1"],
                universal_newlines=True
            )
        else:
            merge_base = subprocess.check_output(
                ["git", "merge-base", "origin/main", "HEAD"],
                universal_newlines=True
            )
            output = subprocess.check_output(
                ["git", "diff", "--name-only", "--diff-filter=ACMDR", merge_base.strip()],
                universal_newlines=True
            )
        return [line for line in output.split('\n') if line.strip()]
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to get git diff: {e}")

def get_pr_labels(pull_request: str) -> List[str]:
    if not pull_request or pull_request == "false":
        return []
    request_url = f"https://api.github.com/repos/vllm-project/vllm/pulls/{pull_request}"
    response = requests.get(request_url)
    response.raise_for_status()
    return [label["name"] for label in response.json()["labels"]]

def should_use_precompiled(self, run_all: bool) -> bool:
    if os.getenv("VLLM_USE_PRECOMPILED") == "1":
        return True
    if run_all:
        return False
    return True

def should_fail_fast(pr_labels: List[str]) -> bool:
    if "ci-no-fail-fast" in pr_labels:
        return False
    return True

def get_image(registries: List[str], repositories: Dict[str, str], branch: str, commit: str) -> str:
    if branch == "main":
        return f"{registries}:{repositories['main']}:{commit}"
    else:
        return f"{registries}:{repositories['premerge']}:{commit}"
