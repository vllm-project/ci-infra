from typing import Dict, List, Optional

from .utils import GPUType, get_agent_queue, get_full_test_command, get_multi_node_test_command
from .step import TestStep, BuildkiteStep, get_step_key
from .plugin import get_docker_plugin_config, get_kubernetes_plugin_config

def get_plugin_config(
        container_image: str,
        no_gpu: Optional[bool] = None,
        gpu_type: Optional[GPUType] = None,
        num_gpus: Optional[int] = None
    ) -> Dict:
    """Returns the plugin configuration for the Buildkite step."""
    if gpu_type and gpu_type == GPUType.A100 and num_gpus:
        return get_kubernetes_plugin_config(
            container_image,
            num_gpus
        )
    return get_docker_plugin_config(
        container_image,
        no_gpu or False,
    )


def convert_test_step_to_buildkite_step(step: TestStep, container_image: str) -> BuildkiteStep:
    """Convert TestStep into BuildkiteStep."""
    buildkite_step = BuildkiteStep(
        label=step.label,
        key=get_step_key(step.label),
        commands=step.commands,
        parallelism=step.parallelism,
        soft_fail=step.soft_fail,
        plugins=[get_plugin_config(container_image, step.no_gpu, step.gpu, step.num_gpus)],
        agents={"queue": get_agent_queue(step.no_gpu, step.gpu, step.num_gpus).value}
    )
    # If test is multi-node, configure step to run with custom script
    if step.num_nodes and step.num_nodes > 1:
        buildkite_step.commands = [get_multi_node_test_command(
                step.commands,
                step.working_dir,
                step.num_nodes,
                step.num_gpus,
                container_image
            )
        ]
        buildkite_step.plugins = None
    return buildkite_step

def get_build_commands(container_registry: str, buildkite_commit: str, container_image: str) -> List[str]:
    ecr_login_command = (
        "aws ecr-public get-login-password --region us-east-1 | "
        f"docker login --username AWS --password-stdin {container_registry}"
    )
    image_check_command = f"""#!/bin/bash
if [[ -z $(docker manifest inspect {container_image}) ]]; then
echo "Image not found, proceeding with build..."
else
echo "Image found"
exit 0
fi
"""
    docker_build_command = (
        f"docker build "
        f"--file docker/Dockerfile "
        f"--build-arg max_jobs=64 "
        f"--build-arg buildkite_commit={buildkite_commit} "
        f"--build-arg USE_SCCACHE=1 "
        f"--tag {container_image} "
        f"--target test "
        f"--progress plain ."
    )
    # TODO: Stop using . in docker build command
    docker_push_command = f"docker push {container_image}"
    return [ecr_login_command, image_check_command, docker_build_command, docker_push_command]
