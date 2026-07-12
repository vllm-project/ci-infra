from pydantic import BaseModel
from typing import Dict, List, Optional, Any, Union, Literal
import os

from step import Step
from utils_lib.docker_utils import get_image, get_ecr_cache_registry, get_torch_nightly_image
from global_config import get_global_config
from plugin.k8s_plugin import get_k8s_plugin
from plugin.docker_plugin import get_docker_plugin
from constants import DeviceType, AgentQueue

# Key for the dedicated pre-commit step. Test steps that depend on an image
# build also depend on this so pre-commit and image build can run in parallel.
PRECOMMIT_STEP_KEY = "pre-commit"
PRECOMMIT_MAX_WAIT = 3600  # 30 minutes
PRECOMMIT_WAIT_INTERVAL = 60

AMD_TEST_COMMAND = "bash .buildkite/scripts/hardware_ci/run-amd-test.sh"
AMD_RETRY = {
    "automatic": [
        {"exit_status": -1, "limit": 1},
        {"exit_status": 1, "limit": 1},
        {"exit_status": 128, "limit": 1},
        {"signal_reason": "agent_stop", "limit": 1},
        {"signal_reason": "agent_refused", "limit": 1},
    ],
}
ROCM_DEBUG_AGENT_ENV_VAR = "VLLM_CI_ENABLE_ROCM_DEBUG_AGENT"
ROCM_DEBUG_AGENT_LIB = "/opt/rocm/lib/librocm-debug-agent.so.2"
ALWAYS_RUN_STEP_KEYS = {
    "ensure-ci-base-amd",
    "refresh-rocm-base-amd",
}


def _get_rocm_debug_agent_setup_command() -> str:
    if os.getenv(ROCM_DEBUG_AGENT_ENV_VAR, "0") != "1":
        return (
            "echo 'ROCm debug agent disabled; set "
            f"{ROCM_DEBUG_AGENT_ENV_VAR}=1 at pipeline generation time "
            "to enable coredump setup'"
        )

    return (
        f"if test -f {ROCM_DEBUG_AGENT_LIB}; then "
        f"export HSA_TOOLS_LIB={ROCM_DEBUG_AGENT_LIB} && export HSA_ENABLE_DEBUG=1 "
        f"&& echo ROCm debug agent enabled: {ROCM_DEBUG_AGENT_LIB}; "
        f"else echo 'WARNING: ROCm debug agent not found at {ROCM_DEBUG_AGENT_LIB}; "
        "skipping coredump setup'; "
        "fi"
    )


# Self-contained poll of the pre-commit GitHub Actions check run. Baked with the
# commit/repo at generation time and run on a CI agent as its own step, so it
# carries no shell variables (nothing for `buildkite-agent pipeline upload` to
# interpolate) and needs no third-party Python packages on the agent.
_PRECOMMIT_POLL_PROGRAM = """\
import json, subprocess, time, urllib.request
commit = "{commit}"
repo = "{repo}"
api_url = "https://api.github.com/repos/" + repo + "/commits/" + commit + "/check-runs"
max_wait = {max_wait}
wait_interval = {wait_interval}
elapsed = 0
while True:
    request = urllib.request.Request(api_url)
    request.add_header("User-Agent", "vllm-ci-precommit-check")
    request.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(request) as response:
        check_runs = json.load(response).get("check_runs", [])
    precommit_run = next((run for run in check_runs if run["name"] == "pre-commit"), None)
    if precommit_run and precommit_run["status"] == "completed":
        conclusion = precommit_run["conclusion"]
        if conclusion == "success":
            print("pre-commit check passed on commit " + commit + ".")
            raise SystemExit(0)
        # Only block downstream tests on a confirmed pre-commit failure. Other
        # conclusions (skipped, cancelled, neutral, stale, ...) are not real
        # failures, and pre-commit is also a required check on the PR, so let CI
        # proceed rather than false-failing the whole build.
        if conclusion in ("failure", "timed_out", "action_required"):
            subprocess.run(["buildkite-agent", "annotate", ":x: pre-commit check failed on this PR (conclusion: " + str(conclusion) + "). Please fix pre-commit issues before running CI.", "--style", "error"], check=False)
            raise SystemExit("pre-commit check failed on commit " + commit + " (conclusion: " + str(conclusion) + ").")
        print("pre-commit check did not fail (conclusion: " + str(conclusion) + "); allowing CI to proceed.")
        raise SystemExit(0)
    if elapsed >= max_wait:
        # Do not block CI if the check never completes; the pre-commit check on
        # the PR still surfaces a genuine failure independently.
        subprocess.run(["buildkite-agent", "annotate", ":warning: Timed out waiting for the pre-commit check to complete; allowing CI to proceed. See the pre-commit check on the PR for its status.", "--style", "warning"], check=False)
        print("Timed out after " + str(max_wait) + "s waiting for pre-commit check on commit " + commit + "; allowing CI to proceed.")
        raise SystemExit(0)
    status = precommit_run["status"] if precommit_run else "not found"
    print("pre-commit check is not yet complete (status: " + status + "). Waiting " + str(wait_interval) + "s... (" + str(elapsed) + "/" + str(max_wait) + "s)")
    time.sleep(wait_interval)
    elapsed += wait_interval
"""


def create_precommit_group_step(repo_name: str, commit: str) -> "BuildkiteGroupStep":
    """Dedicated step that waits on the pre-commit GitHub Actions check.

    It has no dependencies so it runs in parallel with the image build. It only
    fails (and so blocks the downstream tests that depend on it) on a confirmed
    pre-commit failure; benign conclusions (skipped, cancelled, ...) and a poll
    timeout let CI proceed, since pre-commit is also a required check on the PR.
    """
    program = _PRECOMMIT_POLL_PROGRAM.format(
        commit=commit,
        repo=repo_name,
        max_wait=PRECOMMIT_MAX_WAIT,
        wait_interval=PRECOMMIT_WAIT_INTERVAL,
    )
    precommit_step = BuildkiteCommandStep(
        label=":github: GitHub pre-commit check",
        key=PRECOMMIT_STEP_KEY,
        commands=["python3 -c '" + program + "'"],
        depends_on=[],
        agents={"queue": AgentQueue.SMALL_CPU_PREMERGE},
        priority=1000 if os.getenv("PRIORITY", "") == "HIGH" else 0,
    )
    return BuildkiteGroupStep(
        group="GitHub pre-commit check", steps=[precommit_step]
    )


def add_precommit_dependency(
    buildkite_group_steps: List["BuildkiteGroupStep"],
) -> None:
    """Make every step that depends on an image build also depend on pre-commit.

    Image build steps themselves are left untouched so they keep running in
    parallel with pre-commit.
    """
    for group_step in buildkite_group_steps:
        for step in group_step.steps:
            if not isinstance(step, BuildkiteCommandStep) or not step.depends_on:
                continue
            # Don't gate the image build steps themselves on pre-commit.
            if step.key and "image-build" in step.key:
                continue
            if not any("image-build" in dep for dep in step.depends_on):
                continue
            if PRECOMMIT_STEP_KEY not in step.depends_on:
                step.depends_on.append(PRECOMMIT_STEP_KEY)


def _get_step_agents(step: Step) -> Dict[str, str]:
    agents = {"queue": get_agent_queue(step)}
    if step.device == DeviceType.INTEL_GPU and step.agent_tags:
        agents.update(
            {key: value for key, value in step.agent_tags.items() if key != "queue"}
        )
    return agents


SetupProfile = Literal["nvidia", "amd", "none"]


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
    timeout_in_minutes: Optional[int] = None
    priority: Optional[int] = None

    def to_yaml(self):
        return {
            "label": self.label,
            "key": self.key,
            "group": self.group,
            "agents": self.agents,
            "commands": self.commands,
            "depends_on": self.depends_on,
            "soft_fail": self.soft_fail,
            "retry": self.retry,
            "plugins": self.plugins,
            "env": self.env,
            "parallelism": self.parallelism,
            "timeout_in_minutes": self.timeout_in_minutes,
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
    use_cpu = step.device in (DeviceType.CPU, DeviceType.CPU_SMALL, DeviceType.CPU_MEDIUM)
    use_arm64 = step.device == DeviceType.DGX_SPARK
    if step.device in [
        DeviceType.H100.value,
        DeviceType.A100.value,
        DeviceType.B200_K8S.value,
    ]:
        return get_k8s_plugin(step, get_image(use_cpu))
    else:
        return {"docker#v5.2.0": get_docker_plugin(step, get_image(use_cpu, use_arm64))}


def _device_value(device: Optional[str]) -> Optional[str]:
    if isinstance(device, DeviceType):
        return device.value
    return device


def _get_amd_gpu_agent_queue(device: Optional[str]) -> Optional[AgentQueue]:
    device_value = _device_value(device)
    if not device_value:
        return None
    try:
        return AgentQueue(f"amd_{device_value}")
    except ValueError:
        return None


def get_agent_queue(step: Step):
    branch = get_global_config()["branch"]
    if step.label.startswith(":docker:"):
        if "arm64" in step.label:
            if branch == "main":
                return AgentQueue.ARM64_CPU_POSTMERGE
            else:
                return AgentQueue.ARM64_CPU_PREMERGE
        if branch == "main":
            return AgentQueue.CPU_POSTMERGE_US_EAST_1
        else:
            return AgentQueue.CPU_PREMERGE_US_EAST_1
    elif step.label == "Documentation Build":
        return AgentQueue.SMALL_CPU_PREMERGE
    elif step.device == DeviceType.CPU_SMALL:
        return AgentQueue.SMALL_CPU_PREMERGE
    elif step.device == DeviceType.CPU_MEDIUM:
        return AgentQueue.MEDIUM_CPU_PREMERGE
    elif step.device == DeviceType.CPU:
        return AgentQueue.CPU_PREMERGE_US_EAST_1
    elif step.device == DeviceType.A100:
        return AgentQueue.A100
    elif step.device == DeviceType.H100:
        # Route multi-GPU H100 tests to RedHat Frankfurt queue
        if step.num_devices is not None and step.num_devices >= 4:
            return AgentQueue.MITHRIL_H100
        else:
            return AgentQueue.MITHRIL_H100
    elif step.device == DeviceType.H200:
        return AgentQueue.H200
    elif step.device == DeviceType.H200_18GB:
        return AgentQueue.H200_18GB
    elif step.device == DeviceType.H200_35GB:
        return AgentQueue.H200_35GB
    elif step.device == DeviceType.B200:
        return AgentQueue.B200
    elif step.device == DeviceType.B200_K8S:
        return AgentQueue.B200_K8S
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
    elif amd_gpu_queue := _get_amd_gpu_agent_queue(step.device):
        return amd_gpu_queue
    elif step.device == DeviceType.GH200:
        return AgentQueue.GH200
    elif step.device == DeviceType.ASCEND:
        return AgentQueue.ASCEND
    elif step.device == DeviceType.DGX_SPARK:
        return AgentQueue.DGX_SPARK
    elif step.num_devices == 2 or step.num_devices == 4:
        return AgentQueue.GPU_4
    else:
        return AgentQueue.GPU_1


def _is_amd_gpu_device(device: Optional[str]) -> bool:
    return _get_amd_gpu_agent_queue(device) is not None


def _valid_amd_gpu_devices() -> List[str]:
    return [
        device.value
        for device in DeviceType
        if _get_amd_gpu_agent_queue(device.value) is not None
    ]


def _get_amd_label(label: str, amd_device: Optional[str]) -> str:
    device_value = _device_value(amd_device) or ""
    device_type = (
        device_value.replace("amd_", "")
        if device_value.startswith("amd_")
        else device_value
    )
    return f"AMD: {label} ({device_type})"


def _normalize_amd_depends_on(depends_on: Optional[List[str]]) -> List[str]:
    normalized = []
    for dependency in depends_on or []:
        if dependency == "image-build":
            dependency = "image-build-amd"
        if dependency not in normalized:
            normalized.append(dependency)
    if "image-build-amd" not in normalized:
        normalized.insert(0, "image-build-amd")
    return normalized


def _get_amd_env(
    commands: str,
    extra_env: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    env = dict(extra_env or {})
    env.update(
        {
            "DOCKER_BUILDKIT": "1",
            # Agent hooks read DOCKER_IMAGE_NAME before run-amd-test.py starts.
            # Keep the hook warmup on ci_base; the runner uses the full image
            # only if ci_base or artifact setup fails before tests begin.
            "DOCKER_IMAGE_NAME": "rocm/vllm-dev:ci_base",
            "VLLM_CI_BASE_IMAGE": "rocm/vllm-dev:ci_base",
            "VLLM_CI_FALLBACK_IMAGE": "rocm/vllm-ci:$BUILDKITE_COMMIT",
            "VLLM_CI_USE_ARTIFACTS": "1",
            "VLLM_CI_ARTIFACT_GLOB": (
                "artifacts/vllm-rocm-install/vllm-rocm-install.tar.gz"
            ),
            "VLLM_CI_RESULTS_ROOT": (
                "/home/buildkite-agent/huggingface/amd-ci-results"
            ),
            "VLLM_TEST_COMMANDS": commands,
        }
    )
    return env


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
        "$CACHE_FROM": cache_from_tag,
        "$CACHE_TO": cache_to_tag,
        "$IMAGE_TAG": f"{global_config['registries']}/{global_config['repositories']['main']}:$BUILDKITE_COMMIT"
            if global_config["branch"] == "main"
            else f"{global_config['registries']}/{global_config['repositories']['premerge']}:$BUILDKITE_COMMIT",
        "$IMAGE_TAG_LATEST": f"{global_config['registries']}/{global_config['repositories']['main']}:latest"
            if global_config["branch"] == "main"
            else None,
        "$IMAGE_TAG_TORCH_NIGHTLY": get_torch_nightly_image(),
    }


def _is_multi_gpu_step(step: Step) -> bool:
    """Whether a step exercises more than one GPU.

    Multi-GPU (tensor/pipeline parallel) and multi-node steps rely on the
    cross-GPU fabric (NVLink/NVSwitch) and P2P being healthy on whichever node
    they land on. Single-GPU steps don't, so the topology dump is only worth the
    log noise for these.
    """
    if step.num_nodes and step.num_nodes >= 2:
        return True
    return bool(step.num_devices and step.num_devices >= 2)


def _get_setup_commands(step: Step, setup_profile: SetupProfile) -> List[str]:
    if step.label.startswith(":docker:") or step.no_plugin or setup_profile == "none":
        return []

    if setup_profile == "nvidia":
        commands = [
            "echo '--- :nvidia: GPU Info'",
            "(command nvidia-smi || true)",
        ]
        if _is_multi_gpu_step(step):
            # Dump the GPU/NVLink topology for multi-GPU jobs so infra flakes on
            # a node with a degraded fabric (unhealthy NVSwitch/fabric-manager,
            # missing P2P) are visible in the log and distinguishable from real
            # regressions. Cheap and never fails the step.
            commands += [
                "echo '--- :nvidia: GPU Topology'",
                "(command nvidia-smi topo -m || true)",
            ]
        commands += [
            "echo '--- :gear: CUDA Coredump Setup'",
            "export CUDA_ENABLE_COREDUMP_ON_EXCEPTION=1 && export CUDA_COREDUMP_SHOW_PROGRESS=1 && export CUDA_COREDUMP_GENERATION_FLAGS='skip_nonrelocated_elf_images,skip_global_memory,skip_shared_memory,skip_local_memory,skip_constbank_memory'",
        ]
        return commands

    if setup_profile == "amd":
        return [
            "echo '--- :amd: GPU Info'",
            "(command amd-smi || true)",
            "echo '--- :gear: ROCm Debug Agent Setup'",
            _get_rocm_debug_agent_setup_command(),
        ]

    raise ValueError(f"Unsupported setup profile: {setup_profile}")


def _prepare_commands(
    step: Step,
    variables_to_inject: Dict[str, str],
    setup_profile: SetupProfile = "nvidia",
) -> List[str]:
    """Prepare step commands with variables injected and default setup commands."""
    commands = _get_setup_commands(step, setup_profile)

    continue_on_failure = os.getenv("CONTINUE_ON_FAILURE") == "1"

    if continue_on_failure:
        commands.append("CI_OVERALL_STATUS=0")

    if step.commands:
        for i, cmd in enumerate(step.commands):
            # Sanitize command preview for use in echo (remove quotes and special chars)
            preview = cmd[:80].replace("'", "").replace('"', '').replace('$', '')
            commands.append(f"echo '+++ :test_tube: Command ({i+1}/{len(step.commands)}): {preview}'")
            if continue_on_failure:
                # Note: We don't use a subshell here to preserve environment changes between commands
                # (export, cd, etc).
                commands.append(f"{{ {cmd}\n}} || CI_OVERALL_STATUS=1")
            else:
                commands.append(cmd)

    if continue_on_failure:
        commands.append("exit $$CI_OVERALL_STATUS")

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


def _create_block_step(
    *,
    block: str,
    key: str,
    command_step: BuildkiteCommandStep,
    depends_on: Optional[Union[str, List[str]]] = None,
    append_to_command_depends_on: bool = True,
) -> BuildkiteBlockStep:
    if isinstance(depends_on, list):
        block_depends_on = list(depends_on)
    else:
        block_depends_on = depends_on
    block_step = BuildkiteBlockStep(
        block=block,
        depends_on=block_depends_on,
        key=key,
    )
    command_depends_on = list(command_step.depends_on or [])
    if not append_to_command_depends_on:
        command_depends_on = [
            block_step.key,
            *[
                dependency
                for dependency in command_depends_on
                if dependency != block_step.key
            ],
        ]
    elif block_step.key not in command_depends_on:
        command_depends_on.append(block_step.key)
    command_step.depends_on = command_depends_on
    return block_step


def _matches_source_dependency(source_file: str, diff_file: str) -> bool:
    normalized = source_file.rstrip("/")
    if not normalized:
        return False
    return diff_file == normalized or diff_file.startswith(f"{normalized}/")


def convert_group_step_to_buildkite_step(
    group_steps: Dict[str, List[Step]],
) -> List[BuildkiteGroupStep]:
    buildkite_group_steps = []
    variables_to_inject = _get_variables_to_inject()
    print(variables_to_inject)
    global_config = get_global_config()
    list_file_diff = global_config["list_file_diff"]

    amd_hardware_steps = []

    for group, steps in group_steps.items():
        group_steps_list = []
        for step in steps:
            if _is_amd_gpu_device(step.device):
                amd_commands = [
                    "export VLLM_TEST_GROUP_NAME="
                    f"{_generate_step_key(step.key or step.label)}"
                ]
                amd_commands.extend(
                    _prepare_commands(
                        step,
                        variables_to_inject,
                        setup_profile="amd",
                    )
                )
                amd_step = _create_amd_step(
                    label=step.label,
                    key=step.key,
                    device=step.device,
                    commands_str=" && ".join(amd_commands),
                    depends_on=step.depends_on,
                    extra_env=step.env,
                    soft_fail=step.soft_fail,
                    parallelism=step.parallelism,
                    timeout_in_minutes=step.timeout_in_minutes,
                )
                if not _step_should_run(step, list_file_diff):
                    block_step = _create_block_step(
                        block=f"Run {amd_step.label}",
                        key=f"block-amd-{_generate_step_key(step.key or step.label)}",
                        command_step=amd_step,
                        depends_on=amd_step.depends_on,
                    )
                    amd_hardware_steps.append(block_step)
                amd_hardware_steps.append(amd_step)
                continue

            # command step
            step_commands = _prepare_commands(step, variables_to_inject)

            buildkite_step = BuildkiteCommandStep(
                label=step.label,
                commands=step_commands,
                depends_on=step.depends_on,
                soft_fail=step.soft_fail,
                agents=_get_step_agents(step),
                priority=1000 if os.getenv("PRIORITY", "") == "HIGH" else 0,
            )

            if step.env:
                buildkite_step.env = step.env
            if step.retry:
                buildkite_step.retry = step.retry
            if step.key:
                buildkite_step.key = step.key
            if step.parallelism:
                buildkite_step.parallelism = step.parallelism
            if step.timeout_in_minutes:
                buildkite_step.timeout_in_minutes = step.timeout_in_minutes

            if not _step_should_run(step, list_file_diff):
                block_step = _create_block_step(
                    block=f"Run {step.label}",
                    key=f"block-{_generate_step_key(step.label)}",
                    command_step=buildkite_step,
                    depends_on=[],
                    append_to_command_depends_on=False,
                )
                group_steps_list.append(block_step)

            # add plugin
            if not step.no_plugin and not (
                step.label.startswith(":docker:")
                or (step.num_nodes and step.num_nodes >= 2)
            ):
                buildkite_step.plugins = [_get_step_plugin(step)]

            group_steps_list.append(buildkite_step)

            # Create AMD mirror step and its block step if specified/applicable
            if step.mirror and step.mirror.get("amd"):
                amd = step.mirror["amd"]
                custom_commands = amd.get("commands")
                if custom_commands:
                    amd_command_step = step.model_copy(
                        update={
                            "commands": custom_commands,
                            "working_dir": amd.get("working_dir", step.working_dir),
                        }
                    )
                    amd_commands_str = " && ".join(
                        _prepare_commands(
                            amd_command_step,
                            variables_to_inject,
                            setup_profile="amd",
                        )
                    )
                else:
                    amd_commands_str = " && ".join(
                        _prepare_commands(
                            step,
                            variables_to_inject,
                            setup_profile="amd",
                        )
                    )

                extra_env = dict(step.env or {})
                extra_env.update(amd.get("env", {}))
                amd_step = _create_amd_step(
                    label=step.label,
                    device=amd["device"],
                    commands_str=amd_commands_str,
                    depends_on=amd.get("depends_on"),
                    extra_env=extra_env,
                    soft_fail=True,
                    parallelism=step.parallelism,
                    timeout_in_minutes=amd.get("timeout_in_minutes"),
                )
                if not _step_should_run(
                    _get_amd_mirror_effective_step(step, amd), list_file_diff
                ):
                    # Block step depends on the shared AMD image build.
                    mirror_build_dep = (
                        amd_step.depends_on[0]
                        if amd_step.depends_on
                        else "image-build-amd"
                    )
                    amd_block_step = _create_block_step(
                        block=f"Run AMD: {step.label}",
                        key=f"block-amd-{_generate_step_key(step.label)}",
                        command_step=amd_step,
                        depends_on=[mirror_build_dep],
                    )
                    amd_hardware_steps.append(amd_block_step)
                amd_hardware_steps.append(amd_step)

        if group_steps_list:
            buildkite_group_steps.append(
                BuildkiteGroupStep(group=group, steps=group_steps_list)
            )

    # If AMD hardware steps exist, make them a group step.
    if amd_hardware_steps:
        buildkite_group_steps.append(
            BuildkiteGroupStep(group="Hardware-AMD Tests", steps=amd_hardware_steps)
        )

    return buildkite_group_steps


def _step_should_run(step: Step, list_file_diff: List[str]) -> bool:
    if os.getenv("NOAUTO") == "1":
        return False
    global_config = get_global_config()
    if step.key and (
        step.key.startswith("image-build") or step.key in ALWAYS_RUN_STEP_KEYS
    ):
        return True
    if global_config["nightly"] == "1":
        return True
    if step.optional:
        return False
    if global_config["run_all"]:
        return True
    return _source_file_dependencies_match(
        step.source_file_dependencies, list_file_diff
    )


def _get_amd_mirror_effective_step(step: Step, amd: Dict[str, Any]) -> Step:
    source_file_dependencies = list(amd.get("source_file_dependencies") or [])
    for dependency in step.source_file_dependencies or []:
        if dependency not in source_file_dependencies:
            source_file_dependencies.append(dependency)
    if not source_file_dependencies:
        source_file_dependencies = None

    return step.model_copy(
        update={
            "key": None,
            "optional": amd.get("optional", step.optional),
            "source_file_dependencies": source_file_dependencies,
        }
    )


def _source_file_dependencies_match(
    source_file_dependencies: Optional[List[str]], list_file_diff: List[str]
) -> bool:
    if source_file_dependencies:
        for source_file in source_file_dependencies:
            for diff_file in list_file_diff:
                if _matches_source_dependency(source_file, diff_file):
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


def _create_amd_step(
    *,
    label: str,
    device: Optional[str],
    commands_str: str,
    depends_on: Optional[List[str]],
    extra_env: Optional[Dict[str, str]],
    soft_fail: Optional[bool],
    parallelism: Optional[int],
    key: Optional[str] = None,
    timeout_in_minutes: Optional[int] = None,
) -> BuildkiteCommandStep:
    """Create a Buildkite command step that runs through the AMD CI wrapper."""
    if not _is_amd_gpu_device(device):
        raise ValueError(
            f"Invalid AMD device: {device}. "
            f"Valid devices: {_valid_amd_gpu_devices()}"
        )
    queue_step = Step(label=label, device=_device_value(device))

    return BuildkiteCommandStep(
        label=_get_amd_label(label, device),
        key=key,
        commands=[AMD_TEST_COMMAND],
        depends_on=_normalize_amd_depends_on(depends_on),
        agents={"queue": get_agent_queue(queue_step)},
        env=_get_amd_env(commands_str, extra_env),
        priority=200,
        soft_fail=soft_fail or False,
        retry=AMD_RETRY,
        parallelism=parallelism,
        timeout_in_minutes=timeout_in_minutes,
    )
