"""Map steps off closed queues to the next device in their declared chain.

Entries come from `<fallback_dir>/{common,nvidia,amd}/*.yaml` (pipeline config
`fallback_dirs`). The generator reads only the chains (`load_chains`), the
health checker only the policies, groups and pins (`load_health_policies`);
it watches only queues with a complete policy, a group's queues as one.
Queue state, pins included, comes only from the checker's snapshot, the
meta-data `queue-health` of the latest passed main build of its pipeline
(`read_snapshot`). Without config the generator is unchanged; without a fresh
snapshot every queue is open. Only CUDA and AMD GPU devices take part. Builds
of main also run the snapshot's probes of probing queues, as soft-failing
shadow steps.
"""

import inspect
import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

import yaml
from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)
from typing_extensions import Annotated

from amd import AMD_DEVICE_CONFIGS
from buildkite_step import (
    BuildkiteCommandStep,
    BuildkiteGroupStep,
    _generate_step_key,
    convert_group_step_to_buildkite_step,
    get_agent_queue,
)
from constants import DeviceType
from fallback_rules import PLATFORMS, rules_for
from recorder_switches import fnrec_enabled, kernrec_enabled
from step import Step

COMMON = "common"
REST_URL = "https://api.buildkite.com/v2"
SNAPSHOT_META_KEY = "queue-health"
SNAPSHOT_VERSION = 1
ANNOTATION_CONTEXT = "queue-fallback"
PROBE_GROUP = "Queue probes"
PROBE_PREFIX = "probe-"
_KEY_LENGTH = 100
_DURATION = re.compile(r"^([1-9][0-9]*)([mhd])$")
_DURATION_S = {"m": 60, "h": 3600, "d": 86400}
_SLUG = r"^[a-z0-9][a-z0-9-]*$"


def parse_duration(value: Any) -> int:
    """'30m', '2h', '1d' -> seconds."""
    match = _DURATION.match(value) if isinstance(value, str) else None
    if not match:
        raise ValueError(f"invalid duration {value!r}, expected e.g. '30m' or '2h'")
    return int(match.group(1)) * _DURATION_S[match.group(2)]


Duration = Annotated[int, BeforeValidator(parse_duration)]


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CloseWhen(_Entry):
    rule: str
    window: Duration
    min_jobs: int = Field(gt=0, strict=True)
    # The entry's other keys, e.g. `above`.
    params: Mapping[str, Any]

    @model_validator(mode="before")
    @classmethod
    def _collect_params(cls, entry: Any) -> Any:
        if not isinstance(entry, dict):
            return entry
        named = cls.model_fields.keys() - {"params"}
        return {
            **{k: v for k, v in entry.items() if k in named},
            "params": {k: v for k, v in entry.items() if k not in named},
        }

    @field_validator("params", mode="after")
    @classmethod
    def _freeze(cls, params: Mapping[str, Any]) -> Mapping[str, Any]:
        return MappingProxyType(dict(params))


class Defaults(_Entry):
    close_when: Optional[CloseWhen] = None
    reopen_after: Optional[Duration] = None
    probes: Optional[int] = Field(default=None, gt=0, strict=True)


class DeviceEntry(Defaults):
    device: str
    # Empty: only overrides the device's policy.
    fallbacks: Tuple[str, ...] = ()


class GroupEntry(Defaults):
    """Devices of one platform that share one state, the first running its
    probes."""

    name: str = Field(pattern=_SLUG)
    devices: Tuple[str, ...] = Field(min_length=1)


class Pin(_Entry):
    device: str
    state: Literal["open", "closed"]
    until: AwareDatetime
    reason: str = Field(min_length=1)


class HealthConfig(_Entry):
    pipeline: str = Field(pattern=_SLUG)
    token_secret: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    max_age: Duration
    pipelines: Tuple[Annotated[str, Field(pattern=_SLUG)], ...] = Field(min_length=1)


class Policy(_Entry):
    close_when: CloseWhen
    reopen_after: Duration
    probes: int = Field(gt=0, strict=True)


@dataclass(frozen=True)
class ChainConfig:
    """The generator's config: each chain's fallbacks by its device."""

    chains: Mapping[str, Tuple[str, ...]]
    health: HealthConfig


@dataclass(frozen=True)
class QueuePolicy:
    # The device whose policy the queues follow and that runs their probes.
    device: str
    policy: Policy
    # The queues that share one state, the device's first.
    queues: Tuple[str, ...]
    # The group of the queues; None for a lone device's queue.
    group: Optional[str] = None


@dataclass(frozen=True)
class HealthPolicies:
    """The checker's config, by the queue that runs the probes: every
    watched group's or other supported device queue's policy, and the
    pins."""

    health: HealthConfig
    units: Mapping[str, QueuePolicy]
    pins: Mapping[str, Pin]


CUDA_DEVICES = frozenset(
    device.value
    for device in (
        DeviceType.H100,
        DeviceType.H200,
        DeviceType.H200_18GB,
        DeviceType.H200_35GB,
        DeviceType.B200,
        DeviceType.B200_K8S,
        DeviceType.L4,
    )
)
SUPPORTED_DEVICES = CUDA_DEVICES | frozenset(AMD_DEVICE_CONFIGS)


def platform_of(device: Optional[str]) -> str:
    if device in AMD_DEVICE_CONFIGS:
        return "amd"
    if device in CUDA_DEVICES:
        return "nvidia"
    raise ValueError(
        f"unsupported device for queue fallback: {device} "
        "(only CUDA and AMD GPU devices)"
    )


def device_queue(device: str) -> str:
    """The queue the generator sends a step on `device` to."""
    if device in AMD_DEVICE_CONFIGS:
        return AMD_DEVICE_CONFIGS[device].queue.value
    return get_agent_queue(Step(label=device, device=device)).value


_KNOWN_DEVICES = frozenset(device.value for device in DeviceType)
_ENTRY_KINDS = ("defaults", "device", "group", "pin", "health")


def config_fallback_dirs(pipeline_config: Mapping[str, Any]) -> Tuple[str, ...]:
    """A pipeline config's `fallback_dirs`, each once, in order."""
    dirs = pipeline_config.get("fallback_dirs")
    if dirs is None:
        return ()
    if not isinstance(dirs, list) or not all(isinstance(d, str) and d for d in dirs):
        raise ValueError("fallback_dirs must be a list of paths")
    return tuple(dict.fromkeys(dirs))


def load_chains(dirs: Sequence[str], repo_root: str = ".") -> ChainConfig:
    _, devices, _, _, health = _parse_entries(dirs, repo_root)
    chains = {
        device: entry.fallbacks
        for device, (entry, _) in devices.items()
        if entry.fallbacks
    }
    return ChainConfig(MappingProxyType(chains), health)


def load_health_policies(dirs: Sequence[str], repo_root: str = ".") -> HealthPolicies:
    """Each group, then each other supported device's queue, is watched if
    its policy is complete. A group's first device runs its probes, a lone
    queue's first device on it by name. A pin pins its device's group."""
    defaults, devices, groups, pins, health = _parse_entries(dirs, repo_root)
    grouped = {device for group, _ in groups.values() for device in group.devices}
    candidates = [(g.name, g.devices, (g, source)) for g, source in groups.values()]
    for device in sorted(SUPPORTED_DEVICES - grouped):
        candidates.append((None, (device,), devices.get(device)))
    units: Dict[str, QueuePolicy] = {}
    claimed: Set[str] = set()
    for group, members, own in candidates:
        platform = platform_of(members[0])
        layers = [
            layer
            for layer in (defaults.get(COMMON), defaults.get(platform), own)
            if layer is not None
        ]
        try:
            policy = _resolve_policy(
                platform,
                [entry for entry, _ in layers],
                required=own is not None and bool(_policy_keys(own[0])),
            )
        except ValueError as error:
            # Blame the most specific entry behind the policy.
            raise ValueError(
                f"{layers[-1][1]}: {group or members[0]}: {error}"
            ) from error
        queues = tuple(
            queue
            for queue in dict.fromkeys(device_queue(d) for d in members)
            if queue not in claimed
        )
        if policy is None or not queues:
            continue
        claimed.update(queues)
        units[queues[0]] = QueuePolicy(members[0], policy, queues, group)

    unit_of = {queue: lead for lead, unit in units.items() for queue in unit.queues}
    unit_pins: Dict[str, Pin] = {}
    for queue, (pin, source) in pins.items():
        lead = unit_of.get(queue)
        if lead is None:
            raise ValueError(
                f"{source}: {pin.device} is not watched (no complete policy)"
            )
        if lead in unit_pins:
            raise ValueError(f"{source}: second pin for group {units[lead].group}")
        unit_pins[lead] = pin
    return HealthPolicies(health, MappingProxyType(units), MappingProxyType(unit_pins))


def _policy_keys(entry: Defaults) -> Set[str]:
    return entry.model_fields_set & Defaults.model_fields.keys()


def _parse_entries(dirs: Sequence[str], repo_root: str):
    """(defaults by kind dir, device entries, groups by name, pins by queue,
    health), each entry validated and with its source."""
    defaults: Dict[str, Tuple[Defaults, str]] = {}
    devices: Dict[str, Tuple[DeviceEntry, str]] = {}
    groups: Dict[str, Tuple[GroupEntry, str]] = {}
    group_of: Dict[str, str] = {}
    pins: Dict[str, Tuple[Pin, str]] = {}
    health: Optional[HealthConfig] = None

    for kind_dir, source, entry in _read_entries(dirs, repo_root):
        try:
            kind = _entry_kind(entry)
            if kind == "defaults":
                if kind_dir in defaults:
                    raise ValueError(f"second defaults entry for {kind_dir}/")
                defaults[kind_dir] = (
                    Defaults.model_validate(entry["defaults"]),
                    source,
                )
            elif kind == "health":
                if kind_dir != COMMON:
                    raise ValueError("health belongs in common/")
                if health is not None:
                    raise ValueError("second health entry")
                health = HealthConfig.model_validate(entry["health"])
            elif kind == "pin":
                pin = Pin.model_validate(entry["pin"])
                _check_device(pin.device)
                queue = device_queue(pin.device)
                if queue in pins:
                    raise ValueError(f"second pin for queue {queue}")
                pins[queue] = (pin, source)
            elif kind == "group":
                if kind_dir == COMMON:
                    raise ValueError("group belongs in a platform dir, not common/")
                group = GroupEntry.model_validate(entry["group"])
                if group.name in groups:
                    raise ValueError(
                        f"second group {group.name}, first in {groups[group.name][1]}"
                    )
                if len(set(group.devices)) != len(group.devices):
                    raise ValueError(f"duplicate devices in group {group.name}")
                for name in group.devices:
                    _check_device(name)
                    if platform_of(name) != kind_dir:
                        raise ValueError(
                            f"{name} is not an {kind_dir} device "
                            f"(group {group.name} is in {kind_dir}/)"
                        )
                    if name in group_of:
                        raise ValueError(
                            f"{name} is in groups {group_of[name]} and {group.name}"
                        )
                group_of.update(dict.fromkeys(group.devices, group.name))
                groups[group.name] = (group, source)
            else:
                device = DeviceEntry.model_validate(entry)
                for name in (device.device, *device.fallbacks):
                    _check_device(name)
                if device.device in devices:
                    raise ValueError(
                        f"second entry for {device.device}, "
                        f"first in {devices[device.device][1]}"
                    )
                if device.device in device.fallbacks:
                    raise ValueError(f"{device.device} is in its own fallbacks")
                if len(set(device.fallbacks)) != len(device.fallbacks):
                    raise ValueError(f"duplicate fallbacks for {device.device}")
                if not device.fallbacks and not _policy_keys(device):
                    raise ValueError(f"{device.device} has no fallbacks or policy")
                devices[device.device] = (device, source)
        except (ValidationError, ValueError) as error:
            raise ValueError(f"{source}: {error}") from error

    for name, (device, source) in devices.items():
        if name in group_of and _policy_keys(device):
            raise ValueError(
                f"{source}: {name} is in group {group_of[name]}: "
                "set its policy on the group"
            )
    if health is None:
        raise ValueError(f"queue fallbacks {dirs}: no health entry in common/")
    return defaults, devices, groups, pins, health


def _read_entries(dirs: Sequence[str], repo_root: str):
    for fallback_dir in dirs:
        root = os.path.join(repo_root, fallback_dir)
        if not os.path.isdir(root):
            raise ValueError(f"Queue fallback directory not found: {fallback_dir}")
        for kind_dir in (COMMON, *PLATFORMS):
            path = os.path.join(root, kind_dir)
            if not os.path.isdir(path):
                continue
            for name in sorted(os.listdir(path)):
                if not name.endswith(".yaml"):
                    continue
                file_path = os.path.join(fallback_dir, kind_dir, name)
                with open(os.path.join(path, name)) as f:
                    entries = yaml.safe_load(f)
                if entries is None:
                    continue
                if not isinstance(entries, list):
                    raise ValueError(f"{file_path}: expected a list of entries")
                for index, entry in enumerate(entries):
                    yield kind_dir, f"{file_path} entry {index}", entry


def _entry_kind(entry: Any) -> str:
    if not isinstance(entry, dict):
        raise ValueError(f"entry must be a mapping, got {entry!r}")
    kinds = [kind for kind in _ENTRY_KINDS if kind in entry]
    if len(kinds) != 1:
        raise ValueError(f"entry must have exactly one of {_ENTRY_KINDS}")
    if kinds[0] != "device" and len(entry) != 1:
        raise ValueError(f"unexpected keys next to {kinds[0]}: {sorted(entry)}")
    return kinds[0]


def _check_device(device: str) -> None:
    if device not in _KNOWN_DEVICES:
        raise ValueError(f"unknown device {device!r}")
    platform_of(device)


def _resolve_policy(
    platform: str, layers: List[Defaults], required: bool
) -> Optional[Policy]:
    """The merged policy, or None if it is incomplete and not `required`."""
    merged: Dict[str, Any] = {}
    for layer in layers:
        for key in Defaults.model_fields:
            value = getattr(layer, key)
            if value is not None:
                merged[key] = value
    close = merged.get("close_when")
    if close is not None:
        rule = rules_for(platform).get(close.rule)
        if rule is None:
            raise ValueError(f"unknown rule {close.rule!r}")
        try:
            inspect.signature(rule).bind([], **close.params)
        except TypeError as error:
            raise ValueError(f"rule {close.rule}: {error}") from None
    missing = [key for key in Policy.model_fields if key not in merged]
    if not missing:
        return Policy.model_construct(**merged)
    if required:
        raise ValueError(f"no {', '.join(missing)} (set them in defaults)")
    return None


class Probe(_Entry):
    """{"round": 1, "device": "mi300_1", "keys": ["amd-kernels"]}"""

    round: int = Field(gt=0, strict=True)
    device: str = Field(strict=True)
    keys: Tuple[Annotated[str, Field(min_length=1, strict=True)], ...]


class QueueState(_Entry):
    """A queue's snapshot entry, written only by the checker."""

    state: Literal["open", "closed", "probing"]
    since: Optional[AwareDatetime] = None
    until: Optional[AwareDatetime] = None
    reason: str = ""
    infra_rate: Optional[float] = None
    jobs: int = 0
    probe_round: int = 0
    backoff_s: int = 0
    pinned: bool = False
    probe: Optional[Probe] = None


class Snapshot(_Entry):
    """Queue state by queue; a queue missing from it is open."""

    version: Literal[SNAPSHOT_VERSION]
    generated_at: AwareDatetime
    queues: Mapping[str, QueueState]

    @field_validator("queues", mode="after")
    @classmethod
    def _freeze(cls, queues: Mapping[str, QueueState]) -> Mapping[str, QueueState]:
        return MappingProxyType(dict(queues))

    @field_serializer("queues")
    def _dump(self, queues: Mapping[str, QueueState]) -> Dict[str, QueueState]:
        return dict(queues)


def parse_snapshot(text: str) -> Snapshot:
    return Snapshot.model_validate_json(text)


def _fetch_json(url: str, token: str) -> Any:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def _get_secret(name: str) -> str:
    result = subprocess.run(
        ["buildkite-agent", "secret", "get", name],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    secret = result.stdout.strip()
    if not secret:
        raise ValueError(f"secret {name} is empty")
    return secret


def read_snapshot(
    health: HealthConfig,
    org: Optional[str],
    token: str,
    fetch: Callable[[str, str], Any] = _fetch_json,
    now: Optional[datetime] = None,
    allow_stale: bool = False,
) -> Optional[Snapshot]:
    """The snapshot of the latest passed main build of `health.pipeline`, or None
    if it has no passed build. Raises if that build has no well-formed
    snapshot or, unless `allow_stale`, one older than `health.max_age`."""
    if not org or not re.match(_SLUG, org):
        raise ValueError(f"invalid organization {org!r}")
    builds = fetch(
        f"{REST_URL}/organizations/{org}/pipelines/{health.pipeline}/builds?"
        + urllib.parse.urlencode({"branch": "main", "state": "passed", "per_page": 1}),
        token,
    )
    if not builds:
        return None
    snapshot = parse_snapshot(builds[0]["meta_data"][SNAPSHOT_META_KEY])
    age = (now or datetime.now(timezone.utc)) - snapshot.generated_at
    if not allow_stale and age.total_seconds() > health.max_age:
        raise ValueError(f"snapshot is stale, generated at {snapshot.generated_at}")
    return snapshot


def build_snapshot(
    health: HealthConfig,
    org: Optional[str],
    fetch: Callable[[str, str], Any] = _fetch_json,
    get_secret: Callable[[str], str] = _get_secret,
    now: Optional[datetime] = None,
) -> Optional[Snapshot]:
    """The fresh snapshot for this build, or None (every queue open)."""
    try:
        token = get_secret(health.token_secret)
        snapshot = read_snapshot(health, org, token, fetch, now)
        if snapshot is None:
            raise ValueError(f"no passed {health.pipeline} build")
        return snapshot
    except Exception as error:  # noqa: BLE001 - any failure means all open
        print(
            f"Queue health snapshot unavailable ({type(error).__name__}: {error}); "
            "every queue is treated as open."
        )
        return None


def _closed_reason(device: str, snapshot: Optional[Snapshot]) -> Optional[str]:
    """Why `device`'s queue takes no new steps, or None if it is open."""
    entry = snapshot.queues.get(device_queue(device)) if snapshot else None
    if entry is None or entry.state == "open":
        return None
    return f"{device} {entry.state}" + (f" ({entry.reason})" if entry.reason else "")


def map_device(
    step: Step, cfg: ChainConfig, snapshot: Optional[Snapshot]
) -> Tuple[Optional[str], Optional[str]]:
    """(device, reason): the first device in the step's chain whose queue is
    open, and why it moved. All closed keeps the step's device, with a reason."""
    fallbacks = cfg.chains.get(step.device) if step.fallback else None
    if fallbacks is None:
        return step.device, None
    closed = []
    for device in (step.device, *fallbacks):
        reason = _closed_reason(device, snapshot)
        if reason is None:
            if device == step.device:
                return step.device, None
            return device, f"{'; '.join(closed)} -> {device}"
        closed.append(reason)
    return step.device, f"{'; '.join(closed)} -> kept {step.device}"


def apply_fallbacks(
    steps: List[Step], cfg: ChainConfig, snapshot: Optional[Snapshot]
) -> Tuple[List[Step], Dict[str, List[str]]]:
    """Steps with their device and AMD mirror device mapped, and the step
    keys behind each decision."""
    decisions: Dict[str, List[str]] = {}
    mapped = []
    for step in steps:
        key = step.key or _generate_step_key(step.label)
        update: Dict[str, Any] = {}
        device, reason = map_device(step, cfg, snapshot)
        if reason:
            decisions.setdefault(reason, []).append(key)
        if device != step.device:
            update["device"] = device
        amd = (step.mirror or {}).get("amd")
        if amd and amd.get("device"):
            mirror_step = step.model_copy(update={"device": amd["device"]})
            amd_device, reason = map_device(mirror_step, cfg, snapshot)
            if reason:
                decisions.setdefault(reason, []).append(f"amd-{key}")
            if amd_device != amd["device"]:
                update["mirror"] = {**step.mirror, "amd": {**amd, "device": amd_device}}
        mapped.append(step.model_copy(update=update) if update else step)
    return mapped, decisions


def probe_key(queue: str, round_: int, key: str) -> str:
    """The key of the probe of step `key` on `queue` in round `round_`."""
    key = re.sub(r"[^A-Za-z0-9_-]", "-", f"{PROBE_PREFIX}{queue}-r{round_}-{key}")
    return key[:_KEY_LENGTH]


def probe_prefix(queue: str, round_: int) -> str:
    """The start of every probe key of `queue` in round `round_`."""
    return probe_key(queue, round_, "")


_PROBE_KEY = re.compile(rf"^{PROBE_PREFIX}(.+?)-r([1-9][0-9]*)-")


def parse_probe_key(step_key: Optional[str]) -> Optional[Tuple[str, int]]:
    """(queue, round) of a probe key, None for any other step key. The first
    `-r<round>-` ends the queue; no supported queue contains one."""
    match = _PROBE_KEY.match(step_key or "")
    return (match.group(1), int(match.group(2))) if match else None


def probe_steps(
    steps: List[Step],
    snapshot: Optional[Snapshot],
    decisions: Optional[Dict[str, List[str]]] = None,
) -> List[BuildkiteGroupStep]:
    """One soft-failing shadow of each probe step of a probing queue that is in
    this build, on the probe's device. An AMD mirror's key is `amd-<key>`."""
    by_key: Dict[str, Tuple[Step, Optional[Dict[str, Any]]]] = {}
    for step in steps:
        key = step.key or _generate_step_key(step.label)
        by_key[key] = (step, None)
        amd = (step.mirror or {}).get("amd")
        if amd and amd.get("device"):
            by_key[f"amd-{key}"] = (step, amd)
    probes: Dict[str, BuildkiteCommandStep] = {}
    for queue, entry in sorted(snapshot.queues.items() if snapshot else ()):
        probe = entry.probe
        if entry.state != "probing" or not probe:
            continue
        device, round_ = probe.device, probe.round
        if device not in SUPPORTED_DEVICES or device_queue(device) != queue:
            print(f"Queue probes of {queue} skipped: {device} is not on it")
            continue
        for key in probe.keys:
            new_key = probe_key(queue, round_, key)
            if key not in by_key or new_key in probes:
                continue
            rendered = _render_probe(key, *by_key[key], device)
            if rendered is None:
                print(f"Queue probe {new_key} skipped: {key} cannot run on {device}")
                continue
            probes[new_key] = rendered.model_copy(
                update={
                    "key": new_key,
                    "label": f"Probe {queue} r{round_}: {rendered.label}",
                    "soft_fail": True,
                    "deferred": False,
                }
            )
            if decisions is not None:
                reason = f"{queue} probe round {round_} on {device}"
                decisions.setdefault(reason, []).append(new_key)
    if not probes:
        return []
    return [BuildkiteGroupStep(group=PROBE_GROUP, steps=list(probes.values()))]


def _render_probe(
    key: str, step: Step, amd: Optional[Dict[str, Any]], device: str
) -> Optional[BuildkiteCommandStep]:
    """The step rendered as written for `device`, or None if it cannot be."""
    declared = amd["device"] if amd else step.device
    if declared not in SUPPORTED_DEVICES:
        return None
    if platform_of(declared) != platform_of(device):
        return None
    # One job per probe key: no shards, no parallel jobs, no concurrency gate.
    single = {"parallelism": None, "concurrency": None, "concurrency_group": None}
    update: Dict[str, Any] = {"automatic_shard": False, **single}
    if amd:
        amd = {k: v for k, v in amd.items() if k not in single}
        update["mirror"] = {**step.mirror, "amd": {**amd, "device": device}}
    else:
        update["device"] = device
    try:
        groups = convert_group_step_to_buildkite_step(
            {PROBE_GROUP: [step.model_copy(update=update)]}
        )
    except ValueError:
        return None
    return next((s for g in groups for s in g.steps if s.key == key), None)


def annotate(decisions: Dict[str, List[str]]) -> None:
    """One info annotation for the build; never fails generation."""
    if not decisions:
        return
    lines = ["**Queue fallback** (queue-health snapshot):", ""]
    for reason, keys in decisions.items():
        lines.append(f"- {reason}: {', '.join(f'`{key}`' for key in keys)}")
    message = "\n".join(lines)
    print(message)
    if shutil.which("buildkite-agent") is None:
        return
    try:
        subprocess.run(
            [
                "buildkite-agent",
                "annotate",
                "--context",
                ANNOTATION_CONTEXT,
                "--style",
                "info",
                message,
            ],
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        print(f"Queue fallback annotation failed: {error}")


def apply_queue_fallbacks(
    steps: List[Step], global_config: Dict[str, Any]
) -> Tuple[List[Step], List[BuildkiteGroupStep]]:
    """The mapped steps, and the rendered probe group (main builds only;
    nothing depends on it, so not recording builds either). The snapshot is
    read once, for both."""
    fallback_dirs = global_config.get("fallback_dirs")
    if not fallback_dirs:
        return steps, []
    cfg = load_chains(fallback_dirs)
    snapshot = build_snapshot(cfg.health, os.getenv("BUILDKITE_ORGANIZATION_SLUG"))
    mapped, decisions = apply_fallbacks(steps, cfg, snapshot)
    probes = []
    if (
        global_config["branch"] == "main"
        and global_config["only_step_keys"] is None
        and not (kernrec_enabled() or fnrec_enabled())
    ):
        probes = probe_steps(steps, snapshot, decisions)
    annotate(decisions)
    return mapped, probes
