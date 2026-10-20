import dataclasses
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from pydantic import ValidationError

import global_config
import pipeline_generator
import queue_fallbacks
from buildkite_step import convert_group_step_to_buildkite_step
from amd import AMD_DEVICE_CONFIGS
from constants import AgentQueue
from fallback_rules import PLATFORMS, common, rules_for
from queue_fallbacks import (
    SUPPORTED_DEVICES,
    apply_fallbacks,
    build_snapshot,
    config_fallback_dirs,
    device_queue,
    load_chains,
    load_health_policies,
    map_device,
    parse_duration,
    parse_probe_key,
    parse_snapshot,
    platform_of,
    probe_key,
    probe_prefix,
    probe_steps,
    read_snapshot,
)
from step import Step, group_steps

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "queue_fallbacks")
CONFIG_DIR = os.path.join(FIXTURES, "config")
JOBS_DIR = os.path.join(FIXTURES, "jobs")
UNSUPPORTED_JOBS_DIR = os.path.join(FIXTURES, "unsupported_jobs")
NVIDIA = ("h100", "h200", "h200_18gb", "h200_35gb", "b200", "b200-k8s", "l4", "a100")
UNSUPPORTED = (
    *NVIDIA,
    "cpu",
    "cpu-small",
    "cpu-medium",
    "intel_cpu",
    "intel_hpu",
    "intel_gpu",
    "arm_cpu",
    "gh200",
    "ascend_npu",
    "amd_cpu",
    "zen5",
    "dgx-spark",
)
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def raw_snapshot(queues=None, generated_at=NOW):
    return {
        "version": 1,
        "generated_at": generated_at.isoformat(),
        "queues": queues or {},
    }


def snapshot(queues=None, generated_at=NOW):
    return parse_snapshot(json.dumps(raw_snapshot(queues, generated_at)))


def closed(reason="72% infra failures over 2h", state="closed"):
    return {"state": state, "since": NOW.isoformat(), "reason": reason}


def probing(round=1, device="mi300_1", keys=None, **extra):
    probe = {"round": round, "device": device, "keys": keys or ["amd-kernels"]}
    probe.update(extra)
    return {**closed("probe round 1", "probing"), "probe": probe}


@pytest.fixture
def cfg(fake_global_config):
    return load_chains([CONFIG_DIR])


@pytest.fixture
def policies(fake_global_config):
    return load_health_policies([CONFIG_DIR])


def policy_for(policies, device):
    return policies.units[device_queue(device)].policy


def config_text(name):
    with open(os.path.join(CONFIG_DIR, name)) as f:
        return f.read()


def write_config(tmp_path, files):
    base = {
        name: config_text(name) for name in ("common/rules.yaml", "common/health.yaml")
    }
    base.update(files)
    for name, content in base.items():
        if content is None:
            continue
        path = tmp_path / "qf" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return [str(tmp_path / "qf")]


LOADERS = [load_chains, load_health_policies]


# Config


def test_load_chains(cfg):
    assert dict(cfg.chains) == {
        "mi300_1": ("mi355_1", "mi355_dpx"),
        "mi355_dpx": ("mi355_1",),
    }
    assert cfg.health.pipeline == "queue-health"
    assert cfg.health.token_secret == "QUEUE_FALLBACK_BUILDKITE_API_TOKEN"
    assert cfg.health.max_age == 30 * 60
    assert cfg.health.pipelines == ("ci",)


def test_load_health_policies(policies, cfg):
    assert policies.health == cfg.health
    assert dict(policies.pins) == {
        "amd_mi250_1": queue_fallbacks.Pin(
            device="mi250_1",
            state="closed",
            until=datetime(2026, 10, 20, tzinfo=timezone.utc),
            reason="node upgrade",
        )
    }
    # Every supported device queue, chained or not, and its first device.
    assert set(policies.units) == {device_queue(d) for d in SUPPORTED_DEVICES}
    for queue, entry in policies.units.items():
        assert entry.device == min(
            d for d in SUPPORTED_DEVICES if device_queue(d) == queue
        )
        assert (entry.queues, entry.group) == ((queue,), None)
    assert policies.units["amd_mi300_1"].device == "mi300_1"
    assert AgentQueue.MITHRIL_H100.value not in policies.units


def test_health_policies_do_not_read_chains(fake_global_config, tmp_path):
    chain = {"amd/x.yaml": "- device: mi300_1\n  fallbacks: [mi355_1, mi355_dpx]\n"}
    with_chain = load_health_policies(write_config(tmp_path / "a", chain))
    without = load_health_policies(write_config(tmp_path / "b", {}))
    assert dict(with_chain.units) == dict(without.units)
    assert set(without.units) == {device_queue(d) for d in SUPPORTED_DEVICES}


def test_config_is_immutable(cfg, policies):
    with pytest.raises(TypeError):
        cfg.chains["mi355_1"] = ("mi300_1",)
    with pytest.raises(TypeError):
        policies.units["amd_mi300_1"] = None
    with pytest.raises(TypeError):
        policies.pins["amd_mi300_1"] = None
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.health = None
    with pytest.raises(dataclasses.FrozenInstanceError):
        policies.units["amd_mi300_1"].device = "mi355_1"
    with pytest.raises(ValidationError):
        policy_for(policies, "mi300_1").reopen_after = 1


def test_policy_layers(policies):
    # common defaults, then amd/ defaults, then the device's own keys.
    mi300 = policy_for(policies, "mi300_1")
    assert mi300.reopen_after == 3600
    assert mi300.close_when.params == {"above": 0.7}
    assert mi300.close_when.window == 2 * 3600
    assert mi300.probes == 3
    assert policy_for(policies, "mi355_8").reopen_after == 3600
    dpx = policy_for(policies, "mi355_dpx")
    assert (dpx.close_when.params, dpx.close_when.window) == ({"above": 0.5}, 3600)
    assert dpx.close_when.min_jobs == 10
    assert dpx.probes == 3


def test_close_when_params_are_immutable(policies):
    close = policy_for(policies, "mi300_1").close_when
    with pytest.raises(TypeError):
        close.params["above"] = 1.0
    with pytest.raises(ValidationError, match="frozen"):
        close.params = {"above": 1.0}
    assert close.params == {"above": 0.7}


@pytest.mark.parametrize("load", LOADERS)
@pytest.mark.parametrize(
    "files, message",
    [
        ({"amd/x.yaml": "- device: mi999\n  fallbacks: [mi355_1]\n"}, "mi999"),
        ({"amd/x.yaml": "- device: mi300_1\n  fallbacks: [mi9]\n"}, "mi9"),
        (
            {"amd/x.yaml": "- device: mi300_1\n  fallbacks: [mi300_1]\n"},
            "own fallbacks",
        ),
        (
            {"amd/x.yaml": "- device: mi300_1\n  fallbacks: [mi355_1, mi355_1]\n"},
            "duplicate fallbacks",
        ),
        (
            {
                "amd/a.yaml": "- device: mi300_1\n  fallbacks: [mi355_1]\n",
                "amd/b.yaml": "- device: mi300_1\n  fallbacks: [mi355_dpx]\n",
            },
            "second entry for mi300_1",
        ),
        (
            {"amd/x.yaml": "- device: mi300_1\n  fallbacks: [mi355_1]\n  fallbak: 1\n"},
            "fallbak",
        ),
        ({"amd/x.yaml": "- device: mi300_1\n"}, "no fallbacks or policy"),
        (
            {"amd/x.yaml": "- device: mi300_1\n  fallbacks: []\n"},
            "no fallbacks or policy",
        ),
        (
            {
                "amd/x.yaml": "- device: mi300_1\n  fallbacks: [mi355_1]\n"
                "- device: mi300_1\n  reopen_after: 1h\n"
            },
            "second entry for mi300_1",
        ),
        ({"amd/x.yaml": "- pin: {device: mi300_1}\n  device: mi300_1\n"}, "one of"),
        ({"amd/x.yaml": "- hello\n"}, "mapping"),
        ({"amd/x.yaml": "device: mi300_1\n"}, "expected a list"),
        (
            {"amd/x.yaml": "- defaults: {reopen_after: 2 hours}\n"},
            "invalid duration",
        ),
        ({"amd/x.yaml": "- defaults: {}\n- defaults: {}\n"}, "second defaults"),
        (
            {"amd/x.yaml": "- defaults: {reopen_when: {probes_passed: 1}}\n"},
            "reopen_when",
        ),
        (
            {
                "amd/x.yaml": "- pin: {device: mi300_1, state: closed,"
                " until: '2026-10-20T00:00:00', reason: r}\n"
            },
            "timezone",
        ),
        (
            {
                "amd/x.yaml": "- pin: {device: mi300_1, state: shut,"
                " until: '2026-10-20T00:00:00Z', reason: r}\n"
            },
            "state",
        ),
        (
            {
                "amd/x.yaml": "- pin: {device: mi300_1, state: closed,"
                " until: '2026-10-20T00:00:00Z', reason: a}\n"
                "- pin: {device: mi300_1, state: open,"
                " until: '2026-10-20T00:00:00Z', reason: b}\n"
            },
            "second pin",
        ),
        ({"common/health.yaml": None}, "no health entry"),
        (
            {"amd/x.yaml": config_text("common/health.yaml")},
            "health belongs in common/",
        ),
        (
            {"common/x.yaml": "- group: {name: g, devices: [mi300_1]}\n"},
            "group belongs in a platform dir",
        ),
        (
            {
                "amd/x.yaml": "- group: {name: a, devices: [mi300_1, mi300_2]}\n"
                "- group: {name: b, devices: [mi300_4, mi300_1]}\n"
            },
            "mi300_1 is in groups a and b",
        ),
        (
            {
                "amd/x.yaml": "- group: {name: g, devices: [mi300_1]}\n"
                "- group: {name: g, devices: [mi300_2]}\n"
            },
            "second group g",
        ),
        (
            {"amd/x.yaml": "- group: {name: g, devices: [mi300_1, mi300_1]}\n"},
            "duplicate devices in group g",
        ),
        ({"amd/x.yaml": "- group: {name: g, devices: []}\n"}, "at least 1"),
        ({"amd/x.yaml": "- group: {name: G 1, devices: [mi300_1]}\n"}, "pattern"),
        ({"amd/x.yaml": "- group: {name: g, devices: [mi999]}\n"}, "mi999"),
        (
            {"amd/x.yaml": "- group: {name: g, devices: [mi300_1], fallbacks: []}\n"},
            "fallbacks",
        ),
        (
            {
                "amd/x.yaml": "- device: mi300_2\n  probes: 2\n"
                "- group: {name: g, devices: [mi300_1, mi300_2]}\n"
            },
            "mi300_2 is in group g: set its policy on the group",
        ),
    ],
)
def test_load_rejects(fake_global_config, tmp_path, load, files, message):
    dirs = write_config(tmp_path, files)
    with pytest.raises(ValueError, match=message) as error:
        load(dirs)
    if "amd/x.yaml" in files and "list" not in message:
        assert "amd/x.yaml entry" in str(error.value)


@pytest.mark.parametrize(
    "files, message",
    [
        (
            {
                "amd/x.yaml": "- device: mi300_1\n  fallbacks: [mi355_1]\n"
                "  close_when: {rule: nope, window: 1h, min_jobs: 1}\n"
            },
            "amd/x.yaml entry 0: mi300_1: unknown rule 'nope'",
        ),
        (
            {
                "amd/x.yaml": "- device: mi300_1\n  fallbacks: [mi355_1]\n"
                "  close_when: {rule: infra_failure_rate, abve: 0.5,"
                " window: 1h, min_jobs: 1}\n"
            },
            "amd/x.yaml entry 0: mi300_1: rule infra_failure_rate",
        ),
        (
            {
                "amd/x.yaml": "- device: mi355_1\n"
                "  close_when: {rule: nope, window: 1h, min_jobs: 1}\n"
            },
            "amd/x.yaml entry 0: mi355_1: unknown rule 'nope'",
        ),
        (
            {
                "common/rules.yaml": "- defaults: {probes: 1}\n",
                "amd/x.yaml": "- device: mi355_1\n  reopen_after: 1h\n",
            },
            "amd/x.yaml entry 0: mi355_1: no close_when \\(",
        ),
        (
            {
                "common/rules.yaml": None,
                "amd/x.yaml": "- group: {name: mi300, devices: [mi300_1], probes: 2}\n",
            },
            "amd/x.yaml entry 0: mi300: no close_when, reopen_after \\(",
        ),
        (
            {
                "common/rules.yaml": None,
                "amd/x.yaml": "- defaults:\n"
                "    close_when: {rule: nope, window: 1h, min_jobs: 1}\n",
            },
            "amd/x.yaml entry 0: mi250_1: unknown rule 'nope'",
        ),
        (
            {
                "common/rules.yaml": None,
                "amd/x.yaml": "- pin: {device: mi300_1, state: closed,"
                " until: '2026-10-20T00:00:00Z', reason: r}\n",
            },
            "amd/x.yaml entry 0: mi300_1 is not watched \\(no complete policy\\)",
        ),
    ],
)
def test_policies_rejected_by_the_checker_only(
    fake_global_config, tmp_path, files, message
):
    dirs = write_config(tmp_path, files)
    with pytest.raises(ValueError, match=message):
        load_health_policies(dirs)
    load_chains(dirs)


UNSUPPORTED_MESSAGE = (
    "unsupported device for queue fallback: {} \\(only AMD GPU devices\\)"
)


@pytest.mark.parametrize("load", LOADERS)
@pytest.mark.parametrize("device", UNSUPPORTED)
@pytest.mark.parametrize(
    "entry",
    [
        "- device: {}\n  fallbacks: [mi355_1]\n",
        "- device: mi300_1\n  fallbacks: [mi355_1, {}]\n",
        "- device: {}\n  reopen_after: 1h\n",
        "- group: {{name: g, devices: [mi300_1, {}]}}\n",
        "- pin: {{device: {}, state: closed, until: '2026-10-20T00:00:00Z',"
        " reason: r}}\n",
    ],
)
def test_load_rejects_unsupported_devices(
    fake_global_config, tmp_path, load, device, entry
):
    dirs = write_config(tmp_path, {"amd/x.yaml": entry.format(device)})
    with pytest.raises(ValueError, match=UNSUPPORTED_MESSAGE.format(device)):
        load(dirs)


@pytest.mark.parametrize("load", LOADERS)
@pytest.mark.parametrize(
    "files",
    [
        {"nvidia/h200.yaml": "- device: h200_18gb\n  fallbacks: [h200_35gb]\n"},
        {"nvidia/defaults.yaml": ""},
    ],
)
def test_load_rejects_an_nvidia_dir(fake_global_config, tmp_path, load, files):
    dirs = write_config(tmp_path, files)
    with pytest.raises(ValueError, match="qf/nvidia/: unsupported for queue fallback"):
        load(dirs)


def test_supported_devices():
    assert PLATFORMS == ("amd",)
    assert {platform_of(device) for device in AMD_DEVICE_CONFIGS} == {"amd"}
    assert SUPPORTED_DEVICES == set(AMD_DEVICE_CONFIGS)
    for device in (*UNSUPPORTED, None):
        with pytest.raises(ValueError, match="unsupported device"):
            platform_of(device)


def test_policy_only_entry(fake_global_config, tmp_path):
    dirs = write_config(
        tmp_path,
        {
            "amd/chains.yaml": "- device: mi300_1\n  fallbacks: [mi355_1]\n",
            "amd/policy.yaml": "- device: mi355_1\n"
            "  close_when: {rule: infra_failure_rate, above: 0.4,"
            " window: 1h, min_jobs: 5}\n"
            "- device: mi355_dpx\n  fallbacks: []\n  reopen_after: 4h\n",
        },
    )
    policies = load_health_policies(dirs)
    assert policy_for(policies, "mi355_1").close_when.params == {"above": 0.4}
    assert policy_for(policies, "mi300_1").close_when.params == {"above": 0.7}
    assert policy_for(policies, "mi355_dpx").reopen_after == 4 * 3600
    cfg = load_chains(dirs)
    assert dict(cfg.chains) == {"mi300_1": ("mi355_1",)}
    snap = snapshot({"amd_mi355_1": closed()})
    assert map_device(step("mi355_1"), cfg, snap) == ("mi355_1", None)


GROUPS = (
    "- group:\n"
    "    name: mi300\n"
    "    devices: [mi300_1, mi300_2, mi300_8]\n"
    "    reopen_after: 3h\n"
    "- group: {name: mi355, devices: [mi355_8, mi355_1]}\n"
)


def test_groups(fake_global_config, tmp_path):
    dirs = write_config(
        tmp_path,
        {
            "amd/defaults.yaml": "- defaults: {probes: 2}\n",
            "amd/groups.yaml": GROUPS,
            # A grouped device can still have a chain.
            "amd/chains.yaml": "- device: mi300_2\n  fallbacks: [mi355_2]\n",
        },
    )
    policies = load_health_policies(dirs)
    mi300 = policies.units["amd_mi300_1"]
    assert (mi300.device, mi300.group) == ("mi300_1", "mi300")
    assert mi300.queues == ("amd_mi300_1", "amd_mi300_2", "amd_mi300_8")
    # The group's keys over amd/ defaults over common/ defaults.
    assert (mi300.policy.reopen_after, mi300.policy.probes) == (3 * 3600, 2)
    assert mi300.policy.close_when.params == {"above": 0.7}
    mi355 = policies.units["amd_mi355_8"]
    assert (mi355.device, mi355.queues) == ("mi355_8", ("amd_mi355_8", "amd_mi355_1"))
    assert mi355.policy.reopen_after == 2 * 3600
    # Every supported queue is in one unit; the other devices are their own.
    members = [queue for unit in policies.units.values() for queue in unit.queues]
    assert sorted(members) == sorted(device_queue(d) for d in SUPPORTED_DEVICES)
    assert policies.units["amd_mi300_4"].queues == ("amd_mi300_4",)
    assert policies.units["amd_mi300_4"].group is None
    assert dict(load_chains(dirs).chains) == {"mi300_2": ("mi355_2",)}


def test_pin_pins_the_group(fake_global_config, tmp_path):
    pin = (
        "- pin: {{device: {}, state: closed, until: '2026-10-20T00:00:00Z',"
        " reason: r}}\n"
    )
    files = {"amd/groups.yaml": GROUPS, "amd/pins.yaml": pin.format("mi300_8")}
    pins = load_health_policies(write_config(tmp_path / "a", files)).pins
    assert list(pins) == ["amd_mi300_1"] and pins["amd_mi300_1"].device == "mi300_8"
    files["amd/pins.yaml"] += pin.format("mi300_2")
    with pytest.raises(ValueError, match="pins.yaml entry 1: second pin for group"):
        load_health_policies(write_config(tmp_path / "b", files))


def test_only_devices_with_a_complete_policy_are_watched(fake_global_config, tmp_path):
    rules = config_text("common/rules.yaml")
    assert dict(load_health_policies(write_config(tmp_path / "a", {})).units)
    none = write_config(tmp_path / "b", {"common/rules.yaml": None})
    assert dict(load_health_policies(none).units) == {}
    # Thresholds for AMD only, and no nvidia/ dir.
    amd = write_config(
        tmp_path / "c", {"common/rules.yaml": None, "amd/defaults.yaml": rules}
    )
    assert not os.path.isdir(os.path.join(amd[0], "nvidia"))
    units = load_health_policies(amd).units
    assert set(units) == {device_queue(d) for d in AMD_DEVICE_CONFIGS}
    # A device's or group's own keys can complete the defaults.
    own = write_config(
        tmp_path / "d",
        {
            "common/rules.yaml": "- defaults: {probes: 1}\n",
            "amd/x.yaml": "- device: mi355_1\n  reopen_after: 1h\n"
            "  close_when: {rule: infra_failure_rate, above: 0.5,"
            " window: 1h, min_jobs: 5}\n"
            "- group:\n    name: mi300\n    devices: [mi300_1, mi300_2]\n"
            "    reopen_after: 1h\n"
            "    close_when: {rule: infra_failure_rate, above: 0.5,"
            " window: 1h, min_jobs: 5}\n",
        },
    )
    assert set(load_health_policies(own).units) == {"amd_mi355_1", "amd_mi300_1"}


def test_config_fallback_dirs():
    assert config_fallback_dirs({}) == ()
    assert config_fallback_dirs({"fallback_dirs": None}) == ()
    assert config_fallback_dirs({"fallback_dirs": ["b", "a", "b"]}) == ("b", "a")
    for bad in ("a", ["a", ""], ["a", 1], {"a": "b"}):
        with pytest.raises(ValueError, match="fallback_dirs must be a list of paths"):
            config_fallback_dirs({"fallback_dirs": bad})


@pytest.mark.parametrize("dirs, expected", [("[a, b, a]", ("a", "b")), ("a", None)])
def test_global_config_reads_fallback_dirs(monkeypatch, tmp_path, dirs, expected):
    path = tmp_path / "ci_config.yaml"
    path.write_text(
        f"name: ci\njob_dirs: [{tmp_path}]\nregistries: r\n"
        f"repositories: {{main: m}}\nfallback_dirs: {dirs}\n"
    )
    monkeypatch.setattr(global_config, "config", None)
    monkeypatch.setattr(global_config, "get_merge_base_commit", lambda: None)
    monkeypatch.setattr(global_config, "get_list_file_diff", lambda *a: [])
    monkeypatch.setattr(global_config, "get_pr_labels", lambda *a: [])
    monkeypatch.setenv("BUILDKITE_BRANCH", "main")
    if expected is None:
        with pytest.raises(ValueError, match="fallback_dirs must be a list"):
            global_config.init_global_config(str(path))
    else:
        global_config.init_global_config(str(path))
        assert global_config.get_global_config()["fallback_dirs"] == expected


@pytest.mark.parametrize("load", LOADERS)
def test_load_missing_dir(fake_global_config, tmp_path, load):
    with pytest.raises(ValueError, match="not found: nope"):
        load(["nope"], str(tmp_path))


def test_parse_duration():
    assert [parse_duration(v) for v in ("30m", "2h", "8h", "1d")] == [
        1800,
        7200,
        28800,
        86400,
    ]
    for bad in ("0h", "2", "h", 2, "1.5h"):
        with pytest.raises(ValueError):
            parse_duration(bad)


# Rules


@pytest.mark.parametrize(
    "job, infra",
    [
        # Signatures seen on the live API.
        ({"state": "failed", "exit_status": -1, "signal_reason": "stack_error"}, 1),
        ({"state": "timed_out", "exit_status": -1, "signal_reason": "cancel"}, 0),
        (
            {
                "state": "failed",
                "exit_status": -1,
                "signal_reason": "process_run_error",
            },
            1,
        ),
        ({"state": "failed", "exit_status": 134, "signal_reason": None}, 0),
        ({"state": "failed", "exit_status": -1, "signal_reason": "agent_stop"}, 1),
        ({"state": "failed", "exit_status": -1, "signal_reason": None}, 1),
        ({"state": "failed", "exit_status": -1}, 1),
        ({"state": "failed", "exit_status": 255, "signal_reason": "agent_stop"}, 1),
        ({"state": "failed", "exit_status": 255, "signal_reason": None}, 0),
        ({"state": "failed", "exit_status": 255}, 0),
        (
            {
                "state": "failed",
                "exit_status": 255,
                "signal_reason": "signature_rejected",
            },
            0,
        ),
        ({"state": "failed", "exit_status": -1, "signal_reason": "cancel"}, 0),
        ({"state": "failed", "exit_status": 1, "signal_reason": None}, 0),
        ({"state": "passed", "exit_status": 0, "signal_reason": None}, 0),
        ({"state": "canceled", "exit_status": -1, "signal_reason": None}, 0),
    ],
)
def test_is_infra_failure(job, infra):
    assert common.is_infra_failure(job) == bool(infra)


def test_infra_failure_rate_rule():
    infra = {"state": "failed", "exit_status": -1, "signal_reason": "stack_error"}
    passed = {"state": "passed", "exit_status": 0}
    rule = rules_for("amd")["infra_failure_rate"]
    assert rule([infra] * 3 + [passed], above=0.7)
    assert not rule([infra] * 7 + [passed] * 3, above=0.7)
    assert not rule([], above=0.0)


def test_platform_rules_override_common(monkeypatch):
    from fallback_rules import amd

    monkeypatch.setitem(amd.RULES, "infra_failure_rate", lambda jobs, above: True)
    assert rules_for("amd")["infra_failure_rate"]([], above=1.0)
    assert not common.RULES["infra_failure_rate"]([], above=1.0)


# Snapshot


def fake_fetch(builds, calls=None):
    def fetch(url, token):
        if calls is not None:
            calls.append((url, token))
        return builds

    return fetch


def build_with(snap):
    return [{"meta_data": {"queue-health": json.dumps(snap)}}]


def test_read_snapshot(cfg):
    snap = raw_snapshot({"amd_mi300_1": closed()}, NOW - timedelta(minutes=10))
    calls = []
    got = read_snapshot(
        cfg.health, "vllm", "s3cret", fake_fetch(build_with(snap), calls), now=NOW
    )
    assert got == parse_snapshot(json.dumps(snap))
    assert got.queues["amd_mi300_1"].reason == "72% infra failures over 2h"
    assert calls == [
        (
            "https://api.buildkite.com/v2/organizations/vllm/pipelines/"
            "queue-health/builds?branch=main&state=passed&per_page=1",
            "s3cret",
        )
    ]


def test_read_snapshot_with_probe(cfg):
    snap = raw_snapshot({"amd_mi300_1": probing(), "amd_mi355_1": closed()})
    got = read_snapshot(cfg.health, "vllm", "t", fake_fetch(build_with(snap)), NOW)
    probe = got.queues["amd_mi300_1"].probe
    assert (probe.round, probe.device, probe.keys) == (1, "mi300_1", ("amd-kernels",))


def test_read_snapshot_stale(cfg):
    stale = build_with(raw_snapshot(generated_at=NOW - timedelta(hours=1)))
    with pytest.raises(ValueError, match="stale"):
        read_snapshot(cfg.health, "vllm", "t", fake_fetch(stale), NOW)
    got = read_snapshot(
        cfg.health, "vllm", "t", fake_fetch(stale), NOW, allow_stale=True
    )
    assert got.generated_at == NOW - timedelta(hours=1)


def test_read_snapshot_without_passed_build(cfg):
    assert read_snapshot(cfg.health, "vllm", "t", fake_fetch([]), NOW) is None


def test_snapshot_round_trips(cfg):
    pinned = {
        **closed("pinned: node upgrade"),
        "until": "2026-10-20T00:00:00Z",
        "pinned": True,
        "infra_rate": 0.5,
        "jobs": 4,
        "probe_round": 2,
        "backoff_s": 7200,
    }
    snap = snapshot({"amd_mi250_1": pinned, "amd_mi300_1": probing()})
    assert parse_snapshot(snap.model_dump_json()) == snap


def test_snapshot_is_immutable():
    snap = snapshot({"amd_mi300_1": probing()})
    entry = snap.queues["amd_mi300_1"]
    with pytest.raises(TypeError):
        snap.queues["amd_mi355_1"] = entry
    with pytest.raises(TypeError):
        del snap.queues["amd_mi300_1"]
    for obj, field in ((snap, "queues"), (entry, "state"), (entry.probe, "keys")):
        with pytest.raises(ValidationError, match="frozen"):
            setattr(obj, field, None)
    assert isinstance(entry.probe.keys, tuple)


def _raise(error):
    def f(*args, **kwargs):
        raise error

    return f


def test_build_snapshot(cfg, capsys):
    snap = raw_snapshot({"amd_mi300_1": closed()})
    got = build_snapshot(
        cfg.health,
        "vllm",
        fetch=lambda url, token: build_with(snap) if token == "s3cret" else [],
        get_secret=lambda name: "s3cret"
        if name == "QUEUE_FALLBACK_BUILDKITE_API_TOKEN"
        else "",
        now=NOW,
    )
    assert got == parse_snapshot(json.dumps(snap))
    assert "s3cret" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "org, fetch, get_secret",
    [
        (
            "vllm",
            fake_fetch(build_with(raw_snapshot(generated_at=NOW - timedelta(hours=1)))),
            None,
        ),
        ("vllm", fake_fetch([]), None),
        ("vllm", fake_fetch([{"meta_data": {}}]), None),
        ("vllm", fake_fetch([{"meta_data": {"queue-health": "{nope"}}]), None),
        ("vllm", fake_fetch(build_with({**raw_snapshot(), "version": 2})), None),
        (
            "vllm",
            fake_fetch(build_with(raw_snapshot({"q": {"state": "half-open"}}))),
            None,
        ),
        (
            "vllm",
            fake_fetch(build_with(raw_snapshot({"q": {**closed(), "extra": 1}}))),
            None,
        ),
        (
            "vllm",
            fake_fetch(
                build_with({**raw_snapshot(), "generated_at": "2026-10-16T12:00"})
            ),
            None,
        ),
        ("vllm", _raise(OSError("unreachable")), None),
        (
            "vllm",
            fake_fetch(build_with(raw_snapshot())),
            _raise(subprocess.CalledProcessError(1, "buildkite-agent")),
        ),
        (None, fake_fetch(build_with(raw_snapshot())), None),
        *[
            ("vllm", fake_fetch(build_with(raw_snapshot({"q": probing(**bad)}))), None)
            for bad in (
                {"round": 0},
                {"round": True},
                {"round": "1"},
                {"device": 1},
                {"keys": "amd-kernels"},
                {"keys": [""]},
                {"keys": [1]},
                {"extra": 1},
            )
        ],
        (
            "vllm",
            fake_fetch(
                build_with(raw_snapshot({"q": {"state": "probing", "probe": []}}))
            ),
            None,
        ),
        ("../x", fake_fetch(build_with(raw_snapshot())), None),
    ],
)
def test_build_snapshot_unavailable(cfg, capsys, org, fetch, get_secret):
    got = build_snapshot(
        cfg.health, org, fetch=fetch, get_secret=get_secret or (lambda _: "t"), now=NOW
    )
    assert got is None
    assert "every queue is treated as open" in capsys.readouterr().out


# Mapping


def step(device="mi300_1", **kwargs):
    return Step(label="Kernels", key="kernels", device=device, **kwargs)


def test_map_device_open(cfg):
    assert map_device(step(), cfg, None) == ("mi300_1", None)
    assert map_device(step(), cfg, snapshot()) == ("mi300_1", None)
    open_snap = snapshot({"amd_mi300_1": {"state": "open"}})
    assert map_device(step(), cfg, open_snap) == ("mi300_1", None)


def test_map_device_next_open(cfg):
    snap = snapshot({"amd_mi300_1": closed()})
    assert map_device(step(), cfg, snap) == (
        "mi355_1",
        "mi300_1 closed (72% infra failures over 2h) -> mi355_1",
    )


def test_map_device_skips_probing(cfg):
    snap = snapshot(
        {"amd_mi300_1": closed(), "amd_mi355_1": closed("probe 1", "probing")}
    )
    device, reason = map_device(step(), cfg, snap)
    assert device == "mi355_dpx"
    assert "mi355_1 probing (probe 1)" in reason


def test_map_device_all_closed_keeps_first(cfg):
    snap = snapshot(
        {q: closed() for q in ("amd_mi300_1", "amd_mi355_1", "amd_mi355_dpx")}
    )
    device, reason = map_device(step(), cfg, snap)
    assert device == "mi300_1"
    assert reason.endswith("-> kept mi300_1")


def test_map_device_unchanged(cfg):
    snap = snapshot({"amd_mi300_1": closed(), "amd_mi355_8": closed()})
    assert map_device(step(fallback=False), cfg, snap) == ("mi300_1", None)
    assert map_device(step("mi355_8"), cfg, snap) == ("mi355_8", None)
    assert map_device(step(None), cfg, snap) == (None, None)
    # NVIDIA steps are never moved, whatever the snapshot says of their queue.
    nvidia = snapshot({"h200_18gb": closed(), "h200_35gb": closed()})
    for device in NVIDIA:
        assert map_device(step(device), cfg, nvidia) == (device, None)


def test_pins_come_from_the_snapshot(fake_global_config, tmp_path):
    dirs = write_config(
        tmp_path,
        {
            "amd/chains.yaml": "- device: mi250_1\n  fallbacks: [mi300_1]\n",
            "amd/pins.yaml": "- pin: {device: mi250_1, state: closed,"
            " until: '2026-10-17T00:00:00Z', reason: node upgrade}\n"
            "- pin: {device: mi300_1, state: open,"
            " until: '2026-10-17T00:00:00Z', reason: verified}\n",
        },
    )
    cfg = load_chains(dirs)
    # The generator reads no pins: only the checker's snapshot entries count.
    assert map_device(step("mi250_1"), cfg, None) == ("mi250_1", None)
    snap = snapshot({"amd_mi300_1": closed()})
    assert map_device(step("mi250_1"), cfg, snap)[0] == "mi250_1"
    pinned = {
        **closed("pinned: node upgrade"),
        "until": "2026-10-17T00:00:00Z",
        "pinned": True,
    }
    snap = snapshot({"amd_mi250_1": pinned})
    assert map_device(step("mi250_1"), cfg, snap) == (
        "mi300_1",
        "mi250_1 closed (pinned: node upgrade) -> mi300_1",
    )


def test_apply_fallbacks_maps_step_and_amd_mirror(cfg):
    steps = [
        step("mi355_dpx", mirror={"amd": {"device": "mi300_1", "env": {"A": "1"}}}),
        Step(label="Other", device="mi300_1", fallback=False),
    ]
    snap = snapshot({"amd_mi300_1": closed(), "amd_mi355_dpx": closed("maintenance")})
    mapped, decisions = apply_fallbacks(steps, cfg, snap)
    assert mapped[0].device == "mi355_1"
    assert mapped[0].mirror == {"amd": {"device": "mi355_1", "env": {"A": "1"}}}
    assert steps[0].mirror["amd"]["device"] == "mi300_1"
    assert mapped[1] is steps[1]
    assert decisions == {
        "mi355_dpx closed (maintenance) -> mi355_1": ["kernels"],
        "mi300_1 closed (72% infra failures over 2h) -> mi355_1": ["amd-kernels"],
    }


def test_apply_fallbacks_moves_only_the_amd_mirror_of_an_nvidia_step(cfg):
    mirror = {"amd": {"device": "mi300_1", "env": {"A": "1"}}}
    nvidia = step("h200_18gb", mirror=mirror)
    snap = snapshot({"amd_mi300_1": closed(), "h200_18gb": closed("maintenance")})
    (mapped,), decisions = apply_fallbacks([nvidia], cfg, snap)
    assert mapped.device == "h200_18gb"
    assert mapped.mirror == {"amd": {"device": "mi355_1", "env": {"A": "1"}}}
    assert decisions == {
        "mi300_1 closed (72% infra failures over 2h) -> mi355_1": ["amd-kernels"],
    }
    # Along its AMD chain: mi300_1, mi355_1, mi355_dpx, else kept.
    snap = snapshot({q: closed() for q in ("amd_mi300_1", "amd_mi355_1")})
    (mapped,), _ = apply_fallbacks([nvidia], cfg, snap)
    assert (mapped.device, mapped.mirror["amd"]["device"]) == ("h200_18gb", "mi355_dpx")
    snap = snapshot(
        {q: closed() for q in ("amd_mi300_1", "amd_mi355_1", "amd_mi355_dpx")}
    )
    (mapped,), decisions = apply_fallbacks([nvidia], cfg, snap)
    assert mapped is nvidia
    ((reason, keys),) = decisions.items()
    assert reason.endswith("-> kept mi300_1") and keys == ["amd-kernels"]


def test_apply_fallbacks_unchanged_steps_are_the_same_objects(cfg):
    steps = [step(), step("h200_18gb", mirror={"amd": {"device": "mi300_1"}})]
    mapped, decisions = apply_fallbacks(steps, cfg, snapshot())
    assert all(a is b for a, b in zip(mapped, steps))
    assert decisions == {}


def render(steps):
    groups = convert_group_step_to_buildkite_step(group_steps(steps))
    return [group.model_dump(exclude_none=True) for group in groups]


@pytest.mark.parametrize(
    "kwargs, written",
    [
        # Native AMD step.
        ({"device": "mi300_1"}, {"device": "mi355_1"}),
        # AMD mirror of an NVIDIA step.
        (
            {
                "device": "h100",
                "mirror": {"amd": {"device": "mi300_1", "depends_on": ["x"]}},
            },
            {
                "device": "h100",
                "mirror": {"amd": {"device": "mi355_1", "depends_on": ["x"]}},
            },
        ),
    ],
)
def test_mapped_step_renders_as_written_for_its_device(cfg, kwargs, written):
    def make(**fields):
        return Step(label="Kernels", commands=["pytest -v -s kernels"], **fields)

    snap = snapshot({"amd_mi300_1": closed()})
    mapped, _ = apply_fallbacks([make(**kwargs)], cfg, snap)
    assert render(mapped) == render([make(**written)])
    assert render(mapped) != render([make(**kwargs)])


# Annotation


def test_annotate(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(queue_fallbacks.shutil, "which", lambda _: "/bin/bk")
    monkeypatch.setattr(
        queue_fallbacks.subprocess, "run", lambda cmd, **kw: calls.append(cmd)
    )
    queue_fallbacks.annotate({"mi300_1 closed -> mi355_1": ["a", "amd-b"]})
    assert len(calls) == 1
    assert calls[0][:6] == [
        "buildkite-agent",
        "annotate",
        "--context",
        "queue-fallback",
        "--style",
        "info",
    ]
    assert "- mi300_1 closed -> mi355_1: `a`, `amd-b`" in calls[0][6]
    queue_fallbacks.annotate({})
    assert len(calls) == 1


def test_annotate_never_fails(monkeypatch, capsys):
    monkeypatch.setattr(queue_fallbacks.shutil, "which", lambda _: None)
    queue_fallbacks.annotate({"r": ["a"]})
    assert "- r: `a`" in capsys.readouterr().out

    monkeypatch.setattr(queue_fallbacks.shutil, "which", lambda _: "/bin/bk")
    monkeypatch.setattr(
        queue_fallbacks.subprocess, "run", _raise(OSError("exec format error"))
    )
    queue_fallbacks.annotate({"r": ["a"]})
    assert "annotation failed" in capsys.readouterr().out


# The generator end to end


@pytest.fixture
def generate(fake_global_config, monkeypatch, tmp_path):
    fake_global_config.update(job_dirs=[JOBS_DIR], run_all=True)
    monkeypatch.setattr(
        pipeline_generator, "get_global_config", lambda: fake_global_config
    )
    annotations = []
    monkeypatch.setattr(queue_fallbacks, "annotate", annotations.append)

    def run(fallback_dirs=None, snap=None, job_dir=JOBS_DIR, branch="test-branch"):
        fake_global_config.update(job_dirs=[job_dir], branch=branch)
        fake_global_config["only_step_keys"] = None
        if fallback_dirs is not None:
            fake_global_config["fallback_dirs"] = fallback_dirs
        else:
            fake_global_config.pop("fallback_dirs", None)
        monkeypatch.setattr(queue_fallbacks, "build_snapshot", lambda *a: snap)
        out = tmp_path / "pipeline.yaml"
        generator = pipeline_generator.PipelineGenerator.__new__(
            pipeline_generator.PipelineGenerator
        )
        generator.output_file_path = str(out)
        generator.generate()
        return out.read_text()

    run.annotations = annotations
    return run


def test_generate_is_unchanged_without_closed_queues(generate):
    today = generate()
    assert "amd_mi300_1" in today and "h200_18gb" in today
    all_open = snapshot(
        {"amd_mi300_1": {"state": "open"}, "h200_18gb": {"state": "open"}}
    )
    for fallback_dirs, snap in (
        ([], None),
        ([CONFIG_DIR], None),
        ([CONFIG_DIR], snapshot()),
        ([CONFIG_DIR], all_open),
    ):
        assert generate(fallback_dirs, snap) == today
    assert generate.annotations == [{}, {}, {}]


def test_generate_maps_closed_queues(generate):
    today = generate()
    snap = snapshot({"amd_mi300_1": closed()})
    mapped = generate([CONFIG_DIR], snap)
    assert mapped != today
    jobs = jobs_of(mapped)
    queues = {
        key: job["agents"]["queue"] for key, job in jobs.items() if "agents" in job
    }
    assert queues["amd-kernels"] == "amd_mi355_1"
    assert queues["basic-correctness"] == "amd_mi355_1"
    assert queues["no-fallback"] == "amd_mi300_1"
    assert queues["kernels"] == "h200_18gb"
    # The NVIDIA step is rendered exactly as before; only its AMD mirror moved.
    assert jobs["kernels"] == jobs_of(today)["kernels"]
    ((reason, keys),) = generate.annotations[-1].items()
    assert reason == "mi300_1 closed (72% infra failures over 2h) -> mi355_1"
    assert sorted(keys) == ["amd-kernels", "basic-correctness"]


def jobs_of(pipeline):
    return {
        job["key"]: {**job, "group": group["group"]}
        for group in yaml.safe_load(pipeline)["steps"]
        for job in group.get("steps", [])
    }


@pytest.mark.parametrize("branch", ["test-branch", "main"])
def test_generate_never_maps_unsupported_devices(generate, branch):
    today = generate(job_dir=UNSUPPORTED_JOBS_DIR, branch=branch)
    jobs = jobs_of(today)
    queue_of = {
        key[len("on-") :]: job["agents"]["queue"]
        for key, job in jobs.items()
        if key.startswith("on-")
    }
    # The retired A100 fleet renders no steps.
    assert set(queue_of) == set(UNSUPPORTED) - {"a100"}
    assert {"intel-cpu", "gh200_queue", "amd-cpu", "amd-zen5-cpu"} <= {
        *queue_of.values()
    }
    keys = sorted(jobs)
    everything_closed = snapshot({queue.value: closed() for queue in AgentQueue})
    # Each device's own queue probing on it, and every AMD queue probing on
    # its own device with the keys of all these steps.
    everything_probing = snapshot(
        {
            **{
                queue: probing(device=device, keys=[f"on-{device}"])
                for device, queue in queue_of.items()
            },
            **{
                device_queue(device): probing(device=device, keys=keys)
                for device in AMD_DEVICE_CONFIGS
            },
        }
    )
    for snap in (everything_closed, everything_probing):
        got = generate([CONFIG_DIR], snap, job_dir=UNSUPPORTED_JOBS_DIR, branch=branch)
        assert got == today
    assert generate.annotations == [{}, {}]


# Probes


def test_probe_key():
    assert probe_key("amd_mi300_1", 2, "amd-kernels") == (
        "probe-amd_mi300_1-r2-amd-kernels"
    )
    assert probe_key("amd_mi355_1", 1, "a.b c:d/e") == (
        "probe-amd_mi355_1-r1-a-b-c-d-e"
    )
    long = probe_key("amd_mi300_1", 1, "x" * 200)
    assert len(long) == 100 and long.startswith("probe-amd_mi300_1-r1-xxx")


def test_parse_probe_key():
    for queue in {device_queue(device) for device in SUPPORTED_DEVICES}:
        key = probe_key(queue, 12, "amd-x-r3-y")
        assert key.startswith(probe_prefix(queue, 12))
        assert parse_probe_key(key) == (queue, 12)
    for key in (None, "", "kernels", "probe-amd_mi300_1", "probe-q-r0-x"):
        assert parse_probe_key(key) is None


def probe_snapshot(**probe):
    return snapshot({"amd_mi300_1": probing(**probe)})


def test_generate_probes_on_main_only(generate):
    snap = probe_snapshot(keys=["amd-kernels", "basic-correctness", "not-here"])
    pr = jobs_of(generate([CONFIG_DIR], snap))
    assert not any(key.startswith("probe-") for key in pr)

    jobs = jobs_of(generate([CONFIG_DIR], snap, branch="main"))
    probes = {key: job for key, job in jobs.items() if key.startswith("probe-")}
    assert sorted(probes) == [
        "probe-amd_mi300_1-r1-amd-kernels",
        "probe-amd_mi300_1-r1-basic-correctness",
    ]
    for job in probes.values():
        # On the probed queue, not mapped off it like the step itself.
        assert job["agents"]["queue"] == "amd_mi300_1"
        assert job["soft_fail"] is True
        assert job["group"] == "Queue probes"
        assert job["label"].startswith("Probe amd_mi300_1 r1: ")
    assert jobs["amd-kernels"]["agents"]["queue"] == "amd_mi355_1"
    assert jobs["basic-correctness"]["agents"]["queue"] == "amd_mi355_1"
    assert not any(
        key in (job.get("depends_on") or []) for job in jobs.values() for key in probes
    )
    assert generate.annotations[-1]["amd_mi300_1 probe round 1 on mi300_1"] == list(
        probes
    )


def test_probe_renders_as_written_for_its_device(fake_global_config):
    native = Step(label="Basic", key="basic", device="mi355_1", commands=["pytest"])
    mirrored = Step(
        label="Kernels",
        key="kernels",
        device="h200_18gb",
        commands=["pytest -v -s kernels"],
        mirror={"amd": {"device": "mi355_1", "env": {"A": "1"}}},
    )
    snap = probe_snapshot(round=3, keys=["basic", "amd-kernels"])
    (group,) = probe_steps([native, mirrored], snap)
    rendered = {s.key: s for s in group.steps}

    def as_written(original, key, update):
        groups = convert_group_step_to_buildkite_step(
            {"g": [original.model_copy(update=update)]}
        )
        (written,) = [s for g in groups for s in g.steps if s.key == key]
        return written

    for key, original, update in (
        ("basic", native, {"device": "mi300_1"}),
        (
            "amd-kernels",
            mirrored,
            {"mirror": {"amd": {"device": "mi300_1", "env": {"A": "1"}}}},
        ),
    ):
        probe = rendered[f"probe-amd_mi300_1-r3-{key}"]
        written = as_written(original, key, update)
        assert written.agents["queue"] == "amd_mi300_1"
        ignore = {"key", "label", "soft_fail"}
        assert probe.model_dump(exclude=ignore) == written.model_dump(exclude=ignore)
        assert probe.soft_fail is True and written.soft_fail is False


def test_probe_is_one_job(fake_global_config):
    gated = {"parallelism": 4, "concurrency": 1, "concurrency_group": "g"}
    native = Step(label="Basic", key="basic", device="mi300_1", commands=["x"], **gated)
    mirrored = Step(
        label="Kernels",
        key="kernels",
        device="h200_18gb",
        commands=["x"],
        mirror={"amd": {"device": "mi300_1", **gated, "parallelism": 2}},
    )
    (written,) = convert_group_step_to_buildkite_step({"g": [native]})[0].steps
    assert (written.parallelism, written.concurrency) == (4, 1)
    snap = snapshot(
        {
            "amd_mi355_1": probing(device="mi355_1", keys=["basic", "amd-kernels"]),
        }
    )
    (group,) = probe_steps([native, mirrored], snap)
    assert len(group.steps) == 2
    for probe in group.steps:
        assert probe.parallelism is None
        assert probe.concurrency is None and probe.concurrency_group is None


def test_probe_runs_even_if_its_step_does_not(fake_global_config):
    fake_global_config.update(run_all=False, list_file_diff=["docs/x.md"])
    step = Step(
        label="Basic",
        key="basic",
        device="mi300_1",
        commands=["pytest"],
        source_file_dependencies=["vllm/"],
    )
    (written,) = convert_group_step_to_buildkite_step({"g": [step]})[0].steps
    assert written.deferred
    (group,) = probe_steps([step], probe_snapshot(keys=["basic"]))
    assert not group.steps[0].deferred


@pytest.mark.parametrize(
    "queues",
    [
        # The probe device is not on the probed queue.
        {"amd_mi300_1": probing(device="mi355_1", keys=["basic"])},
        {"amd_mi300_1": probing(device="nope", keys=["basic"])},
        # Unsupported devices.
        {"amd-cpu": probing(device="amd_cpu", keys=["basic"])},
        {"H200": probing(device="h200", keys=["kernels"])},
        {"h200_18gb": probing(device="h200_18gb", keys=["kernels"])},
        # An NVIDIA step cannot probe an AMD queue.
        {"amd_mi300_1": probing(keys=["kernels"])},
        # Only probing queues run probes.
        {"amd_mi300_1": {**probing(keys=["basic"]), "state": "closed"}},
        {"amd_mi300_1": {**probing(keys=["basic"]), "state": "open"}},
        # Not in this build.
        {"amd_mi300_1": probing(keys=["amd-basic", "other"])},
        {"amd_mi300_1": closed("probe round 1", "probing")},
    ],
)
def test_probes_skipped(fake_global_config, queues):
    steps = [
        Step(label="Basic", key="basic", device="mi300_1", commands=["pytest"]),
        Step(label="Kernels", key="kernels", device="h200_18gb", commands=["pytest"]),
    ]
    assert probe_steps(steps, snapshot(queues)) == []
    assert probe_steps(steps, None) == []


def test_probe_that_cannot_render_is_skipped(fake_global_config):
    # mi300_8 has 8 GPUs; rendering a 1-GPU step there raises.
    step = Step(
        label="Basic", key="basic", device="mi300_1", num_devices=1, commands=["x"]
    )
    snap = snapshot({"amd_mi300_8": probing(device="mi300_8", keys=["basic"])})
    assert probe_steps([step], snap) == []


@pytest.mark.parametrize(
    "update, env",
    [
        ({"only_step_keys": frozenset({"basic-correctness"})}, {}),
        ({}, {"VLLM_CI_KERNREC": "1"}),
        ({}, {"VLLM_CI_FNREC": "1"}),
    ],
)
def test_no_probes_in_targeted_or_recording_builds(
    fake_global_config, monkeypatch, update, env
):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    fake_global_config.update(branch="main", fallback_dirs=[CONFIG_DIR], **update)
    monkeypatch.setattr(queue_fallbacks, "annotate", lambda decisions: None)
    monkeypatch.setattr(
        queue_fallbacks,
        "build_snapshot",
        lambda *a: probe_snapshot(keys=["basic-correctness"]),
    )
    step = Step(label="Basic Correctness", device="mi300_1", commands=["x"])
    _, probes = queue_fallbacks.apply_queue_fallbacks([step], fake_global_config)
    assert probes == []
    fake_global_config.update(only_step_keys=None)
    monkeypatch.delenv("VLLM_CI_KERNREC", raising=False)
    monkeypatch.delenv("VLLM_CI_FNREC", raising=False)
    _, probes = queue_fallbacks.apply_queue_fallbacks([step], fake_global_config)
    assert [s.key for s in probes[0].steps] == [
        "probe-amd_mi300_1-r1-basic-correctness"
    ]


def test_snapshot_is_read_once_for_mapping_and_probes(fake_global_config, monkeypatch):
    fake_global_config.update(branch="main", fallback_dirs=[CONFIG_DIR])
    monkeypatch.setattr(queue_fallbacks, "annotate", lambda decisions: None)
    snap = probe_snapshot(keys=["basic"])
    reads, seen = [], []
    monkeypatch.setattr(
        queue_fallbacks, "build_snapshot", lambda *a: reads.append(a) or snap
    )
    for name, position in (("apply_fallbacks", 2), ("probe_steps", 1)):
        real = getattr(queue_fallbacks, name)

        def spy(*args, real=real, position=position):
            seen.append(args[position])
            return real(*args)

        monkeypatch.setattr(queue_fallbacks, name, spy)
    step = Step(label="Basic", key="basic", device="mi300_1", commands=["x"])
    _, probes = queue_fallbacks.apply_queue_fallbacks([step], fake_global_config)
    assert len(reads) == 1
    assert len(seen) == 2 and all(got is snap for got in seen)
    assert [s.key for s in probes[0].steps] == ["probe-amd_mi300_1-r1-basic"]


def test_device_queue_without_global_config(monkeypatch):
    monkeypatch.setattr(global_config, "config", None)
    assert queue_fallbacks.device_queue("mi300_1") == AgentQueue.AMD_MI300_1.value
