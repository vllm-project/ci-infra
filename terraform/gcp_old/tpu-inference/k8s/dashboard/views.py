"""Turns what fleet.py read into the two pages: the live view and history."""

from __future__ import annotations

import collections
import hashlib
import html
import json
import re
import statistics
import string
import time
import urllib.parse
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
OVERVIEW_PAGE = string.Template((TEMPLATES / "overview.html").read_text())
BASELINE_PAGE = string.Template((TEMPLATES / "baseline.html").read_text())
# The bare-metal fleet before the migration, built once from the snapshot by
# baseline/build_baseline.py; static, since that fleet is being torn down.
BASELINE_FILE = Path(__file__).parent / "baseline" / "premigration-2026-09.json"
GLOSSARY = (TEMPLATES / "glossary.html").read_text()
# In the stylesheet and script URLs, so a browser holding the last version
# for its five minutes fetches the new one as soon as the page changes.
ASSETS = hashlib.sha256(
    b"".join(
        p.read_bytes() for p in sorted((Path(__file__).parent / "static").iterdir())
    )
).hexdigest()[:10]

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

# A node that has just booted can be handed a pod before it is fully up, and
# the kubelet retries with backoff until it is: the gcsfuse CSI driver not yet
# registered, the GKE metadata server not yet answering the gcsfuse sidecar,
# the node not yet authorized for the pod's PVC, or the service-account token's
# ConfigMap watch not yet synced. Measured 2026-10-08 on us-east5: cleared
# within 2 s to 2 min, and the pods started on the new-node path. Routine while
# it repeats a handful of times per pod; a mount that keeps failing repeats
# every couple of minutes and stays a fleet problem.
NODE_BOOT = (
    "driver name gcsfuse.csi.storage.gke.io not found in the list of registered CSI drivers",
    "failed to setup metadata service",
    "no relationship found between node",
    "failed to sync configmap cache: timed out waiting for the condition",
)
NODE_BOOT_REPEATS = 10

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

# History ranges offered as presets. /api/history also takes ?start=&end= in
# epoch seconds, for a range the presets do not cover.
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
    if not r.get("pipeline"):
        return '<span class="muted">no build</span>'
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


def live_nodes(pools: list, present: dict, busy: dict) -> dict:
    """Per Kueue queue: TPU nodes up now, the chips computing on them, and the
    pools' bounds."""
    by_hash = pool_index(pools)
    per_pool = collections.Counter()
    for key in present:
        p = pool_of(by_hash, key)
        if p:
            per_pool[(p["cluster"], p["name"])] += 1
    busy_pool = collections.Counter()
    for key, value in busy.items():
        p = pool_of(by_hash, key)
        if p:
            busy_pool[(p["cluster"], p["name"])] += value
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
                "busy": 0.0,
            },
        )
        up = per_pool[(p["cluster"], p["name"])]
        n["up"] += up
        n["chips"] += up * p["chips_per_node"]
        n["busy"] += busy_pool[(p["cluster"], p["name"])]
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


# What each data source is, for the Overview's Data sources section: what it
# reads, through what, the grant it needs, and where to look when it fails.
SOURCES = {
    "Kueue": {
        "through": "Connect Gateway to the manager, as the dashboard's service account",
        "reads": [
            "ClusterQueues and LocalQueues",
            "Workloads",
            "MultiKueue clusters and admission checks",
            "Jobs and JobSets",
            "Agent pods",
            "Each queue's pending order (visibility API)",
        ],
        "needs": [
            "roles/gkehub.gatewayReader",
            "tpu-ci-dashboard ClusterRole and Role - "
            "kueue/generated/manager/workload/40-dashboard-rbac.yaml, applied by hand",
        ],
        "fails": [
            "403 from the gateway: the IAM grant",
            "403 naming a resource: the RBAC file is not applied",
        ],
    },
    "Buildkite": {
        "through": "the Buildkite REST API",
        "reads": [
            "Running and scheduled builds of this cluster's pipelines",
            "Their kube jobs",
        ],
        "needs": [
            "The REST token in Secret Manager named by dashboard_buildkite_token_secret_id, "
            "shared with the Buildkite-to-BigQuery puller",
        ],
        "fails": [
            "401: the token expired or was revoked - add a secret version, roll the service"
        ],
    },
    "Events": {
        "through": "Connect Gateway to each cluster",
        "reads": ["Events in the buildkite namespace of every cluster"],
        "needs": [
            "tpu-ci-dashboard Role on each worker, under its generated workload directory"
        ],
        "fails": [
            "A cluster that cannot be read is named on Live; the rest still show"
        ],
    },
    "Metrics": {
        "through": "the Managed Prometheus query API",
        "reads": [
            "Kueue: quota, usage, pending, evictions",
            "agent-stack-k8s: polling and job creates",
            "Nodes: presence and TensorCore duty cycle",
        ],
        "needs": ["roles/monitoring.viewer"],
        "fails": [
            "The error names the query",
            "A cluster missing from Kueue up: its PodMonitoring "
            "(system/30-monitoring.yaml) is not applied",
        ],
    },
    "GKE": {
        "through": "the GKE API, hourly",
        "reads": ["Every cluster's node pools: shape, bounds, instance groups"],
        "needs": ["roles/container.clusterViewer on the project"],
        "fails": ["Node counts and the node autoscaling table go blank"],
    },
    "BigQuery": {
        "through": "BigQuery",
        "reads": [
            "Workload outcomes and timings: ci_efficiency_metrics.kube_workload_timing"
        ],
        "needs": ["roles/bigquery.dataViewer on the dataset", "roles/bigquery.jobUser"],
        "fails": ["Only the 24-hour checks and History's outcomes go blank"],
    },
}


# Which of fleet_links' entries open each source.
SOURCE_LINKS = {
    "Kueue": ["cluster:manager"],
    "Buildkite": ["buildkite", "secret"],
    "Events": ["events"],
    "Metrics": ["metrics"],
    "GKE": ["gke"],
    "BigQuery": ["bigquery"],
}


def out_links(items: list) -> str:
    """Links that leave the dashboard, in a new tab."""
    return " · ".join(
        f'<a href="{E(url)}" target="_blank" rel="noopener">{E(label)} ↗</a>'
        for label, url in items
    )


def source_id(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower())


def render_source_table(sources: dict, links: dict) -> str:
    """The Overview's Data sources section, one entry per source."""

    def items(values: list) -> str:
        return "<ul>" + "".join(f"<li>{E(v)}</li>" for v in values) + "</ul>"

    seen, entries = set(), []
    for s in sources.values():
        if s["name"] in seen:
            continue
        seen.add(s["name"])
        info = SOURCES.get(s["name"], {})
        opens = [
            item
            for key in SOURCE_LINKS.get(s["name"], [])
            for item in links.get(key, [])
        ]
        state = "fail" if s["error"] else "ok"
        last = (
            f'<time data-ts="{s["at"]}" data-fmt="time"></time>'
            if s.get("at")
            else "never"
        )
        error = (
            f'<p class="err-line"><b class="err">{E(s["error"])}</b></p>'
            if s["error"]
            else ""
        )
        entries.append(
            f"""<div class="source {state}" id="source-{source_id(s["name"])}">
  <div class="source-head"><span class="pill {state}"><span class="dot"></span>{E(s["name"])}{" failed" if s["error"] else ""}</span>
    <span class="muted">through {E(info.get("through", ""))} · last read {last}</span></div>
  {f'<p class="source-links">{out_links(opens)}</p>' if opens else ""}
  {error}<div class="source-cols">
    <div><h4>Reads</h4>{items(info.get("reads", []))}</div>
    <div><h4>Needs</h4>{items(info.get("needs", []))}</div>
    <div><h4>If it fails</h4>{items(info.get("fails", []))}</div>
  </div>
</div>"""
        )
    return f"""<div class="section-label" id="sources">Data sources</div>
<div class="card">
  <p class="muted">Whether the dashboard could read each source on its last try - not whether the
  fleet is healthy, which Health on Live says. A source that fails keeps its last good data on
  every page, under a banner at the top. The full error is in {out_links(links.get("logs", []))};
  grants are in {out_links(links.get("iam", []))}.</p>
  <div class="sources-list">{"".join(entries)}</div>
</div>"""


def render_errors(sources: dict) -> str:
    errors = "".join(
        f'<p class="error">{E(s["name"])} failed: {E(s["error"])}'
        + (
            f' - showing data from <time data-ts="{s["at"]}"></time>'
            if s.get("at")
            else ""
        )
        + f' · <a href="./#source-{source_id(s["name"])}">what it reads and how to fix it</a></p>'
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
            "priority_class": None,
            "pending": 0,
            "running": 0,
            "held": 0.0,
            "oldest_wait": None,
            "running_since": None,
        }

    by_name = {w["name"]: w for w in kq["workloads"]}
    bk_jobs = {j["id"]: j for j in bk["jobs"]}

    def next_up(q: dict, wls: list) -> dict | None:
        """The first of a queue's pending workloads in Kueue's order, and what
        priorities the rest carry."""
        order = (kq.get("next_up") or {}).get(q["name"])
        if order is None:
            return None
        rows = []
        for item in order["items"]:
            w = by_name.get(item["workload"])
            rows.append(
                {
                    "position": item["position"] + 1,
                    "priority": item["priority"],
                    "priority_class": w["priority_class"] if w else None,
                    "build": build_row(w["pipeline"], w["number"]) if w else None,
                    "job": bk_jobs.get(w["job_id"].replace("-", "")) if w else None,
                    "workload": item["workload"],
                    "waiting": now - w["created"] if w and w["created"] else None,
                }
            )
        listed = {r["workload"] for r in rows}
        rest = collections.Counter(
            (w["pipeline"], w["number"], w["priority"], w["priority_class"])
            for w in wls
            if w["state"] == "pending" and w["name"] not in listed
        )
        return {
            "rows": rows,
            "rest": [
                {
                    "build": build_row(pipeline, number),
                    "priority": value,
                    "priority_class": name,
                    "count": count,
                }
                for (pipeline, number, value, name), count in sorted(
                    rest.items(), key=lambda kv: (-kv[0][2], -kv[1])
                )
            ],
            "error": order["error"],
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
            # A step can name its own class, so a build shows its highest.
            if row["priority"] is None or w["priority"] > row["priority"]:
                row["priority"] = w["priority"]
                row["priority_class"] = w["priority_class"]
            if w["state"] == "pending":
                row["pending"] += 1
                row["oldest_wait"] = max(row["oldest_wait"] or 0, now - w["since"])
                reason = pending_reason(w["reason"])
                reasons[reason] = reasons.get(reason, 0) + 1
            else:
                row["running"] += 1
                row["held"] += w["amount"]
                if w["since"]:
                    row["running_since"] = min(
                        row["running_since"] or w["since"], w["since"]
                    )
        for row in rows.values():
            row["running_for"] = (
                now - row["running_since"] if row["running_since"] else None
            )
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
                "next_up": next_up(q, wls) if pending else None,
                # Pending is the next-up list's; this is what holds the chips.
                "builds": sorted(
                    (r for r in rows.values() if r["running"]),
                    key=lambda r: (-r["held"], -(r["priority"] or 0)),
                ),
            }
        )
    queues = sort_queues(queues)
    nodes = live_nodes(
        data.get("pools") or [], health.get("nodes", {}), health.get("busy", {})
    )
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
        members = [
            q["nodes"] for q in queues if q["cohort"] == c["name"] and q["nodes"]
        ]
        c["on_nodes"] = sum(n["chips"] for n in members) if members else None
        c["busy"] = sum(n["busy"] for n in members) if members else None

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
    snapshot["fleet"] = fleet_map(cfg, kq, health, data.get("pools") or [], snapshot)
    return snapshot


def console(path: str, project: str, **params: str) -> str:
    query = urllib.parse.urlencode({"project": project, **params})
    return f"https://console.cloud.google.com/{path}?{query}"


def fleet_links(cfg: Config) -> dict:
    """Where each component and data source lives, for the Overview's links:
    id -> [(label, url)]. Every cluster is taken to be in the dashboard's project."""
    p = cfg.project
    links: dict = {
        "buildkite": [
            (
                "Cluster queues in Buildkite",
                f"https://buildkite.com/organizations/{cfg.org}/clusters/{cfg.cluster_id}/queues",
            )
        ],
        "metrics": [("Metrics Explorer", console("monitoring/metrics-explorer", p))],
        "gke": [("Clusters in GKE", console("kubernetes/list/overview", p))],
        "iam": [("IAM", console("iam-admin/iam", p))],
        "events": [
            (
                "Events in Logs Explorer",
                console(
                    "logs/query;query="
                    + urllib.parse.quote(
                        f'logName="projects/{p}/logs/events"\n'
                        f'resource.labels.namespace_name="{cfg.namespace}"',
                        safe="",
                    ),
                    p,
                ),
            )
        ],
        "logs": [
            (
                "This service's logs",
                console(
                    "logs/query;query="
                    + urllib.parse.quote(
                        'resource.type="cloud_run_revision"\n'
                        f'resource.labels.service_name="{cfg.service}"',
                        safe="",
                    ),
                    p,
                ),
            )
        ],
    }
    if cfg.token_secret:
        links["secret"] = [
            (
                "Token in Secret Manager",
                console(
                    f"security/secret-manager/secret/{cfg.token_secret}/versions", p
                ),
            )
        ]
    if cfg.timing_table.count(".") == 2:
        bq_project, dataset, table = cfg.timing_table.split(".")
        links["bigquery"] = [
            (
                f"{table} in BigQuery",
                # ws is the console's own path syntax; its ! must stay literal.
                console("bigquery", bq_project)
                + f"&ws=!1m5!1m4!4m3!1s{bq_project}!2s{dataset}!3s{table}",
            )
        ]
    for c in cfg.clusters:
        where = cluster_location(c)
        base = f"kubernetes/clusters/details/{where}/{c['name']}"
        links[f"cluster:{c['name']}"] = [
            (f"{c['name']} in GKE", console(f"{base}/details", p)),
            ("Node pools", console(f"{base}/nodes", p)),
        ]
    return links


def cluster_location(c: dict) -> str:
    m = re.search(r"/locations/([^/]+)/gkeMemberships/", c.get("gateway", ""))
    return m[1] if m else ""


def fleet_map(cfg: Config, kq: dict, health: dict, pools: list, snap: dict) -> dict:
    """What the overview draws: the manager, each worker with its node pools,
    and how many steps are at each stage."""
    kueue_up = health.get("kueue_up", {})
    links = {w["name"]: w for w in kq["workers"]}
    names = [c["name"] for c in cfg.clusters]
    manager = next(
        (n for n in names if n not in links),
        cfg.metrics_cluster,
    )
    titles = {q["name"]: queue_title(q) for q in snap["queues"]}
    order = {q["name"]: i for i, q in enumerate(snap["queues"])}
    tpu_queues = {q["name"] for q in snap["queues"] if q["resource"] == TPU}

    # Nodes up and chips computing per pool, then per (cluster, queue).
    by_hash = pool_index(pools)
    up, busy = collections.Counter(), collections.Counter()
    for key in health.get("nodes", {}):
        p = pool_of(by_hash, key)
        if p:
            up[(p["cluster"], p["name"])] += 1
    for key, value in health.get("busy", {}).items():
        p = pool_of(by_hash, key)
        if p:
            busy[(p["cluster"], p["name"])] += value
    shapes: dict = {}
    for p in pools:
        row = shapes.setdefault(
            (p["cluster"], p["queue"]),
            {
                "queue": p["queue"],
                "title": titles.get(p["queue"], p["queue"]),
                "multi_host": p["multi_host"],
                "pools": 0,
                "pools_up": 0,
                "up": 0,
                "max": 0,
                "chips": 0.0,
                "max_chips": 0,
                "busy": 0.0,
            },
        )
        n = up[(p["cluster"], p["name"])]
        row["pools"] += 1
        row["pools_up"] += bool(n)
        row["up"] += n
        row["max"] += p["max_nodes"]
        row["chips"] += n * p["chips_per_node"]
        row["max_chips"] += p["max_nodes"] * p["chips_per_node"]
        row["busy"] += busy[(p["cluster"], p["name"])]

    steps = collections.Counter(j["real"] for j in snap["jobs"])
    on_cluster = collections.defaultdict(collections.Counter)
    for j in snap["jobs"]:
        if j.get("cluster"):
            on_cluster[j["cluster"]][j["real"]] += 1
    held = collections.Counter()
    for w in kq["workloads"]:
        if w["cluster"] and w["state"] != "pending" and w["queue"] in tpu_queues:
            held[w["cluster"]] += w["amount"]
    problems = collections.Counter(g["cluster"] for g in snap["events"] if g["fleet"])

    workers = []
    for c in cfg.clusters:
        if c["name"] == manager:
            continue
        name = c["name"]
        link = links.get(name)
        rows = sorted(
            (r for (cl, _), r in shapes.items() if cl == name),
            key=lambda r: order.get(r["queue"], len(order)),
        )
        workers.append(
            {
                "name": name,
                "location": cluster_location(c),
                "kueue_up": kueue_up.get(name),
                "connected": link["active"] if link else None,
                "link_message": link["message"] if link else "",
                "starting": on_cluster[name]["starting"],
                "running": on_cluster[name]["running"],
                "held": held[name],
                "problems": problems[name],
                "shapes": rows,
            }
        )
    return {
        "manager": {
            "name": manager,
            "location": cluster_location(
                next((c for c in cfg.clusters if c["name"] == manager), {})
            ),
            "kueue_up": kueue_up.get(manager),
        },
        "workers": workers,
        "queue": cfg.queue,
        "links": fleet_links(cfg),
        "steps": dict(steps),
        # Finished agent pods linger until agent-stack-k8s collects them.
        "agent_pods": sum(
            1 for p in kq["pods"] if p["phase"] not in ("Succeeded", "Failed")
        ),
    }


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
                "cluster": w["cluster"] if w else None,
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
            and not (
                any(m in g["message"] for m in NODE_BOOT)
                and g["count"] <= NODE_BOOT_REPEATS * max(1, g["objects"])
            )
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
            if c["on_nodes"]
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


def pending_reason(message: str) -> str:
    """Kueue's reason for a pending workload, in fewer words.

    Only the head of a queue has one. "Couldn't assign flavors to pod set X:
    insufficient unused quota ... in flavor F" repeats per pod set and carries a
    per-workload "N more needed", so the same cause would read as several.
    """
    if not message:
        return "queued behind the head of the queue"
    short = re.findall(r"insufficient unused quota for \S+ in flavor ([\w-]+)", message)
    leftover = re.sub(
        r"couldn't assign flavors to pod set [\w-]+: insufficient unused quota for "
        r"\S+ in flavor [\w-]+(, \d+ more needed)?;?\s*",
        "",
        message,
    )
    if short and not leftover.strip():
        return f"not enough unused {', '.join(sorted(set(short)))} quota, borrowing included"
    return re.sub(r", \d+ more needed", "", message)


def show_more(target: str, rows: str, n: int) -> tuple:
    """A hidden tbody of the rows a table keeps back, and the button that shows
    them; both empty when there are none."""
    if not n:
        return "", ""
    return (
        f'<tbody id="{E(target)}" hidden>{rows}</tbody>',
        f'<button type="button" class="show-more" data-target="{E(target)}" '
        f'data-more="Show {n} more" data-fewer="Show fewer">Show {n} more</button>',
    )


def split_rows(rows: list, target: str, empty: str) -> tuple:
    """A long table's first FIRST_ROWS rows, a hidden tbody with the rest, and
    the button that shows them."""
    shown = "".join(rows[:FIRST_ROWS]) or empty
    hidden, button = show_more(
        target, "".join(rows[FIRST_ROWS:]), max(0, len(rows) - FIRST_ROWS)
    )
    return shown, hidden, button


def render_next_up(q: dict) -> str:
    n = q.get("next_up")
    if not n:
        return ""
    if n["error"] and not n["rows"]:
        return f'<p class="muted note">Kueue\'s order unavailable: {E(n["error"])}</p>'

    def row(r: dict) -> str:
        return f"""<tr><td class="n">{r["position"]}</td>
<td>{job_link(r["job"]) if r["job"] else f'<span class="muted">{E(r["workload"])}</span>'}</td>
<td class="nowrap">{build_link(r["build"]) if r["build"] and r["build"]["number"] else ""}</td>
<td class="nowrap">{priority_cell(r)}</td><td class="n">{ago(r["waiting"])}</td></tr>"""

    rows, more_rows, more_button = split_rows(
        [row(r) for r in n["rows"]], f"next-{q['name']}", ""
    )
    rest = sum(r["count"] for r in n["rest"])
    more = (
        f"+{rest} more pending: "
        + "; ".join(
            f"{r['count']} from {build_link(r['build'])} at {priority_cell(r)}"
            for r in n["rest"]
        )
        + ". "
        if rest
        else ""
    )
    return f"""<div class="block"><h4>Next up, in Kueue's order</h4>
  <div class="table-wrap"><table class="dense"><thead><tr><th class="n">#</th><th>Step</th><th>Build</th>
    <th>Priority</th><th class="n">Waiting</th></tr></thead><tbody>{rows}</tbody>{more_rows}</table></div>
  {more_button}
  <p class="muted note">{more}Kueue considers higher priority first, then earlier submission. A workload
  behind the head can start first when the head does not fit.</p></div>"""


def priority_cell(r: dict) -> str:
    value = f' <span class="muted">({num(r["priority"])})</span>'
    if r["priority_class"]:
        return f"{E(r['priority_class'])}{value}"
    return (
        f'<span class="muted" title="No WorkloadPriorityClass">unclassed</span>{value}'
    )


def render_live_queue(q: dict) -> str:
    builds, builds_more, builds_button = split_rows(
        [
            "<tr>"
            f'<td class="nowrap">{build_link(r)}</td><td class="branch">{E(r["branch"])}</td><td>{E(r["source"])}</td>'
            f'<td class="nowrap">{priority_cell(r)}</td>'
            f'<td class="n">{num(r["running"])}</td>'
            f'<td class="n">{num(r["held"])}</td>'
            f'<td class="n">{ago(r["running_for"])}</td></tr>'
            for r in q["builds"]
        ],
        f"run-{q['name']}",
        '<tr><td colspan="7" class="empty">Nothing running.</td></tr>',
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
  <div class="block"><h4>Running, by build</h4>
  <div class="table-wrap"><table class="dense"><thead><tr><th>Build</th><th>Branch</th><th>Source</th><th>Priority</th>
    <th class="n">Steps</th><th class="n">{u.capitalize()} held</th><th class="n">Running for</th>
  </tr></thead><tbody>{builds}</tbody>{builds_more}</table></div>{builds_button}</div>
  {render_next_up(q)}
  {f'<div class="reasons block"><h4>Why workloads are pending</h4><ul>{reasons}</ul></div>' if reasons else ""}
</div>"""


def render_events(snap: dict) -> str:
    def table(groups: list, empty: str, target: str) -> str:
        body, hidden, button = split_rows(
            [
                f"""<tr><td class="nowrap"><time data-ts="{g["last"]}" data-fmt="time"></time></td><td class="nowrap">{E(g["cluster"])}</td>
<td>{E(g["reason"])}<div class="sub-row">{E(g["kind"])}{" · Warning" if g["type"] == "Warning" else ""}</div></td>
<td class="n">{num(g["count"])}{f'<div class="sub-row">{g["objects"]} objects</div>' if g["objects"] > 1 else ""}</td>
<td class="msg">{E(g["message"][:300])}</td>
<td class="nowrap">{"<br>".join(f'<a href="{E(url)}" target="_blank" rel="noopener">{E(name)}</a>' for name, url in g["builds"][:3])}{f'<div class="sub-row">+{len(g["builds"]) - 3} more</div>' if len(g["builds"]) > 3 else ""}</td></tr>"""
                for g in groups[:60]
            ],
            target,
            f'<tr><td colspan="6" class="empty">{empty}</td></tr>',
        )
        return (
            '<div class="table-wrap"><table class="dense"><thead><tr><th>Last seen</th><th>Cluster</th><th>Reason</th>'
            f'<th class="n">Count</th><th>Message</th><th>Builds</th></tr></thead><tbody>{body}</tbody>{hidden}</table></div>'
            f"{button}"
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
{table(problems, "None in the last hour.", "problems-more")}
<details><summary>Other events ({len(other)} kinds, {sum(g["count"] for g in other):,} events)</summary>
<p class="muted note">Routine warnings and notable normal events: Kueue's backlog, scheduling while pools scale,
mounts retried while a new node finishes booting, scale-ups, preemptions, agent teardown, test failures.</p>
{table(other, "None.", "other-events-more")}</details>"""


def job_link(j: dict) -> str:
    # Every step here is a kube step, so a leading :kubernetes: says nothing.
    label = re.sub(r"^(?::[\w+-]+:\s*)+", "", j["label"] or "") or "(unnamed step)"
    return f'<a href="{E(j["url"])}" target="_blank" rel="noopener">{E(label)}</a>'


# Rows a long table shows before its Show more button: Not running yet, and
# each queue's Next up.
FIRST_ROWS = 5


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

    def waiting_row(g: dict) -> str:
        return (
            f'<tr><td class="nowrap">{build_cell(g)}</td><td class="nowrap"><b>{E(JOB_STATES[g["real"]][0])}</b>'
            "</td>"
            f'<td class="nowrap" title="{E(g["queue_name"])}">{E(g["queue"]) or "-"}</td>'
            f"<td>{steps_cell(g)}</td>"
            f'<td class="n nowrap">{ago(g["oldest"])}</td>'
            f'<td class="msg">{"<br>".join(E(d[:200]) for d in sorted(g["details"])[:2])}</td></tr>'
        )

    # The first rows, furthest from running first; the rest behind a button.
    waiting_groups = groups(waiting)
    waiting_rows, more_rows, more_button = split_rows(
        [waiting_row(g) for g in waiting_groups],
        "waiting-more",
        '<tr><td colspan="6" class="empty">Every kube job is running.</td></tr>',
    )
    running_rows, running_more, running_button = split_rows(
        [
            f'<tr><td class="nowrap">{build_cell(g)}</td><td class="nowrap" title="{E(g["queue_name"])}">{E(g["queue"])}</td>'
            f'<td>{steps_cell(g)}</td><td class="n nowrap">{ago(g["oldest"])}</td></tr>'
            for g in groups(running)
        ],
        "running-more",
        '<tr><td colspan="4" class="empty">None.</td></tr>',
    )
    return f"""{states}
<p class="muted lead">Where each kube job is, grouped by build. Hover a count for what the state means.</p>
<h3>Not running yet <span class="muted">({len(waiting)} jobs)</span></h3>
<div class="table-wrap"><table class="dense"><thead><tr><th>Build</th><th>State</th><th>Queue</th><th>Steps</th>
<th class="n">Longest</th><th>Detail</th></tr></thead><tbody>{waiting_rows}</tbody>{more_rows}</table></div>
{more_button}
<h3 class="jobs-running">Running <span class="muted">({len(running)} jobs)</span></h3>
<div class="table-wrap"><table class="dense"><thead><tr><th>Build</th><th>Queue</th><th>Steps</th><th class="n">Longest</th></tr></thead>
<tbody>{running_rows}</tbody>{running_more}</table></div>{running_button}"""


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
    return LIVE_PAGE.substitute(
        assets=ASSETS,
        generated=snap["generated_at"],
        nav=nav,
        errors=render_errors(snap["sources"]),
        checks=checks,
        summary=render_live_summary(snap),
        queues="".join(render_live_queue(q) for q in tpu),
        events=render_events(snap),
        jobs=render_jobs(snap),
        other="".join(render_live_queue(q) for q in other),
    )


# --------------------------------------------------------------------------
# Overview

RANK = {"ok": 0, "info": 1, "unknown": 2, "warn": 3, "fail": 4}


def worst(items: list) -> str:
    return max((i[0] for i in items), key=RANK.get, default="unknown")


def status_chip(status: str) -> str:
    icon, label = ICONS[status]
    return f'<span class="status-chip {status}"><span class="icon" aria-hidden="true">{icon}</span>{label}</span>'


def check_items(items: list) -> str:
    """Health lines inside a component: (status, title, detail)."""
    return (
        '<ul class="hc">'
        + "".join(
            f'<li class="{st}"><span class="icon" aria-hidden="true">{ICONS[st][0]}</span>'
            f'<span><b>{E(title)}</b><span class="sr-only"> {ICONS[st][1]}</span> - {E(detail)}</span></li>'
            for st, title, detail in items
        )
        + "</ul>"
    )


def count(n: float, label: str) -> str:
    return f'<div class="count{"" if n else " zero"}"><b>{num(n)}</b><span>{E(label)}</span></div>'


def kueue_line(name: str, value: float | None) -> tuple:
    if value is None:
        return ("unknown", "Kueue", f"no scrape data from {name}")
    if value < 1:
        return ("fail", "Kueue", f"not scraped as up on {name}")
    return ("ok", "Kueue", f"up on {name}")


# The diagram's columns, in viewBox units: x and width of each box.
COLUMNS = {
    "buildkite": (10, 160),
    "agent": (225, 165),
    "kueue": (445, 195),
    "multikueue": (695, 150),
    "worker": (905, 265),
}
# Where a box's bars sit, as fractions of its width: after the longest label
# the box shows, before its right-hand figure.
BARS = {"kueue": (0.26, 0.40), "worker": (0.36, 0.26)}
DIAGRAM_WIDTH = 1180


def bar(x: float, y: float, w: float, frac: float) -> str:
    frac = max(0.0, min(1.0, frac))
    return (
        f'<rect class="track" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="8" rx="4"/>'
        f'<rect class="fill" x="{x:.1f}" y="{y:.1f}" width="{w * frac:.1f}" height="8" rx="4"/>'
    )


class Box:
    """One component in the diagram: a box that links to its details."""

    def __init__(self, key, column, title, sub, big, label, lines, status, caption=""):
        self.key, self.status, self.caption = key, status, caption
        self.x, self.w = COLUMNS[column]
        self.bar = BARS.get(column, (0.4, 0.3))
        self.title, self.sub, self.big, self.label = title, sub, big, label
        # Each line: (text, bar fraction or None, right-hand figure).
        self.lines = lines
        self.h = 112 + 24 * len(lines) + (22 if caption else 0)
        self.y = 0.0

    def svg(self) -> str:
        x, y, w = self.x, self.y, self.w
        icon, word = ICONS[self.status]
        out = [
            f'<rect class="box" x="{x}" y="{y:.1f}" width="{w}" height="{self.h}" rx="14"/>',
            f'<text class="n-title" x="{x + 16}" y="{y + 27:.1f}">{E(self.title)}</text>',
            f'<text class="n-sub" x="{x + 16}" y="{y + 46:.1f}">{E(self.sub)}</text>',
            f'<g class="badge {self.status}"><circle cx="{x + w - 22}" cy="{y + 23:.1f}" r="11"/>'
            f'<text x="{x + w - 22}" y="{y + 27.5:.1f}" text-anchor="middle">{icon}</text></g>',
            f'<text class="n-big{"" if self.big else " zero"}" x="{x + 16}" y="{y + 82:.1f}">{num(self.big)}</text>',
            f'<text class="n-label" x="{x + 16}" y="{y + 101:.1f}">{E(self.label)}</text>',
        ]
        top = y + 128
        if self.caption:
            out.append(
                f'<text class="n-cap" x="{x + w - 14}" y="{top:.1f}" text-anchor="end">{E(self.caption)}</text>'
            )
            top += 22
        for i, (text, frac, figure) in enumerate(self.lines):
            ly = top + 24 * i
            out.append(
                f'<text class="n-line" x="{x + 16}" y="{ly:.1f}">{E(text)}</text>'
            )
            if frac is not None:
                out.append(bar(x + w * self.bar[0], ly - 9, w * self.bar[1], frac))
            if figure:
                out.append(
                    f'<text class="n-line" x="{x + w - 14}" y="{ly:.1f}" text-anchor="end">{E(figure)}</text>'
                )
        return (
            f'<a href="#c-{self.key}" class="node {self.status}">'
            f"<title>{E(self.title)}: {word}. Open its details.</title>{''.join(out)}</a>"
        )

    @property
    def mid(self) -> float:
        return self.y + self.h / 2


def edge(key: str, d: str, n: int, length: float, label: str = "") -> str:
    """A path with dots moving along it while n steps are past it: more steps,
    more dots, as many as its length leaves room for. Same speed on every path."""
    dots = 0 if n <= 0 else max(1, min(1 + n // 8, int(length // 55)))
    seconds = round(max(1.2, length / 60), 2)
    moving = "".join(
        f'<circle class="dot" r="4.5"><animateMotion dur="{seconds}s" repeatCount="indefinite" '
        f'begin="-{seconds * i / dots:.2f}s"><mpath href="#e-{key}"/></animateMotion></circle>'
        for i in range(dots)
    )
    title = f"<title>{E(label)}</title>" if label else ""
    return (
        f'<path id="e-{key}" class="edge {"active" if dots else "idle"}" d="{d}" '
        f'marker-end="url(#arrow)">{title}</path>{moving}'
    )


def render_overview(snap: dict) -> str:
    f = snap["fleet"]
    steps = f["steps"]
    checks = {c["title"]: c for c in snap["checks"]}

    def from_checks(*titles: str) -> list:
        return [
            (checks[t]["status"], t, checks[t]["detail"]) for t in titles if t in checks
        ]

    held, waiting = steps.get("held", 0), steps.get("waiting_agent", 0)
    queued, dispatching = steps.get("queued", 0), steps.get("dispatching", 0)
    on_workers = steps.get("starting", 0) + steps.get("running", 0)
    m = f["manager"]
    tpu_cohorts = [c for c in snap["cohorts"] if c["resource"] == TPU]
    connected = sum(1 for w in f["workers"] if w["connected"])

    bk_checks = from_checks("Steps waiting for an agent")
    agent_checks = from_checks("Buildkite controller", "Agent pods")
    kueue_checks = [kueue_line(m["name"], m["kueue_up"])] + from_checks("Queue waits")
    mk_checks = from_checks("Worker connections", "Dispatch")

    def worker_checks(w: dict) -> list:
        items = [kueue_line(w["name"], w["kueue_up"])]
        if w["connected"] is None:
            items.append(
                ("unknown", "Manager link", "not listed as a MultiKueue cluster")
            )
        elif w["connected"]:
            items.append(("ok", "Manager link", "connected"))
        else:
            items.append(("fail", "Manager link", w["link_message"] or "not active"))
        items.append(
            (
                "warn",
                "Cluster problems",
                f"{w['problems']} kind{'s' if w['problems'] != 1 else ''} of fleet event in the last hour",
            )
            if w["problems"]
            else ("ok", "Cluster problems", "none in the last hour")
        )
        return items

    def shape_line(r: dict) -> tuple:
        return (
            r["title"],
            r["chips"] / r["max_chips"] if r["max_chips"] else 0,
            f"{num(r['chips'])}/{num(r['max_chips'])}",
        )

    boxes = {
        "buildkite": Box(
            "buildkite",
            "buildkite",
            "Buildkite",
            f"queue={f['queue']}",
            held + waiting,
            "waiting to start",
            [
                (f"{num(held)} held", None, ""),
                (f"{num(waiting)} need an agent", None, ""),
            ],
            worst(bk_checks),
        ),
        "agent": Box(
            "agent",
            "agent",
            "Agent pods",
            "agent-stack-k8s",
            f["agent_pods"],
            "launchers running",
            [
                (f"{num(steps.get('agent_pending', 0))} not scheduled", None, ""),
                (f"{num(steps.get('launching', 0))} submitting", None, ""),
            ],
            worst(agent_checks),
        ),
        "kueue": Box(
            "kueue",
            "kueue",
            "Kueue",
            "quota, per shape",
            queued,
            "waiting for quota",
            [
                (
                    c["generation"] or c["name"],
                    c["used"] / c["nominal"] if c["nominal"] else 0,
                    f"{num(c['used'])}/{num(c['nominal'])}",
                )
                for c in tpu_cohorts
            ],
            worst(kueue_checks),
            caption="chips in use / quota",
        ),
        "multikueue": Box(
            "multikueue",
            "multikueue",
            "MultiKueue",
            "to a worker",
            dispatching,
            "handing over",
            [(f"{connected}/{len(f['workers'])} workers up", None, "")],
            worst(mk_checks),
        ),
    }
    workers = [
        Box(
            f"w-{w['name']}",
            "worker",
            w["name"],
            w["location"],
            w["running"],
            f"running · {num(w['starting'])} starting",
            [shape_line(r) for r in w["shapes"]],
            worst(worker_checks(w)),
            caption="chips on nodes / pool max",
        )
        for w in f["workers"]
    ]

    # Vertical layout: the workers stack on the right; the main row sits at
    # their middle, inside the manager's boundary.
    top, gap = 52, 18
    y = top
    for b in workers:
        b.y = y
        y += b.h + gap
    workers_bottom = y - gap if workers else top
    main_h = max(b.h for b in boxes.values())
    mid = max((top + workers_bottom) / 2, top + main_h / 2 + 20)
    for b in boxes.values():
        b.y = mid - b.h / 2
    lo = mid - main_h / 2 - 36
    hi = mid + main_h / 2 + 16
    back_y = max(workers_bottom, hi) + 40
    height = back_y + 30

    bk, ag, ku, mk = (boxes[k] for k in ("buildkite", "agent", "kueue", "multikueue"))
    edges = [
        edge(
            "bk",
            f"M{bk.x + bk.w} {mid:.1f} L{ag.x - 4} {mid:.1f}",
            f["agent_pods"],
            ag.x - bk.x - bk.w,
            "agent-stack-k8s starts an agent pod per step",
        ),
        edge(
            "ag",
            f"M{ag.x + ag.w} {mid:.1f} L{ku.x - 4} {mid:.1f}",
            queued + dispatching + on_workers,
            ku.x - ag.x - ag.w,
            "the launcher submits a Job or JobSet to the shape's queue",
        ),
        edge(
            "ku",
            f"M{ku.x + ku.w} {mid:.1f} L{mk.x - 4} {mid:.1f}",
            dispatching + on_workers,
            mk.x - ku.x - ku.w,
            "Kueue admits it when quota is free",
        ),
    ]
    for i, w in enumerate(workers):
        n = f["workers"][i]["running"] + f["workers"][i]["starting"]
        x0, x1 = mk.x + mk.w, w.x - 4
        edges.append(
            edge(
                f"w{i}",
                f"M{x0} {mid:.1f} C{x0 + 40} {mid:.1f} {x1 - 40} {w.mid:.1f} {x1} {w.mid:.1f}",
                n,
                (x1 - x0) + abs(w.mid - mid),
                f"MultiKueue creates the workload on {f['workers'][i]['name']}",
            )
        )
    wx = COLUMNS["worker"][0] + COLUMNS["worker"][1] / 2
    bx = bk.x + bk.w / 2
    edges.append(
        edge(
            "back",
            f"M{wx} {workers_bottom:.1f} L{wx} {back_y:.1f} L{bx} {back_y:.1f} L{bx} {bk.y + bk.h + 4:.1f}",
            on_workers,
            (back_y - workers_bottom) + (wx - bx) + (back_y - bk.y - bk.h),
            "pod output goes back to the step log",
        )
    )

    diagram = f"""<div class="diagram-wrap"><svg class="diagram" viewBox="0 0 {DIAGRAM_WIDTH} {height:.0f}" role="img"
  aria-label="Buildkite, then the manager's agent pods, Kueue and MultiKueue, then {len(workers)} worker clusters; details for each follow below.">
  <defs><marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
    <path d="M0 0L10 5L0 10z" class="arrowhead"/></marker></defs>
  <rect class="boundary" x="{ag.x - 13}" y="{lo:.1f}" width="{mk.x + mk.w + 26 - ag.x}" height="{hi - lo:.1f}" rx="18"/>
  <text class="boundary-label" x="{ag.x + 3}" y="{lo + 22:.1f}">Manager · {E(m["name"])}{f" · {E(m['location'])}" if m["location"] else ""}</text>
  <text class="boundary-label" x="{COLUMNS["worker"][0]}" y="{top - 14}">Workers</text>
  {"".join(edges)}
  {"".join(b.svg() for b in boxes.values())}
  {"".join(b.svg() for b in workers)}
  <text class="edge-label" x="{(wx + bx) / 2:.0f}" y="{back_y - 9:.1f}" text-anchor="middle">pod output back to the step log, through Connect Gateway</text>
</svg></div>"""

    # Details, one per component, collapsed until a box or the toggle opens them.
    links = f["links"]
    manager_links = links.get(f"cluster:{m['name']}", [])[:1]

    def component(
        key: str, title: str, sub: str, status: str, role: str, body: str, opens=()
    ) -> str:
        return f"""<details class="component" id="c-{key}">
  <summary><span class="c-title">{E(title)}</span><span class="c-sub">{E(sub)}</span>{status_chip(status)}
  <span class="c-role">{role}</span></summary>
  <div class="c-body">{f'<p class="source-links">{out_links(opens)}</p>' if opens else ""}{body}</div>
</details>"""

    cohort_rows = "".join(
        f"""<div class="cohort-line"><b>{E(c["generation"] or c["name"])}</b>
  <span>{num(c["used"])} of {num(c["nominal"])} chips in use</span>
  <div class="bar" role="img" aria-label="{num(c["used"])} of {
            num(c["nominal"])
        } chips"><span class="seg own" style="width:{
            min(100, 100 * c["used"] / c["nominal"])
            if c["nominal"]
            else 0:.1f}%"></span></div>
  <span class="queued">{
            " · ".join(
                f"{E(queue_title(q).split(' ', 1)[-1])} {num(q['pending'])} waiting"
                for q in snap["queues"]
                if q["cohort"] == c["name"] and q["pending"]
            )
            or "nothing waiting"
        }</span></div>"""
        for c in tpu_cohorts
    )

    def pools_table(w: dict) -> str:
        rows = (
            "".join(
                f"""<tr><td class="nowrap"><a href="live#{E(r["queue"])}">{E(r["title"])}</a></td>
<td class="n">{f"{r['pools_up']} of {r['pools']} slices" if r["multi_host"] else f"{num(r['up'])} of {num(r['max'])}"}</td>
<td class="n">{num(r["chips"])}</td><td class="n">{num(r["max_chips"])}</td><td class="n">{num(r["busy"])}</td></tr>"""
                for r in w["shapes"]
            )
            or '<tr><td colspan="5" class="empty">No TPU node pools.</td></tr>'
        )
        return f"""<div class="table-wrap"><table><thead><tr><th>Shape</th><th class="n">Nodes up</th>
  <th class="n">Chips on nodes</th><th class="n">Pool max, chips</th><th class="n">Busy</th></tr></thead><tbody>{rows}</tbody></table></div>
  <p class="muted">Kueue's quota counts chips too: compare a shape's chips on nodes with what Kueue
  admitted for it on Live.</p>"""

    across = from_checks(
        "Long-running workloads", "Infrastructure failures (24h)", "Evictions (24h)"
    )
    components = [
        component(
            "buildkite",
            "Buildkite",
            f"queue={f['queue']}",
            worst(bk_checks),
            "Pipelines send their TPU steps to the kube queue. A step in a concurrency group waits here until its group lets it go.",
            f'<div class="counts">{count(held, "held by a concurrency group")}{count(waiting, "waiting for an agent pod")}</div>'
            f'{check_items(bk_checks)}<a class="more" href="live#jobs">Kube jobs on Live →</a>',
            links.get("buildkite", []),
        ),
        component(
            "agent",
            "Agent pods",
            "agent-stack-k8s, on the manager",
            worst(agent_checks),
            "agent-stack-k8s polls the queue and starts one pod per step. The pod runs the launcher, which picks the queue from the step's TPU shape, submits a Job or JobSet, and streams its output back.",
            f'<div class="counts">{count(f["agent_pods"], "agent pods")}{count(steps.get("agent_pending", 0), "not scheduled yet")}'
            f"{count(steps.get('launching', 0), 'submitting')}</div>{check_items(agent_checks)}",
            manager_links,
        ),
        component(
            "kueue",
            "Kueue",
            f"on {m['name']}",
            worst(kueue_checks),
            "Admits a workload once its shape's quota is free. The queues of one TPU generation share a cohort and borrow each other's idle chips.",
            f'<div class="counts">{count(queued, "waiting for quota")}</div>{cohort_rows}'
            f'{check_items(kueue_checks)}<a class="more" href="live#quota">Quota on Live →</a>',
            manager_links,
        ),
        component(
            "multikueue",
            "MultiKueue",
            f"on {m['name']}",
            worst(mk_checks),
            "Hands each admitted workload to a worker that can run it, and watches it there.",
            f'<div class="counts">{count(dispatching, "being handed over")}{count(connected, f"of {len(f["workers"])} workers connected")}</div>'
            f"{check_items(mk_checks)}",
            manager_links,
        ),
    ]
    for w in f["workers"]:
        items = worker_checks(w)
        components.append(
            component(
                f"w-{w['name']}",
                w["name"],
                f"worker · {w['location']}",
                worst(items),
                "Runs the TPU pods. Each shape has its own node pool, scaled up when a workload is admitted and down once it has stood idle; a multi-host shape has one pool per slice.",
                f'<div class="counts">{count(w["starting"], "pods starting")}{count(w["running"], "running")}{count(w["held"], "chips held")}</div>'
                f'{pools_table(w)}{check_items(items)}<a class="more" href="live#problems">Cluster problems on Live →</a>',
                links.get(f"cluster:{w['name']}", []),
            )
        )
    components.append(
        component(
            "across",
            "Across workers",
            "every worker",
            worst(across),
            "Workloads that run too long, end for the fleet's reasons, or are evicted.",
            check_items(across),
        )
    )

    return OVERVIEW_PAGE.substitute(
        assets=ASSETS,
        generated=snap["generated_at"],
        errors=render_errors(snap["sources"]),
        diagram=diagram,
        components="".join(components),
        glossary=GLOSSARY,
        source_table=render_source_table(
            snap["sources"],
            {**links, "cluster:manager": manager_links},
        ),
    )


# --------------------------------------------------------------------------
# Baseline


def gcs_link(path: str, label: str, root: str) -> str:
    """A link to part of the snapshot's copy in Cloud Storage."""
    bucket_path = root.removeprefix("gs://").rstrip("/") + ("/" + path if path else "")
    kind = "browser/_details" if "." in path.rsplit("/", 1)[-1] else "browser"
    url = f"https://console.cloud.google.com/storage/{kind}/{bucket_path}"
    return f'<a href="{E(url)}" target="_blank" rel="noopener">{E(label)} ↗</a>'


def render_baseline() -> str:
    data = json.loads(BASELINE_FILE.read_text())
    start, step = data["start"], data["step"]
    n = len(data["generations"][0]["held"])
    ticks = [start + i * step for i in range(n)]
    every = max(1, n // 24)
    # Hour of day on one reference day in Pacific time, for the daily profile.
    day0 = start - (start - 7 * 3600) % 86400 + 86400
    hour_ticks = [day0 + h * 3600 for h in range(24)]

    cards = []
    for g in data["generations"]:
        cap = g["capacity"]
        share = g["mean_held"] / cap if cap else None
        cards.append(f"""<div class="card">
  <div class="card-head"><h2>{E(g["name"])} chips</h2><span class="sub">bare metal, {num(cap)} chips across the queues' agents</span></div>
  <div class="tiles">
    <div class="tile"><span>Held by jobs, average</span><b>{pct(share)}</b><small>{num(g["mean_held"])} of {num(cap)} chips</small></div>
    <div class="tile"><span>Busiest hour</span><b>{num(g["peak_held"])}</b><small>chips held at once</small></div>
    <div class="tile"><span>Hours near full</span><b>{num(g["hours_over_80"])}</b><small>at 80% or more; {num(g["hours_under_20"])} at 20% or less, of {num(n)}</small></div>
    <div class="tile"><span>Jobs</span><b>{num(g["jobs"])}</b><small>{num(g["chip_hours"])} chip-hours</small></div>
  </div>
  <div class="charts">
  {line_chart("Chips held by running jobs, hourly", ticks, [("used", "held", g["held"])], ref=("capacity", [cap] * n), fmt="day", width=520, height=200)}
  {line_chart("By hour of day (PT), averaged", hour_ticks, [("used", "held", g["by_hour_pt"])], ref=("capacity", [cap] * 24), fmt="time", width=520, height=200)}
  </div>
  {values_table(ticks, [("Held", g["held"]), ("Capacity", [cap] * n)], every, "Values")}
</div>""")

    rows = "".join(
        f"""<tr><td class="nowrap"><code>{E(q["queue"])}</code></td>
<td class="n">{num(q["agents"])} <span class="muted">({num(q["agents_active"])} active a day)</span></td>
<td class="n">{num(q["chips_per_job"])}</td><td class="n">{num(q["capacity"])}</td>
<td class="n">{num(q["jobs"])}</td><td class="n">{num(q["chip_hours"])}</td><td class="n"><b>{pct(q["busy_share"])}</b></td>
<td class="n nowrap">{ago((q["wait_p50"] or 0) * 60)} / {ago((q["wait_p90"] or 0) * 60)}</td>
<td>{E(", ".join(f"{k} {num(v)}" for k, v in q["by_kind"].items()))}</td></tr>"""
        for q in data["queues"]
    )
    queues = f"""<div class="table-wrap"><table class="dense"><thead><tr><th>Queue</th><th class="n">Agents</th>
<th class="n">Chips a job</th><th class="n">Capacity, chips</th><th class="n">Jobs</th><th class="n">Chip-hours</th>
<th class="n">Busy</th><th class="n">Wait p50 / p90</th><th>Chip-hours by build kind</th></tr></thead><tbody>{rows}</tbody></table></div>
<p class="muted note">Capacity is the queue's agents at the snapshot times the chips a job holds, which Buildkite's
queue names count in TensorCores (two a chip). Busy is chip-hours over capacity for the window; a queue whose
agents came and went reads lower than its active agents did, and an hour can hold more than the snapshot's agents
had. Wait is runnable to started.</p>"""

    root = data["snapshot"]["gcs"]
    data_html = f"""<ul class="data-links">
  <li>{gcs_link("", "The whole snapshot", root)} - and its {gcs_link("MANIFEST.md", "manifest", root)}</li>
  <li>{gcs_link("baseline", "Per-queue and per-lane tables", root)}, from the same dumps over 30 days - six of which
    the dumps do not cover, so their busy shares read about a fifth low</li>
  <li>{gcs_link("buildkite/org=vllm", "Buildkite build dumps", root)}: every job's queue, agent and times, the source of
    every number here</li>
  <li>{gcs_link("buildkite/agents_all_2026-09-29T2115Z.json", "Agents at the snapshot", root)}, for capacity</li>
  <li>{gcs_link("gcp/monitoring/tpu_duty_cycle", "TensorCore duty cycle, per minute", root)} - not on this page: its
    series name TPU hosts, which the snapshot does not tie to CI agents</li>
</ul>
<p class="muted note">Rebuild with <code>dashboard/baseline/build_baseline.py &lt;snapshot dir&gt;</code>.</p>"""

    return BASELINE_PAGE.substitute(
        assets=ASSETS,
        window=E(data["window"]),
        days=num((data["end"] - data["start"]) / 86400),
        taken=E(data["snapshot"]["taken"]),
        generations="".join(cards),
        queues=queues,
        data=data_html,
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


def failures_cell(r: dict) -> str:
    """Test failures / infrastructure failures, with what the latter were."""
    infra = f"<b>{num(r['infra'])}</b>" if r["infra"] else "0"
    detail = ", ".join(f"{k} {v}" for k, v in r["infra_outcomes"])
    return (
        f'<td class="n nowrap">{num(r["failed"])} / {infra}'
        + (f'<div class="sub-row">{E(detail)}</div>' if detail else "")
        + "</td>"
    )


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
{failures_cell(q["stats"])}
<td class="n nowrap">{num(q["stats"]["evictions"])} / {num(q["stats"]["requeues"])} / {num(q["stats"]["redispatches"])}</td>
<td class="n nowrap">{ago(q["stats"]["wait_p50"])} / {ago(q["stats"]["wait_p90"])}</td>
<td class="n">{ago(q["stats"]["run_p50"])}</td>
<td class="n">{num(q["stats"]["chip_hours"])}</td></tr>"""
        for q in tpu
    )
    autoscaled = [q for q in tpu if q.get("nodes")]
    node_rows = (
        "".join(
            f"""<tr><td class="nowrap">{queue_link(q)}</td>
<td class="n"><b>{num(q["nodes"]["created"])}</b></td><td class="n">{ago(q["nodes"]["lifetime_p50"])}</td>
<td class="n">{pct(q["nodes"]["held_share"])}</td>
<td class="n">{num(q["nodes"]["idle_chip_hours"])}</td>
<td class="n nowrap">{ago(q["stats"]["startup_p50"])} / {ago(q["stats"]["startup_p90"])}</td></tr>"""
            for q in autoscaled
        )
        or '<tr><td colspan="6" class="empty">No node pool data.</td></tr>'
    )
    autoscaling = f"""<div class="table-wrap"><table class="dense"><thead><tr><th>Topology</th>
<th class="n">Nodes created</th><th class="n">Node lifetime p50</th><th class="n">Held by workloads</th>
<th class="n">Idle chip-hours</th><th class="n">Admitted → running p50 / p90</th></tr></thead><tbody>{node_rows}</tbody></table></div>
<p class="muted note">From GKE's per-node metrics: every TPU node that existed in the range, tied to its pool by the
pool's instance group. <i>Nodes created</i> counts nodes that appeared during the range, each one a node the pool scaled
up for; <i>lifetime</i> is appearing to disappearing, for nodes that did both. <i>Idle chip-hours</i> are chips on nodes
that no admitted workload held - scale-down lag, a pool's minimum, or a node waiting for the workload it came for.
<i>Held by workloads</i> is the share of chip-hours on nodes that an admitted workload held.
<i>Admitted → running</i> includes the wait for a node when the pool had to scale up.</p>"""

    pipeline_rows, pipelines_more, pipelines_button = split_rows(
        [
            f"""<tr><td>{E(p["pipeline"])}</td><td class="n">{num(p["finished"])}</td><td class="n">{success_rate(p)}</td>
{failures_cell(p)}<td class="n">{num(p["chip_hours"])}</td></tr>"""
            for p in h["pipelines"][:20]
        ],
        "pipelines-more",
        '<tr><td colspan="5" class="empty">No finished workloads.</td></tr>',
    )
    outcomes = f"""
<h3>By queue</h3>
<div class="table-wrap"><table class="dense"><thead><tr><th>Queue</th><th class="n">Finished</th><th class="n">Success</th>
<th class="n">Failures: test / infra</th><th class="n">Evicted / requeued / redispatched</th>
<th class="n">Wait p50 / p90</th><th class="n">Run p50</th><th class="n">Chip-hours</th></tr></thead>
<tbody>{queue_rows}</tbody></table></div>
<p class="muted note">Success is over workloads that finished either way; cancelled ones are left out. Wait is
submitted to quota reserved; how long an admitted workload took to start is under Node autoscaling.</p>
<h3 style="margin-top:20px">By pipeline</h3>
<div class="table-wrap"><table class="dense"><thead><tr><th>Pipeline</th><th class="n">Finished</th><th class="n">Success</th>
<th class="n">Failures: test / infra</th><th class="n">Chip-hours</th></tr></thead>
<tbody>{pipeline_rows}</tbody>{pipelines_more}</table></div>
{pipelines_button}"""

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
        assets=ASSETS,
        errors=render_errors(sources),
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
