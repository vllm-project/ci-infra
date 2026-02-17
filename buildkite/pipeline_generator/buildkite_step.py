from pydantic import BaseModel
from typing import Dict, List, Optional, Any, Union
from step import Step
from utils_lib.docker_utils import get_image, get_ecr_cache_registry
from global_config import get_global_config
from plugin.k8s_plugin import get_k8s_plugin
from plugin.docker_plugin import get_docker_plugin
from constants import DeviceType, AgentQueue


class BuildkiteCommandStep(BaseModel):
    label: str
    group: Optional[str] = None
    key: Optional[str] = None
    agents: Dict[str, str] = {}
    commands: List[str] = []
    depends_on: Optional[List[str]] = None
    soft_fail: Optional[bool] = False
    retry: Optional[Dict[str, Any]] = None
    plugins: Optional[List[Dict[str, Any]]] = None
    env: Optional[Dict[str, str]] = None
    parallelism: Optional[int] = None
    priority: Optional[int] = None

    def to_yaml(self):
        return {
            "label": self.label,
            "group": self.group,
            "commands": self.commands,
            "depends_on": self.depends_on,
            "soft_fail": self.soft_fail,
            "retry": self.retry,
            "plugins": self.plugins,
            "env": self.env,
            "parallelism": self.parallelism,
            "priority": self.priority,
        }


class BuildkiteBlockStep(BaseModel):
    block: str
    depends_on: Optional[Union[str, List[str]]] = None
    key: Optional[str] = None

    def to_yaml(self):
        return {"block": self.block, "depends_on": self.depends_on, "key": self.key}


class BuildkiteGroupStep(BaseModel):
    group: str
    steps: List[Union[BuildkiteCommandStep, BuildkiteBlockStep]]


def _get_step_plugin(step: Step):
    # Use K8s plugin
    use_cpu = step.device == DeviceType.CPU or False
    if step.device in [DeviceType.H100.value, DeviceType.A100.value]:
        return get_k8s_plugin(step, get_image(use_cpu))
    else:
        return {"docker#v5.2.0": get_docker_plugin(step, get_image(use_cpu))}


def get_agent_queue(step: Step):
    branch = get_global_config()["branch"]
    if step.label.startswith(":docker:"):
        if branch == "main":
            return AgentQueue.CPU_POSTMERGE_US_EAST_1
        else:
            return AgentQueue.CPU_PREMERGE_US_EAST_1
    elif step.label == "Documentation Build":
        return AgentQueue.SMALL_CPU_PREMERGE
    elif step.device == DeviceType.CPU:
        return AgentQueue.CPU_PREMERGE_US_EAST_1
    elif step.device == DeviceType.A100:
        return AgentQueue.A100
    elif step.device == DeviceType.H100:
        return AgentQueue.MITHRIL_H100
    elif step.device == DeviceType.H200:
        return AgentQueue.SKYLAB_H200
    elif step.device == DeviceType.B200:
        return AgentQueue.B200
    elif step.device == DeviceType.INTEL_CPU:
        return AgentQueue.INTEL_CPU
    elif step.device == DeviceType.INTEL_HPU:
        return AgentQueue.INTEL_HPU
    elif step.device == DeviceType.INTEL_GPU:
        return AgentQueue.INTEL_GPU
    elif step.device == DeviceType.ARM_CPU:
        return AgentQueue.ARM_CPU
    elif step.device == DeviceType.AMD_CPU or step.device == DeviceType.AMD_CPU.value:
        return AgentQueue.AMD_CPU
    elif step.device == DeviceType.GH200:
        return AgentQueue.GH200
    elif step.device == DeviceType.ASCEND:
        return AgentQueue.ASCEND
    elif step.num_devices == 2 or step.num_devices == 4:
        return AgentQueue.GPU_4
    else:
        return AgentQueue.GPU_1


def _get_variables_to_inject() -> Dict[str, str]:
    global_config = get_global_config()
    if global_config["name"] != "vllm_ci":
        return {}

    cache_from_tag, cache_to_tag = get_ecr_cache_registry()
    return {
        "$REGISTRY": global_config["registries"],
        "$REPO": global_config["repositories"]["main"]
        if global_config["branch"] == "main"
        else global_config["repositories"]["premerge"],
        "$BUILDKITE_COMMIT": "$$BUILDKITE_COMMIT",
        "$BRANCH": global_config["branch"],
        "$VLLM_USE_PRECOMPILED": "1" if global_config["use_precompiled"] else "0",
        # Only pass merge base commit when using precompiled wheels. When
        # building from source (use_precompiled=False), this arg is unused but
        # participates in Docker's layer cache key — passing a different hash
        # on every build invalidates the ~11min compilation layer.
        "$VLLM_MERGE_BASE_COMMIT": global_config["merge_base_commit"]
        if global_config["use_precompiled"]
        else '""',
        "$CACHE_FROM": cache_from_tag,
        "$CACHE_TO": cache_to_tag,
        "$IMAGE_TAG": f"{global_config['registries']}/{global_config['repositories']['main']}:$BUILDKITE_COMMIT"
            if global_config["branch"] == "main"
            else f"{global_config['registries']}/{global_config['repositories']['premerge']}:$BUILDKITE_COMMIT",
        "$IMAGE_TAG_LATEST": f"{global_config['registries']}/{global_config['repositories']['main']}:latest"
            if global_config["branch"] == "main"
            else None,
    }


def _prepare_commands(step: Step, variables_to_inject: Dict[str, str]) -> List[str]:
    """Prepare step commands with variables injected and default setup commands."""
    commands = []
    # Default setup commands
    if not step.label.startswith(":docker:") and not step.no_plugin:
        commands.append("(command nvidia-smi || true)")
        commands.append("export CUDA_ENABLE_COREDUMP_ON_EXCEPTION=1 && export CUDA_COREDUMP_SHOW_PROGRESS=1 && export CUDA_COREDUMP_GENERATION_FLAGS='skip_nonrelocated_elf_images,skip_global_memory,skip_shared_memory,skip_local_memory,skip_constbank_memory'")

    if step.commands:
        commands.extend(step.commands)

    final_commands = []
    for command in commands:
        if not step.num_nodes:
            command = command.replace("'", '"')
        for variable, value in variables_to_inject.items():
            if not value:
                continue
            # Use regex to only replace whole variable matches (not substrings)
            import re
            # Escape variable (may have $ or special characters)
            pattern = re.escape(variable)
            command = re.sub(pattern + r'\b', value, command)
        final_commands.append(command)

    if step.working_dir and not (
        step.label.startswith(":docker:") or (step.num_nodes and step.num_nodes >= 2)
    ):
        final_commands.insert(0, f"cd {step.working_dir}")

    return final_commands


def _create_block_step(step: Step, list_file_diff: List[str]) -> BuildkiteBlockStep:
    block_step = BuildkiteBlockStep(
        block=f"Run {step.label}",
        depends_on=[],
        key=f"block-{_generate_step_key(step.label)}",
    )
    if step.label.startswith(":docker:"):
        block_step.depends_on = []
    return block_step


def convert_group_step_to_buildkite_step(
    group_steps: Dict[str, List[Step]],
) -> List[BuildkiteGroupStep]:
    buildkite_group_steps = []
    variables_to_inject = _get_variables_to_inject()
    print(variables_to_inject)
    global_config = get_global_config()
    list_file_diff = global_config["list_file_diff"]

    amd_mirror_steps = []

    for group, steps in group_steps.items():
        group_steps_list = []
        for step in steps:
            # block step
            block_step = None
            if not _step_should_run(step, list_file_diff):
                block_step = _create_block_step(step, list_file_diff)
            if block_step:
                group_steps_list.append(block_step)

            # command step
            step_commands = _prepare_commands(step, variables_to_inject)

            buildkite_step = BuildkiteCommandStep(
                label=step.label,
                commands=step_commands,
                depends_on=step.depends_on,
                soft_fail=step.soft_fail,
                agents={"queue": get_agent_queue(step)},
            )

            if block_step:
                buildkite_step.depends_on = [block_step.key]
                if step.depends_on:
                    buildkite_step.depends_on.extend(step.depends_on)
            if step.env:
                buildkite_step.env = step.env
            if step.retry:
                buildkite_step.retry = step.retry
            if step.key:
                buildkite_step.key = step.key
            if step.parallelism:
                buildkite_step.parallelism = step.parallelism

            # add plugin
            if not step.no_plugin and not (
                step.label.startswith(":docker:")
                or (step.num_nodes and step.num_nodes >= 2)
            ):
                buildkite_step.plugins = [_get_step_plugin(step)]

            group_steps_list.append(buildkite_step)

            # Create AMD mirror step and its block step if specified/applicable
            if step.mirror and step.mirror.get("amd"):
                amd_block_step = None
                if not _step_should_run(step, list_file_diff):
                    amd_block_step = BuildkiteBlockStep(
                        block=f"Run AMD: {step.label}",
                        depends_on=["image-build-amd"],
                        key=f"block-amd-{_generate_step_key(step.label)}",
                    )
                    amd_mirror_steps.append(amd_block_step)
                amd_step = _create_amd_mirror_step(step, step_commands, step.mirror["amd"])
                if amd_block_step:
                    amd_step.depends_on.extend([amd_block_step.key])
                amd_mirror_steps.append(amd_step)

        buildkite_group_steps.append(
            BuildkiteGroupStep(group=group, steps=group_steps_list)
        )

    # If AMD mirror step exists, make it a group step
    if amd_mirror_steps:
        buildkite_group_steps.append(
            BuildkiteGroupStep(group="Hardware-AMD Tests", steps=amd_mirror_steps)
        )

    return buildkite_group_steps


def _step_should_run(step: Step, list_file_diff: List[str]) -> bool:
    global_config = get_global_config()
    if step.key and step.key.startswith("image-build"):
        return True
    if global_config["nightly"] == "1":
        return True
    if step.optional:
        return False
    if global_config["run_all"]:
        return True
    if step.source_file_dependencies:
        for source_file in step.source_file_dependencies:
            for diff_file in list_file_diff:
                if source_file in diff_file:
                    return True
    return False


def _generate_step_key(step_label: str) -> str:
    return (
        step_label.replace(" ", "-")
        .lower()
        .replace("(", "")
        .replace(")", "")
        .replace("%", "")
        .replace(",", "-")
        .replace("+", "-")
        .replace(":", "-")
        .replace(".", "-")
        .replace("/", "-")
    )


def _create_amd_mirror_step(step: Step, original_commands: List[str], amd: Dict[str, Any]) -> BuildkiteCommandStep:
    """Create an AMD mirrored step from the original step."""
    amd_device = amd["device"]
    amd_commands = amd.get("commands", original_commands)
    amd_commands_str = " && ".join(amd_commands)
    working_dir = amd.get("working_dir", step.working_dir)
    if working_dir:
        amd_commands_str = f"cd {working_dir} && {amd_commands_str}"

    # Add AMD test script wrapper
    amd_command_wrapped = f'bash .buildkite/scripts/hardware_ci/run-amd-test.sh "{amd_commands_str}"'

    # Extract device name from queue name
    device_type = amd_device.replace("amd_", "") if amd_device.startswith("amd_") else amd_device
    amd_label = f"AMD: {step.label} ({device_type})"

    # Get AMD queue name from device name
    amd_queue = None
    if amd_device == DeviceType.AMD_MI325_1:
        amd_queue = AgentQueue.AMD_MI325_1
    elif amd_device == DeviceType.AMD_MI325_8:
        amd_queue = AgentQueue.AMD_MI325_8
    
    if not amd_queue:
        raise ValueError(f"Invalid device: {amd_device}")

    amd_retry = {
        "automatic": [
            {"exit_status": -1, "limit": 2},   # Agent was lost
            {"exit_status": -10, "limit": 2},  # Agent was lost
            {"exit_status": 128, "limit": 2},  # Git connectivity issues
        ]
    }

    return BuildkiteCommandStep(
        label=amd_label,
        commands=[amd_command_wrapped],
        depends_on=["image-build-amd"],
        agents={"queue": amd_queue},
        env={"DOCKER_BUILDKIT": "1"},
        priority=200,
        soft_fail=False,
        retry=amd_retry,
        parallelism=step.parallelism,
    )
