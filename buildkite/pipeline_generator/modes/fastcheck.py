"""Fastcheck mode pipeline generation - simple dict generation with inline structures."""

from typing import Any, Dict, List

from ..config import *  # noqa: F403, F405
from ..helpers.builds import build_amd_image, build_main_image
from ..helpers.commands import flatten_commands, normalize_commands

# ==============================================================================
# UTILITIES
# ==============================================================================


def get_step_key(label: str) -> str:
    """Generate step key from label."""
    return (
        label.lower()
        .replace(" ", "-")
        .replace("(", "")
        .replace(")", "")
        .replace("%", "")
        .replace(",", "-")
        .replace("+", "-")
    )


def create_notification_command(
    check_step: str,
    notification_label: str,
    queue: str,
    message: str,
    slack_channel: str,
) -> str:
    """Create a shell command that uploads a notification step on failure."""
    return f'''if [ $$(buildkite-agent step get "outcome" --step "{check_step}") != "passed" ]; then
   cat <<- YAML | buildkite-agent pipeline upload
   steps:
     - label: "{notification_label}"
       agents:
         queue: {queue}
       command: echo "{message}"
       notify:
         - slack:
             channels:
               - "{slack_channel}"
YAML
fi
'''


def get_agent_queue(test) -> str:
    """Determine agent queue for fastcheck test."""
    if test.label == LABEL_DOC_BUILD:
        return QUEUE_SMALL_CPU_PREMERGE
    elif test.no_gpu:
        return QUEUE_CPU_PREMERGE
    elif test.gpu == GPUType.A100:
        return QUEUE_A100
    elif test.num_gpus in [2, 4]:
        return QUEUE_GPU_4
    else:
        return QUEUE_GPU_1


# ==============================================================================
# COMMAND BUILDING
# ==============================================================================


def build_docker_command(test, config) -> str:
    """Build command for docker container."""
    commands = flatten_commands(test.commands or [])
    commands = normalize_commands(commands)
    command_str = " && ".join(commands)
    
    working_dir = test.working_dir or DEFAULT_WORKING_DIR
    return f"(command nvidia-smi || true) && export VLLM_ALLOW_DEPRECATED_BEAM_SEARCH=1 && cd {working_dir} && {command_str}"


def build_multi_node_command(test, config) -> str:
    """Build multi-node test command."""
    working_dir = test.working_dir or DEFAULT_WORKING_DIR
    
    # Extract commands for each node
    if test.commands and len(test.commands) > 0 and isinstance(test.commands[0], list):
        node_commands = test.commands
    else:
        simple_commands = test.commands if test.commands else []
        node_commands = [simple_commands] * (test.num_nodes or 2)
    
    # Build quoted node commands
    quoted_node_commands = []
    for node_cmds in node_commands:
        node_cmd_str = " && ".join(node_cmds)
        quoted_node_commands.append(f'"{node_cmd_str}"')
    
    image = config.container_image
    return f"{SCRIPT_RUN_MULTI_NODE} {working_dir} {test.num_nodes} {test.num_gpus or 1} {image} {' '.join(quoted_node_commands)}"


# ==============================================================================
# PLUGIN GENERATION
# ==============================================================================


def build_docker_plugin(test, image, config) -> Dict:
    """Build Docker plugin for fastcheck."""
    command = build_docker_command(test, config)
    bash_flag = "-xce" if config.fail_fast else "-xc"
    
    plugin = {
        "image": image,
        "always-pull": True,
        "propagate-environment": True,
        "command": ["bash", bash_flag, command],
        "environment": [
            "VLLM_USAGE_SOURCE=ci-test",
            "NCCL_CUMEM_HOST_ENABLE=0",
            f"HF_HOME={HF_HOME_FSX}",
            "HF_TOKEN",
        ],
        "volumes": ["/dev/shm:/dev/shm", f"{HF_HOME_FSX}:{HF_HOME_FSX}"],
    }
    
    if not test.no_gpu:
        plugin["gpus"] = "all"
    
    if config.fail_fast:
        plugin["environment"].append("PYTEST_ADDOPTS=-x")
    
    if test.label == LABEL_SPEC_DECODE:
        plugin["environment"].append("VLLM_ATTENTION_BACKEND=XFORMERS")
    
    if test.label == LABEL_BENCHMARKS:
        plugin["mount-buildkite-agent"] = True
        
    return {DOCKER_PLUGIN: plugin}


def build_a100_kubernetes_plugin(test, image, config) -> Dict:
    """Build Kubernetes plugin for A100 in fastcheck."""
    commands = flatten_commands(test.commands or [])
    commands = normalize_commands(commands)
    command_str = " && ".join(commands)
    
    working_dir = test.working_dir or DEFAULT_WORKING_DIR
    full_command = f"(command nvidia-smi || true) && export VLLM_ALLOW_DEPRECATED_BEAM_SEARCH=1 && cd {working_dir} && {command_str}"
    
    num_gpus = test.num_gpus or 1
    
    pod_spec = {
        "priorityClassName": K8S_PRIORITY_CLASS,
        "containers": [{
            "image": image,
            "command": ["bash"],
            "args": ["-c", f"'{full_command}'"],
            "resources": {"limits": {K8S_NVIDIA_GPU_RESOURCE: num_gpus}},
            "volumeMounts": [
                {"name": K8S_DEVSHM_VOLUME, "mountPath": K8S_DEV_SHM_PATH},
                {"name": K8S_HF_CACHE_VOLUME, "mountPath": HF_HOME},
            ],
            "env": [
                {"name": "VLLM_USAGE_SOURCE", "value": "ci-test"},
                {"name": "NCCL_CUMEM_HOST_ENABLE", "value": 0},
                {"name": "HF_HOME", "value": HF_HOME},
                {
                    "name": "HF_TOKEN",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": K8S_HF_TOKEN_SECRET,
                            "key": K8S_HF_TOKEN_KEY,
                        }
                    },
                },
            ],
        }],
        "nodeSelector": {K8S_NVIDIA_GPU_PRODUCT: K8S_NVIDIA_A100_PRODUCT},
        "volumes": [
            {"name": K8S_DEVSHM_VOLUME, "emptyDir": {"medium": "Memory"}},
            {"name": K8S_HF_CACHE_VOLUME, "hostPath": {"path": HF_HOME, "type": "Directory"}},
        ],
    }
    
    return {"kubernetes": {"podSpec": pod_spec}}


# ==============================================================================
# TEST GENERATION
# ==============================================================================


def generate_test_step(test, config) -> Dict[str, Any]:
    """Generate a single test step."""
    # Multi-node test (NO retry or soft_fail in fastcheck)
    if test.num_nodes and test.num_nodes >= 2:
        return {
            "label": test.label,
            "agents": {"queue": get_agent_queue(test)},
            "depends_on": BUILD_KEY_MAIN,
            "commands": [build_multi_node_command(test, config)],
        }
    
    # A100 test (uses kubernetes)
    if test.gpu == GPUType.A100:
        step = {
            "label": test.label,
            "agents": {"queue": QUEUE_A100},
            "soft_fail": test.soft_fail or False,
            "plugins": [build_a100_kubernetes_plugin(test, config.container_image, config)],
            "retry": {
                "automatic": [
                    {"exit_status": RETRY_EXIT_STATUS_AGENT_LOST, "limit": 5},
                    {"exit_status": RETRY_EXIT_STATUS_AGENT_TERMINATED, "limit": 5},
                ]
            },
            "priority": PRIORITY_A100,
        }
        if test.parallelism:
            step["parallelism"] = test.parallelism
        return step
    
    # Regular test
    step = {
        "label": test.label,
        "agents": {"queue": get_agent_queue(test)},
        "soft_fail": test.soft_fail or False,
        "plugins": [build_docker_plugin(test, config.container_image, config)],
        "depends_on": BUILD_KEY_MAIN,
        "retry": {
            "automatic": [
                {"exit_status": RETRY_EXIT_STATUS_AGENT_LOST, "limit": 5},
                {"exit_status": RETRY_EXIT_STATUS_AGENT_TERMINATED, "limit": 5},
            ]
        },
    }
    
    if test.parallelism:
        step["parallelism"] = test.parallelism
    
    return step


def generate_fast_check_tests(test_steps, config) -> List[Dict[str, Any]]:
    """Generate tests that run immediately (fast_check=true, not multi-node, not A100)."""
    steps = []
    
    for test in test_steps:
        if not test.fast_check:
            continue
        if test.num_nodes and test.num_nodes >= 2:
            continue
        if test.gpu == GPUType.A100:
            continue
        
        steps.append(generate_test_step(test, config))
    
    return steps


def generate_blocked_tests(test_steps, config) -> List[Dict[str, Any]]:
    """Generate blocked tests (non-fast-check, multi-node, A100)."""
    steps = []
    
    # Regular non-fast-check tests
    for test in test_steps:
        if test.fast_check:
            continue
        if test.num_nodes and test.num_nodes >= 2:
            continue
        if test.gpu == GPUType.A100:
            continue
        
        block_key = f"block-{get_step_key(test.label)}"
        steps.append({
            "block": f"Run {test.label}",
            "key": block_key,
            "depends_on": BUILD_KEY_MAIN,
        })
        
        test_step = generate_test_step(test, config)
        test_step["depends_on"] = block_key
        steps.append(test_step)
    
    # Multi-node tests
    for test in test_steps:
        if not (test.num_nodes and test.num_nodes >= 2):
            continue
        
        block_key = f"block-{get_step_key(test.label)}"
        steps.append({
            "block": f"Run {test.label}",
            "key": block_key,
            "depends_on": BUILD_KEY_MAIN,
        })
        
        test_step = generate_test_step(test, config)
        test_step["depends_on"] = block_key
        steps.append(test_step)
    
    # A100 tests (all behind single block)
    a100_tests = [t for t in test_steps if t.gpu == GPUType.A100]
    if a100_tests:
        steps.append({
            "block": "Run A100 tests",
            "depends_on": BUILD_KEY_MAIN,
        })
        for test in a100_tests:
            steps.append(generate_test_step(test, config))
    
    return steps


# ==============================================================================
# HARDWARE TESTS
# ==============================================================================


def generate_hardware_tests(config) -> List[Dict[str, Any]]:
    """Generate hardware tests for fastcheck."""
    steps = []
    
    # TPU V0
    steps.append({
        "block": "Run TPU V0 Test",
        "depends_on": None,
        "key": "block-tpu-v0",
    })
    steps.append({
        "label": "TPU V0 Test",
        "key": "run-tpu-v0-test",
        "depends_on": "block-tpu-v0",
        "soft_fail": True, # type: ignore
        "agents": {"queue": QUEUE_TPU_V5}, # type: ignore
        "commands": [
            f'if [[ -f "{SCRIPT_RUN_TPU}" ]]; then bash {SCRIPT_RUN_TPU}; fi',
            "yes | docker system prune -a",
        ], # type: ignore
    })
    steps.append({
        "label": "TPU V0 Test Notification",
        "depends_on": "run-tpu-v0-test",
        "soft_fail": True, # type: ignore
        "agents": {"queue": QUEUE_TPU_V5}, # type: ignore
        "commands": create_notification_command(
            check_step="run-tpu-v0-test",
            notification_label="Notify owners about failing test",
            queue="tpu_v5_queue",
            message="TPU V0 Test failed",
            slack_channel="#collab-google-ci",
        ),
    })
    
    # TPU V1
    steps.append({
        "block": "Run TPU V1 Test",
        "key": "block-tpu-v1",
        "depends_on": None,
    })
    steps.append({
        "label": "TPU V1 Test",
        "key": "run-tpu-v1-test",
        "depends_on": "block-tpu-v1",
        "agents": {"queue": QUEUE_TPU_V6E}, # type: ignore
        "commands": [
            SCRIPT_TPU_CLEANUP,
            f'if [[ -f "{SCRIPT_RUN_TPU_V1}" ]]; then bash {SCRIPT_RUN_TPU_V1}; fi',
        ], # type: ignore
    })
    steps.append({
        "label": "TPU V1 Test Notification",
        "depends_on": "run-tpu-v1-test",
        "soft_fail": True, # type: ignore
        "agents": {"queue": QUEUE_TPU_V5}, # type: ignore
        "commands": create_notification_command(
            check_step="run-tpu-v1-test",
            notification_label="Notify owners about failing test",
            queue="tpu_v5_queue",
            message="TPU V1 Test failed",
            slack_channel="#tpu-ci-notifications",
        ),
    })
    
    # GH200
    steps.append({
        "block": "Run GH200 Test",
        "depends_on": None,
        "key": "block-gh200",
    })
    steps.append({
        "label": "GH200 Test",
        "depends_on": "block-gh200",
        "agents": {"queue": QUEUE_GH200}, # type: ignore
        "command": f"nvidia-smi && bash {SCRIPT_RUN_GH200}",
        "soft_fail": True, # type: ignore
    })
    
    # Intel CPU
    steps.append({
        "block": "Run Intel CPU test",
        "key": "block-intel-cpu",
        "depends_on": None,
    })
    steps.append({
        "label": "Intel CPU Test",
        "depends_on": "block-intel-cpu",
        "agents": {"queue": QUEUE_INTEL_CPU}, # type: ignore
        "command": f"bash {SCRIPT_RUN_INTEL_CPU}",
        "soft_fail": True, # type: ignore
    })
    
    # Intel GPU
    steps.append({
        "block": "Run Intel GPU test",
        "key": "block-intel-gpu",
        "depends_on": None,
    })
    steps.append({
        "label": "Intel GPU Test",
        "depends_on": "block-intel-gpu",
        "agents": {"queue": QUEUE_INTEL_GPU}, # type: ignore
        "command": f"bash {SCRIPT_RUN_INTEL_GPU}",
        "soft_fail": True, # type: ignore   
    })
    
    return steps


# ==============================================================================
# AMD GROUP (FASTCHECK VERSION)
# ==============================================================================


def generate_amd_group(test_steps, config) -> Dict[str, Any]:
    """Generate AMD test group for fastcheck."""
    group_steps = []
    
    # AMD build
    amd_build = build_amd_image(config)
    # Fastcheck AMD build has depends_on: null inside the group
    if "depends_on" not in amd_build:
        amd_build["depends_on"] = None
    group_steps.append(amd_build)
    
    # Only Basic Correctness Test in fastcheck
    for test in test_steps:
        if not test.mirror_hardwares or config.mirror_hw not in test.mirror_hardwares:
            continue
        
        if test.label != LABEL_BASIC_CORRECTNESS:
            continue
        
        # Add block
        block_key = f"block-amd-{get_step_key(test.label)}"
        group_steps.append({
            "block": f"Run AMD MI300: {test.label} with {config.mirror_hw}",
            "key": block_key,
            "depends_on": BUILD_KEY_AMD,
        })
        
        # Format commands
        commands = flatten_commands(test.commands or [])
        commands_str = " && ".join(commands)
        working_dir = test.working_dir or DEFAULT_WORKING_DIR
        
        # AMD tests use a wrapper script (same as CI)
        inner_command = f"(command rocm-smi || true) && export VLLM_ALLOW_DEPRECATED_BEAM_SEARCH=1 && cd {working_dir} ; {commands_str}"
        full_command = f'bash .buildkite/scripts/hardware_ci/run-amd-test.sh "{inner_command}"'
        
        group_steps.append({
            "label": f"AMD MI300: {test.label} with {config.mirror_hw}",
            "depends_on": block_key,
            "agents": {"queue": QUEUE_AMD_MI300_1},
            "env": {"DOCKER_BUILDKIT": "1"},
            "soft_fail": True,
            "priority": PRIORITY_AMD,
            "command": full_command,
        })
    
    return {
        "group": "AMD Tests",
        "depends_on": None,
        "steps": group_steps,
    }


# ==============================================================================
# MAIN FASTCHECK PIPELINE
# ==============================================================================


def generate_fastcheck_pipeline(test_steps, config) -> List[Dict[str, Any]]:
    """Generate complete fastcheck pipeline."""
    steps = []
    
    # Main build only
    steps.append(build_main_image(config))
    
    # Neuron test (at the top, before regular tests)
    steps.append({
        "block": "Run Neuron Test",
        "depends_on": None,
        "key": "run-neuron-test",
    })
    steps.append({
        "label": "Neuron Test",
        "depends_on": "run-neuron-test",
        "agents": {"queue": QUEUE_NEURON},
        "command": f"bash {SCRIPT_RUN_NEURON}",
        "soft_fail": False,
    })
    
    # Fast-check tests (run immediately)
    steps.extend(generate_fast_check_tests(test_steps, config))
    
    # Blocked tests
    steps.extend(generate_blocked_tests(test_steps, config))
    
    # Hardware tests (TPU V0, TPU V1, GH200 - without Intel)
    hw_steps = generate_hardware_tests(config)
    # Hardware tests are: TPU V0 (3 steps), TPU V1 (3 steps), GH200 (2 steps), Intel CPU (2 steps), Intel GPU (2 steps)
    # We want TPU V0, TPU V1, GH200 before AMD, then Intel after AMD
    # So take first 8 steps (TPU V0 + TPU V1 + GH200)
    steps.extend(hw_steps[:8])
    
    # AMD group
    steps.append(generate_amd_group(test_steps, config))
    
    # Intel tests (remaining hardware tests)
    steps.extend(hw_steps[8:])
    
    return steps

