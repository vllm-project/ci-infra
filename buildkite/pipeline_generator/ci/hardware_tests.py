"""Hardware-specific test generation using data-driven configuration."""

from typing import Any, Dict, List

from ..hardware_test_configs import (
    ASCEND_CONFIG,
    GH200_CONFIG,
    INTEL_GPU_CONFIG,
    INTEL_HPU_CONFIG,
    NEURON_CONFIG,
    TPU_V1_BENCHMARK,
    TPU_V1_TEST,
    TPU_V1_TEST_PART2,
    get_ibm_power_config,
    get_ibm_power_notification_config,
    get_ibm_s390x_config,
    get_intel_cpu_config,
    get_tpu_notification_config,
)


def generate_all_hardware_tests(
        branch: str, nightly: bool) -> List[Dict[str, Any]]:
    """Generate all hardware-specific test steps using data-driven configuration."""
    steps = []

    # Simple hardware tests
    steps.append(NEURON_CONFIG.to_buildkite_step())

    # Intel tests
    intel_cpu = get_intel_cpu_config(branch)
    steps.extend(intel_cpu.to_buildkite_steps())

    steps.append(INTEL_HPU_CONFIG.to_buildkite_step())
    steps.append(INTEL_GPU_CONFIG.to_buildkite_step())

    # Ascend NPU
    steps.append(ASCEND_CONFIG.to_buildkite_step())

    # IBM Power
    ibm_power = get_ibm_power_config(branch)
    steps.extend(ibm_power.to_buildkite_steps())

    # Add IBM Power notification for main branch
    if branch == "main":
        ibm_power_notif = get_ibm_power_notification_config()
        steps.append(ibm_power_notif.to_buildkite_step())

    # IBM Z (s390x)
    ibm_s390x = get_ibm_s390x_config(nightly)
    steps.extend(ibm_s390x.to_buildkite_steps())

    # GH200 (nightly only)
    if nightly:
        steps.append(GH200_CONFIG.to_buildkite_step())

    # TPU tests (V1 only in CI mode, V0 is fastcheck only)
    steps.append(TPU_V1_TEST.to_buildkite_step())
    steps.append(TPU_V1_TEST_PART2.to_buildkite_step())
    steps.append(TPU_V1_BENCHMARK.to_buildkite_step())

    # Add TPU notification for main branch
    if branch == "main":
        tpu_depends_on = get_tpu_notification_config()
        # Build notification command with proper indentation
        tpu_notif_command = """if [[ $$(buildkite-agent step get "outcome" --step "run-tpu-v1-test") != "passed" || $$(buildkite-agent step get "outcome" --step "run-tpu-v1-test-part2") != "passed" ]]; then
   cat <<- YAML | buildkite-agent pipeline upload
   steps:
     - label: "Notify owners about failing test"
       agents:
         queue: tpu_v6e_queue
       command: echo "TPU V1 Test failed"
       notify:
         - slack:
             channels:
               - "vllm#tpu-ci-notifications"
YAML
fi"""
        steps.append(
            {
                "label": "TPU V1 Test Notification",
                "depends_on": tpu_depends_on,
                "soft_fail": True,
                "agents": {"queue": "tpu_v6e_queue"},
                "commands": tpu_notif_command,
            }
        )

    return steps
