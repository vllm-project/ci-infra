"""Fastcheck-specific hardware test generation."""
from typing import List, Dict, Any

from .hardware_steps import generate_all_hardware_tests
from ..shared.job_labels import HardwareLabels, BlockLabels
from ..shared.script_paths import Scripts


def _add_tpu_v0_tests() -> List[Dict[str, Any]]:
    """Add TPU V0 tests (fastcheck-only)."""
    from ..hardware_config import get_tpu_v0_notification_config
    
    return [
        {
            "block": BlockLabels.RUN_TPU_V0_TEST,
            "key": "block-tpu-v0",
            "depends_on": None
        },
        {
            "label": HardwareLabels.TPU_V0_TEST,
            "key": "run-tpu-v0-test",
            "depends_on": "block-tpu-v0",
            "soft_fail": True,
            "agents": {"queue": "tpu_v5_queue"},
            "commands": [
                f'if [[ -f "{Scripts.RUN_TPU_TEST}" ]]; then bash {Scripts.RUN_TPU_TEST}; fi',
                "yes | docker system prune -a"
            ]
        },
        get_tpu_v0_notification_config()
    ]


def _add_tpu_v1_tests(all_hw_tests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add TPU V1 tests for fastcheck."""
    steps = []
    
    for test in all_hw_tests:
        if isinstance(test, dict) and test.get("label", "") == "TPU V1 Test":
            steps.append({
                "block": "Run TPU V1 Test",
                "key": "block-tpu-v1",
                "depends_on": None
            })
            test_copy = test.copy()
            test_copy["depends_on"] = "block-tpu-v1"
            if "timeout_in_minutes" in test_copy:
                del test_copy["timeout_in_minutes"]
            if "soft_fail" in test_copy:
                del test_copy["soft_fail"]
            steps.append(test_copy)
    
    # Add notification
    tpu_v1_notif_cmd = '''if [ $$(buildkite-agent step get "outcome" --step "run-tpu-v1-test") != "passed" ]; then
   cat <<- YAML | buildkite-agent pipeline upload
   steps:
     - label: "Notify owners about failing test"
       agents:
         queue: tpu_v5_queue
       command: echo "TPU V1 Test failed"
       notify:
         - slack:
             channels:
               - "#tpu-ci-notifications"
YAML
fi
'''
    notification_step: Dict[str, Any] = {
        "label": "TPU V1 Test Notification",
        "depends_on": "run-tpu-v1-test",
        "soft_fail": True,
        "agents": {"queue": "tpu_v5_queue"},
        "commands": tpu_v1_notif_cmd
    }
    steps.append(notification_step)
    
    return steps


def _add_gh200_test() -> List[Dict[str, Any]]:
    """Add GH200 test for fastcheck."""
    from ..hardware_config import GH200_CONFIG
    
    gh200_step = GH200_CONFIG.to_buildkite_step()
    gh200_step["depends_on"] = "block-gh200"
    
    return [
        {
            "block": "Run GH200 Test",
            "depends_on": None,
            "key": "block-gh200"
        },
        gh200_step
    ]


def _add_intel_tests(all_hw_tests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add Intel tests for fastcheck."""
    steps = []
    
    for test in all_hw_tests:
        if isinstance(test, dict):
            label = test.get("label", "")
            if "Intel CPU Test" in label:
                steps.append({
                    "block": "Run Intel CPU test",
                    "key": "block-intel-cpu",
                    "depends_on": None
                })
                test_copy = test.copy()
                test_copy["depends_on"] = "block-intel-cpu"
                steps.append(test_copy)
            elif "Intel GPU Test" in label:
                steps.append({
                    "block": "Run Intel GPU test",
                    "key": "block-intel-gpu",
                    "depends_on": None
                })
                test_copy = test.copy()
                test_copy["depends_on"] = "block-intel-gpu"
                steps.append(test_copy)
    
    return steps


def generate_fastcheck_hardware_tests(branch: str, nightly: bool) -> List[Dict[str, Any]]:
    """
    Generate hardware tests for fastcheck mode.
    
    Fastcheck includes (in order):
    - TPU V0 (blocked) + notification
    - TPU V1 (blocked) + notification  
    - GH200 (blocked)
    - AMD Tests (inserted by caller)
    - Intel CPU (blocked)
    - Intel GPU (blocked)
    
    Note: Neuron is added separately at the beginning of the pipeline.
    """
    steps = []
    all_hw_tests = generate_all_hardware_tests(branch, nightly)
    
    steps.extend(_add_tpu_v0_tests())
    steps.extend(_add_tpu_v1_tests(all_hw_tests))
    steps.extend(_add_gh200_test())
    # AMD Tests inserted by caller here (at position 8)
    steps.extend(_add_intel_tests(all_hw_tests))
    
    return steps

