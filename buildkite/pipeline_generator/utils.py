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

def _clean_docker_tag(tag: str) -> str:
    # Only allows alphanumeric, dashes and underscores for Docker tags, and replaces others with '-'
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", tag or "")

def _docker_manifest_exists(image_tag: str) -> bool:
    try:
        subprocess.run(
            ["docker", "manifest", "inspect", image_tag],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )
        return True
    except subprocess.CalledProcessError:
        return False

def get_ecr_cache_registry() -> Tuple[str, str]:
    global_config = get_global_config()
    branch = global_config["branch"]
    test_cache_ecr = "936637512419.dkr.ecr.us-east-1.amazonaws.com/vllm-ci-test-cache"
    postmerge_cache_ecr = "936637512419.dkr.ecr.us-east-1.amazonaws.com/vllm-ci-postmerge-cache"
    cache_from_tag, cache_to_tag = None, None
    # Authenticate Docker to AWS ECR
    login_cmd = [
        "aws", "ecr", "get-login-password",
        "--region", "us-east-1"
    ]
    try:
        proc = subprocess.Popen(login_cmd, stdout=subprocess.PIPE)
        subprocess.run(
            [
                "docker", "login",
                "--username", "AWS",
                "--password-stdin",
                "936637512419.dkr.ecr.us-east-1.amazonaws.com"
            ],
            stdin=proc.stdout,
            check=True
        )
        proc.stdout.close()
        proc.wait()
    except Exception as e:
        raise RuntimeError(f"Failed to authenticate with AWS ECR: {e}")

    if global_config["pull_request"]: # PR build
        cache_to_tag = f"{test_cache_ecr}:pr-{global_config['pull_request']}"
        if _docker_manifest_exists(cache_to_tag): # use PR cache if exists
            cache_from_tag = cache_to_tag
        elif os.getenv("BUILDKITE_PULL_REQUEST_BASE_BRANCH") != "main": # use base branch cache if exists
            clean_base = _clean_docker_tag(os.getenv("BUILDKITE_PULL_REQUEST_BASE_BRANCH"))
            if _docker_manifest_exists(f"{test_cache_ecr}:{clean_base}"):
                cache_from_tag = f"{test_cache_ecr}:{clean_base}"
            else: # fall back to postmerge cache ecr if base branch cache does not exist
                cache_from_tag = f"{postmerge_cache_ecr}:latest"
        else:
            cache_from_tag = f"{postmerge_cache_ecr}:latest"
    else: # non-PR build
        if branch == "main": # postmerge
            cache_to_tag = f"{postmerge_cache_ecr}:latest"
            cache_from_tag = f"{postmerge_cache_ecr}:latest"
        else:
            clean_branch = _clean_docker_tag(branch)
            cache_to_tag = f"{test_cache_ecr}:{clean_branch}"
            if _docker_manifest_exists(f"{test_cache_ecr}:{clean_branch}"):
                cache_from_tag = f"{test_cache_ecr}:{clean_branch}"
            else:
                cache_from_tag = f"{postmerge_cache_ecr}:latest"
    if not cache_from_tag or not cache_to_tag:
        raise RuntimeError("Failed to get ECR cache tags")
    return cache_from_tag, cache_to_tag
