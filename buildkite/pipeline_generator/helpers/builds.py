"""Build step generation - returns simple dicts."""

from typing import Any, Dict, List, Optional

from ..config import (
    BUILD_KEY_AMD,
    BUILD_KEY_CPU,
    BUILD_KEY_CU118,
    BUILD_KEY_MAIN,
    BUILD_KEY_TORCH_NIGHTLY,
    DOCKERFILE,
    DOCKERFILE_ROCM,
    QUEUE_AMD_CPU,
    RETRY_EXIT_STATUS_AGENT_LOST,
    RETRY_EXIT_STATUS_AGENT_TERMINATED,
)


def _build_docker_build_cmd(dockerfile: str, build_args: Dict[str, str], image_tag: str, target: str, fastcheck_format: bool = False) -> str:
    """Build docker build command with proper formatting."""
    if fastcheck_format:
        # Fastcheck uses folded scalar (>) with specific spacing
        lines = [f"docker build --file {dockerfile}  "]
        
        args_list = list(build_args.items())
        for idx, (key, value) in enumerate(args_list):
            # First 3 args always get double space
            if idx < 3:
                suffix = "  "
            # Last 3 args before --tag get single space
            elif idx >= len(args_list) - 3:
                suffix = " "
            else:
                suffix = "  "
            
            if key == "buildkite_commit":
                lines.append(f"--build-arg {key}=$BUILDKITE_COMMIT{suffix}")
            else:
                if " " in str(value):
                    lines.append(f'--build-arg {key}="{value}"{suffix}')
                else:
                    lines.append(f"--build-arg {key}={value}{suffix}")
        
        lines.append(f"--tag {image_tag}  ")
        lines.append(f"--target {target} ")
        lines.append("--progress plain .\n")
        return "".join(lines)
    else:
        # CI mode uses simple space-separated
        cmd_parts = ["docker build", f"--file {dockerfile}"]
        
        for key, value in build_args.items():
            if key == "buildkite_commit":
                cmd_parts.append(f"--build-arg {key}=$BUILDKITE_COMMIT")
            elif " " in str(value):
                cmd_parts.append(f'--build-arg {key}="{value}"')
            else:
                cmd_parts.append(f"--build-arg {key}={value}")
        
        cmd_parts.extend([f"--tag {image_tag}", f"--target {target}", "--progress plain ."])
        return " ".join(cmd_parts)


def _build_image_check_cmd(image_tag: str, fastcheck_format: bool = False) -> str:
    """Build command to check if image exists."""
    suffix = "\n" if fastcheck_format else ""
    return f"""#!/bin/bash
if [[ -z $(docker manifest inspect {image_tag}) ]]; then
  echo "Image not found, proceeding with build..."
else
  echo "Image found"
  exit 0
fi{suffix}"""


def build_main_image(config) -> Dict[str, Any]:
    """Build main CUDA image step."""
    is_fastcheck = config.pipeline_mode.value == "fastcheck"
    retry_limit = 5 if is_fastcheck else 2
    
    build_args = {
        "max_jobs": "16",
        "buildkite_commit": config.commit,
        "USE_SCCACHE": "1",
    }
    
    # Add CUDA arch lists for CI
    if not is_fastcheck:
        build_args["TORCH_CUDA_ARCH_LIST"] = "8.0 8.9 9.0 10.0"
        build_args["FI_TORCH_CUDA_ARCH_LIST"] = "8.0 8.9 9.0a 10.0a"
    
    # Add precompiled args if needed (order matters!)
    if is_fastcheck:
        # Fastcheck has different order than CI
        build_args["VLLM_DOCKER_BUILD_CONTEXT"] = "1"
        build_args["VLLM_USE_PRECOMPILED"] = config.vllm_use_precompiled
        if config.vllm_use_precompiled == "1":
            build_args["USE_FLASHINFER_PREBUILT_WHEEL"] = "true"
    elif config.branch != "main":
        # CI non-main branch
        build_args["VLLM_USE_PRECOMPILED"] = config.vllm_use_precompiled
        build_args["VLLM_DOCKER_BUILD_CONTEXT"] = "1"
        if config.vllm_use_precompiled == "1":
            build_args["USE_FLASHINFER_PREBUILT_WHEEL"] = "true"
    
    image_tag = config.container_image
    
    commands = [
        "aws ecr-public get-login-password --region us-east-1 | docker login --username AWS --password-stdin public.ecr.aws/q9t5s3a7",
        _build_image_check_cmd(image_tag, is_fastcheck),
        _build_docker_build_cmd(DOCKERFILE, build_args, image_tag, "test", is_fastcheck),
        f"docker push {image_tag}",
    ]
    
    # Add latest tag push for main branch in CI mode
    if config.branch == "main" and not is_fastcheck:
        latest_tag = image_tag.replace("$BUILDKITE_COMMIT", "latest")
        commands.extend([f"docker tag {image_tag} {latest_tag}", f"docker push {latest_tag}"])
    
    # Determine queue
    if is_fastcheck:
        queue = "cpu_queue_premerge"
    elif config.branch == "main":
        queue = "cpu_queue_postmerge_us_east_1"
    else:
        queue = "cpu_queue_premerge_us_east_1"
    
    step = {
        "label": ":docker: build image",
        "key": BUILD_KEY_MAIN,
        "agents": {"queue": queue},
        "commands": commands,
        "env": {"DOCKER_BUILDKIT": "1"},
        "retry": {
            "automatic": [
                {"exit_status": RETRY_EXIT_STATUS_AGENT_LOST, "limit": retry_limit},
                {"exit_status": RETRY_EXIT_STATUS_AGENT_TERMINATED, "limit": retry_limit},
            ]
        },
    }
    
    # CI mode includes depends_on: null, fastcheck doesn't
    if not is_fastcheck:
        step["depends_on"] = None
    
    return step


def build_cu118_image(config) -> List[Dict[str, Any]]:
    """Build CUDA 11.8 image (CI only, with block)."""
    build_args = {
        "max_jobs": "16",
        "buildkite_commit": config.commit,
        "USE_SCCACHE": "1",
        "CUDA_VERSION": "11.8.0",
    }
    
    if config.branch != "main":
        build_args["VLLM_USE_PRECOMPILED"] = config.vllm_use_precompiled
        build_args["VLLM_DOCKER_BUILD_CONTEXT"] = "1"
        if config.vllm_use_precompiled == "1":
            build_args["USE_FLASHINFER_PREBUILT_WHEEL"] = "true"
    
    queue = "cpu_queue_postmerge_us_east_1" if config.branch == "main" else "cpu_queue_premerge_us_east_1"
    image_tag = config.container_image_cu118
    
    commands = [
        "aws ecr-public get-login-password --region us-east-1 | docker login --username AWS --password-stdin public.ecr.aws/q9t5s3a7",
        _build_image_check_cmd(image_tag),
        _build_docker_build_cmd(DOCKERFILE, build_args, image_tag, "test"),
        f"docker push {image_tag}",
    ]
    
    return [
        {
            "block": "Build CUDA 11.8 image",
            "key": "block-build-cu118",
            "depends_on": None,
        },
        {
            "label": ":docker: build image CUDA 11.8",
            "key": BUILD_KEY_CU118,
            "depends_on": "block-build-cu118",
            "agents": {"queue": queue},
            "commands": commands,
            "env": {"DOCKER_BUILDKIT": "1"},
            "retry": {
                "automatic": [
                    {"exit_status": RETRY_EXIT_STATUS_AGENT_LOST, "limit": 2},
                    {"exit_status": RETRY_EXIT_STATUS_AGENT_TERMINATED, "limit": 2},
                ]
            },
        },
    ]


def build_cpu_image(config) -> Dict[str, Any]:
    """Build CPU image (CI only)."""
    build_args = {
        "max_jobs": "16",
        "buildkite_commit": config.commit,
        "VLLM_CPU_AVX512BF16": "true",
        "VLLM_CPU_AVX512VNNI": "true",
    }
    
    queue = "cpu_queue_postmerge_us_east_1" if config.branch == "main" else "cpu_queue_premerge_us_east_1"
    image_tag = config.container_image_cpu
    
    commands = [
        "aws ecr-public get-login-password --region us-east-1 | docker login --username AWS --password-stdin public.ecr.aws/q9t5s3a7",
        _build_image_check_cmd(image_tag),
        _build_docker_build_cmd("docker/Dockerfile.cpu", build_args, image_tag, "vllm-test"),
        f"docker push {image_tag}",
    ]
    
    # CI only (always has depends_on: null)
    return {
        "label": ":docker: build image CPU",
        "key": BUILD_KEY_CPU,
        "depends_on": None,
        "agents": {"queue": queue},
        "commands": commands,
        "env": {"DOCKER_BUILDKIT": "1"},
        "retry": {
            "automatic": [
                {"exit_status": RETRY_EXIT_STATUS_AGENT_LOST, "limit": 2},
                {"exit_status": RETRY_EXIT_STATUS_AGENT_TERMINATED, "limit": 2},
            ]
        },
    }


def build_torch_nightly_image(config, depends_on: Optional[str]) -> Dict[str, Any]:
    """Build torch nightly image (CI only)."""
    build_args = {
        "max_jobs": "16",
        "buildkite_commit": config.commit,
        "USE_SCCACHE": "1",
    }
    
    queue = "cpu_queue_postmerge_us_east_1" if config.branch == "main" else "cpu_queue_premerge_us_east_1"
    image_tag = config.container_image_torch_nightly
    
    commands = [
        "aws ecr-public get-login-password --region us-east-1 | docker login --username AWS --password-stdin public.ecr.aws/q9t5s3a7",
        _build_image_check_cmd(image_tag),
        _build_docker_build_cmd("docker/Dockerfile.nightly_torch", build_args, image_tag, "test"),
        f"docker push {image_tag}",
    ]
    
    step = {
        "label": ":docker: build image torch nightly",
        "key": BUILD_KEY_TORCH_NIGHTLY,
        "agents": {"queue": queue},
        "commands": commands,
        "env": {"DOCKER_BUILDKIT": "1"},
        "soft_fail": True,
        "timeout_in_minutes": 360,
        "retry": {
            "automatic": [
                {"exit_status": RETRY_EXIT_STATUS_AGENT_LOST, "limit": 2},
                {"exit_status": RETRY_EXIT_STATUS_AGENT_TERMINATED, "limit": 2},
            ]
        },
    }
    
    if depends_on is not None:
        step["depends_on"] = depends_on
    
    return step


def build_amd_image(config) -> Dict[str, Any]:
    """Build AMD image."""
    is_fastcheck = config.pipeline_mode.value == "fastcheck"
    image_tag = config.container_image_amd
    
    # CI mode has trailing newline, fastcheck doesn't
    trailing = "\n" if not is_fastcheck else ""
    
    build_cmd = (
        "docker build "
        "--build-arg max_jobs=16 "
        "--build-arg REMOTE_VLLM=1 "
        "--build-arg ARG_PYTORCH_ROCM_ARCH='gfx90a;gfx942' "
        "--build-arg VLLM_BRANCH=$BUILDKITE_COMMIT "
        f"--tag {image_tag} "
        f"-f {DOCKERFILE_ROCM} "
        "--target test "
        "--no-cache "
        f"--progress plain .{trailing}"
    )
    
    # Fastcheck includes mirror_hw in label
    if is_fastcheck and config.mirror_hw:
        label = f"AMD: :docker: build image with {config.mirror_hw}"
    else:
        label = "AMD: :docker: build image"
    
    return {
        "label": label,
        "key": BUILD_KEY_AMD,
        "depends_on": None,
        "agents": {"queue": QUEUE_AMD_CPU},
        "env": {"DOCKER_BUILDKIT": "1"},
        "soft_fail": is_fastcheck,  # true for fastcheck, false for CI
        "retry": {
            "automatic": [
                {"exit_status": RETRY_EXIT_STATUS_AGENT_LOST, "limit": 1},
                {"exit_status": RETRY_EXIT_STATUS_AGENT_TERMINATED, "limit": 1},
                {"exit_status": 1, "limit": 1},
            ]
        },
        "commands": [build_cmd, f"docker push {image_tag}"],
    }

