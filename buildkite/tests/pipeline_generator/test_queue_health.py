import dataclasses
import json
import os
import shutil
import subprocess
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

import global_config
import queue_health
from amd import AMD_DEVICE_CONFIGS
from queue_fallbacks import (
    SUPPORTED_DEVICES,
    Probe,
    QueueState,
    Snapshot,
    build_snapshot,
    device_queue,
    load_chains,
    load_health_policies,
    map_device,
    probe_key,
    probe_steps,
)
from queue_health import (
    MAX_BACKOFF_S,
    Checker,
    candidate_keys,
    fallback_dirs,
    fetch_history,
    job_queue,
)
from step import Step

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
CONFIG_DIR = os.path.join(FIXTURES, "queue_fallbacks", "config")
REPO = os.path.join(FIXTURES, "queue_health", "repo")
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
ORG = "vllm"
TOKEN = "s3cr3t-token"
MI300 = "amd_mi300_1"  # common policy, amd reopen_after 1h, probes 3
DPX = "amd_mi355_dpx"  # above 0.5, window 1h, min_jobs 10
HOUR = timedelta(hours=1)


def iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


OUTCOMES = {
    "passed": {"state": "passed", "exit_status": 0},
    "infra": {"state": "failed", "exit_status": -1, "signal_reason": "stack_error"},
    "test": {"state": "failed", "exit_status": 1},
    "timeout": {"state": "timed_out", "exit_status": -1, "signal_reason": "cancel"},
    "canceled": {"state": "canceled"},
    "running": {"state": "running"},
    "scheduled": {"state": "scheduled"},
}


def job(
    queue,
    outcome="passed",
    key="kernels",
    finished=NOW - timedelta(minutes=10),
    ran=timedelta(minutes=5),
    **extra,
):
    """A job that became runnable and started `ran` before `finished`; a
    running one has not finished, a scheduled one has not started."""
    runnable = finished - ran
    started = None if outcome == "scheduled" else runnable
    if outcome in ("running", "scheduled"):
        finished = None
    return {
        "type": "script",
        "signal_reason": None,
        **OUTCOMES[outcome],
        "step_key": key,
        "agent_query_rules": [f"queue={queue}"],
        "runnable_at": iso(runnable),
        "started_at": iso(started) if started else None,
        "finished_at": iso(finished) if finished else None,
        **extra,
    }


def probe(outcome, key="a", round_=2, **kwargs):
    return job(MI300, outcome, key=probe_key(MI300, round_, key), **kwargs)


@pytest.fixture
def cfg(fake_global_config):
    return load_health_policies([CONFIG_DIR])


def snapshot(queues, generated_at=NOW):
    return Snapshot(version=1, generated_at=generated_at, queues=queues)


def check(cfg, prev=None, jobs=(), now=NOW):
    """The next entries from `prev` entries and `jobs`."""
    previous = None if prev is None else snapshot(prev)
    return Checker(cfg, now).run(previous, list(jobs)).queues


def closed_entry(until=NOW, backoff=3600, round_=0):
    return QueueState(
        state="closed",
        since=until - timedelta(seconds=backoff),
        until=until,
        reason="infra_failure_rate: 80% infra failures in 20 jobs over 2h",
        backoff_s=backoff,
        probe_round=round_,
    )


def probing_entry(deadline=NOW, round_=2, backoff=7200, keys=("a", "b", "c")):
    """A round of MI300 (reopen_after 1h) that ends at `deadline`."""
    return QueueState(
        state="probing",
        since=deadline - HOUR,
        until=deadline,
        reason=f"probe round {round_} on mi300_1 until {iso(deadline)}",
        backoff_s=backoff,
        probe_round=round_,
        probe=Probe(round=round_, device="mi300_1", keys=keys),
    )


def test_job_queue():
    assert job_queue({"agent_query_rules": ["os=linux", "queue=q1"]}) == "q1"
    assert job_queue({"agent_query_rules": None}) is None


def test_every_supported_queue_is_watched(cfg):
    queues = check(cfg)
    assert set(queues) == {device_queue(d) for d in SUPPORTED_DEVICES}
    assert {e.state for q, e in queues.items() if q != "amd_mi250_1"} == {"open"}
    assert queues["amd_mi250_1"].pinned


# History


class FakeBuildkite:
    """The builds REST endpoint over builds per pipeline, newest first."""

    def __init__(self, builds=None, error=None, history_error=None):
        self.builds = builds or {}
        self.error, self.history_error = error, history_error
        self.urls = []

    def __call__(self, url, token):
        assert token == TOKEN
        self.urls.append(url)
        parsed = urllib.parse.urlparse(url)
        pipeline = parsed.path.split("/")[-2]
        params = dict(urllib.parse.parse_qsl(parsed.query))
        if self.error or (self.history_error and pipeline != "queue-health"):
            raise self.error or self.history_error
        builds = self.builds.get(pipeline, [])
        builds = [b for b in builds if b.get("branch", "main") == params["branch"]]
        if "state" in params:
            builds = [b for b in builds if b.get("state") == params["state"]]
        if "finished_from" in params:
            builds = [b for b in builds if b.get("state") not in ("running", "failing")]
        page, per_page = int(params.get("page", 1)), int(params["per_page"])
        return builds[(page - 1) * per_page : page * per_page]


def history(*jobs):
    """Finished jobs in a passed build, unfinished ones in a running one."""
    done = [j for j in jobs if j.get("finished_at")]
    running = [j for j in jobs if not j.get("finished_at")]
    return {
        "ci": [
            {"id": "b2", "number": 2, "state": "running", "jobs": running},
            {"id": "b1", "number": 1, "state": "passed", "jobs": done},
        ]
    }


def test_fetch_history_merges_finished_and_running_builds():
    since = NOW - timedelta(hours=2)
    running = job("q", "running", key="running")
    builds = history(
        job("q", key="ok"),
        job("q", "infra", key="retried", retried=True),
        {**job("q", key="waiter"), "type": "waiter"},
        running,
    )
    builds["ci"].append({"number": 1, "state": "failed", "jobs": [job("q")]})
    fake = FakeBuildkite(builds)
    jobs = fetch_history(fake, TOKEN, ORG, ["ci"], since)
    assert sorted(j["step_key"] for j in jobs) == ["kernels", "ok", "running"]
    assert running in jobs
    first = urllib.parse.urlparse(fake.urls[0])
    assert first.path == f"/v2/organizations/{ORG}/pipelines/ci/builds"
    assert dict(urllib.parse.parse_qsl(first.query))["finished_from"] == iso(since)


def test_fetch_history_reads_main_builds_only():
    pr = {"id": "pr", "number": 3, "state": "passed", "branch": "fix", "jobs": []}
    pr["jobs"] = [job(MI300, "infra", key="pr") for _ in range(20)]
    builds = history(job(MI300, key="main"))
    builds["ci"].append(pr)
    fake = FakeBuildkite(builds)
    jobs = fetch_history(fake, TOKEN, ORG, ["ci"], NOW - 2 * HOUR)
    assert [j["step_key"] for j in jobs] == ["main"]
    for url in fake.urls:
        assert (
            dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))["branch"]
            == "main"
        )


def test_fetch_history_paginates_and_caps(monkeypatch, capsys):
    monkeypatch.setattr(queue_health, "HISTORY_PER_PAGE", 2)
    monkeypatch.setattr(queue_health, "HISTORY_MAX_PAGES", 3)
    builds = [
        {"id": str(i), "number": i, "state": "passed", "jobs": [job("q", key=str(i))]}
        for i in range(10)
    ]
    jobs = fetch_history(FakeBuildkite({"ci": builds}), TOKEN, ORG, ["ci"], NOW)
    assert len(jobs) == 6
    assert "capped at 3 pages" in capsys.readouterr().out


def test_history_since_covers_windows_and_probing_rounds(cfg):
    checker = Checker(cfg, NOW)
    assert checker.history_since(None) == NOW - timedelta(hours=2)
    old_round = probing_entry(deadline=NOW - timedelta(hours=3))
    previous = snapshot({MI300: old_round, DPX: closed_entry(until=NOW - 9 * HOUR)})
    assert checker.history_since(previous) == old_round.since


# Open -> closed


def test_open_queue_closes_on_threshold(cfg):
    jobs = [job(MI300, "infra", key=f"k{i % 4}") for i in range(15)]
    jobs += [job(MI300, key="ok") for _ in range(5)]
    entry = check(cfg, jobs=jobs)[MI300]
    assert entry == QueueState(
        state="closed",
        since=NOW,
        until=NOW + HOUR,
        reason="infra_failure_rate: 75% infra failures in 20 jobs over 2h",
        infra_rate=0.75,
        jobs=20,
        backoff_s=3600,
        # The next round, on the keys of the jobs that closed it.
        probe=Probe(round=1, device="mi300_1", keys=("k0", "k1", "k2")),
    )


@pytest.mark.parametrize(
    "infra_jobs, ok_jobs, state",
    [
        (14, 6, "open"),  # 70% is not above 0.7
        (19, 0, "open"),  # min_jobs 20 not reached
        (15, 5, "closed"),
    ],
)
def test_threshold_and_min_jobs(cfg, infra_jobs, ok_jobs, state):
    jobs = [job(MI300, "infra") for _ in range(infra_jobs)]
    jobs += [job(MI300) for _ in range(ok_jobs)]
    assert check(cfg, jobs=jobs)[MI300].state == state


def test_only_infra_failures_count(cfg):
    jobs = [job(MI300, "test") for _ in range(10)]
    jobs += [job(MI300, "timeout") for _ in range(10)]
    jobs += [job(MI300, "test", exit_status=-1, signal_reason="cancel")] * 10
    jobs += [job(MI300, "running") for _ in range(30)]
    entry = check(cfg, jobs=jobs)[MI300]
    assert entry.state == "open" and (entry.infra_rate, entry.jobs) == (0.0, 30)


def test_device_policy_window_and_threshold(cfg):
    recent = [job(DPX, "infra") for _ in range(6)] + [job(DPX) for _ in range(4)]
    old = [job(DPX, finished=NOW - timedelta(minutes=90)) for _ in range(20)]
    entry = check(cfg, jobs=recent + old)[DPX]
    assert entry.state == "closed" and entry.jobs == 10
    assert "over 1h" in entry.reason


# Closed -> probing


def test_candidate_keys():
    jobs = [
        job("q", key="ok", finished=NOW),
        job("q", "infra", key="amd-kernels", finished=NOW - 2 * HOUR),
        job("q", "infra", key="kernels", finished=NOW - HOUR),
        job("q", "infra", key=probe_key("q", 3, "models")),
        job("q", "test", key="lora"),
        job("q", key="kernels"),
    ]
    probed = {probe_key("q", 3, "models"): "models"}
    assert candidate_keys(jobs, probed) == [
        "models",
        "kernels",
        "amd-kernels",
        "ok",
        "lora",
    ]
    # A probe job of an unknown round stands for no step.
    assert candidate_keys(jobs) == ["kernels", "amd-kernels", "ok", "lora"]


@pytest.mark.parametrize("key", ["tests-[1-2]", "a" * 120])
def test_candidate_keys_of_sanitized_probe_keys(key):
    # Probe keys are sanitized and cut, so only the round's own keys map back.
    jobs = [job("q", "infra", key=probe_key("q", 2, key))]
    assert candidate_keys(jobs, {probe_key("q", 2, key): key}) == [key]


def test_closed_queue_waits_until_its_until(cfg):
    prev = closed_entry(until=NOW + timedelta(minutes=1))
    entry = check(cfg, {MI300: prev}, jobs=[job(MI300, "infra")])[MI300]
    assert entry == prev.model_copy(update={"infra_rate": 1.0, "jobs": 1})


def test_closed_queue_starts_a_round_when_due(cfg):
    jobs = [
        job(MI300, key="d"),
        job(MI300, "infra", key="amd-c"),
        job(MI300, "infra", key=probe_key(MI300, 4, "b")),
        job(MI300, "infra", key="old", finished=NOW - 3 * HOUR),
        job(MI300, key="e"),
    ]
    prev = closed_entry(backoff=7200, round_=4).model_copy(
        update={"probe": Probe(round=4, device="mi300_1", keys=("b", "x"))}
    )
    entry = check(cfg, {MI300: prev}, jobs=jobs)[MI300]
    assert entry.state == "probing" and entry.since == NOW
    # Fixed deadline: round start + reopen_after (1h).
    assert entry.until == NOW + HOUR
    assert (entry.probe_round, entry.backoff_s) == (5, 7200)
    # Up to `probes` keys of the current window, infra-failed first, over the
    # last round's.
    assert entry.probe == Probe(round=5, device="mi300_1", keys=("amd-c", "b", "d"))
    assert entry.reason == f"probe round 5 on mi300_1 until {iso(NOW + HOUR)}"


def test_probe_device_is_the_queues_policy_device(cfg):
    for queue, device in [("amd_mi355_1", "mi355_1"), ("h200_35gb", "h200_35gb")]:
        entry = check(cfg, {queue: closed_entry()})[queue]
        assert entry.probe.device == device == cfg.units[queue].device


# Probing -> open or closed, at the deadline


def test_round_waits_for_its_deadline(cfg):
    prev = probing_entry(deadline=NOW + timedelta(minutes=1))
    jobs = [probe("passed", k) for k in "abc"] + [probe("infra", k) for k in "abc"]
    entry = check(cfg, {MI300: prev}, jobs=jobs)[MI300]
    assert entry == prev.model_copy(update={"infra_rate": 0.5, "jobs": 6})


def test_deterministic_test_failures_reopen_at_the_deadline(cfg):
    jobs = [probe("test", "a"), probe("timeout", "b"), probe("test", "c")]
    entry = check(cfg, {MI300: probing_entry()}, jobs=jobs)[MI300]
    assert entry.state == "open" and entry.since == NOW
    assert entry.reason == "probe round 2: 0 infra failures in 3 probes; reopened"
    assert (entry.probe_round, entry.backoff_s, entry.probe) == (2, 7200, None)


@pytest.mark.parametrize(
    "jobs",
    [
        [],
        # Not a key of the round, or another round.
        [probe("infra", "z"), *(probe("infra", k, round_=1) for k in "abc")],
        # Runnable only after the deadline, or never (blocked).
        [probe("scheduled", "a", finished=NOW + timedelta(minutes=10))],
        [probe("scheduled", "a", runnable_at=None)],
        # Canceled before it started.
        [probe("canceled", "a", started_at=None)],
    ],
)
def test_probes_without_evidence_reopen_at_the_deadline(cfg, jobs):
    entry = check(cfg, {MI300: probing_entry()}, jobs=jobs)[MI300]
    assert entry.state == "open"
    assert entry.reason == "probe round 2: 0 infra failures in 0 probes; reopened"


@pytest.mark.parametrize(
    "late",
    [
        probe("scheduled", "a"),
        # Started only after the deadline.
        probe("infra", "a", finished=NOW + timedelta(minutes=9)),
    ],
)
def test_runnable_probes_that_never_started_stay_closed(cfg, late):
    late = {**late, "runnable_at": iso(NOW - HOUR / 2)}
    entry = check(cfg, {MI300: probing_entry()}, jobs=[late], now=NOW + HOUR / 6)
    assert entry[MI300].state == "closed"
    assert entry[MI300].reason == "probe round 2: 1 infra failures in 1 probes"


def test_infra_failing_probes_stay_closed_with_doubled_backoff(cfg):
    jobs = [probe("infra", k) for k in "abc"]
    entry = check(cfg, {MI300: probing_entry(backoff=7200)}, jobs=jobs)[MI300]
    assert entry == QueueState(
        state="closed",
        since=NOW,
        until=NOW + 4 * HOUR,
        reason="probe round 2: 3 infra failures in 3 probes",
        probe_round=2,
        backoff_s=4 * 3600,
        infra_rate=1.0,
        jobs=3,
        probe=Probe(round=3, device="mi300_1", keys=("a", "b", "c")),
    )
    # With the round-2 probe jobs out of the window, round 3 runs on the keys
    # recorded at the close.
    entry = check(cfg, {MI300: entry}, jobs=jobs, now=NOW + 4 * HOUR)[MI300]
    assert entry.state == "probing" and entry.probe.round == 3
    assert entry.probe.keys == ("a", "b", "c")


def test_policy_threshold_decides_the_round(cfg):
    # 1 of 2 is 50%, not above 0.7.
    jobs = [probe("infra", "a"), probe("passed", "b")]
    assert check(cfg, {MI300: probing_entry()}, jobs=jobs)[MI300].state == "open"


@pytest.mark.parametrize(
    "jobs, infra, probes",
    [
        ([probe("infra", "a"), probe("running", "b")], 1, 1),
        ([probe("infra", "a"), probe("infra", "b"), probe("running", "c")], 2, 2),
        # Passed only after the deadline.
        ([probe("infra", "a"), probe("passed", "b", finished=NOW + HOUR / 12)], 1, 1),
    ],
)
def test_probes_running_at_the_deadline_are_no_evidence(cfg, jobs, infra, probes):
    entry = check(cfg, {MI300: probing_entry()}, jobs=jobs, now=NOW + HOUR / 6)
    assert entry[MI300].state == "closed"
    assert entry[MI300].reason == (
        f"probe round 2: {infra} infra failures in {probes} probes"
    )


@pytest.mark.parametrize(
    "jobs, state",
    [
        (
            [probe("infra", "a", finished=NOW - HOUR / 3), probe("passed", "a")],
            "closed",
        ),
        (
            [probe("running", "a", finished=NOW - HOUR / 2), probe("passed", "a")],
            "open",
        ),
    ],
)
def test_one_probe_per_key_the_first_with_evidence(cfg, jobs, state):
    entry = check(cfg, {MI300: probing_entry()}, jobs=jobs)[MI300]
    assert entry.state == state and " in 1 probes" in entry.reason


@pytest.mark.parametrize("reopen_after, cap", [("1h", MAX_BACKOFF_S), ("10h", 36000)])
def test_backoff_is_capped_but_never_shrinks(tmp_path, reopen_after, cap):
    shutil.copytree(CONFIG_DIR, tmp_path / "qf")
    (tmp_path / "qf" / "amd" / "defaults.yaml").write_text(
        f"- defaults:\n    reopen_after: {reopen_after}\n"
    )
    cfg = load_health_policies([str(tmp_path / "qf")])
    for backoff in (6 * 3600, cap):
        prev = probing_entry(backoff=backoff)
        entry = check(cfg, {MI300: prev}, jobs=[probe("infra")])[MI300]
        assert entry.backoff_s == cap


def test_every_state_has_a_bounded_exit(cfg):
    """Each round ends at its deadline: closed again on infra evidence, with
    a doubled backoff up to the cap, and reopened once a round has none."""

    def failing(at):
        # Steps kept on the queue (e.g. `fallback: false`) still fail for infra.
        return [job(MI300, "infra", key=f"k{i % 2}", finished=at) for i in range(20)]

    entry = check(cfg, jobs=failing(NOW))[MI300]
    assert (entry.state, entry.until) == ("closed", NOW + HOUR)
    rounds = []
    for backoff in (2, 4, 8, 8):
        start = entry.until
        entry = check(cfg, {MI300: entry}, jobs=failing(start), now=start)[MI300]
        assert entry.state == "probing" and entry.until == start + HOUR
        rounds.append(entry.probe.round)
        keys = [probe_key(MI300, entry.probe.round, k) for k in entry.probe.keys]
        probes = [job(MI300, "infra", key=k, finished=entry.until) for k in keys]
        entry = check(cfg, {MI300: entry}, jobs=probes, now=entry.until)[MI300]
        assert entry.state == "closed" and entry.backoff_s == backoff * 3600
    assert rounds == [1, 2, 3, 4]

    start = entry.until
    entry = check(cfg, {MI300: entry}, jobs=failing(start), now=start)[MI300]
    entry = check(cfg, {MI300: entry}, now=entry.until)[MI300]
    assert entry.state == "open" and entry.probe_round == 5


def test_broken_queue_stays_closed_until_its_probes_pass(cfg):
    """A Kubernetes queue outage: jobs never start and fail with stack_error 5h after
    they became runnable, longer than the 2h window plus a backoff of up to
    8h can see. Every 10 minutes the checker runs and every 20 minutes a
    main build runs 6 steps on the queue while it is open, or its probes."""
    queue, keys, fixed = "l4-k8s", [f"k{i}" for i in range(6)], NOW + 50 * HOUR
    runs = []  # (runnable_at, step key)

    def view(runnable, key, now):
        broken = runnable < fixed
        ran = 5 * HOUR if broken else timedelta(minutes=20)
        if runnable + ran <= now:
            outcome = "infra" if broken else "passed"
        else:
            outcome = "scheduled" if broken else "running"
        view = job(queue, outcome, key=key, finished=runnable + ran, ran=ran)
        return {**view, "started_at": None} if broken else view

    entry, states = None, []
    for tick in range(64 * 6):
        now = NOW + tick * timedelta(minutes=10)
        if tick % 2 == 0:
            if entry is None or entry.state == "open":
                runs += [(now, key) for key in keys]
            elif entry.state == "probing":
                probe = entry.probe
                runs += [(now, probe_key(queue, probe.round, k)) for k in probe.keys]
        jobs = [view(at, key, now) for at, key in runs]
        prev = None if entry is None else {queue: entry}
        new = check(cfg, prev, jobs, now)[queue]
        if entry is None or new.state != entry.state:
            states.append((now - NOW, new.state, new.backoff_s // 3600))
        entry = new

    closes = [(at, backoff) for at, state, backoff in states if state == "closed"]
    reopened = [at for at, state, _ in states[1:] if state == "open"]
    # Closed once the failures show, then kept closed by every round.
    assert [backoff for _, backoff in closes] == [2, 4, 8, 8, 8, 8]
    assert closes[0][0] == 6 * HOUR and closes[-1][0] < fixed - NOW
    # Reopened once, at the deadline of the first round after the fix.
    assert reopened == [56 * HOUR] and states[-1][1] == "open"


def test_fast_failing_queue_probes_every_round(cfg):
    """DPX (window 1h, reopen_after 1h) fails every job with stack_error 2
    minutes after it became runnable, so the jobs that close it are out of
    the window when its backoff ends. Every 10 minutes the checker runs and
    every 20 minutes a main build runs 6 steps on the queue while it is open,
    or its probes."""
    keys, runs, entry, rounds = [f"k{i}" for i in range(6)], [], None, []
    for tick in range(24 * 6):
        now = NOW + tick * timedelta(minutes=10)
        if tick % 2 == 0:
            if entry is None or entry.state == "open":
                runs += [(now, key) for key in keys]
            elif entry.state == "probing":
                probe = entry.probe
                runs += [(now, probe_key(DPX, probe.round, k)) for k in probe.keys]
        lag = timedelta(minutes=2)
        jobs = [
            job(DPX, "infra", key=key, finished=at + lag, ran=lag)
            for at, key in runs
            if at + lag <= now
        ]
        new = check(cfg, None if entry is None else {DPX: entry}, jobs, now)[DPX]
        if new.state == "probing" and (entry is None or entry.state != "probing"):
            rounds.append(new.probe.keys)
        assert entry is None or entry.state == "open" or new.state != "open"
        entry = new
    assert len(rounds) == 4 and all(rounds)


# Anti-flap


def reopened_entry(ago, backoff=7200, round_=2):
    return QueueState(
        state="open",
        since=NOW - ago,
        reason="probe round 2: 0 infra failures in 3 started probes; reopened",
        backoff_s=backoff,
        probe_round=round_,
    )


def test_failures_before_reopening_do_not_reclose(cfg):
    old = [job(MI300, "infra", finished=NOW - timedelta(minutes=40)) for _ in range(25)]
    new = [job(MI300, finished=NOW - timedelta(minutes=5)) for _ in range(3)]
    prev = reopened_entry(timedelta(minutes=30))
    entry = check(cfg, {MI300: prev}, jobs=old + new)[MI300]
    assert entry == prev.model_copy(update={"infra_rate": 0.0, "jobs": 3})


def test_jobs_runnable_before_reopening_do_not_reclose(cfg):
    # Stuck since before the reopen, they fail with stack_error only now.
    stuck = job(MI300, "infra", finished=NOW - timedelta(minutes=5), ran=3 * HOUR)
    new = [job(MI300, finished=NOW - timedelta(minutes=5)) for _ in range(3)]
    prev = reopened_entry(timedelta(minutes=30))
    entry = check(cfg, {MI300: prev}, jobs=[stuck] * 25 + new)[MI300]
    assert entry == prev.model_copy(update={"infra_rate": 0.0, "jobs": 3})


def test_quick_reclose_doubles_backoff(cfg):
    jobs = [job(MI300, "infra", finished=NOW - timedelta(minutes=5))] * 20
    prev = reopened_entry(timedelta(minutes=30))
    entry = check(cfg, {MI300: prev}, jobs=jobs)[MI300]
    assert entry.state == "closed"
    assert (entry.backoff_s, entry.probe_round) == (4 * 3600, 2)
    assert entry.until == NOW + 4 * HOUR
    assert entry.reason.endswith("; re-closed within 2h of reopening")
    assert check(cfg, {MI300: entry}, now=entry.until)[MI300].probe.round == 3


def test_late_reclose_starts_over(cfg):
    jobs = [job(MI300, "infra", finished=NOW - timedelta(minutes=5))] * 20
    entry = check(cfg, {MI300: reopened_entry(3 * HOUR)}, jobs=jobs)[MI300]
    # Only the backoff starts over; rounds keep counting.
    assert entry.state == "closed"
    assert (entry.backoff_s, entry.probe_round) == (3600, 2)
    assert check(cfg, {MI300: entry}, now=entry.until)[MI300].probe.round == 3


# Pins


def pinned_cfg(tmp_path, state, until="2026-10-17T00:00:00Z", reason="by hand"):
    shutil.copytree(CONFIG_DIR, tmp_path / "qf")
    (tmp_path / "qf" / "amd" / "overrides.yaml").write_text(
        f'- pin: {{device: mi300_1, state: {state}, until: "{until}", '
        f'reason: "{reason}"}}\n'
    )
    return load_health_policies([str(tmp_path / "qf")])


def test_pin_sets_the_snapshot_state(cfg):
    prev = QueueState(state="open", since=NOW - HOUR, probe_round=3, backoff_s=7200)
    entry = check(cfg, {"amd_mi250_1": prev})["amd_mi250_1"]
    assert entry == QueueState(
        state="closed",
        since=NOW,
        until=datetime(2026, 10, 20, tzinfo=timezone.utc),
        reason="pinned: node upgrade",
        pinned=True,
        probe_round=3,
        backoff_s=7200,
    )
    # Unchanged while it lasts.
    later = NOW + HOUR
    assert check(cfg, {"amd_mi250_1": entry}, now=later)["amd_mi250_1"] == entry


def test_open_pin_overrides_a_probing_queue(cfg, tmp_path):
    pinned = pinned_cfg(tmp_path, "open")
    jobs = [probe("infra", k) for k in "abc"]
    entry = check(pinned, {MI300: probing_entry()}, jobs=jobs)[MI300]
    assert (entry.state, entry.pinned, entry.probe) == ("open", True, None)
    assert entry.reason == "pinned: by hand"
    expired = datetime(2026, 10, 17, 1, tzinfo=timezone.utc)
    assert check(pinned, {MI300: entry}, now=expired)[MI300] == QueueState(
        state="open", probe_round=2, backoff_s=7200
    )


def test_rounds_survive_a_pin(tmp_path):
    pinned = pinned_cfg(tmp_path, "closed")
    entry = check(pinned, {MI300: closed_entry(backoff=7200, round_=4)})[MI300]
    assert entry.pinned and (entry.probe_round, entry.backoff_s) == (4, 7200)
    expired = datetime(2026, 10, 17, 1, tzinfo=timezone.utc)
    entry = check(pinned, {MI300: entry}, now=expired)[MI300]
    assert entry.state == "open" and not entry.pinned
    # A pin is no reopen: closing soon after it starts the backoff over.
    closing = expired + timedelta(minutes=30)
    jobs = [job(MI300, "infra", finished=expired + timedelta(minutes=10))] * 20
    entry = check(pinned, {MI300: entry}, jobs=jobs, now=closing)[MI300]
    assert (entry.state, entry.probe_round, entry.backoff_s) == ("closed", 4, 3600)
    assert check(pinned, {MI300: entry}, now=entry.until)[MI300].probe.round == 5


def test_failures_before_a_pin_expires_still_count(tmp_path):
    until = datetime(2026, 10, 17, tzinfo=timezone.utc)
    pinned = pinned_cfg(tmp_path, "open", until=iso(until))
    entry = check(pinned, jobs=[], now=until - HOUR)[MI300]
    assert entry.pinned and entry.state == "open"
    jobs = [
        job(MI300, "infra", finished=until - timedelta(minutes=i)) for i in range(20)
    ]
    entry = check(pinned, {MI300: entry}, jobs=jobs, now=until)[MI300]
    assert (entry.state, entry.until) == ("closed", until + HOUR)


# Groups


MI300_8, MI300_2 = "amd_mi300_8", "amd_mi300_2"
MI300_GROUP = (MI300, MI300_2, "amd_mi300_4", MI300_8)


@pytest.fixture
def group_cfg(fake_global_config, tmp_path):
    """mi300 as one group, with MI300's policy; mi300_1 runs its probes."""
    shutil.copytree(CONFIG_DIR, tmp_path / "qf")
    (tmp_path / "qf" / "amd" / "groups.yaml").write_text(
        "- group: {name: mi300, devices: [mi300_1, mi300_2, mi300_4, mi300_8]}\n"
    )
    return load_health_policies([str(tmp_path / "qf")])


def group_check(cfg, prev=None, jobs=(), now=NOW):
    """The group's entries; every member mirrors MI300's, without its probe."""
    queues = check(cfg, None if prev is None else {MI300: prev}, jobs, now)
    lead = queues[MI300]
    for queue in MI300_GROUP:
        assert queues[queue] == lead.model_copy(
            update={"probe": lead.probe if queue == MI300 else None}
        )
    assert lead.reason.startswith("group mi300: ")
    return lead.model_copy(update={"reason": lead.reason[len("group mi300: ") :]})


def test_one_failing_member_closes_the_group(group_cfg, cfg):
    jobs = [job(MI300_8, "infra", key=f"k{i % 4}") for i in range(20)]
    jobs += [job(MI300, key="lead")]
    assert check(cfg, jobs=jobs)[MI300].state == "open"
    entry = group_check(group_cfg, jobs=jobs)
    assert (entry.state, entry.jobs, entry.infra_rate) == ("closed", 21, 0.9524)
    assert entry.reason == "infra_failure_rate: 95% infra failures in 21 jobs over 2h"
    # Probe keys come from the probing device's jobs only.
    assert entry.probe == Probe(round=1, device="mi300_1", keys=("lead",))
    # Ungrouped queues keep their own state.
    queues = check(group_cfg, jobs=jobs)
    assert queues["amd_mi355_8"].state == "open"
    assert not queues["amd_mi355_8"].reason


def test_group_pools_its_members_jobs(group_cfg):
    # No member reaches min_jobs (20) alone.
    jobs = [job(q, "infra") for q in (MI300, MI300_2, MI300_8) for _ in range(7)]
    assert group_check(group_cfg, jobs=jobs).state == "closed"
    queues = check(group_cfg, jobs=jobs[:19])
    assert {(queues[q].state, queues[q].reason) for q in MI300_GROUP} == {("open", "")}


def test_group_round_probes_the_first_device(group_cfg):
    jobs = [job(MI300_8, "infra", key="m8"), job(MI300, "infra", key="a")]
    prev = closed_entry(round_=1).model_copy(
        update={"probe": Probe(round=1, device="mi300_1", keys=("old",))}
    )
    entry = group_check(group_cfg, prev, jobs)
    assert entry.state == "probing"
    assert entry.probe == Probe(round=2, device="mi300_1", keys=("a",))
    # Without jobs of its own in the window, the keys recorded at the close.
    entry = group_check(group_cfg, prev, jobs[:1])
    assert entry.probe.keys == ("old",)


def test_group_verdict_on_the_first_devices_probes(group_cfg):
    noise = [job(MI300_8, "infra") for _ in range(20)]
    jobs = [probe("passed", k) for k in "abc"] + noise
    entry = group_check(group_cfg, probing_entry(), jobs)
    assert entry.state == "open"
    assert entry.reason == "probe round 2: 0 infra failures in 3 probes; reopened"
    jobs = [probe("infra", k) for k in "abc"]
    entry = group_check(group_cfg, probing_entry(), jobs)
    assert (entry.state, entry.backoff_s) == ("closed", 4 * 3600)


def test_group_reason_is_prefixed_once(group_cfg):
    jobs = [job(MI300_8, "infra")] * 20
    queues = check(group_cfg, jobs=jobs)
    for _ in range(3):
        queues = check(group_cfg, dict(queues), jobs, now=NOW + timedelta(minutes=1))
    assert queues[MI300_8].reason == (
        "group mi300: infra_failure_rate: 100% infra failures in 20 jobs over 2h"
    )


def test_group_anti_flap_and_backoff(group_cfg):
    def failing(at):
        # Mostly on mi300_8, with keys to probe on mi300_1.
        jobs = [job(MI300_8, "infra", key="big", finished=at)] * 18
        return jobs + [job(MI300, "infra", key=k, finished=at) for k in ("a", "b")]

    prev = reopened_entry(timedelta(minutes=30))
    entry = group_check(group_cfg, prev, failing(NOW - timedelta(minutes=5)))
    assert (entry.state, entry.backoff_s) == ("closed", 4 * 3600)
    assert entry.reason.endswith("; re-closed within 2h of reopening")
    # Old failures do not re-close a reopened group.
    old = failing(NOW - timedelta(minutes=40))
    assert group_check(group_cfg, prev, old).state == "open"

    entry = group_check(group_cfg, jobs=failing(NOW))
    for backoff in (2, 4, 8, 8):
        start = entry.until
        entry = group_check(group_cfg, entry, failing(start), now=start)
        assert entry.state == "probing" and entry.until == start + HOUR
        keys = [probe_key(MI300, entry.probe.round, k) for k in entry.probe.keys]
        probes = [job(MI300, "infra", key=k, finished=entry.until) for k in keys]
        entry = group_check(group_cfg, entry, probes, now=entry.until)
        assert entry.state == "closed" and entry.backoff_s == backoff * 3600
    entry = group_check(group_cfg, entry, now=entry.until)
    entry = group_check(group_cfg, entry, now=entry.until)
    assert entry.state == "open" and entry.probe_round == 5


def test_pin_on_a_member_pins_the_group(fake_global_config, tmp_path):
    shutil.copytree(CONFIG_DIR, tmp_path / "qf")
    (tmp_path / "qf" / "amd" / "groups.yaml").write_text(
        "- group: {name: mi300, devices: [mi300_1, mi300_8]}\n"
        "- pin: {device: mi300_8, state: closed, until: '2026-10-17T00:00:00Z',"
        " reason: rack swap}\n"
    )
    cfg = load_health_policies([str(tmp_path / "qf")])
    queues = check(cfg)
    for queue in (MI300, MI300_8):
        assert (queues[queue].state, queues[queue].pinned) == ("closed", True)
        assert queues[queue].reason == "group mi300: pinned: rack swap"
    assert queues[MI300_2].state == "open"


def test_unwatched_devices_get_no_entries(fake_global_config, tmp_path):
    shutil.copytree(CONFIG_DIR, tmp_path / "qf")
    os.remove(tmp_path / "qf" / "common" / "rules.yaml")
    shutil.rmtree(tmp_path / "qf" / "nvidia")
    (tmp_path / "qf" / "amd" / "defaults.yaml").write_text(
        "- defaults:\n"
        "    close_when: {rule: infra_failure_rate, above: 0.7, window: 2h,"
        " min_jobs: 20}\n"
        "    reopen_after: 1h\n"
        "    probes: 3\n"
    )
    cfg = load_health_policies([str(tmp_path / "qf")])
    jobs = [job(q, "infra") for q in ("h200_18gb", MI300) for _ in range(20)]
    prev = {"h200_18gb": closed_entry()}
    queues = check(cfg, prev, jobs)
    assert set(queues) == {device_queue(d) for d in AMD_DEVICE_CONFIGS}
    assert queues[MI300].state == "closed"
    # Nothing to probe either.
    step = Step(label="K", key="kernels", device="h200_18gb", commands=["x"])
    assert probe_steps([step], snapshot(queues)) == []


def test_no_watched_queue_publishes_an_empty_snapshot(fake_global_config, tmp_path):
    shutil.copytree(CONFIG_DIR, tmp_path / "qf")
    os.remove(tmp_path / "qf" / "common" / "rules.yaml")
    os.remove(tmp_path / "qf" / "amd" / "overrides.yaml")
    os.remove(tmp_path / "qf" / "amd" / "mi355.yaml")
    cfg = load_health_policies([str(tmp_path / "qf")])
    assert Checker(cfg, NOW).history_since(None) == NOW
    assert check(cfg, jobs=[job(MI300, "infra")] * 20) == {}


def test_group_round_trip_through_the_generator(group_cfg, fake_global_config):
    jobs = [job(MI300_8, "infra", key="big")] * 20 + [job(MI300, key="basic")]
    snap = Checker(group_cfg, NOW).run(None, jobs)
    snap = Checker(group_cfg, snap.queues[MI300].until).run(snap, [])
    steps = [
        Step(label="Basic", key="basic", device="mi300_1", commands=["pytest"]),
        Step(label="Big", key="big", device="mi300_8", commands=["pytest"]),
    ]
    # One probe, on the first device's queue.
    (group,) = probe_steps(steps, snap)
    assert [step.key for step in group.steps] == ["probe-amd_mi300_1-r1-basic"]
    assert group.steps[0].agents["queue"] == MI300
    # Every member queue is closed to new steps.
    chains = load_chains([CONFIG_DIR])
    chains = dataclasses.replace(chains, chains={"mi300_8": ("mi355_8",)})
    assert map_device(steps[1], chains, snap)[0] == "mi355_8"


# Round trip through the generator


def test_probe_round_trip_through_the_generator(cfg, fake_global_config):
    jobs = [job(MI300, "infra", key="amd-kernels"), job(MI300, "infra", key="basic")]
    snap = Checker(cfg, NOW).run(snapshot({MI300: closed_entry()}), jobs)
    steps = [
        Step(label="Basic", key="basic", device="mi300_1", commands=["pytest"]),
        Step(
            label="Kernels",
            key="kernels",
            device="h200_18gb",
            commands=["pytest"],
            mirror={"amd": {"device": "mi300_1"}},
        ),
    ]
    (group,) = probe_steps(steps, snap)
    keys = [step.key for step in group.steps]
    assert keys == ["probe-amd_mi300_1-r1-amd-kernels", "probe-amd_mi300_1-r1-basic"]

    deadline = snap.queues[MI300].until
    done = [job(MI300, "passed", key=key, finished=deadline) for key in keys]
    entry = Checker(cfg, deadline).run(snap, done).queues[MI300]
    assert entry.state == "open"


# Entry point


CONFIGS = [
    ".buildkite/ci_config.yaml",
    ".buildkite/ci_config_intel.yaml",
    ".buildkite/ci_config_rocm.yaml",
]


@pytest.fixture
def main_env(monkeypatch, fake_global_config):
    monkeypatch.chdir(REPO)
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    env = {"BUILDKITE_ORGANIZATION_SLUG": ORG, "BUILDKITE_BUILD_NUMBER": "42"}
    return calls, run, env


def run_main(main_env, fake, secrets=None):
    calls, run, env = main_env
    secrets = [] if secrets is None else secrets
    code = queue_health.main(
        CONFIGS,
        env=env,
        fetch=fake,
        get_secret=lambda name: secrets.append(name) or TOKEN,
        run=run,
        now=NOW,
    )
    assert code == 0
    # Published once.
    (cmd,) = calls
    assert cmd[:4] == ["buildkite-agent", "meta-data", "set", "queue-health"]
    return cmd[4]


def published_build(queues, generated_at=NOW - timedelta(minutes=10)):
    return {
        "number": 41,
        "state": "passed",
        "meta_data": {"queue-health": snapshot(queues, generated_at).model_dump_json()},
    }


def test_main_publishes_probe_rounds(main_env, capsys):
    # The previous snapshot is stale: it still carries the state over.
    prev = published_build({MI300: closed_entry()}, NOW - 9 * HOUR)
    fake = FakeBuildkite(
        {"queue-health": [prev], "ci": history(job(MI300, "infra", key="a")).get("ci")}
    )
    secrets = []
    raw = run_main(main_env, fake, secrets)
    assert secrets == ["QUEUE_FALLBACK_BUILDKITE_API_TOKEN"]
    assert fake.urls[0] == (
        f"https://api.buildkite.com/v2/organizations/{ORG}/pipelines/"
        "queue-health/builds?branch=main&state=passed&per_page=1"
    )
    out = capsys.readouterr().out
    assert "Previous snapshot: generated at 2026-10-16T03:00:00Z" in out
    assert any(line.split()[:2] == [MI300, "probing"] for line in out.splitlines())
    assert TOKEN not in out

    # The generator's reader accepts what the checker publishes, as published.
    fresh = FakeBuildkite({"queue-health": [{"state": "passed", "meta_data": {}}]})
    fresh.builds["queue-health"][0]["meta_data"]["queue-health"] = raw
    health = load_health_policies([".buildkite/queue_fallbacks"]).health
    snap = build_snapshot(health, ORG, fresh, lambda name: TOKEN, NOW)
    assert snap.model_dump_json() == raw
    assert snap.queues[MI300].probe == Probe(round=1, device="mi300_1", keys=("a",))
    # The dirs of every config, ci_config_rocm.yaml's second one included.
    assert set(snap.queues) == {device_queue(d) for d in SUPPORTED_DEVICES}


def test_main_without_previous_snapshot_starts_open(main_env, capsys):
    raw = run_main(main_env, FakeBuildkite({"ci": []}))
    assert {e["state"] for e in json.loads(raw)["queues"].values()} == {"open"}
    assert "No previous snapshot" in capsys.readouterr().out


def test_main_without_history_publishes_nothing(main_env):
    calls, run, env = main_env
    fake = FakeBuildkite(
        {"queue-health": [published_build({MI300: closed_entry()})]},
        history_error=OSError("down"),
    )
    with pytest.raises(OSError, match="down"):
        run_main(main_env, fake)
    assert calls == []


@pytest.mark.parametrize(
    "builds, env, error",
    [
        (FakeBuildkite(error=OSError("api down")), None, OSError),
        (
            FakeBuildkite({"queue-health": [{"state": "passed", "meta_data": {}}]}),
            None,
            KeyError,
        ),
        (
            FakeBuildkite(
                {
                    "queue-health": [
                        {"state": "passed", "meta_data": {"queue-health": "{}"}}
                    ]
                }
            ),
            None,
            ValueError,
        ),
        (FakeBuildkite(), {"BUILDKITE_ORGANIZATION_SLUG": "Bad/org"}, ValueError),
    ],
)
def test_main_publishes_nothing_on_an_error(main_env, builds, env, error):
    calls, run, default_env = main_env
    with pytest.raises(error):
        queue_health.main(
            CONFIGS,
            env=env or default_env,
            fetch=builds,
            get_secret=lambda name: TOKEN,
            run=run,
            now=NOW,
        )
    assert calls == []


def test_main_publishes_nothing_on_a_malformed_history(main_env):
    calls, run, env = main_env
    with pytest.raises(KeyError):
        run_main(main_env, FakeBuildkite({"ci": [{"state": "passed", "jobs": []}]}))
    assert calls == []


def test_main_publishes_nothing_if_the_checker_fails(main_env, monkeypatch):
    calls, run, env = main_env

    def boom(self, *args):
        raise KeyError("boom")

    monkeypatch.setattr(Checker, "step", boom)
    with pytest.raises(KeyError):
        run_main(main_env, FakeBuildkite({"ci": []}))
    assert calls == []


def test_fallback_dirs_union(main_env, tmp_path):
    assert fallback_dirs(CONFIGS) == [
        ".buildkite/queue_fallbacks",
        ".buildkite/queue_fallbacks_rocm",
    ]
    bad = tmp_path / "ci_config.yaml"
    for value in (".buildkite/queue_fallbacks", "{}"):
        bad.write_text(f"fallback_dirs: {value}\n")
        with pytest.raises(ValueError, match=f"^{bad}: fallback_dirs must be a list"):
            fallback_dirs([str(bad)])


def test_main_without_fallback_dirs_publishes_nothing(main_env, capsys):
    calls, run, env = main_env
    code = queue_health.main(
        [".buildkite/ci_config_intel.yaml"],
        env=env,
        get_secret=lambda name: pytest.fail("no secret needed"),
        run=run,
    )
    assert code == 1 and calls == []
    assert (
        "ERROR: none of .buildkite/ci_config_intel.yaml sets fallback_dirs"
        in capsys.readouterr().out
    )


@pytest.mark.parametrize(
    "path, error",
    [
        ("common/health.yaml", "second health entry"),
        ("amd/mi300.yaml", "second entry for mi300_1"),
    ],
)
def test_main_rejects_configs_that_do_not_load_as_one(
    main_env, monkeypatch, tmp_path, capsys, path, error
):
    # ci_config_rocm.yaml's second dir repeats an entry of the first.
    calls, run, env = main_env
    shutil.copytree(REPO, tmp_path, dirs_exist_ok=True)
    target = tmp_path / ".buildkite/queue_fallbacks_rocm" / path
    target.parent.mkdir(exist_ok=True)
    shutil.copy(tmp_path / ".buildkite/queue_fallbacks" / path, target)
    monkeypatch.chdir(tmp_path)
    code = queue_health.main(
        CONFIGS,
        env=env,
        get_secret=lambda name: pytest.fail("no secret needed"),
        run=run,
    )
    out = capsys.readouterr().out
    assert code == 1 and calls == []
    assert "ERROR: the fallback_dirs of .buildkite/ci_config.yaml" in out
    assert f".buildkite/queue_fallbacks_rocm/{path} entry 0: {error}" in out
    assert "nothing published" in out


def test_main_with_a_cuda_chain_and_pin_needs_no_global_config(monkeypatch, tmp_path):
    monkeypatch.setattr(global_config, "config", None)
    repo = tmp_path / "repo"
    shutil.copytree(REPO, repo)
    (repo / ".buildkite/queue_fallbacks/nvidia/pins.yaml").write_text(
        '- pin: {device: h200_35gb, state: closed, until: "2026-10-20T00:00:00Z", '
        'reason: "maintenance"}\n'
    )
    monkeypatch.chdir(repo)
    calls = []
    raw = run_main(
        (
            calls,
            lambda cmd, **kwargs: calls.append(cmd),
            {"BUILDKITE_ORGANIZATION_SLUG": ORG},
        ),
        FakeBuildkite({"ci": [], "queue-health": []}),
    )
    queues = Snapshot.model_validate_json(raw).queues
    assert queues["h200_18gb"].state == "open"
    assert queues["h200_35gb"].state == "closed"
    assert queues["h200_35gb"].reason == "pinned: maintenance"
