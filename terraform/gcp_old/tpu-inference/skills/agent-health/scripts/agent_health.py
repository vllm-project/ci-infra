#!/usr/bin/env python3
"""Detect degraded Buildkite agents and recreate their VMs.

Degraded agents in this cluster keep reporting `connection_state: connected`
and `paused: false`, so neither the queues page nor `bk agent list` shows the
problem. Two distinct failure modes hide behind that healthy-looking state:

disk-full
    The agent keeps *accepting* jobs and fails them on ENOSPC. It never looks
    idle, so an idleness check misses it entirely. This is the damaging mode:
    it eats real jobs and fails PRs. The disk can fill anywhere - a docker
    pull, a model download, a build cache - so detection is a high failure
    rate confirmed by an ENOSPC marker in the job log, never a guess from
    failure timing. Docker pulls die in seconds; a model download can churn
    for minutes before it runs out of room.

silent-wedge
    The agent stops accepting work altogether while its queue peers cycle
    normally. Capacity quietly drops with no failures to show for it.

Detection is read-only against the Buildkite REST API. Remediation shells out
to the existing rolling_restart.py so the destroy/create stays a reviewed
`terraform apply -replace` rather than something reimplemented in here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import statistics
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ORG = "tpu-commons"
API_ROOT = "https://api.buildkite.com/v2"


def _find_tf_root(start: Path) -> Path:
    """Walk up to the directory holding scripts/rolling_restart.py.

    Searching for the landmark rather than counting parents keeps this working
    when the skill is reached through the .claude/skills symlink, or moved.
    """
    for parent in [start, *start.parents]:
        if (parent / "scripts" / "rolling_restart.py").is_file():
            return parent
    return start


# The per-fleet Terraform configs live alongside rolling_restart.py. Resolved
# from this file's location so the skill works from any cwd.
DEFAULT_TF_ROOT = _find_tf_root(Path(__file__).resolve().parent)
# Fleets live in separate Terraform states and an agent name alone does not say
# which one, so search them all.
DEFAULT_TF_DIRS = (
    "cloud-ullm-inference-ci-cd",
    "cloud-tpu-inference-test",
    "cloud-tpu-inference-test-v7x",
)

# These two only decide whose logs are worth fetching. They are deliberately
# loose: the ENOSPC marker below is what actually convicts an agent, so a
# generous pre-filter costs a few HTTP calls, while a tight one silently misses
# real disk exhaustion.
MIN_JOBS_FOR_RATE = 3
MIN_FAILURES_FOR_LOG_CHECK = 2
FAILURE_RATE_THRESHOLD = 0.25
# Logs to read per suspect agent, newest first. Bounds the scan's cost.
MAX_LOGS_PER_AGENT = 3
# An agent must have been connected at least this long before "no jobs" means
# anything; freshly booted agents legitimately have not won work yet.
WEDGE_MIN_HOURS = 6.0
# Refuse to act when more than this fraction of a queue looks degraded. That
# pattern is a fleet-wide or pipeline-wide fault, and recreating VMs one by one
# would churn the fleet without fixing anything.
MAX_QUEUE_DEGRADED_FRACTION = 0.25

# Whatever fills the disk - docker layers, model weights, pip wheels, build
# caches - the write ultimately fails with ENOSPC, so match on how the various
# runtimes render that rather than on any one workload.
DISK_FULL_MARKERS = (
    "no space left on device",  # kernel ENOSPC, surfaced by most tools
    "errno 28",  # python OSError, e.g. huggingface_hub model downloads
    "enospc",
    "disk quota exceeded",
    "not enough free disk space",  # hf_transfer / huggingface-cli
    "insufficient disk space",
    "write error: no space",
    "failed to register layer",  # docker, which sometimes elides the errno
    "no space left",  # last-resort catch-all for the above phrasings
)

TF_INSTANCE_TYPES = ("google_compute_instance", "google_tpu_v2_vm")
TERMINAL_JOB_STATES = ("passed", "failed", "broken", "timed_out")

REASON_DISK_FULL = "disk-full"
REASON_SILENT_WEDGE = "silent-wedge"
# Failing a lot, but the logs never mentioned ENOSPC. Reported, never acted on:
# flaky tests and broken pipelines look exactly like this, and recreating the VM
# would not fix either.
REASON_HIGH_FAILURE = "high-failure-rate"
# Pre-confirmation label. confirm_disk_full() promotes it to REASON_DISK_FULL or
# demotes it to REASON_HIGH_FAILURE.
REASON_DISK_FULL_SUSPECT = "disk-full-suspect"

ACTIONABLE_REASONS = (REASON_DISK_FULL, REASON_SILENT_WEDGE)


# --------------------------------------------------------------------------
# Buildkite API
# --------------------------------------------------------------------------


def buildkite_token() -> str:
    """Read the API token from the environment, else borrow the bk CLI's.

    Deliberately does not reach into Secret Manager: this runs as whoever is
    driving the loop, under their own Buildkite identity.
    """
    token = os.environ.get("BUILDKITE_API_TOKEN", "").strip()
    if token:
        return token
    if shutil.which("bk"):
        result = subprocess.run(
            ["bk", "auth", "token"], capture_output=True, text=True, check=False
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    raise SystemExit(
        "No Buildkite credentials. Set BUILDKITE_API_TOKEN or run `bk auth login`."
    )


def api_get(path: str, token: str, **params: Any) -> Any:
    url = f"{API_ROOT}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        raise SystemExit(f"Buildkite API {error.code} for {path}: {detail}") from error


def fetch_agents(token: str) -> list[dict[str, Any]]:
    agents: list[dict[str, Any]] = []
    for page in range(1, 11):
        batch = api_get(f"/organizations/{ORG}/agents", token, per_page=100, page=page)
        if not batch:
            break
        agents.extend(batch)
        if len(batch) < 100:
            break
    return agents


def fetch_jobs(
    token: str, window_hours: float, now: dt.datetime
) -> list[dict[str, Any]]:
    """Return jobs from recent builds, each tagged with the agent that ran it.

    Buildkite has no per-agent job history endpoint, so the only way to see what
    an agent has been doing is to walk recent builds and group by
    `job.agent.name`. Builds created before the window can still hold jobs that
    finished inside it, so reach back further than the window itself.
    """
    created_from = now - dt.timedelta(hours=window_hours * 2)
    jobs: list[dict[str, Any]] = []
    for page in range(1, 21):
        builds = api_get(
            f"/organizations/{ORG}/builds",
            token,
            per_page=100,
            page=page,
            created_from=created_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        if not builds:
            break
        for build in builds:
            for job in build.get("jobs") or []:
                agent = job.get("agent") or {}
                if agent.get("name"):
                    job["_agent_name"] = agent["name"]
                    jobs.append(job)
        if len(builds) < 100:
            break
    return jobs


def fetch_job_log(token: str, job: dict[str, Any]) -> str:
    """Fetch a job's log, or "" when it is unavailable."""
    url = job.get("log_url") or ""
    if not url.startswith(API_ROOT):
        return ""
    try:
        payload = api_get(url[len(API_ROOT) :], token)
    except SystemExit:
        return ""
    return payload.get("content", "") if isinstance(payload, dict) else ""


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value: Any) -> dt.datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def hours_since(value: Any, now: dt.datetime) -> float | None:
    moment = parse_time(value)
    return None if moment is None else (now - moment).total_seconds() / 3600.0


@dataclass
class AgentStats:
    name: str
    queue: str
    agent_id: str
    hostname: str
    connected_hours: float | None
    has_running_job: bool
    total_jobs: int = 0
    failed_jobs: int = 0
    median_fail_seconds: float | None = None
    recent_failures: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None
    evidence: str = ""
    log_excerpt: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def failure_rate(self) -> float:
        return self.failed_jobs / self.total_jobs if self.total_jobs else 0.0


def agent_queue(agent: dict[str, Any]) -> str:
    for entry in agent.get("meta_data") or []:
        if isinstance(entry, str) and entry.startswith("queue="):
            return entry[len("queue=") :]
    return agent.get("queue") or "(none)"


def build_stats(
    agents: list[dict[str, Any]],
    jobs: list[dict[str, Any]],
    window_hours: float,
    now: dt.datetime,
) -> list[AgentStats]:
    by_agent: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        finished = hours_since(job.get("finished_at"), now)
        if finished is None or finished > window_hours:
            continue
        if job.get("state") not in TERMINAL_JOB_STATES:
            continue
        by_agent.setdefault(job["_agent_name"], []).append(job)

    stats: list[AgentStats] = []
    for agent in agents:
        name = agent.get("name") or ""
        entry = AgentStats(
            name=name,
            queue=agent_queue(agent),
            agent_id=agent.get("id") or "",
            hostname=agent.get("hostname") or "",
            connected_hours=hours_since(agent.get("connected_at"), now),
            has_running_job=bool(agent.get("job")),
        )
        agent_jobs = sorted(
            by_agent.get(name, []), key=lambda job: job.get("finished_at") or ""
        )
        entry.total_jobs = len(agent_jobs)
        failures = [job for job in agent_jobs if job.get("state") != "passed"]
        entry.failed_jobs = len(failures)
        durations = []
        for job in failures:
            started = parse_time(job.get("started_at"))
            finished_at = parse_time(job.get("finished_at"))
            if started and finished_at:
                durations.append((finished_at - started).total_seconds())
        if durations:
            entry.median_fail_seconds = statistics.median(durations)
        entry.recent_failures = list(reversed(failures))[:MAX_LOGS_PER_AGENT]
        stats.append(entry)
    return stats


def classify(stats: list[AgentStats]) -> list[AgentStats]:
    """Tag each agent with a failure mode. Returns only the flagged ones.

    Disk-full comes back as REASON_DISK_FULL_SUSPECT; only confirm_disk_full()
    can promote it, because a high failure rate on its own is equally well
    explained by flaky tests or a broken pipeline.
    """
    # A quiet queue tells us nothing about its members, so compare every agent
    # against its own queue rather than a global baseline. Without this an
    # overnight lull flags an entire fleet.
    active_queues = {entry.queue for entry in stats if entry.total_jobs > 0}

    flagged: list[AgentStats] = []
    for entry in stats:
        if (
            entry.total_jobs >= MIN_JOBS_FOR_RATE
            and entry.failed_jobs >= MIN_FAILURES_FOR_LOG_CHECK
            and entry.failure_rate >= FAILURE_RATE_THRESHOLD
        ):
            entry.reason = REASON_DISK_FULL_SUSPECT
            entry.evidence = (
                f"{entry.failed_jobs}/{entry.total_jobs} jobs failed "
                f"({entry.failure_rate:.0%})"
            )
            if entry.median_fail_seconds is not None:
                entry.evidence += f", median failure {entry.median_fail_seconds:.0f}s"
            flagged.append(entry)
            continue

        if (
            entry.total_jobs == 0
            and entry.queue in active_queues
            and entry.connected_hours is not None
            and entry.connected_hours >= WEDGE_MIN_HOURS
        ):
            entry.reason = REASON_SILENT_WEDGE
            entry.evidence = (
                f"0 jobs in window while queue {entry.queue} was active; "
                f"connected {entry.connected_hours:.1f}h"
            )
            flagged.append(entry)
    return flagged


def find_disk_full_marker(log: str) -> str:
    """Return a one-line excerpt around the first ENOSPC marker, else ""."""
    lowered = log.lower()
    for marker in DISK_FULL_MARKERS:
        index = lowered.find(marker)
        if index >= 0:
            return " ".join(log[max(0, index - 140) : index + 140].split())
    return ""


def confirm_disk_full(token: str, entry: AgentStats, fetch=fetch_job_log) -> None:
    """Resolve a disk-full suspect by reading its recent failure logs.

    Only a log marker convicts. Without one the agent is downgraded to
    REASON_HIGH_FAILURE, which is reported but never auto-remediated.
    """
    if entry.reason != REASON_DISK_FULL_SUSPECT:
        return
    for job in entry.recent_failures:
        excerpt = find_disk_full_marker(fetch(token, job))
        if excerpt:
            entry.reason = REASON_DISK_FULL
            entry.extra["confirmed"] = True
            entry.log_excerpt = excerpt
            return
    entry.reason = REASON_HIGH_FAILURE
    entry.extra["confirmed"] = False
    entry.evidence += "; no ENOSPC marker in recent logs"


def queue_guard(degraded: list[AgentStats], stats: list[AgentStats]) -> list[str]:
    """Flag queues where too much is broken for a per-VM recreate to be right."""
    sizes: dict[str, int] = {}
    for entry in stats:
        sizes[entry.queue] = sizes.get(entry.queue, 0) + 1
    bad: dict[str, int] = {}
    for entry in degraded:
        bad[entry.queue] = bad.get(entry.queue, 0) + 1
    blocked = []
    for queue, count in sorted(bad.items()):
        total = sizes.get(queue, 0)
        # A lone bad agent is always worth recreating - that is the whole point
        # of the tool - even on a queue of two, where any single agent trips the
        # fraction. Only a pattern of several is suspicious.
        if count > 1 and total and count / total > MAX_QUEUE_DEGRADED_FRACTION:
            blocked.append(
                f"{queue}: {count}/{total} agents degraded "
                f"(over {MAX_QUEUE_DEGRADED_FRACTION:.0%}) - looks like a "
                "fleet-wide fault, not per-VM disk exhaustion"
            )
    return blocked


# --------------------------------------------------------------------------
# Terraform mapping
# --------------------------------------------------------------------------


def terraform_state(tf_root: Path, tf_dir: str) -> dict[str, Any]:
    if not shutil.which("terraform"):
        raise SystemExit("terraform is not on PATH; cannot resolve addresses")
    result = subprocess.run(
        ["terraform", f"-chdir={tf_dir}", "state", "pull"],
        cwd=tf_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"terraform state pull failed in {tf_dir}:\n{result.stderr.strip()}"
        )
    return json.loads(result.stdout)


def find_in_state(state: dict[str, Any], names: list[str]) -> tuple[str, int] | None:
    """Return (module, index) for the first instance resource matching a name.

    The fleets follow a name SSOT: the Buildkite agent, the VM, and its boot
    disk all share one name. Reading state instead of hardcoding a regex per
    fleet means new fleets work without editing this file.
    """
    wanted = {name for name in names if name}
    for resource in state.get("resources", []):
        if resource.get("mode") != "managed":
            continue
        if resource.get("type") not in TF_INSTANCE_TYPES:
            continue
        for instance in resource.get("instances", []):
            attributes = instance.get("attributes") or {}
            if attributes.get("name") not in wanted:
                continue
            index = instance.get("index_key")
            if isinstance(index, int):
                return resource.get("module", ""), index
    return None


def resolve_agent(
    tf_root: Path, tf_dirs: list[str], names: list[str]
) -> tuple[str, str, int]:
    """Search each fleet's state for the VM. Returns (tf_dir, module, index)."""
    for tf_dir in tf_dirs:
        found = find_in_state(terraform_state(tf_root, tf_dir), names)
        if found is None:
            continue
        module, index = found
        if not module:
            raise SystemExit(
                f"{names[0]} is a root-level resource in {tf_dir}; "
                "rolling_restart.py only targets modules."
            )
        return tf_dir, module, index
    raise SystemExit(
        f"No Terraform instance named any of {sorted(n for n in names if n)} "
        f"in {', '.join(tf_dirs)}."
    )


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def describe(entry: AgentStats) -> dict[str, Any]:
    return {
        "agent": entry.name,
        "queue": entry.queue,
        "agent_id": entry.agent_id,
        "reason": entry.reason,
        "evidence": entry.evidence,
        "log_excerpt": entry.log_excerpt,
        "has_running_job": entry.has_running_job,
        "total_jobs": entry.total_jobs,
        "failed_jobs": entry.failed_jobs,
        "median_fail_seconds": entry.median_fail_seconds,
    }


def report(
    flagged: list[AgentStats], stats: list[AgentStats], now: dt.datetime
) -> dict[str, Any]:
    degraded = [e for e in flagged if e.reason in ACTIONABLE_REASONS]
    other = [e for e in flagged if e.reason not in ACTIONABLE_REASONS]
    return {
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agents_scanned": len(stats),
        "degraded_count": len(degraded),
        "blocked_queues": queue_guard(degraded, stats),
        "degraded": [describe(entry) for entry in degraded],
        # Reported for a human to eyeball, never auto-remediated.
        "needs_review": [describe(entry) for entry in other],
    }


def print_human(payload: dict[str, Any]) -> None:
    print(
        f"Scanned {payload['agents_scanned']} agents at {payload['checked_at']}; "
        f"{payload['degraded_count']} actionable, "
        f"{len(payload['needs_review'])} needing review."
    )
    for warning in payload["blocked_queues"]:
        print(f"  !! BLOCKED {warning}")
    for heading, items in (
        ("ACTIONABLE", payload["degraded"]),
        ("NEEDS REVIEW (not auto-remediated)", payload["needs_review"]),
    ):
        if not items:
            continue
        print(f"\n{heading}")
        for item in items:
            print(f"\n  {item['agent']}  [{item['reason']}]")
            print(f"    queue:    {item['queue']}")
            print(f"    evidence: {item['evidence']}")
            if item["log_excerpt"]:
                print(f"    log:      ...{item['log_excerpt']}...")
            if item["has_running_job"]:
                print("    note:     currently holds a job")


def analyze(
    token: str, window_hours: float, now: dt.datetime
) -> tuple[list[AgentStats], list[AgentStats], list[dict[str, Any]]]:
    """Scan the cluster. Returns (flagged, all stats, raw agent records)."""
    agents = fetch_agents(token)
    jobs = fetch_jobs(token, window_hours, now)
    stats = build_stats(agents, jobs, window_hours, now)
    flagged = classify(stats)
    for entry in flagged:
        confirm_disk_full(token, entry)
    return flagged, stats, agents


def command_scan(args: argparse.Namespace) -> int:
    now = utcnow()
    flagged, stats, _ = analyze(buildkite_token(), args.window_hours, now)
    payload = report(flagged, stats, now)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print_human(payload)
    return 0


def replace_disks_for(reason: str, override: bool | None) -> bool:
    """Decide whether the attached data disk goes with the instance.

    The TPU fleets attach a separate google_compute_disk at /mnt/disks/persist
    for models and caches. It is not auto_delete, and the startup script only
    formats it `if ! blkid`, so an instance-only replace hands the new VM back
    the same full disk and the agent comes straight back degraded. Disk-full
    therefore has to take the disk with it.

    A wedged agent's disk is fine, so keep it: reformatting costs every cached
    model on the host and a slow first few jobs.
    """
    if override is not None:
        return override
    return reason == REASON_DISK_FULL


def command_resolve(args: argparse.Namespace) -> int:
    token = buildkite_token()
    agent = next((a for a in fetch_agents(token) if a.get("name") == args.agent), None)
    if agent is None:
        raise SystemExit(f"No connected agent named {args.agent}")
    # Legacy fleets predate the name SSOT, so the agent name and the VM name can
    # differ; the agent's hostname is the VM name in that case.
    names = [args.agent, agent.get("hostname", "")]
    tf_dir, module, index = resolve_agent(args.tf_root, args.dir, names)
    print(f"agent:    {args.agent}")
    print(f"state:    {tf_dir}")
    print(f"module:   {module}, index {index}")
    print(f"cwd:      {args.tf_root}")
    base = f"./scripts/rolling_restart.py -m {module} -i {index} --dir {tf_dir}"
    print(f"wedged:   {base}")
    print(f"disk-full: {base} -d")
    return 0


def command_recreate(args: argparse.Namespace) -> int:
    token = buildkite_token()
    now = utcnow()
    flagged, stats, agents = analyze(token, args.window_hours, now)
    agent = next((a for a in agents if a.get("name") == args.agent), None)
    if agent is None:
        raise SystemExit(f"No connected agent named {args.agent}")

    degraded = [e for e in flagged if e.reason in ACTIONABLE_REASONS]
    entry = next((e for e in degraded if e.name == args.agent), None)

    if entry is None and not args.force:
        current = next((e.reason for e in flagged if e.name == args.agent), "healthy")
        raise SystemExit(
            f"{args.agent} is not actionable (currently: {current}). "
            "Re-run `scan`, or pass --force to override."
        )
    reason = entry.reason if entry else "forced"

    blocked = queue_guard(degraded, stats)
    if blocked and not args.force:
        raise SystemExit("Refusing to act:\n  " + "\n  ".join(blocked))

    # A disk-full agent fails whatever it is holding anyway, so waiting for its
    # job to finish protects nothing and just prolongs the outage. A wedged
    # agent is different: if it somehow holds a real job, killing the VM throws
    # away work that would otherwise have completed.
    if reason != REASON_DISK_FULL and agent.get("job") and not args.force:
        raise SystemExit(
            f"{args.agent} is running a job and the reason is {reason}. "
            "Wait for it to finish, or pass --force."
        )

    names = [args.agent, agent.get("hostname", "")]
    tf_dir, module, index = resolve_agent(args.tf_root, args.dir, names)
    command = [args.rolling_restart, "-m", module, "-i", str(index), "--dir", tf_dir]
    if replace_disks_for(reason, args.replace_disks):
        command.append("--replace-disks")
    command.append("--non-interactive")
    command.append("--dry-run" if args.dry_run else "--auto-approve")

    print(f"agent {args.agent} [{reason}] -> {module} index {index} in {tf_dir}")
    print("running:", " ".join(command), f"(cwd={args.tf_root})")
    return subprocess.run(command, cwd=args.tf_root, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--window-hours",
        type=float,
        default=24.0,
        help="How far back to look at job history (default: 24)",
    )
    parser.add_argument(
        "--tf-root",
        type=Path,
        default=DEFAULT_TF_ROOT,
        help="Directory holding scripts/rolling_restart.py "
        f"(default: {DEFAULT_TF_ROOT})",
    )
    parser.add_argument(
        "--dir",
        action="append",
        metavar="TF_DIR",
        help="Terraform config dir to search; repeatable "
        f"(default: {', '.join(DEFAULT_TF_DIRS)})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="Report degraded agents (read-only)")
    scan.add_argument("--json", action="store_true", help="Emit JSON")
    scan.set_defaults(func=command_scan)

    resolve = subparsers.add_parser(
        "resolve", help="Map an agent onto its Terraform module and index"
    )
    resolve.add_argument("agent")
    resolve.set_defaults(func=command_resolve)

    recreate = subparsers.add_parser("recreate", help="Replace a degraded agent's VM")
    recreate.add_argument("agent")
    recreate.add_argument(
        "--rolling-restart",
        default="./scripts/rolling_restart.py",
        help="Path to rolling_restart.py, relative to --tf-root",
    )
    recreate.add_argument("--dry-run", action="store_true")
    recreate.add_argument(
        "--replace-disks",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Also replace the attached data disk "
        "(default: yes for disk-full, no otherwise)",
    )
    recreate.add_argument(
        "--force",
        action="store_true",
        help="Skip the degraded, blast-radius, and running-job checks",
    )
    recreate.set_defaults(func=command_recreate)

    args = parser.parse_args(argv)
    args.dir = args.dir or list(DEFAULT_TF_DIRS)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
