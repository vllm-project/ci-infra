from typing import Optional

from amd import is_amd_device
from constants import DeviceType, Platform

# The NVIDIA lane: its GPUs, and the CPU queues beside them that run the same
# CI images. A step without a device (image builds, default GPU jobs) runs
# there too. A tuple, not a set: a DeviceType does not hash like its value.
CUDA_DEVICES = (
    DeviceType.H100,
    DeviceType.H200,
    DeviceType.H200_18GB,
    DeviceType.H200_35GB,
    DeviceType.B200,
    DeviceType.B200_K8S,
    DeviceType.A100,
    DeviceType.L4,
    DeviceType.GH200,
    DeviceType.DGX_SPARK,
    DeviceType.CPU,
    DeviceType.CPU_SMALL,
    DeviceType.CPU_MEDIUM,
)


def device_platform(device: Optional[str]) -> Optional[Platform]:
    """The platform a job on `device` belongs to. None for hardware that is
    neither (Intel, Arm, Ascend, the CPU backend): it runs only in builds not
    restricted to a platform."""
    if is_amd_device(device):
        return Platform.AMD
    if device is None or device in CUDA_DEVICES:
        return Platform.CUDA
    return None


def runs_on(platform: Optional[Platform], device: Optional[str]) -> bool:
    """Whether a build restricted to `platform` runs a job on `device`.
    A build with no platform runs every job."""
    return platform is None or device_platform(device) == platform
