"""CI mode pipeline generation - simple dict generation with large inline structures."""

from typing import Any, Dict, List

from ..config import *  # noqa: F403, F405
from ..helpers.builds import (
    build_amd_image,
    build_cpu_image,
    build_cu118_image,
    build_main_image,
    build_torch_nightly_image,
)
from ..helpers.commands import flatten_commands, normalize_commands
from ..helpers.coverage import inject_coverage
from ..helpers.test_selection import apply_intelligent_test_targeting

# ==============================================================================
# UTILITIES
# ==============================================================================


def get_step_key(label: str) -> str:
    """Generate step key from label (matching Jinja logic)."""
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
    soft_fail: bool = False,
) -> str:
    """
    Create a shell command that uploads a notification step on failure.
    
    Args:
        check_step: Step name to check outcome for
        notification_label: Label for the notification step
        queue: Agent queue for notification
        message: Echo message for the command
        slack_channel: Slack channel to notify
        soft_fail: Whether notification step should soft fail
    """
    soft_fail_line = "\n       soft_fail: true" if soft_fail else ""
    return f'''if [ $$(buildkite-agent step get "outcome" --step "{check_step}") != "passed" ]; then
   cat <<- YAML | buildkite-agent pipeline upload
   steps:
     - label: "{notification_label}"{soft_fail_line}
       agents:
         queue: {queue}
       command: echo "{message}"
       notify:
         - slack:
             channels:
               - "{slack_channel}"
YAML
fi  '''


def create_multi_step_notification_command(
    check_steps: List[str],
    notification_label: str,
    queue: str,
    message: str,
    slack_channel: str,
) -> str:
    """Create a notification command that checks multiple steps."""
    conditions = ' || '.join(
        f'$$(buildkite-agent step get "outcome" --step "{step}") != "passed"'
        for step in check_steps
    )
    return f'''if [[ {conditions} ]]; then
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
fi'''


def get_agent_queue(test, config) -> str:
    """Determine agent queue for a test."""
    if test.label == LABEL_DOC_BUILD:
        return QUEUE_SMALL_CPU_PREMERGE
    elif test.no_gpu:
        return QUEUE_CPU_PREMERGE_US_EAST_1
    elif test.gpu == GPUType.A100:
        return QUEUE_A100
    elif test.gpu == GPUType.H100:
        return QUEUE_H100
    elif test.gpu == GPUType.H200:
        return QUEUE_H200
    elif test.gpu == GPUType.B200:
        return QUEUE_B200
    elif test.num_gpus and test.num_gpus >= 2:
        return QUEUE_GPU_4
    else:
        return QUEUE_GPU_1


def should_run_test(test, config) -> bool:
    """Check if test should run based on file changes."""
    if config.run_all or config.nightly:
        return True
    
    if test.source_file_dependencies:
        for source_file in test.source_file_dependencies:
            for changed_file in config.list_file_diff:
                if source_file in changed_file:
                    return True
        return False
    
    return True


def should_block_test(test, config) -> bool:
    """
    Check if test should have a block step.
    
    Jinja logic: block if (ns.blocked == 1 OR (step.optional AND nightly != "1"))
    Where ns.blocked is set to 0 if run_all or nightly or test matches file changes
    """
    # Optional tests are always blocked except in nightly mode
    if test.optional and not config.nightly:
        return True
    
    # In nightly or run_all mode, non-optional tests are not blocked
    if config.nightly or config.run_all:
        return False
    
    # Tests that shouldn't run are blocked
    if not should_run_test(test, config):
        return True
    
    return False


# ==============================================================================
# COMMAND BUILDING
# ==============================================================================


def build_docker_command(test, config) -> str:
    """Build command that runs inside docker container."""
    commands = flatten_commands(test.commands or [])
    commands = normalize_commands(commands)
    
    # Try intelligent test targeting first
    targeted = apply_intelligent_test_targeting(commands, test, config)
    
    # If targeting didn't apply and coverage is enabled, inject coverage
    if targeted == " && ".join(commands) and config.cov_enabled:
        command_str = inject_coverage(commands, test.label, config.vllm_ci_branch)
    else:
        command_str = targeted
    
    working_dir = test.working_dir or DEFAULT_WORKING_DIR
    
    # CI mode adds trailing space
    if command_str and not command_str.endswith(" "):
        command_str += " "
    
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
    
    image = config.container_image_cpu if test.no_gpu else config.container_image
    return f"{SCRIPT_RUN_MULTI_NODE} {working_dir} {test.num_nodes} {test.num_gpus or 1} {image} {' '.join(quoted_node_commands)}"




# ==============================================================================
# TEST STEP GENERATION
# ==============================================================================


def generate_test_step(test, config) -> Dict[str, Any]:
    """Generate a single test step with inline plugin construction."""
    # Multi-node test
    if test.num_nodes and test.num_nodes >= 2:
        return {
            "label": test.label,
            "agents": {"queue": get_agent_queue(test, config)},
            "soft_fail": test.soft_fail or False,
            "depends_on": BUILD_KEY_MAIN,
            "retry": {
                "automatic": [
                    {"exit_status": RETRY_EXIT_STATUS_AGENT_LOST, "limit": 1},
                    {"exit_status": RETRY_EXIT_STATUS_AGENT_TERMINATED, "limit": 1},
                ]
            },
            "commands": [build_multi_node_command(test, config)],
        }
    
    image = config.container_image_cpu if test.no_gpu else config.container_image
    command = build_docker_command(test, config)
    bash_flag = "-xce" if config.fail_fast else "-xc"
    
    # Kubernetes plugin for A100/H100 (large inline structure)
    if test.gpu in [GPUType.H100, GPUType.A100]:
        commands = flatten_commands(test.commands or [])
        commands = normalize_commands(commands)
        command_str = " && ".join(commands)
        working_dir = test.working_dir or DEFAULT_WORKING_DIR
        full_command = f"(command nvidia-smi || true) && export VLLM_ALLOW_DEPRECATED_BEAM_SEARCH=1 && cd {working_dir} && {command_str}"
        num_gpus = test.num_gpus or 1
        hf_cache_path = "/mnt/hf-cache" if test.gpu == GPUType.H100 or (test.num_gpus and test.num_gpus >= 8 and test.gpu != GPUType.A100) else HF_HOME
        
        pod_spec = {
            "containers": [{
                "image": image,
                "command": [f'bash -c "{full_command}"'],
                "resources": {"limits": {K8S_NVIDIA_GPU_RESOURCE: num_gpus}},
                "volumeMounts": [
                    {"name": K8S_DEVSHM_VOLUME, "mountPath": K8S_DEV_SHM_PATH},
                    {"name": K8S_HF_CACHE_VOLUME, "mountPath": HF_HOME},
                ],
                "env": [
                    {"name": "VLLM_USAGE_SOURCE", "value": "ci-test"},
                    {"name": "NCCL_CUMEM_HOST_ENABLE", "value": "0"},
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
            "volumes": [
                {"name": K8S_DEVSHM_VOLUME, "emptyDir": {"medium": "Memory"}},
                {"name": K8S_HF_CACHE_VOLUME, "hostPath": {"path": hf_cache_path, "type": "Directory"}},
            ],
        }
        
        if test.gpu == GPUType.A100:
            pod_spec["priorityClassName"] = K8S_PRIORITY_CLASS # type: ignore[assignment]
            pod_spec["nodeSelector"] = {K8S_NVIDIA_GPU_PRODUCT: K8S_NVIDIA_A100_PRODUCT} # type: ignore[assignment]
        elif test.gpu == GPUType.H100 or (test.num_gpus and test.num_gpus >= 8):
            pod_spec["nodeSelector"] = {K8S_NVIDIA_GPU_PRODUCT: "NVIDIA-H100-80GB-HBM3"} # type: ignore[assignment]
        
        step = {
            "label": test.label,
            "agents": {"queue": get_agent_queue(test, config)},
            "soft_fail": test.soft_fail or False,
            "plugins": [{"kubernetes": {"podSpec": pod_spec}}],
            "depends_on": BUILD_KEY_MAIN,
            "retry": {
                "automatic": [
                    {"exit_status": RETRY_EXIT_STATUS_AGENT_LOST, "limit": 1},
                    {"exit_status": RETRY_EXIT_STATUS_AGENT_TERMINATED, "limit": 1},
                ]
            },
        }
        if test.parallelism:
            step["parallelism"] = test.parallelism
        return step
    
    # Special GPU plugin for H200/B200 (inline)
    if test.gpu in [GPUType.H200, GPUType.B200]:
        env_vars: List[str] = [  # type: ignore[annotation-unchecked]
            "VLLM_USAGE_SOURCE=ci-test",
            "NCCL_CUMEM_HOST_ENABLE=0",
            "HF_HOME=/benchmark-hf-cache",
            "HF_TOKEN",
            "CODECOV_TOKEN",
        ]
        if config.fail_fast:
            env_vars.append("PYTEST_ADDOPTS=-x")
        if config.branch == "main":
            env_vars.append("BUILDKITE_ANALYTICS_TOKEN")
        
        step = {
            "label": test.label,
            "agents": {"queue": get_agent_queue(test, config)},
            "soft_fail": test.soft_fail or False,
            "plugins": [{
                DOCKER_PLUGIN: {
                    "image": image,
                    "always-pull": True,
                    "propagate-environment": True,
                    "gpus": "all" if test.gpu == GPUType.H200 else None,
                    "command": ["bash", bash_flag, command],
                    "environment": env_vars,
                    "volumes": [
                        "/dev/shm:/dev/shm",
                        "/data/benchmark-hf-cache:/benchmark-hf-cache",
                        "/data/benchmark-vllm-cache:/root/.cache/vllm",
                    ],
                }
            }],
            "depends_on": BUILD_KEY_MAIN,
            "retry": {
                "automatic": [
                    {"exit_status": RETRY_EXIT_STATUS_AGENT_LOST, "limit": 1},
                    {"exit_status": RETRY_EXIT_STATUS_AGENT_TERMINATED, "limit": 1},
                ]
            },
        }
        # Remove None gpus key if present
        if step["plugins"][0][DOCKER_PLUGIN]["gpus"] is None:  # type: ignore[index]
            del step["plugins"][0][DOCKER_PLUGIN]["gpus"]  # type: ignore[index]
        if test.parallelism:
            step["parallelism"] = test.parallelism
        return step
    
    # Standard Docker plugin (inline)
    plugin_env = [
        "VLLM_USAGE_SOURCE=ci-test",
        "NCCL_CUMEM_HOST_ENABLE=0",
        f"HF_HOME={HF_HOME_FSX}",
        "HF_TOKEN",
        "CODECOV_TOKEN",
    ]
    if config.fail_fast:
        plugin_env.append("PYTEST_ADDOPTS=-x")
    if config.branch == "main":
        plugin_env.append("BUILDKITE_ANALYTICS_TOKEN")
    if test.label == LABEL_SPEC_DECODE:
        plugin_env.append("VLLM_ATTENTION_BACKEND=XFORMERS")
    
    step = {
        "label": test.label,
        "agents": {"queue": get_agent_queue(test, config)},
        "soft_fail": test.soft_fail or False,
        "plugins": [{
            DOCKER_PLUGIN: {
                "image": image,
                "always-pull": True,
                "propagate-environment": True,
                "gpus": "all" if not test.no_gpu else None,
                "mount-buildkite-agent": True if (test.label == LABEL_BENCHMARKS or test.mount_buildkite_agent or config.cov_enabled) else None,
                "command": ["bash", bash_flag, command],
                "environment": plugin_env,
                "volumes": ["/dev/shm:/dev/shm", f"{HF_HOME_FSX}:{HF_HOME_FSX}"],
            }
        }],
        "depends_on": BUILD_KEY_MAIN,
        "retry": {
            "automatic": [
                {"exit_status": RETRY_EXIT_STATUS_AGENT_LOST, "limit": 1},
                {"exit_status": RETRY_EXIT_STATUS_AGENT_TERMINATED, "limit": 1},
            ]
        },
    }
    
    # Clean up None values in plugin
    plugin_dict = step["plugins"][0][DOCKER_PLUGIN]
    if plugin_dict.get("gpus") is None:
        del plugin_dict["gpus"]
    if plugin_dict.get("mount-buildkite-agent") is None:
        del plugin_dict["mount-buildkite-agent"]
    
    if test.parallelism:
        step["parallelism"] = test.parallelism
    
    return step


def generate_tests(test_steps, config) -> List[Dict[str, Any]]:
    """Generate all test steps for CI mode."""
    steps = []
    
    for test in test_steps:
        # Skip fast_check_only tests
        if test.fast_check_only:
            continue
        
        # Generate block if needed
        if should_block_test(test, config):
            block_key = f"block-{get_step_key(test.label)}"
            steps.append({
                "block": f"Run {test.label}",
                "key": block_key,
                "depends_on": BUILD_KEY_MAIN,
            })
            
            test_step = generate_test_step(test, config)
            test_step["depends_on"] = block_key
            steps.append(test_step)
        else:
            # No block needed
            base_dependency = BUILD_KEY_CPU if test.no_gpu else BUILD_KEY_MAIN
            test_step = generate_test_step(test, config)
            test_step["depends_on"] = base_dependency
            steps.append(test_step)
    
    return steps


# ==============================================================================
# TORCH NIGHTLY GROUP
# ==============================================================================


def generate_torch_nightly_group(test_steps, config) -> Dict[str, Any]:
    """Generate torch nightly test group."""
    group_steps = []
    
    # Add block for torch nightly build (not in nightly mode)
    if not config.nightly:
        group_steps.append({
            "block": "Build torch nightly image",
            "key": "block-build-torch-nightly",
            "depends_on": None,
        })
    
    # Add torch nightly build
    build_depends_on = "block-build-torch-nightly" if not config.nightly else None
    group_steps.append(build_torch_nightly_image(config, build_depends_on))
    
    # Add torch nightly tests
    for test in test_steps:
        if not test.torch_nightly:
            continue
        
        # Skip multi-node tests
        if test.num_nodes and test.num_nodes >= 2:
            continue
        
        # Check if torch nightly test should be blocked
        # Torch nightly uses different logic than main tests (doesn't check run_all)
        should_block = True  # Start blocked
        
        # Set to not blocked if nightly mode
        if config.nightly:
            should_block = False
        # Set to not blocked if no source deps
        elif not test.source_file_dependencies:
            should_block = False
        # Set to not blocked if file dependencies match
        elif test.source_file_dependencies:
            for source_file in test.source_file_dependencies:
                for changed_file in config.list_file_diff:
                    if source_file in changed_file:
                        should_block = False
                        break
                if not should_block:
                    break
        
        # Also block if optional (and not nightly)
        if test.optional and not config.nightly:
            should_block = True
        
        # Add block if needed
        if should_block:
            block_key = f"block-torch-nightly-{get_step_key(test.label)}"
            group_steps.append({
                "block": f"Run Torch Nightly {test.label}",
                "key": block_key,
                "depends_on": BUILD_KEY_TORCH_NIGHTLY,
            })
            test_depends_on = block_key
        else:
            test_depends_on = BUILD_KEY_TORCH_NIGHTLY
        
        image = config.container_image_torch_nightly
        command = build_docker_command(test, config)
        bash_flag = "-xce" if config.fail_fast else "-xc"
        
        # Build plugin inline (same logic as generate_test_step but for torch nightly image)
        plugin = {
            DOCKER_PLUGIN: {
                "image": image,
                "always-pull": True,
                "propagate-environment": True,
                "command": ["bash", bash_flag, command],
                    "environment": (
                        ["VLLM_USAGE_SOURCE=ci-test", "NCCL_CUMEM_HOST_ENABLE=0", f"HF_HOME={HF_HOME_FSX}", "HF_TOKEN", "CODECOV_TOKEN"]
                        + (["PYTEST_ADDOPTS=-x"] if config.fail_fast else [])
                        + (["BUILDKITE_ANALYTICS_TOKEN"] if config.branch == "main" else [])
                        + (["VLLM_ATTENTION_BACKEND=XFORMERS"] if test.label == LABEL_SPEC_DECODE else [])
                    ),
                "volumes": ["/dev/shm:/dev/shm", f"{HF_HOME_FSX}:{HF_HOME_FSX}"],
            }
        }
        
        # Add gpus if not no_gpu
        if not test.no_gpu:
            plugin[DOCKER_PLUGIN]["gpus"] = "all"
        
        # Add mount-buildkite-agent if needed
        if test.label == LABEL_BENCHMARKS or test.mount_buildkite_agent or config.cov_enabled:
            plugin[DOCKER_PLUGIN]["mount-buildkite-agent"] = True
        
        step = {
            "label": f"Torch Nightly {test.label}",
            "agents": {"queue": get_agent_queue(test, config)},
            "soft_fail": True,  # Torch nightly tests are soft_fail
            "plugins": [plugin],
            "depends_on": test_depends_on,
            "retry": {
                "automatic": [
                    {"exit_status": RETRY_EXIT_STATUS_AGENT_LOST, "limit": 1},
                    {"exit_status": RETRY_EXIT_STATUS_AGENT_TERMINATED, "limit": 1},
                ]
            },
        }
        
        if test.parallelism:
            step["parallelism"] = test.parallelism
        
        group_steps.append(step) # type: ignore[arg-type]
    
    return {
        "group": "vllm against torch nightly",
        "depends_on": None,
        "steps": group_steps,
    }


# ==============================================================================
# AMD GROUP
# ==============================================================================


def get_amd_queue(label: str) -> str:
    """Determine AMD queue based on label."""
    if label in AMD_MI325_8_LABELS:
        return QUEUE_AMD_MI325_8
    elif label in AMD_MI325_4_LABELS:
        return QUEUE_AMD_MI325_4
    elif label in AMD_MI325_2_LABELS:
        return QUEUE_AMD_MI325_2
    else:
        return QUEUE_AMD_MI325_1


def format_commands_for_amd(commands) -> str:
    """Format commands for AMD, handling multi-node structure."""
    if not commands:
        return ""
    
    # Check if it's multi-node (list of lists)
    if isinstance(commands[0], list):
        # Multi-node: convert each node's commands to JSON array representation
        node_parts = []
        for node_cmds in commands:
            # Format as JSON array
            formatted_cmds = ", ".join(f'"{cmd}"' for cmd in node_cmds)
            node_parts.append(f"[{formatted_cmds}]")
        return " && ".join(node_parts)
    else:
        # Single node: just join with &&
        return " && ".join(commands)


def generate_amd_group(test_steps, config) -> Dict[str, Any]:
    """Generate AMD test group."""
    group_steps = []
    
    # Add AMD build
    group_steps.append(build_amd_image(config))
    
    # Add AMD tests
    for test in test_steps:
        if not test.mirror_hardwares or config.mirror_hw not in test.mirror_hardwares:
            continue
        
        # Format commands for AMD (handles multi-node)
        commands_str = format_commands_for_amd(test.commands or [])
        working_dir = test.working_dir or DEFAULT_WORKING_DIR
        
        # AMD tests use a wrapper script with commands as argument
        inner_command = f"(command rocm-smi || true) && export VLLM_ALLOW_DEPRECATED_BEAM_SEARCH=1 && cd {working_dir} ; {commands_str}"
        full_command = f'bash .buildkite/scripts/hardware_ci/run-amd-test.sh "{inner_command}"'
        
        step = {
            "label": f"AMD MI300: {test.label}",
            "depends_on": BUILD_KEY_AMD,
            "agents": {"queue": get_amd_queue(test.label)},
            "env": {"DOCKER_BUILDKIT": "1"},
            "soft_fail": False,
            "priority": PRIORITY_AMD,
            "command": full_command,
        }
        
        group_steps.append(step)
    
    return {
        "group": "AMD Tests",
        "depends_on": None,
        "steps": group_steps,
    }


# ==============================================================================
# HARDWARE TESTS
# ==============================================================================


def generate_hardware_tests(config) -> List[Dict[str, Any]]:
    """Generate all hardware-specific tests - large inline structures."""
    steps = []
    
    # Neuron test (inline)
    steps.append({
        "label": "Neuron Test",
        "agents": {"queue": QUEUE_NEURON},
        "command": f"bash {SCRIPT_RUN_NEURON}",
        "soft_fail": True,
    })
    
    # Intel CPU (always has block)
    steps.append({
        "block": "Run Intel CPU test",
        "key": "block-intel-cpu",
        "depends_on": None,
    })
    steps.append({
        "label": "Intel CPU Test",
        "agents": {"queue": QUEUE_INTEL_CPU},
        "command": f"bash {SCRIPT_RUN_INTEL_CPU}",
        "soft_fail": True,
        "depends_on": None if config.branch == "main" else "block-intel-cpu",
    })
    
    # Intel HPU and GPU (inline)
    steps.append({
        "label": "Intel HPU Test",
        "agents": {"queue": QUEUE_INTEL_HPU},
        "command": f"bash {SCRIPT_RUN_INTEL_HPU}",
        "soft_fail": True,
    })
    steps.append({
        "label": "Intel GPU Test",
        "agents": {"queue": QUEUE_INTEL_GPU},
        "command": f"bash {SCRIPT_RUN_INTEL_GPU}",
        "soft_fail": True,
    })
    
    # Ascend NPU (inline)
    steps.append({
        "label": "Ascend NPU Test",
        "agents": {"queue": QUEUE_ASCEND},
        "command": "bash .buildkite/scripts/hardware_ci/run-npu-test.sh",
        "soft_fail": True,
        "timeout_in_minutes": 20,
        "depends_on": None,
    })
    
    # IBM Power
    if config.branch == "main":
        steps.append({
            "block": "Run IBM Power CPU test",
            "key": "block-ibm-power",
            "depends_on": None,
        })
        steps.append({
            "label": "IBM Power(ppc64le) CPU Test",
            "key": "ibm-ppc64-test",
            "depends_on": "block-ibm-power",
            "agents": {"queue": QUEUE_IBM_PPC64LE},
            "command": f"bash {SCRIPT_RUN_IBM_POWER}",
            "soft_fail": True,
        })
        # Add notification
        steps.append({
            "label": "IBM Power(ppc64le) Build Failure Notification",
            "depends_on": "ibm-ppc64-test",
            "soft_fail": True,
            "agents": {"queue": QUEUE_IBM_PPC64LE},
            "commands": create_notification_command(
                check_step="IBM Power(ppc64le) CPU Test",
                notification_label="Notify owners about failing test",
                queue="ibm-ppc64le",
                message="IBM Power(ppc64le) Build/Test failed",
                slack_channel="vllm#vllm-ci-on-power",
                soft_fail=True,
            ),
        })
    else:
        steps.append({
            "block": "Run IBM Power(ppc64le) CPU Test",
            "key": "block-ibm-ppc64-test",
            "depends_on": None,
        })
        steps.append({
            "label": "IBM Power(ppc64le) CPU Test",
            "depends_on": "block-ibm-ppc64-test",
            "agents": {"queue": QUEUE_IBM_PPC64LE},
            "command": f"bash {SCRIPT_RUN_IBM_POWER}",
            "soft_fail": True,
        })
    
    # IBM S390X
    if not config.nightly:
        steps.append({
            "block": 'Run "IBM Z (s390x) CPU Test"',
            "key": "block-ibm-s390x",
            "depends_on": None,
        })
    steps.append({
        "label": "IBM Z (s390x) CPU Test",
        "depends_on": None if config.nightly else "block-ibm-s390x",
        "agents": {"queue": QUEUE_IBM_S390X},
        "command": f"bash {SCRIPT_RUN_IBM_S390X}",
        "soft_fail": True,
    })
    
    # GH200 (nightly only) - inline
    if config.nightly:
        steps.append({
            "label": "GH200 Test",
            "agents": {"queue": QUEUE_GH200},
            "command": f"nvidia-smi && bash {SCRIPT_RUN_GH200}",
            "soft_fail": True,
        })
    
    # TPU tests (inline with all commands)
    steps.append({
        "label": "TPU V1 Test",
        "soft_fail": True,
        "depends_on": None,
        "key": "run-tpu-v1-test",
        "timeout_in_minutes": 180,
        "agents": {"queue": QUEUE_TPU_V6E},
        "commands": [
            SCRIPT_TPU_CLEANUP,
            f'if [[ -f "{SCRIPT_RUN_TPU_V1}" ]]; then bash {SCRIPT_RUN_TPU_V1}; fi',
        ],
    })
    steps.append({
        "label": "TPU V1 Test Part2",
        "soft_fail": True,
        "depends_on": None,
        "key": "run-tpu-v1-test-part2",
        "timeout_in_minutes": 90,
        "agents": {"queue": QUEUE_TPU_V6E},
        "commands": [
            SCRIPT_TPU_CLEANUP,
            f'if [[ -f "{SCRIPT_RUN_TPU_V1_PART2}" ]]; then bash {SCRIPT_RUN_TPU_V1_PART2}; fi',
        ],
    })
    steps.append({
        "label": "TPU V1 Benchmark Test",
        "soft_fail": True,
        "depends_on": None,
        "key": "run-tpu-v1-benchmark-test",
        "timeout_in_minutes": 60,
        "agents": {"queue": QUEUE_TPU_V6E},
        "commands": [
            SCRIPT_TPU_CLEANUP,
            (
                "DOCKER_BUILDKIT=1 docker build --build-arg max_jobs=16 --build-arg USE_SCCACHE=1 "
                "--build-arg GIT_REPO_CHECK=0 --tag vllm/vllm-tpu-bm --progress plain -f docker/Dockerfile.tpu ."
            ),
            "bash .buildkite/scripts/tpu/docker_run_bm.sh .buildkite/scripts/tpu/config_v6e_1.env",
            "bash .buildkite/scripts/tpu/docker_run_bm.sh .buildkite/scripts/tpu/quantized_v6e_1.env",
        ],
    })
    
    # TPU notification for main branch
    if config.branch == "main":
        steps.append({
            "label": "TPU V1 Test Notification",
            "depends_on": ["run-tpu-v1-test", "run-tpu-v1-test-part2"],
            "soft_fail": True,
            "agents": {"queue": QUEUE_TPU_V6E},
            "commands": create_multi_step_notification_command(
                check_steps=["run-tpu-v1-test", "run-tpu-v1-test-part2"],
                notification_label="Notify owners about failing test",
                queue="tpu_v6e_queue",
                message="TPU V1 Test failed",
                slack_channel="vllm#tpu-ci-notifications",
            ),
        })
    
    return steps


# ==============================================================================
# MAIN CI PIPELINE
# ==============================================================================


def generate_ci_pipeline(test_steps, config) -> List[Dict[str, Any]]:
    """Generate complete CI pipeline."""
    steps = []
    
    # Main CUDA build
    steps.append(build_main_image(config))
    
    # CUDA 11.8 build (inline)
    steps.extend(build_cu118_image(config))
    
    # CPU build (inline)
    steps.append(build_cpu_image(config))
    
    # Test steps
    steps.extend(generate_tests(test_steps, config))
    
    # Torch nightly group
    steps.append(generate_torch_nightly_group(test_steps, config))
    
    # AMD group
    steps.append(generate_amd_group(test_steps, config))
    
    # Hardware tests
    steps.extend(generate_hardware_tests(config))
    
    return steps

