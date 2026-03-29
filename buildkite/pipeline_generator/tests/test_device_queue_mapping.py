import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch

import pytest

from constants import DeviceType, AgentQueue, DEVICE_TO_QUEUE
from step import Step
from buildkite_step import get_agent_queue, _create_amd_mirror_step


# ---------------------------------------------------------------------------
# 1. DEVICE_TO_QUEUE completeness
# ---------------------------------------------------------------------------

def test_device_to_queue_has_all_device_types():
    """Every DeviceType that has a matching AgentQueue name should be in the map."""
    agent_queue_names = {q.name for q in AgentQueue}
    for device in DeviceType:
        if device.name in agent_queue_names:
            assert device in DEVICE_TO_QUEUE, (
                f"{device.name} has a matching AgentQueue but is missing from DEVICE_TO_QUEUE"
            )


@pytest.mark.parametrize(
    "device, expected_queue",
    [
        (DeviceType.H100, AgentQueue.MITHRIL_H100),
        (DeviceType.A100, AgentQueue.A100),
        (DeviceType.CPU, AgentQueue.CPU_PREMERGE_US_EAST_1),
        (DeviceType.CPU_SMALL, AgentQueue.SMALL_CPU_PREMERGE),
        (DeviceType.CPU_MEDIUM, AgentQueue.MEDIUM_CPU_PREMERGE),
        (DeviceType.H200, AgentQueue.H200),
        (DeviceType.B200, AgentQueue.B200),
        (DeviceType.INTEL_CPU, AgentQueue.INTEL_CPU),
        (DeviceType.INTEL_HPU, AgentQueue.INTEL_HPU),
        (DeviceType.INTEL_GPU, AgentQueue.INTEL_GPU),
        (DeviceType.ARM_CPU, AgentQueue.ARM_CPU),
        (DeviceType.GH200, AgentQueue.GH200),
        (DeviceType.ASCEND, AgentQueue.ASCEND),
        (DeviceType.AMD_CPU, AgentQueue.AMD_CPU),
        (DeviceType.AMD_MI325_4, AgentQueue.AMD_MI325_4),
        (DeviceType.AMD_MI250_1, AgentQueue.AMD_MI250_1),
        (DeviceType.AMD_MI355_8, AgentQueue.AMD_MI355_8),
    ],
)
def test_device_to_queue_representative_mappings(device, expected_queue):
    """Spot-check that representative device types map to the correct queue."""
    assert DEVICE_TO_QUEUE[device] == expected_queue


# ---------------------------------------------------------------------------
# 2. get_agent_queue() with device types
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "device, expected_queue",
    [
        (DeviceType.H100, AgentQueue.MITHRIL_H100),
        (DeviceType.CPU, AgentQueue.CPU_PREMERGE_US_EAST_1),
        (DeviceType.INTEL_CPU, AgentQueue.INTEL_CPU),
        (DeviceType.AMD_CPU, AgentQueue.AMD_CPU),
    ],
)
@patch("buildkite_step.get_global_config", return_value={"branch": "main"})
def test_get_agent_queue_device_types(mock_config, device, expected_queue):
    step = Step(label="test", device=device.value)
    assert get_agent_queue(step) == expected_queue


# ---------------------------------------------------------------------------
# 3. get_agent_queue() docker special cases
# ---------------------------------------------------------------------------

@patch("buildkite_step.get_global_config", return_value={"branch": "main"})
def test_docker_step_main_branch(mock_config):
    step = Step(label=":docker: build image")
    assert get_agent_queue(step) == AgentQueue.CPU_POSTMERGE_US_EAST_1


@patch("buildkite_step.get_global_config", return_value={"branch": "feature-branch"})
def test_docker_step_non_main_branch(mock_config):
    step = Step(label=":docker: build image")
    assert get_agent_queue(step) == AgentQueue.CPU_PREMERGE_US_EAST_1


@patch("buildkite_step.get_global_config", return_value={"branch": "main"})
def test_docker_arm64_step_main_branch(mock_config):
    step = Step(label=":docker: build arm64 image")
    assert get_agent_queue(step) == AgentQueue.ARM64_CPU_POSTMERGE


@patch("buildkite_step.get_global_config", return_value={"branch": "feature-branch"})
def test_docker_arm64_step_non_main_branch(mock_config):
    step = Step(label=":docker: build arm64 image")
    assert get_agent_queue(step) == AgentQueue.ARM64_CPU_PREMERGE


# ---------------------------------------------------------------------------
# 4. get_agent_queue() GPU fallback
# ---------------------------------------------------------------------------

@patch("buildkite_step.get_global_config", return_value={"branch": "main"})
def test_gpu_fallback_num_devices_2(mock_config):
    step = Step(label="test", num_devices=2)
    assert get_agent_queue(step) == AgentQueue.GPU_4


@patch("buildkite_step.get_global_config", return_value={"branch": "main"})
def test_gpu_fallback_num_devices_4(mock_config):
    step = Step(label="test", num_devices=4)
    assert get_agent_queue(step) == AgentQueue.GPU_4


@patch("buildkite_step.get_global_config", return_value={"branch": "main"})
def test_gpu_fallback_num_devices_1(mock_config):
    step = Step(label="test", num_devices=1)
    assert get_agent_queue(step) == AgentQueue.GPU_1


@patch("buildkite_step.get_global_config", return_value={"branch": "main"})
def test_gpu_fallback_no_device_no_num_devices(mock_config):
    step = Step(label="test")
    assert get_agent_queue(step) == AgentQueue.GPU_1


# ---------------------------------------------------------------------------
# 5. _create_amd_mirror_step() uses centralized mapping
# ---------------------------------------------------------------------------

def test_create_amd_mirror_step_valid_device():
    step = Step(label="some test", working_dir="/workspace")
    amd_config = {"device": DeviceType.AMD_MI325_1}
    result = _create_amd_mirror_step(step, ["echo hello"], amd_config)
    assert result.agents["queue"] == AgentQueue.AMD_MI325_1


def test_create_amd_mirror_step_invalid_device():
    step = Step(label="some test", working_dir="/workspace")
    amd_config = {"device": "nonexistent_device"}
    with pytest.raises(ValueError, match="Invalid AMD device"):
        _create_amd_mirror_step(step, ["echo hello"], amd_config)
