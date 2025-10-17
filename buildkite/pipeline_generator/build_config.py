"""Data classes for Docker image build configurations."""
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


@dataclass
class DockerBuildConfig:
    """Configuration for building a Docker image."""
    label: str
    key: str
    dockerfile: str
    image_tag: str
    queue: str
    build_args: Dict[str, str] = field(default_factory=dict)
    depends_on: Optional[str] = None
    target: str = "test"
    push_latest: bool = False
    latest_tag: Optional[str] = None
    retry_limit: int = 2  # Default retry limit
    use_fastcheck_formatting: bool = False  # Use fastcheck-style spacing
    
    def _get_image_check_command(self) -> str:
        """Generate command to check if image already exists."""
        # Use $BUILDKITE_COMMIT in image tag
        image_tag = self.image_tag.replace(self.build_args.get("buildkite_commit", ""), "$BUILDKITE_COMMIT")
        # Fastcheck template adds trailing newline
        suffix = "\n" if self.use_fastcheck_formatting else ""
        return f"""#!/bin/bash
if [[ -z $(docker manifest inspect {image_tag}) ]]; then
  echo "Image not found, proceeding with build..."
else
  echo "Image found"
  exit 0
fi{suffix}"""
    
    def _get_docker_build_command(self) -> str:
        """Generate docker build command."""
        if self.use_fastcheck_formatting:
            # Fastcheck uses folded scalar (>) which adds double spaces except for last 3 args before tag
            lines = [f"docker build --file {self.dockerfile}  "]
            
            # Fixed base args: max_jobs, buildkite_commit, USE_SCCACHE
            # Last 2-3 args depend on vllm_use_precompiled
            # Pattern: first 3 get double space, then it varies
            args_list = list(self.build_args.items())
            for idx, (key, value) in enumerate(args_list):
                # First 3 args always get double space
                if idx < 3:
                    suffix = "  "
                # Last 3 args before --tag get single space
                elif idx >= len(args_list) - 3:
                    suffix = " "
                else:
                    # Middle args get double space
                    suffix = "  "
                
                if key == "buildkite_commit":
                    lines.append(f"--build-arg {key}=$BUILDKITE_COMMIT{suffix}")
                else:
                    if ' ' in str(value):
                        lines.append(f'--build-arg {key}="{value}"{suffix}')
                    else:
                        lines.append(f"--build-arg {key}={value}{suffix}")
            
            image_tag = self.image_tag.replace(self.build_args.get("buildkite_commit", ""), "$BUILDKITE_COMMIT")
            lines.append(f"--tag {image_tag}  ")
            lines.append(f"--target {self.target} ")
            lines.append("--progress plain .\n")
            return "".join(lines)
        else:
            # CI mode uses simple space-separated
            cmd_parts = ["docker build", f"--file {self.dockerfile}"]
            
            for key, value in self.build_args.items():
                if key == "buildkite_commit":
                    cmd_parts.append(f"--build-arg {key}=$BUILDKITE_COMMIT")
                else:
                    if ' ' in str(value):
                        cmd_parts.append(f'--build-arg {key}="{value}"')
                    else:
                        cmd_parts.append(f"--build-arg {key}={value}")
            
            image_tag = self.image_tag.replace(self.build_args.get("buildkite_commit", ""), "$BUILDKITE_COMMIT")
            cmd_parts.extend([f"--tag {image_tag}", f"--target {self.target}", "--progress plain ."])
            
            return " ".join(cmd_parts)
    
    def get_commands(self) -> List[str]:
        """Get all commands for this build step."""
        # Use $BUILDKITE_COMMIT in image tags
        image_tag = self.image_tag.replace(self.build_args.get("buildkite_commit", ""), "$BUILDKITE_COMMIT")
        
        commands = [
            "aws ecr-public get-login-password --region us-east-1 | docker login --username AWS --password-stdin public.ecr.aws/q9t5s3a7",
            self._get_image_check_command(),
            self._get_docker_build_command(),
            f"docker push {image_tag}"
        ]
        
        if self.push_latest and self.latest_tag:
            # latest_tag should stay as "latest", not use $BUILDKITE_COMMIT
            commands.extend([
                f"docker tag {image_tag} {self.latest_tag}",
                f"docker push {self.latest_tag}"
            ])
        
        return commands
    
    def to_buildkite_step(self) -> Dict[str, Any]:
        """Convert to Buildkite step dictionary."""
        step = {
            "label": self.label,
            "key": self.key,
            "agents": {"queue": self.queue},
            "commands": self.get_commands(),
            "env": {"DOCKER_BUILDKIT": "1"},
            "retry": {
                "automatic": [
                    {"exit_status": -1, "limit": self.retry_limit},
                    {"exit_status": -10, "limit": self.retry_limit}
                ]
            }
        }
        
        if self.depends_on is not None:
            step["depends_on"] = self.depends_on
        
        return step


def create_cuda_build_config(label: str, key: str, image_tag: str, queue: str,
                             commit: str, depends_on: Optional[str] = None,
                             extra_build_args: Optional[Dict[str, str]] = None,
                             push_latest: bool = False, latest_tag: Optional[str] = None,
                             dockerfile: str = "docker/Dockerfile",
                             target: str = "test",
                             include_sccache: bool = True,
                             retry_limit: int = 2) -> DockerBuildConfig:
    """Create a CUDA build configuration with common defaults."""
    build_args = {
        "max_jobs": "16",
        "buildkite_commit": commit
    }
    
    # Only add SCCACHE for non-CPU builds
    if include_sccache:
        build_args["USE_SCCACHE"] = "1"
    
    if extra_build_args:
        build_args.update(extra_build_args)
    
    return DockerBuildConfig(
        label=label,
        key=key,
        dockerfile=dockerfile,
        image_tag=image_tag,
        queue=queue,
        build_args=build_args,
        depends_on=depends_on,
        target=target,
        push_latest=push_latest,
        latest_tag=latest_tag,
        retry_limit=retry_limit
    )


@dataclass  
class AMDBuildConfig:
    """Configuration for AMD Docker build."""
    image_tag: str
    commit: str
    mirror_hw: str = ""  # For fastcheck label
    is_fastcheck: bool = False
    
    def get_build_command(self) -> str:
        """Get AMD docker build command."""
        # Use $BUILDKITE_COMMIT variable
        image_tag = self.image_tag.replace(self.commit, "$BUILDKITE_COMMIT")
        return (
            "docker build "
            "--build-arg max_jobs=16 "
            "--build-arg REMOTE_VLLM=1 "
            "--build-arg ARG_PYTORCH_ROCM_ARCH='gfx90a;gfx942' "
            "--build-arg VLLM_BRANCH=$BUILDKITE_COMMIT "
            f"--tag {image_tag} "
            "-f docker/Dockerfile.rocm "
            "--target test "
            "--no-cache "
            "--progress plain ."
        )
    
    def to_buildkite_step(self) -> Dict[str, Any]:
        """Convert to Buildkite step."""
        # Use $BUILDKITE_COMMIT variable
        image_tag = self.image_tag.replace(self.commit, "$BUILDKITE_COMMIT")
        
        # Fastcheck includes mirror_hw in label
        if self.is_fastcheck and self.mirror_hw:
            label = f"AMD: :docker: build image with {self.mirror_hw}"
        else:
            label = "AMD: :docker: build image"
        
        # Fastcheck has soft_fail: true (not false)
        soft_fail = True if self.is_fastcheck else False
        
        return {
            "label": label,
            "key": "amd-build",
            "depends_on": None,
            "agents": {"queue": "amd-cpu"},
            "env": {"DOCKER_BUILDKIT": "1"},
            "soft_fail": soft_fail,
            "retry": {
                "automatic": [
                    {"exit_status": -1, "limit": 1},
                    {"exit_status": -10, "limit": 1},
                    {"exit_status": 1, "limit": 1}
                ]
            },
            "commands": [
                self.get_build_command(),
                f"docker push {image_tag}"
            ]
        }


def create_main_cuda_build(commit: str, image_tag: str, queue: str, branch: str, 
                           vllm_use_precompiled: str, retry_limit: int = 2,
                           push_latest: Optional[bool] = None,
                           include_cuda_arch: bool = True) -> DockerBuildConfig:
    """Create main CUDA build configuration."""
    extra_args = {
        "TORCH_CUDA_ARCH_LIST": "8.0 8.9 9.0 10.0",
        "FI_TORCH_CUDA_ARCH_LIST": "8.0 8.9 9.0a 10.0a"
    }
    
    if branch != "main":
        extra_args["VLLM_USE_PRECOMPILED"] = vllm_use_precompiled
        extra_args["VLLM_DOCKER_BUILD_CONTEXT"] = "1"
        if vllm_use_precompiled == "1":
            extra_args["USE_FLASHINFER_PREBUILT_WHEEL"] = "true"
    
    # Create latest tag - replace $BUILDKITE_COMMIT with latest
    latest_tag = image_tag.replace("$BUILDKITE_COMMIT", "latest") if branch == "main" else None
    
    # Determine if we should push latest
    if push_latest is None:
        push_latest = (branch == "main")
    
    return create_cuda_build_config(
        label=":docker: build image",
        key="image-build",
        image_tag=image_tag,
        queue=queue,
        commit=commit,
        depends_on=None,
        extra_build_args=extra_args,
        push_latest=push_latest,
        latest_tag=latest_tag,
        retry_limit=retry_limit
    )


def create_fastcheck_build(commit: str, image_tag: str, queue: str,
                           vllm_use_precompiled: str) -> DockerBuildConfig:
    """Create fastcheck build configuration."""
    extra_args = {
        "VLLM_DOCKER_BUILD_CONTEXT": "1",
        "VLLM_USE_PRECOMPILED": vllm_use_precompiled
    }
    
    if vllm_use_precompiled == "1":
        extra_args["USE_FLASHINFER_PREBUILT_WHEEL"] = "true"
    
    config = create_cuda_build_config(
        label=":docker: build image",
        key="image-build",
        image_tag=image_tag,
        queue=queue,
        commit=commit,
        depends_on=None,
        extra_build_args=extra_args,
        push_latest=False,  # Fastcheck never pushes latest
        latest_tag=None,
        retry_limit=5  # Fastcheck uses 5
    )
    
    # Enable fastcheck formatting
    config.use_fastcheck_formatting = True
    return config


def create_cu118_build(commit: str, image_tag: str, queue: str, branch: str,
                       vllm_use_precompiled: str) -> DockerBuildConfig:
    """Create CUDA 11.8 build configuration."""
    extra_args = {
        "CUDA_VERSION": "11.8.0"
    }
    
    if branch != "main":
        extra_args["VLLM_USE_PRECOMPILED"] = vllm_use_precompiled
        extra_args["VLLM_DOCKER_BUILD_CONTEXT"] = "1"
        if vllm_use_precompiled == "1":
            extra_args["USE_FLASHINFER_PREBUILT_WHEEL"] = "true"
    
    return create_cuda_build_config(
        label=":docker: build image CUDA 11.8",
        key="image-build-cu118",
        image_tag=image_tag,
        queue=queue,
        commit=commit,
        depends_on="block-build-cu118",
        extra_build_args=extra_args
    )


def create_cpu_build(commit: str, image_tag: str, queue: str) -> DockerBuildConfig:
    """Create CPU build configuration."""
    extra_args = {
        "VLLM_CPU_AVX512BF16": "true",
        "VLLM_CPU_AVX512VNNI": "true"
    }
    
    return create_cuda_build_config(
        label=":docker: build image CPU",
        key="image-build-cpu",
        image_tag=image_tag,
        queue=queue,
        commit=commit,
        depends_on=None,
        extra_build_args=extra_args,
        dockerfile="docker/Dockerfile.cpu",
        target="vllm-test",
        include_sccache=False  # CPU build doesn't use SCCACHE
    )


def create_torch_nightly_build(commit: str, image_tag: str, queue: str, depends_on: Optional[str]) -> DockerBuildConfig:
    """Create torch nightly build configuration."""
    return create_cuda_build_config(
        label=":docker: build image torch nightly",
        key="image-build-torch-nightly",
        image_tag=image_tag,
        queue=queue,
        commit=commit,
        depends_on=depends_on,
        dockerfile="docker/Dockerfile.nightly_torch",
        target="test"
    )

