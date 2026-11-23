"""Compile a step's `vendors:` block into the step dicts the generator reads.

A test is written once, with per-vendor and per-device additions:

    - key: v1-attention
      label: V1 Attention
      source_file_dependencies:
        - vllm/v1/attention/
      vendors:
        nvidia:
          devices:
            h100:
              commands:
                - pytest -v -s v1/attention
        amd:
          env:
            VLLM_ROCM_USE_AITER: "1"
          commands:
            - pytest -v -s v1/attention --ignore=v1/attention/test_mla_backends.py
          devices:
            mi300_dpx:
              timeout_in_minutes: 50

Each device under a vendor is one Buildkite job. Its fields are the test's,
then the vendor's defaults, then the vendor's, then the device's: a more
specific scalar wins, `source_file_dependencies` and `commands` are
concatenated in that order (duplicate dependencies dropped) and `env` is
merged. A vendor's defaults (an AMD job's `depends_on`) beat the test level,
so only the vendor or device level replaces them. One vendor's fields never
reach another vendor's job.

The rest of the generator, and ci_selector, still read the older shape (a
step with a `device`, plus a `mirror.amd` block for its AMD copy), so a
`vendors:` step is compiled into it when the yaml is loaded:

  * the first job of a non-AMD vendor is the step itself, keyed `<key>`;
  * the first AMD job, keyed `amd-<key>`, is that step's `mirror.amd`,
    marked `_exact`: its fields are the whole job, with no union with the
    step's own dependencies, env or commands. A mirror's key is always
    `amd-` plus its step's, so when an explicit key breaks that pairing (on
    either job) the AMD job is a step of its own instead;
  * every other job (an AMD-only test, a second device) is a step of its own;
    an AMD-only test's first job is keyed `amd-<key>`, and a second device in
    one vendor needs a `key` of its own.

Python 3.9 and stdlib only: the generator runs on the CPU queue's python3, and
ci_selector imports this module so the two read the yaml the same way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from amd import AMD_ARTIFACT_STEP, get_amd_label, is_amd_device
from constants import DeviceType

VENDORS_FIELD = "vendors"
DEVICES_FIELD = "devices"
# Set on a compiled `mirror.amd`: its fields are the whole AMD job.
EXACT_MIRROR = "_exact"
MIRROR_HARDWARE = "amd"

# The step fields a test, vendor or device may set. `device` is the devices
# map key, and `group` is the file's.
JOB_FIELDS = (
    "key",
    "label",
    "working_dir",
    "depends_on",
    "commands",
    "agent_tags",
    "num_devices",
    "num_nodes",
    "source_file_dependencies",
    "soft_fail",
    "parallelism",
    "concurrency",
    "concurrency_group",
    "timeout_in_minutes",
    "mount_buildkite_agent",
    "env",
    "retry",
    "optional",
    "no_plugin",
    "no_gpu",
    "dind",
    "automatic_shard",
)
# Fields only the test level may set: the generator does not read them, but
# ci_selector or Buildkite do. Anything else is rejected as a likely typo.
TEST_ONLY_FIELDS = (
    "num_gpus",
    "autorun_on_main",
    "if",
    "allow_dependency_failure",
)
CONCATENATED_FIELDS = ("source_file_dependencies", "commands")
MERGED_FIELDS = ("env",)
# Fields a step's `mirror.amd` block is read for (buildkite_step.py). The rest
# (key, retry, mount_buildkite_agent) mean nothing to an AMD mirror job.
MIRROR_FIELDS = frozenset(JOB_FIELDS) - {"key", "retry", "mount_buildkite_agent"}
# Fields that would make a `vendors:` step ambiguous.
CONFLICTING_FIELDS = ("device", "gpu", "mirror")


class VendorStepError(ValueError):
    pass


@dataclass(frozen=True)
class Vendor:
    name: str
    # Whether a device name belongs to this vendor.
    owns_device: Callable[[Optional[str]], bool]
    # Prepended to the test's key for the vendor's first job.
    key_prefix: str = ""
    # Under the test level: what every job of this vendor gets unless set.
    defaults: Mapping[str, Any] = field(default_factory=dict)
    # The job label from the merged label and the device.
    label: Callable[[str, Optional[str]], str] = lambda label, device: label
    # Whether the vendor may leave out `devices`: one job, on the queue a step
    # without a device gets.
    deviceless: bool = False
    # Whether its first job can be the primary job's mirror.
    mirror: bool = False
    # Fields its jobs never read, so the vendor and its devices may not set
    # them (the test level still may: they are the other vendors').
    unsupported: frozenset = frozenset()


NVIDIA_DEVICES = frozenset(
    device.value
    for device in (
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
    )
)
CPU_DEVICES = frozenset(
    device.value
    for device in (DeviceType.CPU, DeviceType.CPU_SMALL, DeviceType.CPU_MEDIUM)
)

# The vendors a step may name. To add one (intel, ascend, ...), give it its
# devices and what its jobs get by default; a vendor whose jobs run through a
# wrapper of their own also needs that in buildkite_step.py.
VENDORS: Dict[str, Vendor] = {
    "nvidia": Vendor(
        name="nvidia",
        owns_device=lambda device: device in NVIDIA_DEVICES,
        # No device: the default GPU queue, as for a step without one.
        deviceless=True,
    ),
    "amd": Vendor(
        name="amd",
        owns_device=is_amd_device,
        key_prefix="amd-",
        # The image every AMD job runs in. Unlike a `mirror.amd` block (and a
        # plain step on an AMD device), a job here is not dind unless it says
        # so: nearly every AMD job opts out.
        defaults={"depends_on": [AMD_ARTIFACT_STEP], "dind": False},
        label=get_amd_label,
        mirror=True,
        # Never read for an AMD job (buildkite_step.py).
        unsupported=frozenset({"retry", "mount_buildkite_agent"}),
    ),
    "cpu": Vendor(
        name="cpu",
        owns_device=lambda device: device in CPU_DEVICES,
    ),
}


def has_vendors(raw: Any) -> bool:
    return isinstance(raw, dict) and VENDORS_FIELD in raw


def expand_vendor_steps(raw_steps: List[Any]) -> List[Any]:
    """The yaml's steps with every `vendors:` step compiled into the step
    dicts it stands for. Other steps are returned as they are; nothing is
    modified in place."""
    expanded: List[Any] = []
    for raw in raw_steps:
        if has_vendors(raw):
            expanded.extend(compile_vendor_step(raw))
        else:
            expanded.append(raw)
    return expanded


def expand_vendor_document(doc: Any) -> Any:
    """A parsed job file with its `vendors:` steps compiled; anything else
    as it is. For code that compares a file's raw entries."""
    entries = doc.get("steps") if isinstance(doc, dict) else None
    if not isinstance(entries, list) or not any(has_vendors(e) for e in entries):
        return doc
    return {**doc, "steps": expand_vendor_steps(entries)}


@dataclass
class _Job:
    vendor: Vendor
    device: Optional[str]
    key: str
    fields: Dict[str, Any]


def compile_vendor_step(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    test_key = raw.get("key")
    if not isinstance(test_key, str) or not test_key:
        raise VendorStepError(
            f"A step with `{VENDORS_FIELD}` needs a `key`: {raw.get('label')!r}"
        )
    where = f"step {test_key}"
    conflicting = [name for name in CONFLICTING_FIELDS if name in raw]
    if conflicting:
        raise VendorStepError(
            f"{where}: `{VENDORS_FIELD}` replaces "
            + ", ".join(f"`{name}`" for name in conflicting)
            + "; set the device under its vendor's `devices`"
        )
    vendors = raw[VENDORS_FIELD]
    if not isinstance(vendors, dict) or not vendors:
        raise VendorStepError(f"{where}: `{VENDORS_FIELD}` must be a non-empty map")

    test_fields = {
        name: value
        for name, value in raw.items()
        if name in JOB_FIELDS and name != "key"
    }
    # Fields the generator does not read (`num_gpus`, `if`, ...) stay on the
    # step jobs as before; a mirror never read them either.
    extra = {
        name: value
        for name, value in raw.items()
        if name not in JOB_FIELDS and name != VENDORS_FIELD
    }
    unknown = sorted(name for name in extra if name not in TEST_ONLY_FIELDS)
    if unknown:
        raise VendorStepError(
            f"{where}: unknown field(s) " + ", ".join(f"`{name}`" for name in unknown)
        )

    jobs: List[_Job] = []
    for vendor_name, vendor_block in vendors.items():
        vendor = VENDORS.get(vendor_name)
        if vendor is None:
            raise VendorStepError(
                f"{where}: unknown vendor {vendor_name!r}; expected one of "
                + ", ".join(sorted(VENDORS))
            )
        vendor_where = f"{where}, vendor {vendor_name}"
        vendor_block = _block(vendor_block, vendor_where, vendor, allow_devices=True)
        devices = vendor_block.pop(DEVICES_FIELD, None)
        if devices is None:
            if not vendor.deviceless:
                raise VendorStepError(f"{vendor_where}: needs `{DEVICES_FIELD}`")
            device_blocks: List[Tuple[Optional[str], Dict[str, Any]]] = [(None, {})]
        else:
            if not isinstance(devices, dict) or not devices:
                raise VendorStepError(
                    f"{vendor_where}: `{DEVICES_FIELD}` must be a non-empty map"
                )
            device_blocks = []
            for device, device_block in devices.items():
                device_where = f"{vendor_where}, device {device}"
                if not vendor.owns_device(device):
                    raise VendorStepError(f"{device_where}: not a {vendor_name} device")
                device_blocks.append(
                    (
                        device,
                        _block(device_block, device_where, vendor, allow_devices=False),
                    )
                )
        vendor_key = vendor_block.pop("key", None)
        for index, (device, device_block) in enumerate(device_blocks):
            device_key = device_block.pop("key", None)
            if index and device_key is None:
                raise VendorStepError(
                    f"{vendor_where}: device {device} is not the vendor's first, "
                    "so it needs a `key` of its own"
                )
            key = device_key or (vendor_key if index == 0 else None)
            key = key or f"{vendor.key_prefix}{test_key}"
            merged = _merge(test_fields, vendor.defaults, vendor_block, device_block)
            label = merged.get("label")
            if not isinstance(label, str) or not label:
                raise VendorStepError(f"{vendor_where}: no `label` for {key}")
            merged["label"] = vendor.label(label, device)
            jobs.append(_Job(vendor, device, key, merged))

    seen = set()
    for job in jobs:
        if job.key in seen:
            raise VendorStepError(f"{where}: two jobs are keyed {job.key}")
        seen.add(job.key)

    primary = next((job for job in jobs if not job.vendor.mirror), None)
    mirror = None
    if primary is not None:
        mirror = next(
            (
                job
                for job in jobs
                if job.vendor.name == MIRROR_HARDWARE
                and job.key == f"amd-{primary.key}"
            ),
            None,
        )

    compiled = []
    for job in jobs:
        if job is mirror:
            continue
        step = dict(extra)
        step.update(job.fields)
        step["key"] = job.key
        if job.device is not None:
            step["device"] = job.device
        if job is primary and mirror is not None:
            block = {
                name: value
                for name, value in mirror.fields.items()
                if name in MIRROR_FIELDS
            }
            block["device"] = mirror.device
            block[EXACT_MIRROR] = True
            step["mirror"] = {MIRROR_HARDWARE: block}
        compiled.append(step)
    return compiled


def _block(
    value: Any, where: str, vendor: Vendor, allow_devices: bool
) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise VendorStepError(f"{where}: must be a map")
    allowed = set(JOB_FIELDS) - vendor.unsupported
    allowed |= {DEVICES_FIELD} if allow_devices else set()
    unknown = sorted(name for name in value if name not in allowed)
    if unknown:
        raise VendorStepError(
            f"{where}: unknown field(s) " + ", ".join(f"`{name}`" for name in unknown)
        )
    return dict(value)


def _merge(*levels: Mapping[str, Any]) -> Dict[str, Any]:
    """The levels, least specific first. A null value is no value."""
    merged: Dict[str, Any] = {}
    for level in levels:
        for name, value in level.items():
            if value is None:
                continue
            if name in CONCATENATED_FIELDS:
                if not isinstance(value, list):
                    raise VendorStepError(f"`{name}` must be a list, not {value!r}")
                combined = list(merged.get(name) or [])
                for item in value:
                    if name == "commands" or item not in combined:
                        combined.append(item)
                merged[name] = combined
            elif name in MERGED_FIELDS:
                if not isinstance(value, dict):
                    raise VendorStepError(f"`{name}` must be a map, not {value!r}")
                combined_env = dict(merged.get(name) or {})
                combined_env.update(value)
                merged[name] = combined_env
            elif isinstance(value, list):
                merged[name] = list(value)
            elif isinstance(value, dict):
                merged[name] = dict(value)
            else:
                merged[name] = value
    return merged
