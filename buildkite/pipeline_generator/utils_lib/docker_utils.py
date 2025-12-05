import subprocess
import os
import re
from typing import Tuple
from global_config import get_global_config

def get_image(cpu: bool = False) -> str:
    global_config = get_global_config()
    commit = "$BUILDKITE_COMMIT"
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

