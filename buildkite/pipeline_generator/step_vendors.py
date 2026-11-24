"""Compile a test step's `devices:` map into the step dicts the generator reads.

A step names its hardware only as the keys of `devices:`. Each key is one job
on that device; more jobs on the same device are that device's `variants:`:

    - label: V1 Attention
      key: v1-attention
      devices:
        h100:
          timeout_in_minutes: 85
          commands:
            - pytest -v -s v1/attention
          variants:
            eager:
              env:
                VLLM_ENFORCE_EAGER: "1"
        b200-k8s:
          same_as: h100
          timeout_in_minutes: 30
        mi300_1:
          timeout_in_minutes: 125
          source_file_dependencies:
            - vllm/platforms/rocm.py
          commands:
            - pytest -v -s v1/attention --ignore=v1/attention/test_mla_backends.py
      source_file_dependencies:
        - vllm/v1/attention/

Fields (each has exactly one category):

  * step only: label, key, depends_on (other steps, never an image), if,
    autorun_on_main, allow_dependency_failure;
  * software, at the step level (every job's) or on a device or variant (that
    job's): commands, source_file_dependencies, env, working_dir,
    automatic_shard, mount_buildkite_agent, no_plugin;
  * machine, only on a device or variant: timeout_in_minutes, optional,
    soft_fail, parallelism, num_devices (NVIDIA only), num_nodes, agent_tags,
    retry, concurrency, concurrency_group, no_gpu, dind (only `true`);
  * identity, on a device or variant: key, label; on a device only: same_as,
    variants.

One job is resolved like this (no field is ever shadowed):

  * step + device: the device adds `source_file_dependencies` entries and
    `env` keys (repeating one of the step's is an error); the other software
    fields are set at the step level or on the device, never both;
  * `same_as: <sibling>` starts the device from an earlier device of the same
    vendor (which has no `same_as` itself), minus its key, label and variants;
    written fields replace the copied ones, except that dependencies add and
    `env` merges key by key;
  * a variant starts from its device's resolved job, minus key and label:
    dependencies add, anything else it writes replaces (including a step-level
    `commands`, the only sanctioned override), and it must change something;
  * the device registry (DEVICES) adds the rest: the image step the job
    depends on (AMD: image-build-amd; CPU: image-build-cpu; NVIDIA: the file's
    `depends_on`; a device may name its own, e.g. dgx-spark), the label's
    icon, tag and `[Nightly]` (from `optional: true`), the `amd-` key prefix,
    no dind and the pool's GPU count for AMD.

Keys, with K the step's `key` and devices in map order: the first non-AMD
device is K, any other non-AMD device K-<key_tag>; the first AMD device is
amd-K, any other amd-K-<key_tag>; a variant is <its device job's key>-<name>.
A `key:` on a device or variant is used verbatim (its variants derive from it).

The rest of the generator, and ci_selector, read the older shape (a step with
a `device`, plus a `mirror.amd` block for its AMD copy), so a `devices:` step
is compiled into it when the yaml is loaded: the first non-AMD device's job is
the step; the AMD job keyed `amd-` plus that step's key is its `mirror.amd`,
marked `_exact` (its fields are the whole job, with no union with the step's);
every other job is a step of its own. Steps without `devices:` (the legacy
`device` + `mirror` form) are returned as they are.

Python 3.9 and stdlib (plus PyYAML) only: the generator runs on the CPU
queue's python3, and ci_selector imports this module so the two read the yaml
the same way.
"""

from __future__ import annotations

import copy
import difflib
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

import yaml

from amd import AMD_DEVICES
from constants import (
    CPU_IMAGE_BUILD_STEP,
    NON_AMD_DEVICES,
    VENDOR_ICONS,
    VENDOR_KEY_PREFIXES,
    DeviceInfo,
    Vendor,
)

# The device registry: every device a `devices:` step may name.
DEVICES: Dict[str, DeviceInfo] = {**NON_AMD_DEVICES, **AMD_DEVICES}

DEVICES_FIELD = "devices"
VARIANTS_FIELD = "variants"
SAME_AS_FIELD = "same_as"
# Set on a compiled `mirror.amd`: its fields are the whole AMD job.
EXACT_MIRROR = "_exact"
MIRROR_HARDWARE = "amd"

# The categories, each in the order the lint (L4) writes them.
STEP_ONLY_FIELDS = (
    "label",
    "key",
    "depends_on",
    "if",
    "autorun_on_main",
    "allow_dependency_failure",
)
# Step-only fields the generator does not read but ci_selector or Buildkite
# do; every job of the step carries them.
PASSED_THROUGH_FIELDS = ("if", "autorun_on_main", "allow_dependency_failure")
SOFTWARE_FIELDS = (
    "automatic_shard",
    "mount_buildkite_agent",
    "no_plugin",
    "working_dir",
    "env",
    "source_file_dependencies",
    "commands",
)
MACHINE_FIELDS = (
    "optional",
    "soft_fail",
    "num_devices",
    "num_nodes",
    "parallelism",
    "timeout_in_minutes",
    "retry",
    "concurrency",
    "concurrency_group",
    "agent_tags",
    "no_gpu",
    "dind",
)
DEPENDENCIES_FIELD = "source_file_dependencies"
ENV_FIELD = "env"
# Software fields a step and its device may not both set.
SET_ONCE_FIELDS = tuple(
    name for name in SOFTWARE_FIELDS if name not in (DEPENDENCIES_FIELD, ENV_FIELD)
)
DEVICE_ORDER = (SAME_AS_FIELD, "key", "label") + MACHINE_FIELDS + SOFTWARE_FIELDS
VARIANT_ORDER = ("key", "label") + MACHINE_FIELDS + SOFTWARE_FIELDS
STEP_ORDER = ("label", "key", DEVICES_FIELD) + SOFTWARE_FIELDS + STEP_ONLY_FIELDS[2:]
VENDOR_ORDER = (Vendor.NVIDIA, Vendor.AMD, Vendor.CPU)

# The step fields the compiled step dicts carry (the Step model's, less what
# the loader sets).
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
# Fields a step's `mirror.amd` block is read for (buildkite_step.py). The rest
# (key, retry, mount_buildkite_agent) mean nothing to an AMD mirror job.
MIRROR_FIELDS = frozenset(JOB_FIELDS) - {"key", "retry", "mount_buildkite_agent"}
# Never read for an AMD job (buildkite_step.py), so an AMD device may not set
# them.
AMD_IGNORED_FIELDS = ("retry", "mount_buildkite_agent")

KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
VARIANT_NAME_PATTERN = re.compile(r"^[a-z0-9-]+$")
# What the generator puts in front of a title.
LABEL_PREFIX = re.compile(r"^\s*(:[a-z0-9_+-]+:|\(|\[Nightly\])")

_WHERE_HARDWARE_LIVES = (
    "a step names its hardware only as keys under `devices:` (e.g. `h100:`)"
)
_WHERE_VARIANTS_LIVE = (
    "a job's hardware is the `devices:` key it is written under; another job "
    "on the same device is `devices.<device>.variants.<name>`"
)
# Removed or never-valid fields of a `devices:` step, with where each concept
# lives now.
REMOVED_FIELDS = {
    "device": _WHERE_HARDWARE_LIVES,
    "gpu": _WHERE_HARDWARE_LIVES,
    "mirror": "an AMD job is a device under `devices:` (e.g. `mi355_1:`), "
    "not a `mirror.amd` block",
    "vendors": "the `vendors:` form is gone: list every device under "
    "`devices:` and put what all jobs share at the step level",
    "target": _WHERE_VARIANTS_LIVE,
    "run_on": _WHERE_VARIANTS_LIVE,
    "num_gpus": "write `num_devices` on the NVIDIA device (an AMD pool has "
    "its own GPU count)",
}
_MACHINE_HINTS = {
    "optional": "write `optional: true` on the nightly device",
    "dind": "AMD jobs run without dind (device registry); only a device may "
    "opt in with `dind: true`",
}


class DeviceStepError(ValueError):
    """A `devices:` step that does not compile. `code` is the rule (E1-E9)."""

    def __init__(self, code: str, where: str, message: str):
        self.code = code
        self.where = where
        self.detail = message
        super().__init__(f"{code} {where}: {message}")


@dataclass
class Job:
    """One resolved job of a `devices:` step."""

    key: str
    device: str
    info: DeviceInfo
    # The title (no icon, tag or [Nightly]) and the full label.
    title: str
    label: str
    # The resolved software and machine fields.
    fields: Dict[str, Any]
    # From the registry and the step; None leaves it to the file's.
    depends_on: Optional[List[str]]
    variant: Optional[str] = None
    pinned: bool = False

    @property
    def vendor(self) -> Vendor:
        return self.info.vendor


# ---- entry points -------------------------------------------------------------


def is_device_step(raw: Any) -> bool:
    return isinstance(raw, dict) and DEVICES_FIELD in raw


def check_written_step(raw: Any) -> None:
    """Reject what only the compiler may write (a `mirror.amd` marked
    `_exact`) and a leftover `vendors:` step, which a legacy step would
    otherwise silently ignore."""
    if not isinstance(raw, dict):
        return
    where = f"step {raw.get('key') or raw.get('label')!r}"
    if "vendors" in raw:
        raise DeviceStepError("E2", where, f"`vendors`: {REMOVED_FIELDS['vendors']}")
    mirror = raw.get("mirror")
    amd = mirror.get(MIRROR_HARDWARE) if isinstance(mirror, dict) else None
    if isinstance(amd, dict) and EXACT_MIRROR in amd:
        raise DeviceStepError(
            "E2",
            where,
            f"`{EXACT_MIRROR}` is set when a `{DEVICES_FIELD}:` step is "
            f"compiled; write the step with `{DEVICES_FIELD}:` instead",
        )


def expand_steps(
    raw_steps: List[Any], group_depends_on: Optional[List[str]] = None
) -> List[Any]:
    """The yaml's steps with every `devices:` step compiled into the step
    dicts it stands for. Other steps are returned as they are; nothing is
    modified in place. `group_depends_on` is the file's `depends_on`."""
    expanded: List[Any] = []
    for raw in raw_steps:
        if is_device_step(raw):
            expanded.extend(compile_device_step(raw, group_depends_on))
        else:
            check_written_step(raw)
            expanded.append(raw)
    return expanded


def expand_document(doc: Any) -> Any:
    """A parsed job file with its `devices:` steps compiled; anything else
    as it is. For code that compares a file's raw entries."""
    entries = doc.get("steps") if isinstance(doc, dict) else None
    if not isinstance(entries, list) or not any(
        is_device_step(e) or (isinstance(e, dict) and "vendors" in e) for e in entries
    ):
        return doc
    return {**doc, "steps": expand_steps(entries, doc.get("depends_on"))}


def compile_device_step(
    raw: Dict[str, Any], group_depends_on: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """The step dicts of one `devices:` step, in the older step + `mirror.amd`
    shape."""
    jobs = resolve_step(raw, group_depends_on)
    passed = {name: raw[name] for name in PASSED_THROUGH_FIELDS if name in raw}
    # The non-AMD device job keyed K (else the first non-AMD device job).
    candidates = [
        job for job in jobs if job.vendor != Vendor.AMD and job.variant is None
    ]
    primary = next(
        (job for job in candidates if job.key == raw["key"]),
        candidates[0] if candidates else None,
    )
    mirror = None
    if primary is not None:
        mirror = next(
            (
                job
                for job in jobs
                if job.vendor == Vendor.AMD and job.key == f"amd-{primary.key}"
            ),
            None,
        )
    compiled = []
    for job in jobs:
        if job is mirror:
            continue
        step = dict(passed)
        step.update(job_step_fields(job))
        if job is primary and mirror is not None:
            block = {
                name: value
                for name, value in job_step_fields(mirror).items()
                if name in MIRROR_FIELDS or name == "device"
            }
            block[EXACT_MIRROR] = True
            step["mirror"] = {MIRROR_HARDWARE: block}
        compiled.append(step)
    return compiled


def job_step_fields(job: Job) -> Dict[str, Any]:
    """A job as the fields of a step dict."""
    step: Dict[str, Any] = {"label": job.label, "key": job.key, "device": job.device}
    step.update(copy.deepcopy(job.fields))
    if job.depends_on is not None:
        step["depends_on"] = list(job.depends_on)
    if job.vendor == Vendor.AMD:
        step["dind"] = bool(job.fields.get("dind", False))
        step["num_devices"] = job.info.gpus
    return step


# ---- resolving the jobs ----------------------------------------------------------


def resolve_step(raw: Any, group_depends_on: Optional[List[str]] = None) -> List[Job]:
    """Every job of a `devices:` step, in map order (each device, then its
    variants). Raises DeviceStepError (E1-E8) for a step that breaks a rule."""
    if not isinstance(raw, dict):
        raise DeviceStepError("E2", "step", "a step must be a map")
    key = raw.get("key")
    where = f"step {key or raw.get('label')!r}"
    if not isinstance(key, str) or not KEY_PATTERN.match(key):
        raise DeviceStepError(
            "E2", where, "a `devices:` step needs a `key` ([a-zA-Z0-9_-]+)"
        )
    where = f"step {key}"
    title = _title(raw.get("label"), where, required=True)

    step_software: Dict[str, Any] = {}
    for name, value in raw.items():
        if name == DEVICES_FIELD or name in STEP_ONLY_FIELDS:
            continue
        if name in SOFTWARE_FIELDS:
            step_software[name] = _checked(name, value, where)
        elif name in MACHINE_FIELDS:
            hint = _MACHINE_HINTS.get(name, f"`{name}` belongs on each device")
            raise DeviceStepError(
                "E4", where, f"`{name}` is a machine field, set per device: {hint}"
            )
        else:
            _reject(name, where, STEP_ORDER)
    extra_depends_on = _step_depends_on(raw.get("depends_on"), where)

    devices = raw.get(DEVICES_FIELD)
    if not isinstance(devices, dict) or not devices:
        raise DeviceStepError(
            "E2", where, "`devices` must be a map of device names (e.g. `h100:`)"
        )
    entries: Dict[str, Dict[str, Any]] = {}
    for device, entry in devices.items():
        entries[device] = _device_entry(device, entry, f"{where}, device {device}")

    keys = _device_keys(key, entries, where)
    jobs: List[Job] = []
    for device, entry in entries.items():
        info = DEVICES[device]
        device_where = f"{where}, device {device}"
        own = _same_as(device, entry, entries, device_where)
        _check_machine(info, device, own, device_where)
        fields = _merge_into_step(step_software, own, device_where)
        device_title = (
            _title(entry["label"], device_where) if "label" in entry else title
        )
        depends_on = _job_depends_on(info, extra_depends_on, group_depends_on)
        device_job = Job(
            key=keys[device],
            device=device,
            info=info,
            title=device_title,
            label=job_label(info, device_title, fields),
            fields=fields,
            depends_on=depends_on,
            pinned="key" in entry,
        )
        jobs.append(device_job)
        for name, body in (entry.get(VARIANTS_FIELD) or {}).items():
            variant_where = f"{device_where}, variant {name}"
            _check_machine(info, device, body, variant_where)
            variant_fields = _variant_fields(device_job.fields, body, variant_where)
            # `label:` replaces the title only; the ` [<name>]` that tells
            # the variant from its device job stays.
            variant_title = (
                _title(body["label"], variant_where)
                if "label" in body
                else device_title
            ) + f" [{name}]"
            jobs.append(
                Job(
                    key=body.get("key") or f"{device_job.key}-{name}",
                    device=device,
                    info=info,
                    title=variant_title,
                    label=job_label(info, variant_title, variant_fields),
                    fields=variant_fields,
                    depends_on=depends_on,
                    variant=name,
                    pinned="key" in body,
                )
            )

    seen: Dict[str, Job] = {}
    for job in jobs:
        if job.key in seen:
            raise DeviceStepError(
                "E8",
                where,
                f"two jobs are keyed {job.key} ({_job_name(seen[job.key])} and "
                f"{_job_name(job)}); pin a different `key:` on one of them",
            )
        seen[job.key] = job
    return jobs


def job_label(info: DeviceInfo, title: str, fields: Mapping[str, Any]) -> str:
    """`<icon> (<TAG>) [Nightly ]<title>`; a variant's title ends in
    ` [<name>]`."""
    nightly = "[Nightly] " if fields.get("optional") else ""
    return f"{VENDOR_ICONS[info.vendor]} ({info.tag}) {nightly}{title}"


def default_keys(key: str, devices: List[Tuple[str, Optional[str]]]) -> Dict[str, str]:
    """Each device's job key: `devices` is (device, pinned key or None) in map
    order. The first non-AMD device is `key`, the first AMD one `amd-<key>`;
    any other is suffixed with its key_tag."""
    keys: Dict[str, str] = {}
    first: Dict[bool, bool] = {}
    for device, pinned in devices:
        info = DEVICES[device]
        is_amd = info.vendor == Vendor.AMD
        prefix = VENDOR_KEY_PREFIXES.get(info.vendor, "")
        default = (
            f"{prefix}{key}"
            if is_amd not in first
            else (f"{prefix}{key}-{info.key_tag}")
        )
        first[is_amd] = True
        keys[device] = pinned or default
    return keys


def _device_keys(
    key: str, entries: Dict[str, Dict[str, Any]], where: str
) -> Dict[str, str]:
    unpinned: Dict[str, List[str]] = {}
    for device, entry in entries.items():
        if "key" not in entry:
            unpinned.setdefault(DEVICES[device].key_tag, []).append(device)
    for tag, devices in unpinned.items():
        if len(devices) > 1:
            raise DeviceStepError(
                "E8",
                where,
                f"devices {' and '.join(devices)} share the key tag {tag!r}, so "
                "their keys would swap if they were reordered; pin `key:` on "
                "all but one of them",
            )
    return default_keys(
        key, [(device, entry.get("key")) for device, entry in entries.items()]
    )


def _job_name(job: Job) -> str:
    return job.device + (f" variant {job.variant}" if job.variant else "")


def _device_entry(device: Any, entry: Any, where: str) -> Dict[str, Any]:
    if device not in DEVICES:
        raise DeviceStepError(
            "E1",
            where,
            f"{device!r} is not in the device registry; expected one of "
            + ", ".join(sorted(DEVICES)),
        )
    if entry is None:
        return {}
    if not isinstance(entry, dict):
        raise DeviceStepError("E2", where, "a device entry must be a map (or empty)")
    checked: Dict[str, Any] = {}
    for name, value in entry.items():
        if name == SAME_AS_FIELD:
            checked[name] = value
        elif name == "key":
            checked[name] = _pinned_key(value, where)
        elif name == "label":
            checked[name] = value
        elif name == VARIANTS_FIELD:
            checked[name] = _variants(value, where)
        elif name in MACHINE_FIELDS or name in SOFTWARE_FIELDS:
            checked[name] = _checked(name, value, where)
        elif name in STEP_ONLY_FIELDS:
            raise DeviceStepError(
                "E2", where, f"`{name}` is set once, at the step level"
            )
        else:
            _reject(name, where, DEVICE_ORDER + (VARIANTS_FIELD,))
    return checked


def _variants(value: Any, where: str) -> Dict[str, Dict[str, Any]]:
    if not isinstance(value, dict) or not value:
        raise DeviceStepError(
            "E6", where, "`variants` must be a map of variant names to changes"
        )
    variants: Dict[str, Dict[str, Any]] = {}
    for name, body in value.items():
        variant_where = f"{where}, variant {name}"
        if not isinstance(name, str) or not VARIANT_NAME_PATTERN.match(name):
            raise DeviceStepError(
                "E1", variant_where, "a variant name must match [a-z0-9-]+"
            )
        if body is None or body == {}:
            raise DeviceStepError(
                "E6", variant_where, "a variant must change at least one field"
            )
        if not isinstance(body, dict):
            raise DeviceStepError("E2", variant_where, "a variant must be a map")
        checked: Dict[str, Any] = {}
        for field_name, field_value in body.items():
            if field_name == SAME_AS_FIELD:
                raise DeviceStepError(
                    "E5",
                    variant_where,
                    "a variant cannot use `same_as`: it already starts from "
                    "its device's job",
                )
            if field_name == "key":
                checked[field_name] = _pinned_key(field_value, variant_where)
            elif field_name == "label":
                checked[field_name] = field_value
            elif field_name in MACHINE_FIELDS or field_name in SOFTWARE_FIELDS:
                checked[field_name] = _checked(field_name, field_value, variant_where)
            elif field_name == VARIANTS_FIELD:
                raise DeviceStepError(
                    "E2", variant_where, "variants cannot have variants"
                )
            elif field_name in DEVICES:
                raise DeviceStepError(
                    "E2",
                    variant_where,
                    f"a variant runs on its device; {field_name!r} goes under "
                    "`devices:`",
                )
            elif field_name in STEP_ONLY_FIELDS:
                raise DeviceStepError(
                    "E2",
                    variant_where,
                    f"`{field_name}` is set once, at the step level",
                )
            else:
                _reject(field_name, variant_where, VARIANT_ORDER)
        if not any(name not in ("key", "label") for name in checked):
            raise DeviceStepError(
                "E6",
                variant_where,
                "a variant must change at least one field besides key and label",
            )
        variants[name] = checked
    return variants


def _same_as(
    device: str,
    entry: Dict[str, Any],
    entries: Dict[str, Dict[str, Any]],
    where: str,
) -> Dict[str, Any]:
    """The device's own fields (no key, label or variants), with `same_as`
    applied."""
    own = {
        name: copy.deepcopy(value)
        for name, value in entry.items()
        if name not in (SAME_AS_FIELD, "key", "label", VARIANTS_FIELD)
    }
    sibling = entry.get(SAME_AS_FIELD)
    if sibling is None:
        return own
    names = list(entries)
    if not isinstance(sibling, str) or sibling not in entries:
        raise DeviceStepError(
            "E5",
            where,
            f"`same_as: {sibling}` must name an earlier device of this step "
            "(variants cannot be copied)",
        )
    if names.index(sibling) >= names.index(device):
        raise DeviceStepError(
            "E5", where, f"`same_as: {sibling}` must name an earlier device"
        )
    if DEVICES[sibling].vendor != DEVICES[device].vendor:
        raise DeviceStepError(
            "E5",
            where,
            f"`same_as: {sibling}` is a {DEVICES[sibling].vendor.value} device; "
            "copy only a device of the same vendor (put what vendors share at "
            "the step level)",
        )
    if SAME_AS_FIELD in entries[sibling]:
        raise DeviceStepError(
            "E5",
            where,
            f"`same_as: {sibling}` points at a device that has `same_as` "
            f"itself; point at {entries[sibling][SAME_AS_FIELD]} instead",
        )
    resolved = {
        name: copy.deepcopy(value)
        for name, value in entries[sibling].items()
        if name not in ("key", "label", VARIANTS_FIELD)
    }
    for name, value in own.items():
        if name == DEPENDENCIES_FIELD:
            copied = resolved.get(name) or []
            repeated = [dep for dep in value if dep in copied]
            if repeated:
                raise DeviceStepError(
                    "E6",
                    where,
                    f"{_quoted(repeated)} already come from {sibling} (`same_as`)",
                )
            resolved[name] = copied + list(value)
        elif name == ENV_FIELD:
            env = dict(resolved.get(name) or {})
            same = [var for var, val in value.items() if env.get(var) == val]
            if same:
                raise DeviceStepError(
                    "E6",
                    where,
                    f"env {_quoted(same)} already come from {sibling} (`same_as`)",
                )
            env.update(value)
            resolved[name] = env
        else:
            if name in resolved and resolved[name] == value:
                raise DeviceStepError(
                    "E6",
                    where,
                    f"`{name}` is the same as {sibling}'s, which `same_as` "
                    "already copies; drop it",
                )
            resolved[name] = value
    return resolved


def _merge_into_step(
    step_software: Dict[str, Any], own: Dict[str, Any], where: str
) -> Dict[str, Any]:
    """Rule 3a: a device adds dependencies and env vars and sets the other
    software fields the step does not."""
    fields = copy.deepcopy(step_software)
    for name, value in own.items():
        if name == DEPENDENCIES_FIELD:
            shared = step_software.get(name) or []
            repeated = [dep for dep in value if dep in shared]
            if repeated:
                raise DeviceStepError(
                    "E3",
                    where,
                    f"{_quoted(repeated)} already in the step's "
                    "`source_file_dependencies`; a device only adds entries",
                )
            fields[name] = list(shared) + list(value)
        elif name == ENV_FIELD:
            shared_env = step_software.get(name) or {}
            redefined = [var for var in value if var in shared_env]
            if redefined:
                raise DeviceStepError(
                    "E3",
                    where,
                    f"env {_quoted(redefined)} already set at the step level; "
                    "set it on every device or only at step level",
                )
            env = dict(shared_env)
            env.update(value)
            fields[name] = env
        elif name in SET_ONCE_FIELDS and name in step_software:
            raise DeviceStepError(
                "E3",
                where,
                f"`{name}` is set at the step level and on the device; set it "
                "on every device or only at step level",
            )
        else:
            fields[name] = copy.deepcopy(value)
    return fields


def _variant_fields(
    base: Dict[str, Any], body: Dict[str, Any], where: str
) -> Dict[str, Any]:
    fields = copy.deepcopy(base)
    for name, value in body.items():
        if name in ("key", "label"):
            continue
        if name == DEPENDENCIES_FIELD:
            inherited = fields.get(name) or []
            repeated = [dep for dep in value if dep in inherited]
            if repeated:
                raise DeviceStepError(
                    "E6",
                    where,
                    f"{_quoted(repeated)} already in the device job's "
                    "dependencies; a variant only adds entries",
                )
            fields[name] = list(inherited) + list(value)
        elif name == ENV_FIELD:
            env = dict(fields.get(name) or {})
            same = [var for var, val in value.items() if env.get(var) == val]
            if same:
                raise DeviceStepError(
                    "E6",
                    where,
                    f"env {_quoted(same)} already have that value in the device job",
                )
            env.update(value)
            fields[name] = env
        else:
            if fields.get(name) == value:
                raise DeviceStepError(
                    "E6",
                    where,
                    f"`{name}` is the device job's already; a variant states "
                    "only what changes",
                )
            fields[name] = copy.deepcopy(value)
    return fields


def _check_machine(
    info: DeviceInfo, device: str, fields: Dict[str, Any], where: str
) -> None:
    """What a device or variant writes, against what its vendor reads. (A
    step-level `mount_buildkite_agent` is the other vendors'.)"""
    if "num_devices" in fields and info.vendor != Vendor.NVIDIA:
        if info.vendor == Vendor.AMD:
            message = (
                f"`num_devices` on an AMD device: {device} already means "
                f"{info.gpus} GPU(s) (the pool's); drop it"
            )
        else:
            message = "`num_devices` is only for NVIDIA devices"
        raise DeviceStepError("E7", where, message)
    if info.vendor == Vendor.AMD:
        ignored = [name for name in AMD_IGNORED_FIELDS if name in fields]
        if ignored:
            raise DeviceStepError(
                "E2", where, f"AMD jobs do not read {_quoted(ignored)}; drop it"
            )


def _job_depends_on(
    info: DeviceInfo,
    extra: List[str],
    group_depends_on: Optional[List[str]],
) -> Optional[List[str]]:
    """The registry's image step for the job, then the step's other ones."""
    if info.image:
        return [info.image] + extra
    if info.vendor == Vendor.CPU:
        # A file with `depends_on: []` builds no image for its jobs.
        if isinstance(group_depends_on, list) and not group_depends_on:
            return list(extra)
        return [CPU_IMAGE_BUILD_STEP] + extra
    # NVIDIA: the file's, which the loader adds when the step has none.
    if not extra:
        return None
    return list(group_depends_on or []) + extra


def _step_depends_on(value: Any, where: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise DeviceStepError("E2", where, "`depends_on` must be a list of step keys")
    images = [item for item in value if _is_image_step(item)]
    if images:
        raise DeviceStepError(
            "E2",
            where,
            f"{_quoted(images)} comes from the device registry (NVIDIA jobs get "
            "the file's depends_on, AMD jobs image-build-amd, CPU jobs "
            "image-build-cpu); list only other steps in `depends_on`",
        )
    return list(value)


def _is_image_step(key: str) -> bool:
    images = {info.image for info in DEVICES.values() if info.image}
    return key.startswith("image-build") or key in images


def _title(value: Any, where: str, required: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeviceStepError(
            "E2",
            where,
            "`label` must be a title" + (" (it is required)" if required else ""),
        )
    if LABEL_PREFIX.match(value):
        raise DeviceStepError(
            "E2",
            where,
            f"label {value!r}: write the bare title; the generator adds "
            "':amd: (MI355)' (icon and tag, from the device) and '[Nightly]' "
            "(from `optional: true` on the device)",
        )
    return value


def _pinned_key(value: Any, where: str) -> str:
    if not isinstance(value, str) or not KEY_PATTERN.match(value):
        raise DeviceStepError("E2", where, "`key` must match [a-zA-Z0-9_-]+")
    return value


_BOOL_FIELDS = (
    "automatic_shard",
    "mount_buildkite_agent",
    "no_plugin",
    "optional",
    "soft_fail",
    "no_gpu",
)
_INT_FIELDS = (
    "num_devices",
    "num_nodes",
    "parallelism",
    "timeout_in_minutes",
    "concurrency",
)


def _checked(name: str, value: Any, where: str) -> Any:
    """The value of a software or machine field, checked for its type."""
    if value is None or value == [] or value == {}:
        raise DeviceStepError(
            "E6", where, f"`{name}` is empty: write a value or drop the field"
        )
    if name in (DEPENDENCIES_FIELD, "commands"):
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise DeviceStepError("E2", where, f"`{name}` must be a list of strings")
    elif name == ENV_FIELD:
        if not isinstance(value, dict) or not all(
            isinstance(var, str) and isinstance(val, str) for var, val in value.items()
        ):
            raise DeviceStepError(
                "E2", where, "`env` must map names to strings (quote numbers)"
            )
    elif name in _BOOL_FIELDS:
        if not isinstance(value, bool):
            raise DeviceStepError("E2", where, f"`{name}` must be true or false")
    elif name in _INT_FIELDS:
        if isinstance(value, bool) or not isinstance(value, int):
            raise DeviceStepError("E2", where, f"`{name}` must be a number")
    elif name == "dind":
        if value is not True:
            raise DeviceStepError(
                "E2",
                where,
                "only `dind: true` may be written: AMD jobs run without dind "
                "(device registry), the others with it",
            )
    elif name in ("working_dir", "concurrency_group"):
        if not isinstance(value, str):
            raise DeviceStepError("E2", where, f"`{name}` must be a string")
    elif name in ("agent_tags", "retry"):
        if not isinstance(value, dict):
            raise DeviceStepError("E2", where, f"`{name}` must be a map")
    return copy.deepcopy(value)


def _reject(name: Any, where: str, allowed: Tuple[str, ...]) -> None:
    if name in REMOVED_FIELDS:
        raise DeviceStepError("E2", where, f"`{name}`: {REMOVED_FIELDS[name]}")
    message = f"unknown field `{name}`"
    close = difflib.get_close_matches(str(name), allowed, n=1, cutoff=0.5) or [
        known for known in allowed if known.startswith(str(name))
    ]
    if close:
        message += f" (did you mean `{close[0]}`?)"
    raise DeviceStepError("E2", where, message)


def _quoted(items: List[str]) -> str:
    return ", ".join(f"`{item}`" for item in items)


# ---- block style (E9) ------------------------------------------------------------


def check_block_style(text: str, source: str = "") -> None:
    """E9: every list and map of a `devices:` step is in block style. The
    loader cannot tell from the parsed data, so it reads the yaml's nodes."""
    for line, where in flow_style_nodes(text):
        raise DeviceStepError(
            "E9",
            f"{source + ':' if source else 'line '}{line}{where}",
            "write lists with `- ` items and maps with one key per line "
            "(no [...] or {...})",
        )


def flow_style_nodes(text: str) -> Iterator[Tuple[int, str]]:
    """(1-based line, step) of every flow-style list or map inside a
    `devices:` step."""
    root = yaml.compose(text)
    if not isinstance(root, yaml.MappingNode):
        return
    for key_node, value_node in root.value:
        if key_node.value != "steps" or not isinstance(value_node, yaml.SequenceNode):
            continue
        for item in value_node.value:
            if not isinstance(item, yaml.MappingNode):
                continue
            names = {k.value: v for k, v in item.value}
            if DEVICES_FIELD not in names:
                continue
            key = names.get("key")
            where = f" (step {key.value})" if isinstance(key, yaml.ScalarNode) else ""
            for node in _walk(item):
                if getattr(node, "flow_style", False):
                    yield node.start_mark.line + 1, where


def _walk(node: Any) -> Iterator[Any]:
    yield node
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            yield from _walk(key_node)
            yield from _walk(value_node)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from _walk(item)
