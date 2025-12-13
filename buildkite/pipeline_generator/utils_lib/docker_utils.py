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


def clean_docker_tag(tag: str) -> str:
    """
    Function to replace invalid characters in Docker image tags and truncate to 128 chars
    Valid characters: a-z, A-Z, 0-9, _, ., -
    """
    # Replace invalid characters with underscore and truncate to 128 chars
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", tag or "")
    return cleaned[:128]



def resolve_ecr_cache_vars() -> Tuple[str, str, str, str]:
    """
    Resolve ECR cache-from, cache-to using buildkite environment variables:
     -  BUILDKITE_BRANCH 
     -  BUILDKITE_PULL_REQUEST
     -  BUILDKITE_PULL_REQUEST_BASE_BRANCH
    Return tuple of: 
     -  CACHE_FROM: primary cache source
     -  CACHE_FROM_BASE_BRANCH: secondary cache source
     -  CACHE_FROM_MAIN: fallback cache source
     -  CACHE_TO: cache destination
    Note: CACHE_FROM, CACHE_FROM_BASE_BRANCH, CACHE_FROM_MAIN could be the same.
        This is intended behavior to allow BuildKit to merge all possible cache source 
        to maximize cache hit potential.
    """
    global_config = get_global_config()
    branch = global_config["branch"]
    pull_request = global_config["pull_request"]
    
    # Define ECR repository URLs for test and main cache
    test_cache_ecr = "936637512419.dkr.ecr.us-east-1.amazonaws.com/vllm-ci-test-cache"
    main_cache_ecr = "936637512419.dkr.ecr.us-east-1.amazonaws.com/vllm-ci-postmerge-cache"
    
    if not pull_request or pull_request == "false":
        # Not a PR
        if branch == "main":
            cache = f"{main_cache_ecr}:latest"
        else:
            clean_branch = clean_docker_tag(branch)
            cache = f"{test_cache_ecr}:{clean_branch}"
        
        cache_to = cache
        cache_from = cache
        cache_from_base_branch = cache
    else:
        # PR build
        cache_to = f"{test_cache_ecr}:pr-{pull_request}"
        cache_from = f"{test_cache_ecr}:pr-{pull_request}"
        
        base_branch = os.getenv("BUILDKITE_PULL_REQUEST_BASE_BRANCH", "main")
        if base_branch == "main":
            cache_from_base_branch = f"{main_cache_ecr}:latest"
        else:
            clean_base = clean_docker_tag(base_branch)
            cache_from_base_branch = f"{test_cache_ecr}:{clean_base}"
    
    cache_from_main = f"{main_cache_ecr}:latest"
    
    return cache_from, cache_from_base_branch, cache_from_main, cache_to
