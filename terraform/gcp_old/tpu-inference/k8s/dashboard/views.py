"""Turns what fleet.py read into the two pages: the live view and history."""

from __future__ import annotations

import collections
import html
import re
import statistics
import string
import time
from pathlib import Path

from charts import line_chart, values_table
from fleet import (
    BK_LIMITED,
    BK_WAITING,
    DUTY_MODELS,
    GENERATIONS,
    JOB_ID,
    TPU,
    Config,
    job_hex,
)

E = html.escape
TEMPLATES = Path(__file__).parent / "templates"
LIVE_PAGE = string.Template((TEMPLATES / "live.html").read_text())
HISTORY_PAGE = string.Template((TEMPLATES / "history.html").read_text())
GLOSSARY = (TEMPLATES / "glossary.html").read_text()

# kube_workload_timing outcomes that are the test's or Buildkite's doing. Any
# other outcome is the fleet ending a workload.
TEST_OUTCOMES = {"succeeded", "failed", "cancelled"}

# Event reasons that mean something is wrong with the fleet rather than with a
# test. Kueue's own "Pending" warnings are the backlog and are already reported
# per queue; DeadlineExceeded on agent Jobs arrives about once per finished step
# and is routine.
FLEET_WARNINGS = {
    "FailedCreate",
    "NotTriggerScaleUp",
    "FailedMount",
    "Evicted",
    "OOMKilling",
    "FailedAttachVolume",
}

# JobSet's exclusive placement rejects every follower pod of a multi-host slice
# until the slice's leader pod has a node, and the Job controller retries, so a
# FailedCreate with one of these messages comes with every start and restart of
# a multi-host JobSet. Measured 2026-09-30..10-07: followers then start a median
# 7-10 s after the leader. Not a fleet problem; any other FailedCreate is.
PLACEMENT_HANDSHAKE = (
    "follower pod node selector",
    "leader pod not yet scheduled",
    "expected 1 leader pod",
)

# Thresholds for the health checks, as a share of the budgets the launcher
# enforces - a workload past them is about to be killed by the fleet itself.
WARN_SHARE, FAIL_SHARE = 0.25, 0.8
DISPATCH_STALL_SECONDS = 300
STUCK_POD_SECONDS = 600

# Where a kube job is, in the order a job passes through, from its Buildkite
# state, its agent pod and its Kueue workload. Buildkite alone cannot say: it
# shows a kube job as running from the moment its agent pod starts.
JOB_STATES = {
    "held": (
        "Concurrency held",
        "Buildkite will not offer it until another job in its group finishes.",
    ),
    "waiting_agent": (
        "Waiting for agent",
        "Offered to the kube queue; agent-stack-k8s has not started its pod.",
    ),
    "agent_pending": (
        "Agent pending",
        "The manager has not scheduled the agent pod.",
    ),
    "launching": (
        "Launching",
        "The launcher is checking out and submitting, or the step runs on the agent pod itself.",
    ),
    "queued": ("Pending in Kueue", "Submitted, waiting for quota."),
    "dispatching": (
        "Dispatching",
        "Quota reserved on the manager, waiting for a worker to accept.",
    ),
    "starting": (
        "Pods starting",
        "Admitted; its pods are being scheduled, pulled or started.",
    ),
    "running": ("Running", "Admitted and every pod up."),
}

# History ranges offered as presets; any other range is picked by date.
PRESETS = {"6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
PRESET_LABELS = {"6h": "6 hours", "24h": "24 hours", "7d": "7 days", "30d": "30 days"}
STEPS = (300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400)


def choose_step(seconds: int) -> int:
    """The finest step that keeps a chart under ~250 points."""
    return next((s for s in STEPS if seconds / s <= 250), STEPS[-1])


# --------------------------------------------------------------------------
# Formatting


def ago(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d{seconds % 86400 // 3600}h"


def num(value) -> str:
    if value is None:
        return "-"
    if abs(value - round(value)) < 0.05:
        return f"{int(round(value)):,}"
    return f"{value:,.1f}"


def pct(value: float | None) -> str:
    return "-" if value is None else f"{100 * value:.0f}%"


def unit(q: dict) -> str:
    return "chips" if q["resource"] == TPU else "cores"


def queue_title(q: dict) -> str:
    """v7x 2x2x1; the ClusterQueue name is in the hover title."""
    if q["resource"] != TPU:
        return q["name"]
    return f"{q['generation']} {q['topology']}"


def queue_link(q: dict, page: str = "") -> str:
    return f'<a href="{page}#{E(q["name"])}" title="{E(q["name"])}">{E(queue_title(q))}</a>'


def build_link(r: dict) -> str:
    title = E(
        f"{r['branch']} - {r['message']}" if r.get("message") else r.get("branch", "")
    )
    return f'<a href="{E(r["url"])}" title="{title}" target="_blank" rel="noopener">{E(r["pipeline"])} #{r["number"]}</a>'


def mean(values: list) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def add_series(*series: list) -> list:
    out = []
    for values in zip(*series):
        present = [v for v in values if v is not None]
        out.append(round(sum(present), 1) if present else None)
    return out


def resample(points: list[tuple[float, float]], ticks: list[float], step: int) -> list:
    """The last value at or before each tick, if it is recent enough to trust."""
    points = sorted(points)
    out: list = []
    i = -1
    for t in ticks:
        while i + 1 < len(points) and points[i + 1][0] <= t:
            i += 1
        ok = i >= 0 and t - points[i][0] <= 2 * step
        out.append(round(points[i][1], 1) if ok else None)
    return out


def exact(points: list[tuple[float, float]], ticks: list[float]) -> list:
    """A series' value at each tick, None where it has none. For node presence,
    where a missing point means the node was gone, not - as resample assumes for
    a gauge - a scrape that came late."""
    at = {int(round(t)): v for t, v in points}
    return [at.get(int(t)) for t in ticks]


def pool_index(pools: list) -> dict:
    return {h: p for p in pools for h in p["hashes"]}


def pool_of(by_hash: dict, key: str) -> dict | None:
    """The node pool of a "cluster|gke-tpu-<hash>-<id>" series key."""
    parts = key.partition("|")[2].split("-")
    return by_hash.get(parts[2]) if len(parts) >= 4 else None


def live_nodes(pools: list, present: dict) -> dict:
    """Per Kueue queue: TPU nodes up now, and the pools' bounds."""
    by_hash = pool_index(pools)
    per_pool = collections.Counter()
    for key in present:
        p = pool_of(by_hash, key)
        if p:
            per_pool[(p["cluster"], p["name"])] += 1
    out: dict = {}
    for p in pools:
        n = out.setdefault(
            p["queue"],
            {
                "up": 0,
                "chips": 0.0,
                "min": 0,
                "max": 0,
                "pools": 0,
                "pools_up": 0,
                "multi_host": p["multi_host"],
            },
        )
        up = per_pool[(p["cluster"], p["name"])]
        n["up"] += up
        n["chips"] += up * p["chips_per_node"]
        n["min"] += p["min_nodes"]
        n["max"] += p["max_nodes"]
        n["pools"] += 1
        n["pools_up"] += bool(up)
    return out


def sort_queues(queues: list[dict]) -> list[dict]:
    order = list(GENERATIONS)
    return sorted(
        queues,
        key=lambda q: (
            q["resource"] != TPU,
            order.index(q["family"]) if q["family"] in order else 99,
            q["chips"],
            q["name"],
        ),
    )


def render_sources(sources: dict) -> str:
    out = []
    for s in sources.values():
        state = "fail" if s["error"] else "ok"
        title = E(s["error"]) if s["error"] else "ok"
        label = s["name"] + (" failed" if s["error"] else "")
        out.append(
            f'<span class="pill {state}" title="{title}"><span class="dot"></span>{E(label)}</span>'
        )
    return "".join(out)


def render_errors(sources: dict) -> str:
    errors = "".join(
        f'<p class="error">{E(s["name"])} failed: {E(s["error"])}'
        + (
            f' - showing data from <time data-ts="{s["at"]}"></time>'
            if s.get("at")
            else ""
        )
        + "</p>"
        for s in sources.values()
        if s["error"]
    )
    return f'<div class="card errors">{errors}</div>' if errors else ""


# --------------------------------------------------------------------------
# Live


def build_live(cfg: Config, data: dict, sources: dict) -> dict:
    now = time.time()
    kq = data.get("kueue") or {
        "queues": [],
        "workloads": [],
        "workers": [],
        "checks": [],
        "pods": [],
        "job_builds": {},
    }
    bk = data.get("buildkite") or {
        "meta": {},
        "priorities": {},
        "job_builds": {},
        "jobs": [],
    }
    ev = data.get("events") or {"events": [], "errors": {}}
    health = data.get("health") or {}
    stats24 = data.get("stats24") or {"queues": [], "outcomes": []}
    job_builds = {**bk["job_builds"], **kq["job_builds"]}

    def build_row(pipeline: str, number: int) -> dict:
        meta = bk["meta"].get(f"{pipeline}#{number}", {})
        return {
            "pipeline": pipeline,
            "number": number,
            "url": meta.get("url")
            or f"https://buildkite.com/{cfg.org}/{pipeline}/builds/{number}",
            "branch": meta.get("branch", ""),
            "source": meta.get("source", ""),
            "message": meta.get("message", ""),
            "priority": None,
            "pending": 0,
            "running": 0,
            "held": 0.0,
            "oldest_wait": None,
        }

    queues = []
    for q in kq["queues"]:
        wls = [w for w in kq["workloads"] if w["queue"] == q["name"]]
        rows: dict = {}
        reasons: dict[str, int] = {}
        for w in wls:
            row = rows.setdefault(
                (w["pipeline"], w["number"]), build_row(w["pipeline"], w["number"])
            )
            prio = bk["priorities"].get(w["job_id"], w["priority"])
            row["priority"] = max(
                prio, row["priority"] if row["priority"] is not None else prio
            )
            if w["state"] == "pending":
                row["pending"] += 1
                row["oldest_wait"] = max(row["oldest_wait"] or 0, now - w["since"])
                # "3 more needed" differs per workload; the cause does not.
                reason = (
                    re.sub(r", \d+ more needed", "", w["reason"])
                    or "queued behind the head of the queue"
                )
                reasons[reason] = reasons.get(reason, 0) + 1
            else:
                row["running"] += 1
                row["held"] += w["amount"]
        pending = [w for w in wls if w["state"] == "pending"]
        queues.append(
            {
                **q,
                "within_nominal": min(q["used"], q["nominal"]),
                "idle": max(0.0, q["nominal"] - q["used"]),
                "admitted": sum(1 for w in wls if w["state"] == "admitted"),
                "dispatching": sum(1 for w in wls if w["state"] == "dispatching"),
                "pending": len(pending),
                "pending_amount": sum(w["amount"] for w in pending),
                "oldest_wait": max((now - w["since"] for w in pending), default=None),
                "reasons": sorted(reasons.items(), key=lambda kv: -kv[1]),
                "builds": sorted(
                    rows.values(),
                    key=lambda r: (
                        -(r["pending"] > 0),
                        -(r["priority"] or 0),
                        -(r["oldest_wait"] or 0),
                    ),
                ),
            }
        )
    queues = sort_queues(queues)
    nodes = live_nodes(data.get("pools") or [], health.get("nodes", {}))
    for q in queues:
        q["nodes"] = nodes.get(q["name"])

    cohorts: dict[str, dict] = {}
    for q in queues:
        c = cohorts.setdefault(
            q["cohort"],
            {
                "name": q["cohort"],
                "resource": q["resource"],
                "family": q["family"],
                "generation": q["generation"],
                "queues": [],
                "nominal": 0.0,
                "used": 0.0,
                "pending": 0,
                "pending_amount": 0.0,
            },
        )
        c["queues"].append(q["name"])
        for key in ("nominal", "used", "pending", "pending_amount"):
            c[key] += q[key]
    for c in cohorts.values():
        c["free"] = max(0.0, c["nominal"] - c["used"])
        model = DUTY_MODELS.get(c["family"], "")
        c["busy"] = health.get("busy", {}).get(model)
        members = [
            q["nodes"] for q in queues if q["cohort"] == c["name"] and q["nodes"]
        ]
        c["on_nodes"] = sum(n["chips"] for n in members) if members else None

    snapshot = {
        "generated_at": now,
        "sources": sources,
        "event_errors": ev["errors"],
        "cohorts": list(cohorts.values()),
        "queues": queues,
        "events": group_events(ev["events"], job_builds, cfg.org),
        "jobs": kube_jobs(
            bk["jobs"], kq["workloads"], kq["pods"], {q["name"]: q for q in queues}, now
        ),
    }
    snapshot["checks"] = health_checks(cfg, snapshot, kq, ev, health, stats24, now)
    return snapshot


def kube_jobs(
    bk_jobs: list, workloads: list, pods: list, queues: dict, now: float
) -> list:
    """Every Buildkite kube job and where it really is, from Buildkite's state,
    its agent pod and its Kueue workload."""
    by_job = {w["job_id"].replace("-", ""): w for w in workloads if w["job_id"]}
    pod_by_job = {p["job"]: p for p in pods if p["job"]}
    out = []
    for j in bk_jobs:
        w, pod = by_job.get(j["id"]), pod_by_job.get(j["id"])
        detail = ""
        if j["state"] in BK_LIMITED:
            state, since = "held", j["runnable_at"]
        elif j["state"] in BK_WAITING:
            state, since = "waiting_agent", j["runnable_at"]
        elif w is None:
            if pod and pod["phase"] == "Pending":
                state, since, detail = "agent_pending", pod["created"], pod["message"]
            else:
                state, since = "launching", j["started_at"]
        elif w["state"] == "pending":
            state, since = "queued", w["since"]
            detail = (
                re.sub(r", \d+ more needed", "", w["reason"])
                or "queued behind the head of the queue"
            )
        elif w["state"] == "dispatching":
            state, since = "dispatching", w["since"]
        else:
            state, since = (
                ("running" if w.get("pods_ready") else "starting"),
                w["since"],
            )
        q = queues.get(w["queue"]) if w else None
        out.append(
            {
                **j,
                "real": state,
                "queue": queue_title(q) if q else (w["queue"] if w else ""),
                "queue_name": w["queue"] if w else "",
                "for": now - since if since else None,
                "detail": detail,
            }
        )
    order = list(JOB_STATES)
    return sorted(out, key=lambda r: (order.index(r["real"]), -(r["for"] or 0)))


def group_events(events: list, job_builds: dict, org: str) -> list:
    """Repeats of one event on one cluster collapse into a row with a count."""
    groups: dict = {}
    for e in events:
        normalized = re.sub(r"\d+", "#", JOB_ID.sub("<job>", e["message"]))
        key = (e["cluster"], e["type"], e["kind"], e["reason"], normalized)
        g = groups.setdefault(
            key,
            {
                "cluster": e["cluster"],
                "type": e["type"],
                "kind": e["kind"],
                "reason": e["reason"],
                "message": e["message"],
                "count": 0,
                "objects": set(),
                "last": 0.0,
                "builds": {},
            },
        )
        g["count"] += e["count"]
        g["objects"].add(e["name"])
        if e["last"] > g["last"]:
            g["last"], g["message"] = e["last"], e["message"]
        build = job_builds.get(job_hex(e["name"]) or job_hex(e["message"]))
        if build:
            pipeline, number = build
            g["builds"][f"{pipeline} #{number}"] = (
                f"https://buildkite.com/{org}/{pipeline}/builds/{number}"
            )
    out = []
    for g in groups.values():
        g["objects"] = len(g["objects"])
        g["builds"] = sorted(g["builds"].items())
        g["fleet"] = (
            g["type"] == "Warning"
            and g["reason"] in FLEET_WARNINGS
            and not any(m in g["message"] for m in PLACEMENT_HANDSHAKE)
        )
        out.append(g)
    return sorted(out, key=lambda g: (-g["fleet"], g["type"] != "Warning", -g["last"]))


def health_checks(cfg, snap, kq, ev, health, stats24, now) -> list:
    """The on-call's view: one row per stage from Buildkite to a TPU pod."""
    checks = []

    def add(status: str, title: str, detail: str) -> None:
        checks.append({"status": status, "title": title, "detail": detail})

    # Buildkite controller.
    up = health.get("monitor_up", {}).get("")
    creates = health.get("creates", {}).get("", 0.0)
    calls = health.get("create_calls", {}).get("", 0.0)
    errors = health.get("query_errors", {}).get("", 0.0)
    if up is None:
        add(
            "unknown",
            "Buildkite controller",
            "No agent-stack-k8s metrics in the last five minutes.",
        )
    elif up < 1:
        add(
            "fail",
            "Buildkite controller",
            "agent-stack-k8s reports it is not polling Buildkite.",
        )
    elif creates < calls or errors > 0:
        add(
            "warn",
            "Buildkite controller",
            f"{calls - creates:.0f} of {calls:.0f} job creates failed and {errors:.0f} "
            "Buildkite queries errored in the last hour.",
        )
    else:
        add(
            "ok",
            "Buildkite controller",
            f"Polling; {creates:.0f} of {calls:.0f} job creates succeeded in the last hour.",
        )

    # Kueue on every cluster.
    kueue_up = health.get("kueue_up", {})
    down = [c["name"] for c in cfg.clusters if kueue_up.get(c["name"], 0) < 1]
    if not kueue_up:
        add("unknown", "Kueue controllers", "No Kueue scrape data.")
    elif down:
        add("fail", "Kueue controllers", "Not scraped as up: " + ", ".join(down) + ".")
    else:
        add("ok", "Kueue controllers", f"Up on all {len(cfg.clusters)} clusters.")

    # Manager to workers.
    bad = [w for w in kq["workers"] if not w["active"]] + [
        c for c in kq["checks"] if not c["active"]
    ]
    if bad:
        add(
            "fail",
            "Worker connections",
            "; ".join(f"{b['name']}: {b['message']}" for b in bad),
        )
    elif kq["workers"]:
        add(
            "ok",
            "Worker connections",
            f"{len(kq['workers'])} MultiKueue workers connected; all {len(kq['checks'])} admission checks active.",
        )
    else:
        add("unknown", "Worker connections", "No MultiKueue clusters read.")

    # Dispatch: reserved on the manager, never admitted by a worker.
    dispatching = [w for w in kq["workloads"] if w["state"] == "dispatching"]
    stalled = [w for w in dispatching if now - w["since"] > DISPATCH_STALL_SECONDS]
    redispatches = sum(int(r.get("redispatches") or 0) for r in stats24["queues"])
    if stalled:
        oldest = max(now - w["since"] for w in stalled)
        add(
            "warn",
            "Dispatch",
            f"{len(stalled)} workloads reserved but not placed on a worker for over "
            f"{DISPATCH_STALL_SECONDS // 60} minutes (oldest {ago(oldest)}); the launcher resubmits these.",
        )
    else:
        add(
            "ok",
            "Dispatch",
            f"{len(dispatching)} dispatching, none stalled; {redispatches} redispatches in 24 hours.",
        )

    # Queue waits against the launcher's queue budget.
    pending = [(q, q["oldest_wait"]) for q in snap["queues"] if q["oldest_wait"]]
    if pending:
        q, wait = max(pending, key=lambda p: p[1])
        share = wait / cfg.queue_budget
        status = (
            "fail" if share > FAIL_SHARE else "warn" if share > WARN_SHARE else "ok"
        )
        add(
            status,
            "Queue waits",
            f"Oldest pending {ago(wait)} in {queue_title(q)}, against a {ago(cfg.queue_budget)} budget; "
            f"{sum(x['pending'] for x in snap['queues'])} workloads pending in all.",
        )
    else:
        add("ok", "Queue waits", "Nothing pending.")

    # Workloads running towards the test budget - likely hung.
    long_running = [
        w
        for w in kq["workloads"]
        if w["state"] == "admitted" and now - w["since"] > FAIL_SHARE * cfg.test_budget
    ]
    if long_running:
        w = max(long_running, key=lambda w: now - w["since"])
        add(
            "warn",
            "Long-running workloads",
            f"{len(long_running)} admitted over {ago(FAIL_SHARE * cfg.test_budget)} "
            f"(budget {ago(cfg.test_budget)}); longest {ago(now - w['since'])}, "
            f"{w['pipeline']} #{w['number']}.",
        )
    else:
        add(
            "ok",
            "Long-running workloads",
            f"None near the {ago(cfg.test_budget)} test budget.",
        )

    # Infrastructure failures, from the launcher's own record.
    finished = failed = infra = 0
    by_outcome: dict[str, int] = {}
    for row in stats24["outcomes"]:
        n = int(row["n"])
        finished += n
        if row["outcome"] == "failed":
            failed += n
        elif row["outcome"] not in TEST_OUTCOMES:
            infra += n
            by_outcome[row["outcome"]] = by_outcome.get(row["outcome"], 0) + n
    if not cfg.timing_table or not finished:
        add(
            "unknown",
            "Infrastructure failures (24h)",
            "No finished workloads recorded.",
        )
    else:
        status = "fail" if infra / finished > 0.05 else "warn" if infra else "ok"
        breakdown = ", ".join(
            f"{k} {v}" for k, v in sorted(by_outcome.items(), key=lambda kv: -kv[1])
        )
        add(
            status,
            "Infrastructure failures (24h)",
            f"{infra} of {finished} workloads"
            + (f" ({breakdown})" if breakdown else "")
            + f"; {failed} test failures.",
        )

    # Evictions: preemption is reclaim working as designed; anything else is not.
    by_reason: dict[str, float] = {}
    for key, n in health.get("evictions", {}).items():
        reason = key.split("|", 1)[1]
        by_reason[reason] = by_reason.get(reason, 0) + n
    by_reason = {k: v for k, v in by_reason.items() if round(v) > 0}
    if not by_reason:
        add("ok", "Evictions (24h)", "None.")
    else:
        other = {k: v for k, v in by_reason.items() if k != "Preempted"}
        add(
            "warn" if other else "info",
            "Evictions (24h)",
            ", ".join(
                f"{round(v)} {k.lower()}"
                for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1])
            )
            + (
                "."
                if other
                else "; preemption is a queue reclaiming lent quota, and the step reruns."
            ),
        )

    # Fleet warnings in the last hour.
    fleet = [g for g in snap["events"] if g["fleet"]]
    if ev["errors"]:
        add(
            "unknown",
            "Cluster problems (1h)",
            "Could not read events from " + ", ".join(ev["errors"]) + ".",
        )
    elif fleet:
        add(
            "warn",
            "Cluster problems (1h)",
            "; ".join(
                f"{g['reason']} ×{g['count']} on {g['cluster']}" for g in fleet[:4]
            )
            + ". See Cluster problems.",
        )
    else:
        add(
            "ok",
            "Cluster problems (1h)",
            "No failed creates, failed scale-ups, mount failures or evictions.",
        )

    # Agent pods the manager could not schedule.
    stuck = [
        p
        for p in kq["pods"]
        if p["phase"] == "Pending" and now - p["created"] > STUCK_POD_SECONDS
    ]
    running = sum(1 for p in kq["pods"] if p["phase"] == "Running")
    if stuck:
        add(
            "warn",
            "Agent pods",
            f"{len(stuck)} pending over {STUCK_POD_SECONDS // 60} minutes on the manager"
            + (f": {stuck[0]['message'][:120]}" if stuck[0]["message"] else "")
            + ".",
        )
    else:
        add(
            "ok", "Agent pods", f"{running} running; none stuck pending on the manager."
        )

    # Steps Buildkite offered that no agent pod picked up.
    waiting = [
        j
        for j in snap["jobs"]
        if j["real"] == "waiting_agent" and (j["for"] or 0) > STUCK_POD_SECONDS
    ]
    held = sum(1 for j in snap["jobs"] if j["real"] == "held")
    if waiting:
        add(
            "warn",
            "Steps waiting for an agent",
            f"{len(waiting)} kube steps offered over {STUCK_POD_SECONDS // 60} minutes ago with no agent pod.",
        )
    else:
        add(
            "ok",
            "Steps waiting for an agent",
            f"None stuck; {held} held by concurrency groups, as their pipelines ask.",
        )
    return checks


ICONS = {
    "ok": ("✓", "OK"),
    "warn": ("!", "Warning"),
    "fail": ("✕", "Failing"),
    "info": ("i", "Note"),
    "unknown": ("?", "Unknown"),
}


def borrow_cell(q: dict) -> str:
    if q["borrowed"]:
        return f'<span class="pill borrowed">borrowing {num(q["borrowed"])}</span>'
    if q["idle"]:
        return f'<span class="pill idle">{num(q["idle"])} idle, lendable</span>'
    if not q["nominal"]:
        return '<span class="muted">no nominal</span>'
    return '<span class="muted">at nominal</span>'


def quota_bar(q: dict, scale: float) -> str:
    """In use within nominal, borrowed beyond it, and idle nominal, to one scale."""
    if scale <= 0:
        return ""
    parts = [
        ("own", q["within_nominal"], "in use within nominal"),
        ("borrowed", q["borrowed"], "borrowed from the cohort"),
        ("idle", q["idle"], "idle nominal, lendable"),
    ]
    segs = "".join(
        f'<span class="seg {cls}" style="width:{100 * v / scale:.2f}%" title="{num(v)} {E(label)}"></span>'
        for cls, v, label in parts
        if v > 0
    )
    return f'<div class="bar" role="img" aria-label="{E(q["name"])} quota">{segs}</div>'


def render_live_summary(snap: dict) -> str:
    out = []
    by_name = {q["name"]: q for q in snap["queues"]}
    for c in snap["cohorts"]:
        if c["resource"] != TPU:
            continue
        rows = "".join(
            f"""<tr><td class="nowrap">{queue_link(q)}</td>
<td class="n">{num(q["nominal"])}</td><td class="n"><b>{num(q["used"])}</b></td>
<td>{borrow_cell(q)}</td>
<td class="n">{num(q["borrowing_limit"]) if q["borrowing_limit"] else "-"}</td>
<td>{"evicts borrowers" if q["reclaim"] != "Never" else '<span class="muted">never</span>'}</td>
<td class="n">{num(q["admitted"] + q["dispatching"])}</td>
<td class="n">{num(q["pending"])}{f' <span class="muted">({num(q["pending_amount"])} chips)</span>' if q["pending"] else ""}</td>
<td class="barcell">{quota_bar(q, c["nominal"])}</td></tr>"""
            for q in (by_name[n] for n in c["queues"])
        )
        busy = (
            f"<b>{num(c['busy'])}</b><small>chips computing, of {num(c['on_nodes'])} on nodes</small>"
            if c["busy"] is not None
            # No series for the model: no node of that kind is up.
            else f"<b>0</b><small>no {E(c['generation'])} TPU nodes up</small>"
        )
        out.append(f"""
<div class="cohort">
  <h3>{E(c["generation"] or c["name"])} <span class="muted">· cohort <code>{E(c["name"])}</code></span></h3>
  <div class="tiles">
    <div class="tile"><span>Chips in use</span><b>{num(c["used"])}</b><small>of {num(c["nominal"])} nominal ({pct(c["used"] / c["nominal"] if c["nominal"] else None)})</small></div>
    <div class="tile"><span>Free</span><b>{num(c["free"])}</b><small>chips no queue is using</small></div>
    <div class="tile"><span>Pending</span><b>{num(c["pending"])}</b><small>workloads, {num(c["pending_amount"])} chips</small></div>
    <div class="tile"><span>Busy now</span>{busy}</div>
  </div>
  <div class="table-wrap"><table><thead><tr><th>Queue</th><th class="n">Nominal</th><th class="n">In use</th><th>Borrowing</th>
  <th class="n">May borrow</th><th>Reclaim</th><th class="n">Running</th><th class="n">Pending</th>
  <th class="barcell"><span class="key"><i class="sw own"></i>own</span><span class="key"><i class="sw borrowed"></i>borrowed</span><span class="key"><i class="sw idle"></i>idle</span></th>
  </tr></thead><tbody>{rows}</tbody></table></div>
</div>""")
    return "".join(out)


def render_live_queue(q: dict) -> str:
    builds = (
        "".join(
            "<tr>"
            f'<td>{build_link(r)}</td><td class="branch">{E(r["branch"])}</td><td>{E(r["source"])}</td>'
            f'<td class="n">{num(r["priority"])}</td>'
            f'<td class="n">{num(r["pending"]) if r["pending"] else ""}</td>'
            f'<td class="n">{num(r["running"]) if r["running"] else ""}</td>'
            f'<td class="n">{num(r["held"]) if r["held"] else ""}</td>'
            f'<td class="n">{ago(r["oldest_wait"])}</td></tr>'
            for r in q["builds"]
        )
        or '<tr><td colspan="8" class="empty">No workloads.</td></tr>'
    )
    reasons = "".join(f"<li><b>{n}</b> {E(msg)}</li>" for msg, n in q["reasons"])
    u = unit(q)
    tpu = q["resource"] == TPU
    pills = []
    if q["borrowed"]:
        pills.append(
            f'<span class="pill borrowed">borrowing {num(q["borrowed"])} {u}</span>'
        )
    elif q["idle"] and tpu:
        pills.append(f'<span class="pill idle">{num(q["idle"])} idle, lendable</span>')
    if q["borrowing_limit"] and tpu:
        pills.append(
            f'<span class="pill">may borrow {num(q["borrowing_limit"])}</span>'
        )
    if q["reclaim"] != "Never":
        pills.append('<span class="pill">evicts borrowers to reclaim</span>')
    return f"""
<div class="card queue{"" if tpu else " other"}" id="{E(q["name"])}">
  <div class="card-head"><h2 title="{E(q["name"])}">{E(queue_title(q))}</h2>
    <a class="more" href="history#{E(q["name"])}">History →</a></div>
  <div class="stats">
    <p class="stat"><b>{num(q["pending"])}</b><span>pending · oldest {ago(q["oldest_wait"])}</span></p>
    <p class="stat"><b>{num(q["admitted"])}</b><span>admitted{f" · {q['dispatching']} dispatching" if q["dispatching"] else ""}</span></p>
    <p class="stat"><b>{num(q["used"])}</b><span>{u} in use of {num(q["nominal"])} nominal</span></p>
  </div>
  <div class="pills">{"".join(pills)}</div>
  {f'<div class="reasons"><h4>Why workloads are pending</h4><ul>{reasons}</ul></div>' if reasons else ""}
  <div class="table-wrap"><table><thead><tr><th>Build</th><th>Branch</th><th>Source</th><th class="n">Priority</th>
    <th class="n">Pending</th><th class="n">Running</th><th class="n">{u.capitalize()} held</th><th class="n">Oldest wait</th>
  </tr></thead><tbody>{builds}</tbody></table></div>
</div>"""


def render_events(snap: dict) -> str:
    def table(groups: list, empty: str) -> str:
        body = (
            "".join(
                f"""<tr><td class="nowrap"><time data-ts="{g["last"]}" data-fmt="time"></time></td><td class="nowrap">{E(g["cluster"])}</td>
<td>{E(g["reason"])}<div class="sub-row">{E(g["kind"])}{" · Warning" if g["type"] == "Warning" else ""}</div></td>
<td class="n">{num(g["count"])}{f'<div class="sub-row">{g["objects"]} objects</div>' if g["objects"] > 1 else ""}</td>
<td class="msg">{E(g["message"][:300])}</td>
<td class="nowrap">{"<br>".join(f'<a href="{E(url)}" target="_blank" rel="noopener">{E(name)}</a>' for name, url in g["builds"][:3])}{f'<div class="sub-row">+{len(g["builds"]) - 3} more</div>' if len(g["builds"]) > 3 else ""}</td></tr>"""
                for g in groups[:60]
            )
            or f'<tr><td colspan="6" class="empty">{empty}</td></tr>'
        )
        return (
            '<div class="table-wrap"><table class="dense"><thead><tr><th>Last seen</th><th>Cluster</th><th>Reason</th>'
            f'<th class="n">Count</th><th>Message</th><th>Builds</th></tr></thead><tbody>{body}</tbody></table></div>'
        )

    # Kubernetes events are Normal or Warning, nothing finer, and most warnings
    # here are routine: Kueue's Pending backlog, FailedScheduling while a pool
    # scales up, agent-Job teardown, a test's own failure. Only the fleet's
    # problems are shown open; the rest stay one click away for debugging.
    problems = [g for g in snap["events"] if g["fleet"]]
    other = [g for g in snap["events"] if not g["fleet"]]
    errors = "".join(
        f'<p class="error">Events from {E(c)} failed: {E(e)}</p>'
        for c, e in snap["event_errors"].items()
    )
    return f"""{errors}<p class="muted lead">Fleet problems in the <code>buildkite</code> namespace on every cluster,
repeats grouped: {E(", ".join(sorted(FLEET_WARNINGS)))}.</p>
{table(problems, "None in the last hour.")}
<details><summary>Other events ({len(other)} kinds, {sum(g["count"] for g in other):,} events)</summary>
<p class="muted note">Routine warnings and notable normal events: Kueue's backlog, scheduling while pools scale,
scale-ups, preemptions, agent teardown, test failures.</p>
{table(other, "None.")}</details>"""


def job_link(j: dict) -> str:
    return f'<a href="{E(j["url"])}" target="_blank" rel="noopener">{E(j["label"] or "(unnamed step)")}</a>'


def render_jobs(snap: dict) -> str:
    jobs = snap["jobs"]
    counts = {k: 0 for k in JOB_STATES}
    for j in jobs:
        counts[j["real"]] += 1

    states = (
        '<div class="state-row">'
        + "".join(
            f'<div class="state-chip{" zero" if not counts[k] else ""}" title="{E(desc)}">'
            f"<b>{counts[k]}</b><small>{E(name)}</small></div>"
            for k, (name, desc) in JOB_STATES.items()
        )
        + "</div>"
    )

    def groups(selected: list) -> list:
        """One row per build, state and queue: a build's steps usually move
        through the fleet together, and twenty identical rows say less than one
        row that counts them."""
        out: dict = {}
        for j in selected:
            g = out.setdefault(
                (j["pipeline"], j["number"], j["real"], j["queue"]),
                {**j, "steps": [], "oldest": 0.0, "details": set()},
            )
            g["steps"].append(j)
            g["oldest"] = max(g["oldest"], j["for"] or 0)
            if j["detail"]:
                g["details"].add(j["detail"])
        order = list(JOB_STATES)
        return sorted(
            out.values(), key=lambda g: (order.index(g["real"]), -g["oldest"])
        )

    def steps_cell(g: dict) -> str:
        items = "".join(
            f'<li>{job_link(j)} <span class="muted">{ago(j["for"])}</span></li>'
            for j in g["steps"][:40]
        )
        more = (
            f'<li class="muted">and {len(g["steps"]) - 40} more</li>'
            if len(g["steps"]) > 40
            else ""
        )
        label = f"{len(g['steps'])} step{'s' if len(g['steps']) != 1 else ''}"
        return f'<details class="steps"><summary>{label}</summary><ul>{items}{more}</ul></details>'

    def build_cell(g: dict) -> str:
        return f'<a href="{E(g["url"].split("#")[0])}" target="_blank" rel="noopener">{E(g["pipeline"])} #{g["number"]}</a>'

    waiting = [j for j in jobs if j["real"] != "running"]
    running = [j for j in jobs if j["real"] == "running"]
    waiting_rows = (
        "".join(
            f'<tr><td class="nowrap">{build_cell(g)}</td><td class="nowrap"><b>{E(JOB_STATES[g["real"]][0])}</b>'
            "</td>"
            f'<td class="nowrap" title="{E(g["queue_name"])}">{E(g["queue"]) or "-"}</td>'
            f"<td>{steps_cell(g)}</td>"
            f'<td class="n nowrap">{ago(g["oldest"])}</td>'
            f'<td class="msg">{"<br>".join(E(d[:200]) for d in sorted(g["details"])[:2])}</td></tr>'
            for g in groups(waiting)
        )
        or '<tr><td colspan="6" class="empty">Every kube job is running.</td></tr>'
    )
    running_rows = (
        "".join(
            f'<tr><td class="nowrap">{build_cell(g)}</td><td class="nowrap" title="{E(g["queue_name"])}">{E(g["queue"])}</td>'
            f'<td>{steps_cell(g)}</td><td class="n nowrap">{ago(g["oldest"])}</td></tr>'
            for g in groups(running)
        )
        or '<tr><td colspan="4" class="empty">None.</td></tr>'
    )
    return f"""{states}
<p class="muted lead">Where each kube job is, grouped by build. Hover a count for what the state means.</p>
<h3>Not running yet <span class="muted">({len(waiting)} jobs)</span></h3>
<div class="table-wrap"><table class="dense"><thead><tr><th>Build</th><th>State</th><th>Queue</th><th>Steps</th>
<th class="n">Longest</th><th>Detail</th></tr></thead><tbody>{waiting_rows}</tbody></table></div>
<details><summary>Running ({len(running)} jobs)</summary>
<div class="table-wrap"><table class="dense"><thead><tr><th>Build</th><th>Queue</th><th>Steps</th><th class="n">Longest</th></tr></thead>
<tbody>{running_rows}</tbody></table></div></details>"""


def render_live(snap: dict) -> str:
    tpu = [q for q in snap["queues"] if q["resource"] == TPU]
    other = [q for q in snap["queues"] if q["resource"] != TPU]
    nav = "".join(
        f'<a class="pill{" attn" if q["pending"] else ""}" href="#{E(q["name"])}">{E(queue_title(q))}'
        f" · {num(q['pending'])} pending</a>"
        for q in tpu
    )
    checks = "".join(
        f'<div class="check {c["status"]}"><span class="icon" aria-hidden="true">{ICONS[c["status"]][0]}</span>'
        f'<span class="title">{E(c["title"])}<span class="state">{ICONS[c["status"]][1]}</span></span>'
        f'<span class="detail">{E(c["detail"])}</span></div>'
        for c in snap["checks"]
    )
    cpu_line = "; ".join(
        f"<code>{E(q['name'])}</code> {num(q['used'])} {unit(q)} in use, "
        f"{num(q['admitted'] + q['dispatching'])} running, {num(q['pending'])} pending"
        for q in other
    )
    return LIVE_PAGE.substitute(
        generated=snap["generated_at"],
        sources=render_sources(snap["sources"]),
        nav=nav,
        errors=render_errors(snap["sources"]),
        glossary=GLOSSARY,
        checks=checks,
        summary=render_live_summary(snap),
        cpu_line=cpu_line or "none",
        queues="".join(render_live_queue(q) for q in tpu),
        events=render_events(snap),
        jobs=render_jobs(snap),
        other="".join(render_live_queue(q) for q in other),
    )


# --------------------------------------------------------------------------
# History


def build_history(
    cfg: Config, queues: list, pools: list, hist: dict, stats: dict, span: dict
) -> dict:
    start, end, step = span["start"], span["end"], span["step"]
    ticks = list(range(start, end + 1, step))
    seconds = end - start

    def history(key: str, *labels: str) -> list:
        return resample(hist.get(key, {}).get("|".join(labels), []), ticks, step)

    by_queue_outcome: dict[str, dict] = {}
    by_pipeline: dict[str, dict] = {}
    for row in stats.get("outcomes", []):
        n, chip_hours = int(row["n"]), float(row["chip_hours"] or 0)
        for table, key in (
            (by_queue_outcome, row["queue"]),
            (by_pipeline, row["pipeline"]),
        ):
            entry = table.setdefault(
                key, {"finished": 0, "chip_hours": 0.0, "outcomes": {}}
            )
            entry["finished"] += n
            entry["chip_hours"] += chip_hours
            entry["outcomes"][row["outcome"]] = (
                entry["outcomes"].get(row["outcome"], 0) + n
            )
    timings = {r["queue"]: r for r in stats.get("queues", [])}

    def outcome_summary(entry: dict | None) -> dict:
        entry = entry or {"finished": 0, "chip_hours": 0.0, "outcomes": {}}
        outcomes = entry["outcomes"]
        infra = {k: v for k, v in outcomes.items() if k not in TEST_OUTCOMES}
        return {
            "finished": entry["finished"],
            "succeeded": outcomes.get("succeeded", 0),
            "failed": outcomes.get("failed", 0),
            "infra": sum(infra.values()),
            "infra_outcomes": sorted(infra.items(), key=lambda kv: -kv[1]),
            "chip_hours": entry["chip_hours"],
        }

    def seconds_or_none(value) -> float | None:
        return None if value in (None, "") else float(value)

    rows = []
    for q in sort_queues(queues):
        used = history("used", q["name"], q["resource"])
        t = timings.get(q["name"], {})
        rows.append(
            {
                **q,
                "history": {
                    "used": used,
                    "nominal": history("nominal", q["name"], q["resource"]),
                    "pending": history("pending", q["name"]),
                },
                # Not utilization: against the queue's own nominal, a queue that
                # lives on borrowed chips reads hundreds of percent.
                "mean_used": mean(used),
                "stats": {
                    **outcome_summary(by_queue_outcome.get(q["name"])),
                    "evictions": hist.get("evictions", {}).get(q["name"], 0.0),
                    "requeues": int(t.get("requeues") or 0),
                    "redispatches": int(t.get("redispatches") or 0),
                    **{
                        k: seconds_or_none(t.get(k))
                        for k in (
                            "wait_p50",
                            "wait_p90",
                            "startup_p50",
                            "startup_p90",
                            "run_p50",
                        )
                    },
                },
            }
        )

    # Every TPU node that existed in the range, summed by the queue its pool
    # serves.
    by_hash = pool_index(pools)
    agg: dict = {}
    for key, points in hist.get("nodes", {}).items():
        p = pool_of(by_hash, key)
        if not p:
            continue
        values = exact(points, ticks)
        seen = [i for i, v in enumerate(values) if v]
        if not seen:
            continue
        a = agg.setdefault(
            p["queue"],
            {
                "nodes": [0.0] * len(ticks),
                "chips": [0.0] * len(ticks),
                "created": 0,
                "lifetimes": [],
            },
        )
        # A step's share; a subquery boundary can add one sample.
        values = [None if v is None else min(v, 1.0) for v in values]
        for i, v in enumerate(values):
            if v:
                a["nodes"][i] += v
                a["chips"][i] += v * p["chips_per_node"]
        # Appeared after the range began: a node the pool scaled up for.
        if seen[0] > 0:
            a["created"] += 1
            # And gone before it ended: a whole lifetime.
            if seen[-1] < len(ticks) - 1:
                a["lifetimes"].append(sum(v or 0 for v in values) * step)
    pool_bounds: dict = {}
    for p in pools:
        b = pool_bounds.setdefault(
            p["queue"], {"pools": 0, "max": 0, "multi_host": p["multi_host"]}
        )
        b["pools"] += 1
        b["max"] += p["max_nodes"]
    hours = step / 3600
    for q in rows:
        a, b = agg.get(q["name"]), pool_bounds.get(q["name"])
        if not b:
            q["nodes"] = None
            continue
        a = a or {
            "nodes": [0.0] * len(ticks),
            "chips": [0.0] * len(ticks),
            "created": 0,
            "lifetimes": [],
        }
        used = [u or 0.0 for u in q["history"]["used"]]
        on_hours = sum(a["chips"]) * hours
        held_hours = sum(min(c, u) for c, u in zip(a["chips"], used)) * hours
        q["nodes"] = {
            **b,
            "chips": [round(v, 1) for v in a["chips"]],
            "count": [round(v, 1) for v in a["nodes"]],
            "created": a["created"],
            "lifetime_p50": statistics.median(a["lifetimes"])
            if a["lifetimes"]
            else None,
            "peak": max(a["nodes"], default=0.0),
            "node_hours": sum(a["nodes"]) * hours,
            "chip_hours": on_hours,
            "idle_chip_hours": max(0.0, on_hours - held_hours),
            "held_share": held_hours / on_hours if on_hours else None,
        }

    cohorts: dict[str, dict] = {}
    for q in rows:
        if q["resource"] != TPU:
            continue
        c = cohorts.setdefault(
            q["cohort"],
            {
                "name": q["cohort"],
                "family": q["family"],
                "generation": q["generation"],
                "members": [],
            },
        )
        c["members"].append(q)
    for c in cohorts.values():
        c["admitted"] = add_series(*(q["history"]["used"] for q in c["members"]))
        c["nominal"] = add_series(*(q["history"]["nominal"] for q in c["members"]))
        model = DUTY_MODELS.get(c["family"], "")
        c["busy"] = history("busy", model) if model else [None] * len(ticks)
        on_nodes = [q["nodes"]["chips"] for q in c["members"] if q["nodes"]]
        c["on_nodes"] = add_series(*on_nodes) if on_nodes else [None] * len(ticks)
        on_hours = sum(q["nodes"]["chip_hours"] for q in c["members"] if q["nodes"])
        idle_hours = sum(
            q["nodes"]["idle_chip_hours"] for q in c["members"] if q["nodes"]
        )
        c["held_share"] = (on_hours - idle_hours) / on_hours if on_hours else None
        c["idle_chip_hours"] = idle_hours
        admitted, nominal, busy = (
            mean(c["admitted"]),
            mean(c["nominal"]),
            mean(c["busy"]),
        )
        c["utilization"] = (
            admitted / nominal if admitted is not None and nominal else None
        )
        c["busy_share"] = busy / nominal if busy is not None and nominal else None
        c["chip_hours"] = (admitted or 0) * seconds / 3600
        c["peak"] = max((v for v in c["admitted"] if v is not None), default=None)
        c["finished"] = sum(q["stats"]["finished"] for q in c["members"])
        c["infra"] = sum(q["stats"]["infra"] for q in c["members"])
        c["failed"] = sum(q["stats"]["failed"] for q in c["members"])
        del c["members"]

    return {
        "span": span,
        "ticks": ticks,
        "queues": rows,
        "cohorts": list(cohorts.values()),
        "pipelines": sorted(
            (
                {"pipeline": name, **outcome_summary(entry)}
                for name, entry in by_pipeline.items()
            ),
            key=lambda p: -p["chip_hours"],
        ),
    }


def success_rate(r: dict) -> str:
    decided = r["succeeded"] + r["failed"] + r["infra"]
    return pct(r["succeeded"] / decided) if decided else "-"


def infra_cell(r: dict) -> str:
    if not r["infra"]:
        return '<td class="n">0</td>'
    detail = ", ".join(f"{k} {v}" for k, v in r["infra_outcomes"])
    return f'<td class="n"><b>{num(r["infra"])}</b><div class="sub-row">{E(detail)}</div></td>'


def render_history(h: dict, sources: dict, query: str) -> str:
    span = h["span"]
    fmt = "time" if span["end"] - span["start"] <= 2 * 86400 else "day"
    every = max(1, len(h["ticks"]) // 24)
    ticks = h["ticks"]

    cohorts = "".join(
        f"""<div class="card">
  <div class="card-head"><h2>{E(c["generation"])} chips</h2><span class="sub">cohort <code>{E(c["name"])}</code></span></div>
  <div class="tiles">
    <div class="tile"><span>Admitted, average</span><b>{pct(c["utilization"])}</b><small>of nominal</small></div>
    <div class="tile"><span>Busy, average</span><b>{pct(c["busy_share"])}</b><small>of nominal, by TensorCore duty</small></div>
    <div class="tile"><span>Node chips held</span><b>{pct(c["held_share"])}</b><small>{num(c["idle_chip_hours"])} chip-hours on nodes idle</small></div>
    <div class="tile"><span>Workloads finished</span><b>{num(c["finished"])}</b><small>{num(c["failed"])} test, {num(c["infra"])} infra failures</small></div>
  </div>
  {line_chart("Chips on nodes, admitted and busy", ticks, [("nodes", "on nodes", c["on_nodes"]), ("used", "admitted", c["admitted"]), ("busy", "busy", c["busy"])], ref=("nominal", c["nominal"]), fmt=fmt, width=1080, height=240)}
  {values_table(ticks, [("On nodes", c["on_nodes"]), ("Admitted", c["admitted"]), ("Busy", c["busy"]), ("Nominal", c["nominal"])], every, "Values")}
</div>"""
        for c in h["cohorts"]
    )

    tpu = [q for q in h["queues"] if q["resource"] == TPU]
    queue_rows = "".join(
        f"""<tr><td class="nowrap">{queue_link(q)}</td>
<td class="n">{num(q["stats"]["finished"])}</td><td class="n">{success_rate(q["stats"])}</td>
<td class="n">{num(q["stats"]["failed"])}</td>{infra_cell(q["stats"])}
<td class="n">{num(q["stats"]["evictions"])}</td>
<td class="n nowrap">{num(q["stats"]["requeues"])} / {num(q["stats"]["redispatches"])}</td>
<td class="n nowrap">{ago(q["stats"]["wait_p50"])} / {ago(q["stats"]["wait_p90"])}</td>
<td class="n nowrap">{ago(q["stats"]["startup_p50"])} / {ago(q["stats"]["startup_p90"])}</td>
<td class="n">{ago(q["stats"]["run_p50"])}</td>
<td class="n">{num(q["stats"]["chip_hours"])}</td></tr>"""
        for q in tpu
    )
    autoscaled = [q for q in tpu if q.get("nodes")]
    node_rows = (
        "".join(
            f"""<tr><td class="nowrap">{queue_link(q)}</td>
<td class="n">{q["nodes"]["pools"]}{" slices" if q["nodes"]["multi_host"] else ""} · max {num(q["nodes"]["max"])}</td>
<td class="n"><b>{num(q["nodes"]["created"])}</b></td><td class="n">{ago(q["nodes"]["lifetime_p50"])}</td>
<td class="n">{num(q["nodes"]["peak"])}</td><td class="n">{num(q["nodes"]["node_hours"])}</td>
<td class="n">{num(q["nodes"]["chip_hours"])}</td><td class="n">{pct(q["nodes"]["held_share"])}</td>
<td class="n">{num(q["nodes"]["idle_chip_hours"])}</td>
<td class="n nowrap">{ago(q["stats"]["startup_p50"])} / {ago(q["stats"]["startup_p90"])}</td></tr>"""
            for q in autoscaled
        )
        or '<tr><td colspan="10" class="empty">No node pool data.</td></tr>'
    )
    autoscaling = f"""<div class="table-wrap"><table class="dense"><thead><tr><th>Topology</th><th class="n">Pools</th>
<th class="n">Nodes created</th><th class="n">Node lifetime p50</th><th class="n">Peak nodes</th><th class="n">Node-hours</th>
<th class="n">Chip-hours on nodes</th><th class="n">Held by workloads</th><th class="n">Idle chip-hours</th>
<th class="n">Admitted → running p50 / p90</th></tr></thead><tbody>{node_rows}</tbody></table></div>
<p class="muted note">From GKE's per-node metrics: every TPU node that existed in the range, tied to its pool by the
pool's instance group. <i>Nodes created</i> counts nodes that appeared during the range, each one a node the pool scaled
up for; <i>lifetime</i> is appearing to disappearing, for nodes that did both. <i>Idle chip-hours</i> are chips on nodes
that no admitted workload held - scale-down lag, a pool's minimum, or a node waiting for the workload it came for.
<i>Admitted → running</i> includes the wait for a node when the pool had to scale up.</p>"""

    pipeline_rows = (
        "".join(
            f"""<tr><td>{E(p["pipeline"])}</td><td class="n">{num(p["finished"])}</td><td class="n">{success_rate(p)}</td>
<td class="n">{num(p["failed"])}</td>{infra_cell(p)}<td class="n">{num(p["chip_hours"])}</td></tr>"""
            for p in h["pipelines"][:20]
        )
        or '<tr><td colspan="6" class="empty">No finished workloads.</td></tr>'
    )
    outcomes = f"""
<h3>By queue</h3>
<div class="table-wrap"><table class="dense"><thead><tr><th>Queue</th><th class="n">Finished</th><th class="n">Success</th>
<th class="n">Test failures</th><th class="n">Infra failures</th><th class="n">Evictions</th>
<th class="n">Requeues / redispatches</th><th class="n">Wait p50 / p90</th><th class="n">Startup p50 / p90</th>
<th class="n">Run p50</th><th class="n">Chip-hours</th></tr></thead>
<tbody>{queue_rows}</tbody></table></div>
<p class="muted note">Success is over workloads that finished either way; cancelled ones are left out. Wait is
submitted to quota reserved; startup is admitted to the first workload container running.</p>
<h3 style="margin-top:20px">By pipeline</h3>
<div class="table-wrap"><table class="dense"><thead><tr><th>Pipeline</th><th class="n">Finished</th><th class="n">Success</th>
<th class="n">Test failures</th><th class="n">Infra failures</th><th class="n">Chip-hours</th></tr></thead>
<tbody>{pipeline_rows}</tbody></table></div>"""

    def queue_card(q: dict) -> str:
        u = unit(q)
        return f"""<div class="card queue{"" if q["resource"] == TPU else " other"}" id="{E(q["name"])}">
  <div class="card-head"><h2 title="{E(q["name"])}">{E(queue_title(q))}</h2>
    <span class="sub">{num(q["stats"]["finished"])} finished · avg {num(q["mean_used"])} {u}</span></div>
  <div class="charts">
  {line_chart(f"{u.capitalize()} on nodes and in use" if q.get("nodes") else f"{u.capitalize()} in use", ticks, ([("nodes", "on nodes", q["nodes"]["chips"])] if q.get("nodes") else []) + [("used", "in use", q["history"]["used"])], ref=("nominal", q["history"]["nominal"]) if q["resource"] == TPU else None, fmt=fmt, width=520, height=180)}
  {line_chart("Workloads pending", ticks, [("pending", "pending", q["history"]["pending"])], fmt=fmt, width=520, height=180)}
  </div>
  {values_table(ticks, ([("On nodes", q["nodes"]["chips"])] if q.get("nodes") else []) + [(f"{u.capitalize()} in use", q["history"]["used"]), ("Nominal", q["history"]["nominal"]), ("Pending", q["history"]["pending"])], every, "Values")}
</div>"""

    presets = "".join(
        f'<a class="seg-btn{" active" if span.get("preset") == name else ""}" href="?preset={name}">{label}</a>'
        for name, label in PRESET_LABELS.items()
    )
    return HISTORY_PAGE.substitute(
        sources=render_sources(sources),
        errors=render_errors(sources),
        glossary=GLOSSARY,
        presets=presets,
        start=span["start"],
        end=span["end"],
        step=ago(span["step"]),
        cohorts=cohorts,
        autoscaling=autoscaling,
        outcomes=outcomes,
        queues="".join(queue_card(q) for q in h["queues"]),
        query=E(query),
    )
