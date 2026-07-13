import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TypedDict

from constants import AgentQueue, DeviceType
from plugin.amd_k8s_plugin import get_amd_k8s_plugin

AMD_TEST_COMMAND = "bash .buildkite/scripts/hardware_ci/run-amd-test.sh"
AMD_STABLE_CI_BASE_IMAGE = "rocm/vllm-dev:ci_base"
AMD_NATIVE_CI_BASE_IMAGE = "rocm/vllm-dev:ci_base-$BUILDKITE_COMMIT"
AMD_FALLBACK_CI_IMAGE = "rocm/vllm-ci:$BUILDKITE_COMMIT"
AMD_ARTIFACT_GLOB = "artifacts/vllm-rocm-install/vllm-rocm-install.tar.gz"
AMD_ARTIFACT_CHECKSUM_GLOB = f"{AMD_ARTIFACT_GLOB}.sha256"
AMD_ARTIFACT_STEP = "image-build-amd"
AMD_RESULTS_ROOT = "/home/buildkite-agent/huggingface/amd-ci-results"
AMD_HF_HOME = "/home/buildkite-agent/huggingface"
AMD_NATIVE_WORKSPACE = "/vllm-workspace"
AMD_NATIVE_WORKSPACE_VOLUME = "vllm-workspace"
AMD_NATIVE_SHM_SIZE = "16Gi"
AMD_NATIVE_RUNTIME_SOURCE_DEPENDENCIES = (
    ".buildkite/scripts/hardware_ci/run-amd-test.sh",
)
AMD_ALWAYS_RUN_STEP_KEYS = frozenset(
    {
        "ensure-ci-base-amd",
        "refresh-rocm-base-amd",
    }
)
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


@dataclass(frozen=True)
class AmdDeviceConfig:
    queue: AgentQueue
    default_gpu_count: int


AMD_DEVICE_CONFIGS = {
    DeviceType.AMD_MI250_1.value: AmdDeviceConfig(AgentQueue.AMD_MI250_1, 1),
    DeviceType.AMD_MI250_2.value: AmdDeviceConfig(AgentQueue.AMD_MI250_2, 2),
    DeviceType.AMD_MI250_4.value: AmdDeviceConfig(AgentQueue.AMD_MI250_4, 4),
    DeviceType.AMD_MI250_8.value: AmdDeviceConfig(AgentQueue.AMD_MI250_8, 8),
    DeviceType.AMD_MI300_1.value: AmdDeviceConfig(AgentQueue.AMD_MI300_1, 1),
    DeviceType.AMD_MI300_2.value: AmdDeviceConfig(AgentQueue.AMD_MI300_2, 2),
    DeviceType.AMD_MI300_4.value: AmdDeviceConfig(AgentQueue.AMD_MI300_4, 4),
    DeviceType.AMD_MI300_8.value: AmdDeviceConfig(AgentQueue.AMD_MI300_8, 8),
    DeviceType.AMD_MI325_1.value: AmdDeviceConfig(AgentQueue.AMD_MI325_1, 1),
    DeviceType.AMD_MI325_2.value: AmdDeviceConfig(AgentQueue.AMD_MI325_2, 2),
    DeviceType.AMD_MI325_4.value: AmdDeviceConfig(AgentQueue.AMD_MI325_4, 4),
    DeviceType.AMD_MI325_8.value: AmdDeviceConfig(AgentQueue.AMD_MI325_8, 8),
    DeviceType.AMD_MI355_1.value: AmdDeviceConfig(AgentQueue.AMD_MI355_1, 1),
    DeviceType.AMD_MI355_2.value: AmdDeviceConfig(AgentQueue.AMD_MI355_2, 2),
    DeviceType.AMD_MI355_4.value: AmdDeviceConfig(AgentQueue.AMD_MI355_4, 4),
    DeviceType.AMD_MI355_8.value: AmdDeviceConfig(AgentQueue.AMD_MI355_8, 8),
}


class AmdStepOptions(TypedDict):
    label: str
    commands: List[str]
    depends_on: List[str]
    agents: Dict[str, str]
    env: Optional[Dict[str, str]]
    plugins: Optional[List[Dict[str, Any]]]
    priority: int
    retry: Dict[str, List[Dict[str, Any]]]


def _device_value(device: Optional[str]) -> Optional[str]:
    if isinstance(device, DeviceType):
        return device.value
    return device


def get_amd_device_config(device: Optional[str]) -> Optional[AmdDeviceConfig]:
    return AMD_DEVICE_CONFIGS.get(_device_value(device))


def is_amd_gpu_device(device: Optional[str]) -> bool:
    return get_amd_device_config(device) is not None


def valid_amd_gpu_devices() -> List[str]:
    return list(AMD_DEVICE_CONFIGS)


def get_amd_agent_queue(device: Optional[str]) -> Optional[AgentQueue]:
    config = get_amd_device_config(device)
    return config.queue if config else None


def get_amd_label(label: str, device: Optional[str]) -> str:
    return f"AMD: {label} ({_device_value(device) or ''})"


def normalize_amd_depends_on(depends_on: Optional[List[str]]) -> List[str]:
    normalized = []
    for dependency in depends_on or []:
        if dependency == "image-build":
            dependency = AMD_ARTIFACT_STEP
        if dependency not in normalized:
            normalized.append(dependency)
    if AMD_ARTIFACT_STEP not in normalized:
        normalized.insert(0, AMD_ARTIFACT_STEP)
    return normalized


def resolve_amd_gpu_count(
    device: Optional[str],
    num_devices: Optional[int],
    no_gpu: bool,
) -> int:
    if no_gpu:
        return 0
    config = get_amd_device_config(device)
    if config is None:
        raise ValueError(
            f"Invalid AMD device: {device}. Valid devices: {valid_amd_gpu_devices()}"
        )
    if num_devices is not None and num_devices != config.default_gpu_count:
        raise ValueError(
            f"AMD device {_device_value(device)} provides "
            f"{config.default_gpu_count} GPUs, but num_devices={num_devices}."
        )
    return config.default_gpu_count


def get_amd_agents(
    device: Optional[str],
    agent_tags: Optional[Dict[str, str]],
    native_ci: bool,
) -> Dict[str, str]:
    config = get_amd_device_config(device)
    if config is None:
        raise ValueError(
            f"Invalid AMD device: {device}. Valid devices: {valid_amd_gpu_devices()}"
        )

    agents = {"queue": config.queue}
    if agent_tags is not None and not isinstance(agent_tags, dict):
        raise ValueError("AMD agent_tags must be a mapping.")
    if not agent_tags:
        return agents
    if not native_ci:
        raise ValueError("AMD agent_tags are only supported for native CI jobs.")

    for key, value in agent_tags.items():
        if (
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(value, str)
            or not value.strip()
        ):
            raise ValueError(
                "AMD agent_tags keys and values must be non-empty strings."
            )
        if key != "queue":
            agents[key] = value
    return agents


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


def get_amd_setup_commands() -> List[str]:
    return [
        "echo '--- :amd: GPU Info'",
        "(command amd-smi || true)",
        "echo '--- :gear: ROCm Debug Agent Setup'",
        _get_rocm_debug_agent_setup_command(),
    ]


def _get_amd_env(
    *,
    commands: str,
    extra_env: Optional[Dict[str, str]],
    native_ci: bool,
    gpu_count: int,
) -> Dict[str, str]:
    env = dict(extra_env or {})
    if native_ci:
        # Native agents have no DinD sidecar, so Docker hook inputs must not
        # escape into this execution mode.
        env.pop("DOCKER_IMAGE_NAME", None)
        env.pop("DOCKER_BUILDKIT", None)
        env.pop("VLLM_CI_FALLBACK_IMAGE", None)
        env.setdefault("VLLM_CI_ARTIFACT_STEP", AMD_ARTIFACT_STEP)
        env.setdefault("VLLM_CI_ARTIFACT_CHECKSUM_GLOB", AMD_ARTIFACT_CHECKSUM_GLOB)
        env.setdefault("VLLM_CI_REQUIRE_PERSISTENT_HF_CACHE", "1")
        env.setdefault("HF_HOME", AMD_HF_HOME)
        env.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "300")
        env.setdefault("HF_HUB_ETAG_TIMEOUT", "60")
        env.update(
            {
                "VLLM_CI_BASE_IMAGE": AMD_NATIVE_CI_BASE_IMAGE,
                "VLLM_CI_USE_ARTIFACTS": "1",
                "VLLM_CI_ARTIFACT_GLOB": AMD_ARTIFACT_GLOB,
                "VLLM_CI_RESULTS_ROOT": AMD_RESULTS_ROOT,
                "VLLM_CI_EXPECTED_GPU_COUNT": str(gpu_count),
                "VLLM_CI_WORKSPACE": AMD_NATIVE_WORKSPACE,
                "VLLM_CI_REQUIRE_WORKSPACE_MOUNT": "1",
                "VLLM_TEST_COMMANDS": commands,
                "AMD_CI_RUNTIME": "native",
                "NATIVE_CI": "true",
                "VLLM_CI_DOCKER_DISABLED": "1",
                "PYTORCH_ROCM_ARCH": "",
            }
        )
    else:
        env.update(
            {
                "DOCKER_BUILDKIT": "1",
                "DOCKER_IMAGE_NAME": AMD_STABLE_CI_BASE_IMAGE,
                "VLLM_CI_BASE_IMAGE": AMD_STABLE_CI_BASE_IMAGE,
                "VLLM_CI_FALLBACK_IMAGE": AMD_FALLBACK_CI_IMAGE,
                "VLLM_CI_USE_ARTIFACTS": "1",
                "VLLM_CI_ARTIFACT_GLOB": AMD_ARTIFACT_GLOB,
                "VLLM_CI_RESULTS_ROOT": AMD_RESULTS_ROOT,
                "VLLM_TEST_COMMANDS": commands,
            }
        )
    return env


def build_amd_step_options(
    *,
    label: str,
    device: Optional[str],
    num_devices: Optional[int],
    commands: str,
    depends_on: Optional[List[str]],
    extra_env: Optional[Dict[str, str]],
    native_ci: bool,
    no_plugin: bool,
    no_gpu: bool,
    num_nodes: Optional[int],
    agent_tags: Optional[Dict[str, str]],
) -> AmdStepOptions:
    config = get_amd_device_config(device)
    if config is None:
        raise ValueError(
            f"Invalid AMD device: {device}. Valid devices: {valid_amd_gpu_devices()}"
        )

    if not isinstance(native_ci, bool):
        raise ValueError("AMD native_ci must be a boolean.")
    native_runtime = native_ci
    if native_runtime and no_plugin:
        raise ValueError(
            "Native AMD jobs cannot use no_plugin; the wrapper installs the test artifact."
        )
    if native_runtime and num_nodes and num_nodes > 1:
        raise ValueError("Native AMD jobs do not support multi-node execution.")

    gpu_count = resolve_amd_gpu_count(device, num_devices, no_gpu)
    plugins = None
    if native_runtime:
        container_env = {
            "AMD_CI_RUNTIME": "native",
            "NATIVE_CI": "true",
            "VLLM_CI_DOCKER_DISABLED": "1",
            "VLLM_CI_EXPECTED_GPU_COUNT": str(gpu_count),
            "VLLM_CI_WORKSPACE": AMD_NATIVE_WORKSPACE,
            "VLLM_CI_REQUIRE_WORKSPACE_MOUNT": "1",
            "PYTORCH_ROCM_ARCH": "",
        }
        plugins = [
            get_amd_k8s_plugin(
                image=AMD_NATIVE_CI_BASE_IMAGE,
                gpu_count=gpu_count,
                workspace=AMD_NATIVE_WORKSPACE,
                workspace_volume_name=AMD_NATIVE_WORKSPACE_VOLUME,
                shm_size=AMD_NATIVE_SHM_SIZE,
                container_env=container_env,
            )
        ]

    if no_plugin:
        env = dict(extra_env or {}) or None
        step_commands = [commands]
    else:
        env = _get_amd_env(
            commands=commands,
            extra_env=extra_env,
            native_ci=native_ci,
            gpu_count=gpu_count,
        )
        step_commands = [AMD_TEST_COMMAND]

    return {
        "label": get_amd_label(label, device),
        "commands": step_commands,
        "depends_on": normalize_amd_depends_on(depends_on),
        "agents": get_amd_agents(device, agent_tags, native_runtime),
        "env": env,
        "plugins": plugins,
        "priority": 200,
        "retry": AMD_RETRY,
    }
