"""Queue health checker: decides which queues are open, closed or probing.

    python3 queue_health.py <ci_config.yaml> [<ci_config.yaml>...]

Runs in the scheduled queue-health build from a sparse checkout of vLLM main
that holds the pipeline configs and the fallback dirs they list. Each run
loads the health policies and pins under the union of the configs'
`fallback_dirs` (relative to the working directory, the repo root), the
previous snapshot (the generator's, stale or not) and the recent jobs of the
main builds of the scanned pipelines; moves every watched queue, an AMD GPU
queue with a complete policy, through open -> closed -> probing -> open (or
back to closed with a doubled backoff), a group's queues as one on their
pooled jobs; applies the pins; and, as its last action, publishes the new
snapshot as this build's `queue-health` meta-data. Any error before that, an unavailable
history included, fails the build and publishes nothing; the last snapshot
then goes stale and the generator treats every queue as open.

The checker runs no probes itself. A probing queue's entry names a round
`{round, device, keys}`; the generator of the next main builds of the scanned
pipelines adds a soft-failing copy of each of those steps pinned to `device`,
keyed `probe-<queue>-r<round>-<key>`. A round starts when the queue starts
probing (`since`) and ends at a fixed deadline, `until` = `since` +
`reopen_after`. Each key of the round gives at most one probe job as
evidence, as it was at the deadline: one that finished counts as it ended,
one runnable but never started as an infra failure (the queue could not run
it), and one still running or not yet runnable says nothing. The queue stays
closed, with a doubled backoff, only if the policy's rule fires on that
evidence; otherwise it reopens. A closed entry's `probe` is its next round,
with the keys of the jobs that closed it; that round takes fresh keys from
its window instead when the window has any. A group probes on its first
device only, with keys of that device's jobs; every other queue of the group
mirrors its entry, without the probe.
"""

import os
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import yaml

from fallback_rules import rules_for
from fallback_rules.common import HISTORY_STATES, infra_rate, is_infra_failure
from queue_fallbacks import (
    REST_URL,
    SNAPSHOT_META_KEY,
    SNAPSHOT_VERSION,
    HealthPolicies,
    Policy,
    Probe,
    QueuePolicy,
    QueueState,
    Snapshot,
    _fetch_json,
    _get_secret,
    config_fallback_dirs,
    load_health_policies,
    parse_probe_key,
    platform_of,
    probe_key,
    read_snapshot,
)

MAX_BACKOFF_S = 8 * 3600
HISTORY_PER_PAGE = 100
HISTORY_MAX_PAGES = 10
# Builds whose probe jobs may still run.
UNFINISHED_BUILD_STATES = ("running", "failing", "scheduled")

Fetch = Callable[[str, str], Any]
Run = Callable[..., Any]
Job = Dict[str, Any]


def job_queue(job: Job) -> Optional[str]:
    for rule in job.get("agent_query_rules") or []:
        if rule.startswith("queue="):
            return rule[len("queue=") :]
    return None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _fmt_duration(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


def fetch_history(
    fetch: Fetch, token: str, org: str, pipelines: Sequence[str], since: datetime
) -> List[Job]:
    """The current (not retried) script jobs of the main builds of
    `pipelines` that finished since `since` or are still running."""
    builds: Dict[str, Dict[str, Any]] = {}
    for pipeline in pipelines:
        for params in (
            {"finished_from": _iso(since)},
            *({"state": state} for state in UNFINISHED_BUILD_STATES),
        ):
            for page in range(1, HISTORY_MAX_PAGES + 1):
                batch = fetch(
                    f"{REST_URL}/organizations/{org}/pipelines/{pipeline}/builds?"
                    + urllib.parse.urlencode(
                        {
                            "branch": "main",
                            **params,
                            "per_page": HISTORY_PER_PAGE,
                            "page": page,
                        }
                    ),
                    token,
                )
                for build in batch:
                    builds[build.get("id") or f"{pipeline}/{build['number']}"] = build
                if len(batch) < HISTORY_PER_PAGE:
                    break
            else:
                print(f"History of {pipeline} capped at {HISTORY_MAX_PAGES} pages")
    return [
        job
        for build in builds.values()
        for job in build.get("jobs") or []
        if job.get("type") == "script" and not job.get("retried")
    ]


def finished_since(jobs: Sequence[Job], start: datetime) -> List[Job]:
    """The jobs with an outcome for the queue that finished at `start` or
    later."""
    return [
        job
        for job in jobs
        if job.get("state") in HISTORY_STATES
        and job.get("finished_at")
        and _parse_time(job["finished_at"]) >= start
    ]


def candidate_keys(
    jobs: Sequence[Job], probed: Optional[Mapping[str, str]] = None
) -> List[str]:
    """Distinct step keys of `jobs`, infra-failed ones first, each group
    newest first. Keys are kept as the build has them (`amd-<key>` for an AMD
    mirror). A probe job stands for the step it probed, looked up in `probed`
    (probe step key -> step key), since probe keys are sanitized and cut;
    other probe jobs are skipped."""
    newest = sorted(jobs, key=lambda job: _parse_time(job["finished_at"]), reverse=True)
    keys = []
    for job in sorted(newest, key=lambda job: not is_infra_failure(job)):
        key = job.get("step_key") or ""
        if parse_probe_key(key) is not None:
            key = (probed or {}).get(key, "")
        if key:
            keys.append(key)
    return list(dict.fromkeys(keys))


def probe_evidence(job: Job, deadline: datetime) -> Optional[Job]:
    """The probe job as evidence at `deadline`, or None if it is none. One
    runnable that never started counts as an infra failure."""
    runnable = _parse_time(job.get("runnable_at"))
    if runnable is None or runnable > deadline:
        return None
    finished = _parse_time(job.get("finished_at"))
    if finished is not None and finished <= deadline:
        return job if job.get("state") in HISTORY_STATES else None
    started = _parse_time(job.get("started_at"))
    if started is not None and started <= deadline:
        return None
    return {**job, "state": "failed", "exit_status": -1, "signal_reason": None}


class Checker:
    def __init__(self, cfg: HealthPolicies, now: datetime):
        self.cfg, self.now = cfg, now

    def history_since(self, previous: Optional[Snapshot]) -> datetime:
        """Back far enough for every window and every probing round."""
        window = max(
            (qp.policy.close_when.window for qp in self.cfg.units.values()), default=0
        )
        starts = [
            entry.since
            for entry in (previous.queues.values() if previous else ())
            if entry.state == "probing" and entry.since is not None
        ]
        return min([self.now - timedelta(seconds=window), *starts])

    def run(self, previous: Optional[Snapshot], history: List[Job]) -> Snapshot:
        by_queue: Dict[Optional[str], List[Job]] = {}
        for job in history:
            by_queue.setdefault(job_queue(job), []).append(job)
        prev_queues = previous.queues if previous else {}
        queues: Dict[str, QueueState] = {}
        for queue, qp in self.cfg.units.items():
            prefix = f"group {qp.group}: " if qp.group else ""
            prev = prev_queues.get(queue)
            if prev is not None:
                prev = prev.model_copy(
                    update={"reason": prev.reason.removeprefix(prefix)}
                )
            jobs = [job for member in qp.queues for job in by_queue.get(member, [])]
            entry = self.step(queue, prev, jobs)
            for member in qp.queues:
                probe = entry.probe if member == queue else None
                queues[member] = entry.model_copy(
                    update={
                        "reason": prefix + entry.reason if entry.reason else "",
                        "probe": probe,
                    }
                )
        return Snapshot(
            version=SNAPSHOT_VERSION,
            generated_at=self.now,
            queues=dict(sorted(queues.items())),
        )

    def step(
        self, queue: str, prev: Optional[QueueState], jobs: List[Job]
    ) -> QueueState:
        """The entry of the queues that `queue` runs the probes of, from its
        previous one and the jobs of all of them."""
        qp = self.cfg.units[queue]
        # Rounds and backoff outlive pins, so probe keys never repeat.
        carried = (
            {"probe_round": prev.probe_round, "backoff_s": prev.backoff_s}
            if prev
            else {}
        )
        pin = self.cfg.pins.get(queue)
        if pin is not None and pin.until > self.now:
            same = prev is not None and prev.pinned and prev.state == pin.state
            return QueueState(
                state=pin.state,
                since=prev.since if same else self.now,
                until=pin.until,
                reason=f"pinned: {pin.reason}",
                pinned=True,
                **carried,
            )
        if prev is None or prev.pinned:
            # A new queue starts open; an expired pin hands it back, open, and
            # failures from before the expiry still count.
            prev = QueueState(state="open", **carried)

        window = finished_since(
            jobs, self.now - timedelta(seconds=qp.policy.close_when.window)
        )
        if prev.state == "open":
            return self._open(qp, prev, window)
        stats = self._stats(window)
        if prev.until is not None and self.now < prev.until:
            return prev.model_copy(update=stats)
        if prev.state == "closed":
            return self._probe(qp, prev, window, stats)
        return self._verdict(queue, qp, prev, jobs, window, stats)

    @staticmethod
    def _stats(jobs: Sequence[Job]) -> Dict[str, Any]:
        rate = round(infra_rate(jobs), 4) if jobs else None
        return {"infra_rate": rate, "jobs": len(jobs)}

    @staticmethod
    def _fires(qp: QueuePolicy, jobs: Sequence[Job]) -> bool:
        close = qp.policy.close_when
        rule = rules_for(platform_of(qp.device))[close.rule]
        return rule(jobs, **close.params)

    @staticmethod
    def _next_probe(qp: QueuePolicy, prev: QueueState, window: List[Job]) -> Probe:
        """The next round, on up to `probes` keys of the probing queue's jobs
        in `window`, else on the keys `prev` recorded."""
        queue, probed = qp.queues[0], {}
        if prev.probe is not None:
            probed = {probe_key(queue, prev.probe.round, k): k for k in prev.probe.keys}
        own = [job for job in window if job_queue(job) == queue]
        keys = tuple(candidate_keys(own, probed)[: qp.policy.probes])
        if not keys and prev.probe is not None:
            keys = prev.probe.keys
        return Probe(round=prev.probe_round + 1, device=qp.device, keys=keys)

    def _closed(
        self,
        qp: QueuePolicy,
        prev: QueueState,
        window: List[Job],
        backoff: int,
        reason: str,
        stats: Dict[str, Any],
    ) -> QueueState:
        return QueueState(
            state="closed",
            since=self.now,
            until=self.now + timedelta(seconds=backoff),
            reason=reason,
            probe_round=prev.probe_round,
            backoff_s=backoff,
            probe=self._next_probe(qp, prev, window),
            **stats,
        )

    @staticmethod
    def _doubled(prev: QueueState, policy: Policy) -> int:
        cap = max(MAX_BACKOFF_S, policy.reopen_after)
        return min(2 * (prev.backoff_s or policy.reopen_after), cap)

    def _open(self, qp: QueuePolicy, prev: QueueState, window: List[Job]) -> QueueState:
        # After a reopen, only jobs that became runnable since then decide.
        if prev.since is not None:
            window = [
                job
                for job in window
                if job.get("runnable_at")
                and _parse_time(job["runnable_at"]) >= prev.since
            ]
        stats = self._stats(window)
        policy = qp.policy
        close = policy.close_when
        if len(window) < close.min_jobs or not self._fires(qp, window):
            return prev.model_copy(update=stats)
        reason = (
            f"{close.rule}: {infra_rate(window):.0%} infra failures in "
            f"{len(window)} jobs over {_fmt_duration(close.window)}"
        )
        backoff = policy.reopen_after
        flap_s = 2 * policy.reopen_after
        if prev.since is not None and self.now - prev.since < timedelta(seconds=flap_s):
            # Flapping: keep backing off from where the last round left it.
            backoff = self._doubled(prev, policy)
            reason += f"; re-closed within {_fmt_duration(flap_s)} of reopening"
        return self._closed(qp, prev, window, backoff, reason, stats)

    def _probe(
        self,
        qp: QueuePolicy,
        prev: QueueState,
        window: List[Job],
        stats: Dict[str, Any],
    ) -> QueueState:
        probe = self._next_probe(qp, prev, window)
        deadline = self.now + timedelta(seconds=qp.policy.reopen_after)
        return QueueState(
            state="probing",
            since=self.now,
            until=deadline,
            reason=f"probe round {probe.round} on {qp.device} until {_iso(deadline)}",
            probe_round=probe.round,
            backoff_s=prev.backoff_s or qp.policy.reopen_after,
            probe=probe,
            **stats,
        )

    def _verdict(
        self,
        queue: str,
        qp: QueuePolicy,
        prev: QueueState,
        jobs: List[Job],
        window: List[Job],
        stats: Dict[str, Any],
    ) -> QueueState:
        """The probing round's deadline has passed: stay closed only if the
        rule fires on the evidence of its probes, one per key."""
        round_, deadline = prev.probe_round, prev.until or self.now
        by_runnable = sorted(
            (job for job in jobs if job.get("runnable_at")),
            key=lambda job: _parse_time(job["runnable_at"]),
        )
        evidence = []
        for key in prev.probe.keys if prev.probe else ():
            found = (
                probe_evidence(job, deadline)
                for job in by_runnable
                if job.get("step_key") == probe_key(queue, round_, key)
            )
            first = next((job for job in found if job is not None), None)
            if first is not None:
                evidence.append(first)
        infra = sum(1 for job in evidence if is_infra_failure(job))
        reason = (
            f"probe round {round_}: {infra} infra failures in {len(evidence)} probes"
        )
        if evidence and self._fires(qp, evidence):
            backoff = self._doubled(prev, qp.policy)
            return self._closed(qp, prev, window, backoff, reason, stats)
        return QueueState(
            state="open",
            since=self.now,
            reason=f"{reason}; reopened",
            probe_round=round_,
            backoff_s=prev.backoff_s,
            **stats,
        )


def format_table(snapshot: Snapshot) -> str:
    rows = [("queue", "state", "since", "until", "infra", "jobs", "reason")]
    for queue, entry in snapshot.queues.items():
        rows.append(
            (
                queue,
                entry.state,
                _iso(entry.since) if entry.since else "",
                _iso(entry.until) if entry.until else "",
                "" if entry.infra_rate is None else f"{entry.infra_rate:.0%}",
                str(entry.jobs),
                entry.reason,
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths))
        + "  "
        + row[-1]
        for row in rows
    )


def publish(snapshot: Snapshot, run: Run) -> None:
    run(
        [
            "buildkite-agent",
            "meta-data",
            "set",
            SNAPSHOT_META_KEY,
            snapshot.model_dump_json(),
        ],
        check=True,
    )


def fallback_dirs(config_paths: Sequence[str]) -> List[str]:
    """The union of the pipeline configs' `fallback_dirs`, in order."""
    dirs: Dict[str, None] = {}
    for path in config_paths:
        with open(path) as f:
            config = yaml.safe_load(f) or {}
        try:
            dirs.update(dict.fromkeys(config_fallback_dirs(config)))
        except ValueError as error:
            raise ValueError(f"{path}: {error}") from None
    return list(dirs)


def main(
    argv: Optional[List[str]] = None,
    env: Optional[Dict[str, str]] = None,
    fetch: Fetch = _fetch_json,
    get_secret: Callable[[str], str] = _get_secret,
    run: Run = subprocess.run,
    now: Optional[datetime] = None,
) -> int:
    """Publishes once, last; an exception before that publishes nothing."""
    config_paths = sys.argv[1:] if argv is None else argv
    if not config_paths:
        print(__doc__)
        return 2
    env = dict(os.environ if env is None else env)
    dirs = fallback_dirs(config_paths)
    if not dirs:
        print(
            f"ERROR: none of {', '.join(config_paths)} sets fallback_dirs; "
            "nothing published."
        )
        return 1
    try:
        cfg = load_health_policies(dirs)
    except ValueError as error:
        print(
            f"ERROR: the fallback_dirs of {', '.join(config_paths)} must load as "
            f"one config, with one common/ health entry and each device once "
            f"({error}); nothing published."
        )
        return 1
    now = now or datetime.now(timezone.utc)
    org = env.get("BUILDKITE_ORGANIZATION_SLUG")
    token = get_secret(cfg.health.token_secret)
    # The generator's snapshot; its age matters only to the generator.
    previous = read_snapshot(cfg.health, org, token, fetch, now, allow_stale=True)
    if previous is None:
        print("No previous snapshot; every queue starts open.")
    else:
        print(f"Previous snapshot: generated at {_iso(previous.generated_at)}")
    checker = Checker(cfg, now)
    history = fetch_history(
        fetch, token, org, cfg.health.pipelines, checker.history_since(previous)
    )
    snapshot = checker.run(previous, history)
    print(format_table(snapshot))
    publish(snapshot, run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
