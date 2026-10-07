#!/usr/bin/env python3
"""What is waiting for each TPU topology, bare metal beside kube.

Buildkite's queue page answers this for the bare-metal fleet, where a queue is a
topology. It cannot for the kube fleet: every kube step sits on the one `kube`
queue, and its topology is chosen later, by the launcher, as a Kueue queue. This
page puts the two back side by side - per topology, the bare-metal queue and the
ClusterQueue replacing it, what each is running and which builds are waiting.

Three sources, each for what only it has:

- Kueue on the manager, read live through Connect Gateway: kube workloads by
  queue, admitted or pending and why, and the build each belongs to. The
  launcher labels every Job and JobSet it submits with its pipeline, build
  number and Buildkite job id.
- Buildkite's REST API: bare-metal jobs by queue and build, and kube steps that
  Buildkite has not handed to the cluster yet - held by a concurrency group, or
  waiting for an agent pod. Neither exists in Kueue.
- Cloud Monitoring: bare-metal agent counts from the buildkite-agent-metrics
  exporter, and 24 hours of both fleets for the charts.

Nothing polls in the background. A page view refreshes whatever is older than
CACHE_SECONDS, so a dashboard nobody is looking at makes no API calls - which
matters for Buildkite, whose rate limit the whole org shares.

Run locally against the live fleet, with your own credentials:

    ./app.py --local     # gcloud for Google APIs, `bk api` for Buildkite
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TPU = "google.com/tpu"

# Buildkite job states, by what they mean for a queue. "waiting" (dependencies
# not met) and "blocked" are left out: those jobs are not asking for an agent.
BK_WAITING = {"scheduled", "assigned", "accepted", "reserved"}
BK_LIMITED = {"limited", "limiting"}
BK_RUNNING = {"running", "canceling", "timing_out"}
BK_ACTIVE_BUILDS = "state[]=running&state[]=scheduled&state[]=failing&state[]=canceling"

HISTORY_SECONDS = 24 * 3600
HISTORY_STEP = 900

LOCAL = False


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"{name} is not set")
    return value


class Config:
    def __init__(self) -> None:
        self.project = env("PROJECT_ID")
        self.gateway = env("GATEWAY_URL").rstrip("/")
        self.namespace = env("NAMESPACE", "buildkite")
        # The `cluster` label Managed Prometheus puts on the manager's Kueue
        # series. Workers export the same metric names for their own admission,
        # which is not what this page reports.
        self.metrics_cluster = env("KUEUE_METRICS_CLUSTER")
        self.org = env("BUILDKITE_ORG")
        self.cluster_id = env("BUILDKITE_CLUSTER_ID")
        self.token = os.environ.get("BUILDKITE_API_TOKEN", "")
        self.topologies = json.loads(env("TOPOLOGIES"))
        self.cache_seconds = int(env("CACHE_SECONDS", "60"))


# --------------------------------------------------------------------------
# HTTP


class Token:
    """A Google access token, refreshed a minute before it expires."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = ""
        self._expiry = 0.0

    def get(self) -> str:
        with self._lock:
            if time.time() < self._expiry - 60:
                return self._value
            if LOCAL:
                out = subprocess.run(
                    ["gcloud", "auth", "print-access-token"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self._value, self._expiry = out.stdout.strip(), time.time() + 1800
            else:
                req = urllib.request.Request(
                    "http://metadata.google.internal/computeMetadata/v1/instance/"
                    "service-accounts/default/token",
                    headers={"Metadata-Flavor": "Google"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = json.load(resp)
                self._value = body["access_token"]
                self._expiry = time.time() + body["expires_in"]
            return self._value


GOOGLE_TOKEN = Token()


def get_json(url: str, headers: dict[str, str]) -> object:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def google(url: str) -> dict:
    return get_json(url, {"Authorization": f"Bearer {GOOGLE_TOKEN.get()}"})


def buildkite(cfg: Config, path: str) -> list | dict:
    """GET an org-relative REST path, e.g. /builds?state=running."""
    if LOCAL:
        out = subprocess.run(
            ["bk", "api", path], capture_output=True, text=True, check=True
        )
        return json.loads(out.stdout)
    url = f"https://api.buildkite.com/v2/organizations/{cfg.org}{path}"
    return get_json(url, {"Authorization": f"Bearer {cfg.token}"})


def buildkite_pages(cfg: Config, path: str, max_pages: int = 10) -> list:
    sep = "&" if "?" in path else "?"
    items: list = []
    for page in range(1, max_pages + 1):
        batch = buildkite(cfg, f"{path}{sep}per_page=100&page={page}")
        items += batch
        if len(batch) < 100:
            break
    return items


def parse_time(value: str | None) -> float | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def qty(value: object) -> int:
    # TPU counts are whole chips, however Kubernetes chose to serialize them.
    return int(float(str(value or 0)))


# --------------------------------------------------------------------------
# Buildkite


class Hourly:
    """A value that changes rarely - the cluster's queues and pipelines."""

    def __init__(self, fetch) -> None:
        self._fetch = fetch
        self._value = None
        self._at = 0.0

    def get(self):
        if self._value is None or time.time() - self._at > 3600:
            self._value, self._at = self._fetch(), time.time()
        return self._value


def fetch_buildkite(cfg: Config, queues: Hourly, pipelines: Hourly) -> dict:
    queue_keys = queues.get()
    slugs = pipelines.get()

    # Two steps rather than one call per pipeline. The org-wide list without
    # jobs is small and says which of the cluster's pipelines have anything in
    # flight; only those are fetched with their jobs. Fetching the org-wide list
    # with jobs instead is over 100 MB on a busy day, almost all of it other
    # clusters' builds.
    light = buildkite_pages(
        cfg, f"/builds?{BK_ACTIVE_BUILDS}&exclude_jobs=true", max_pages=5
    )
    active = sorted({b["pipeline"]["slug"] for b in light} & slugs)
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        builds = [
            b
            for batch in pool.map(
                lambda s: buildkite_pages(
                    cfg, f"/pipelines/{s}/builds?{BK_ACTIVE_BUILDS}"
                ),
                active,
            )
            for b in batch
        ]

    jobs = []
    for b in builds:
        build = {
            "pipeline": b["pipeline"]["slug"],
            "number": b["number"],
            "url": b["web_url"],
            "branch": b.get("branch") or "",
            "source": b.get("source") or "",
            "message": (b.get("message") or "").split("\n", 1)[0][:120],
        }
        for j in b.get("jobs") or []:
            if j.get("type") != "script" or j.get("cluster_id") != cfg.cluster_id:
                continue
            state = j.get("state")
            if state not in BK_WAITING | BK_LIMITED | BK_RUNNING:
                continue
            jobs.append(
                {
                    **build,
                    "id": j["id"],
                    "label": j.get("name") or j.get("step_key") or "",
                    "state": state,
                    "queue": queue_keys.get(j.get("cluster_queue_id"), "?"),
                    "priority": (j.get("priority") or {}).get("number", 0),
                    "runnable_at": parse_time(j.get("runnable_at")),
                    "started_at": parse_time(j.get("started_at")),
                    "job_url": j.get("web_url"),
                }
            )
    return {"jobs": jobs}


# --------------------------------------------------------------------------
# Kueue


def cq_summary(cq: dict) -> dict:
    nominal = borrowing = 0
    for group in cq["spec"].get("resourceGroups", []):
        for flavor in group["flavors"]:
            for r in flavor["resources"]:
                if r["name"] == TPU:
                    nominal += qty(r.get("nominalQuota"))
                    borrowing += qty(r.get("borrowingLimit"))
    used = borrowed = 0
    for flavor in cq.get("status", {}).get("flavorsUsage", []):
        for r in flavor["resources"]:
            if r["name"] == TPU:
                used += qty(r.get("total"))
                borrowed += qty(r.get("borrowed"))
    return {
        "name": cq["metadata"]["name"],
        "cohort": cq["spec"].get("cohortName") or cq["spec"].get("cohort") or "",
        "nominal": nominal,
        "borrowing_limit": borrowing,
        "used": used,
        "borrowed": borrowed,
    }


def workload_chips(wl: dict) -> int:
    admission = wl.get("status", {}).get("admission")
    if admission:
        return sum(
            qty(a.get("resourceUsage", {}).get(TPU))
            for a in admission.get("podSetAssignments", [])
        )
    total = 0
    for ps in wl["spec"].get("podSets", []):
        per_pod = 0
        for c in ps["template"]["spec"].get("containers", []):
            res = c.get("resources", {})
            per_pod += qty(
                (res.get("requests") or {}).get(TPU)
                or (res.get("limits") or {}).get(TPU)
            )
        total += per_pod * ps.get("count", 1)
    return total


def workload_state(wl: dict, now: float) -> dict | None:
    conds = {c["type"]: c for c in wl.get("status", {}).get("conditions", [])}

    def true(kind: str) -> bool:
        return conds.get(kind, {}).get("status") == "True"

    if true("Finished"):
        return None
    created = parse_time(wl["metadata"]["creationTimestamp"])
    if true("Admitted"):
        return {
            "state": "admitted",
            "since": parse_time(conds["Admitted"]["lastTransitionTime"]),
        }
    # Reserved on the manager, not yet admitted: MultiKueue has the fleet's quota
    # and is waiting for a worker to admit it too.
    if true("QuotaReserved"):
        return {
            "state": "dispatching",
            "since": parse_time(conds["QuotaReserved"]["lastTransitionTime"]),
        }
    # Kueue tries only the head of a BestEffortFIFO queue, so only the head has
    # a QuotaReserved=False condition saying why. The rest are queued behind it
    # and have no reason of their own yet.
    reserved = conds.get("QuotaReserved")
    if true("Evicted"):
        reason = "evicted: " + (conds["Evicted"].get("message") or "")
        since = parse_time(conds["Evicted"]["lastTransitionTime"])
    elif reserved:
        reason = reserved.get("message") or ""
        since = parse_time(reserved["lastTransitionTime"])
    else:
        reason = ""
        since = created
    return {"state": "pending", "since": min(since or now, now), "reason": reason}


def fetch_kueue(cfg: Config) -> dict:
    kueue = f"{cfg.gateway}/apis/kueue.x-k8s.io/v1beta2"
    ns = cfg.namespace
    by_build = "labelSelector=buildkite.com%2Fpipeline"
    urls = {
        "cqs": f"{kueue}/clusterqueues",
        "lqs": f"{kueue}/namespaces/{ns}/localqueues",
        "wls": f"{kueue}/namespaces/{ns}/workloads",
        "jobs": f"{cfg.gateway}/apis/batch/v1/namespaces/{ns}/jobs?{by_build}",
        "jobsets": f"{cfg.gateway}/apis/jobset.x-k8s.io/v1alpha2/namespaces/{ns}/jobsets?{by_build}",
    }
    with concurrent.futures.ThreadPoolExecutor(len(urls)) as pool:
        got = dict(zip(urls, pool.map(lambda u: google(u)["items"], urls.values())))

    lq_to_cq = {lq["metadata"]["name"]: lq["spec"]["clusterQueue"] for lq in got["lqs"]}
    owners = {
        (kind, o["metadata"]["name"]): o["metadata"].get("labels", {})
        for kind, key in (("Job", "jobs"), ("JobSet", "jobsets"))
        for o in got[key]
    }

    now = time.time()
    workloads = []
    for wl in got["wls"]:
        state = workload_state(wl, now)
        if state is None:
            continue
        admission = wl.get("status", {}).get("admission") or {}
        labels = {}
        for ref in wl["metadata"].get("ownerReferences", []):
            labels = owners.get((ref["kind"], ref["name"]), labels)
        workloads.append(
            {
                **state,
                "queue": admission.get("clusterQueue")
                or lq_to_cq.get(
                    wl["spec"].get("queueName"), wl["spec"].get("queueName")
                ),
                "chips": workload_chips(wl),
                "priority": wl["spec"].get("priority", 0),
                "pipeline": labels.get("buildkite.com/pipeline", ""),
                "number": int(labels.get("buildkite.com/build-number") or 0),
                "job_id": labels.get("buildkite.com/job-id", ""),
                "name": wl["metadata"]["name"],
            }
        )
    return {"queues": [cq_summary(cq) for cq in got["cqs"]], "workloads": workloads}


# --------------------------------------------------------------------------
# Cloud Monitoring


def grid(now: float) -> list[float]:
    end = now - now % HISTORY_STEP
    return [
        end - HISTORY_SECONDS + i * HISTORY_STEP
        for i in range(HISTORY_SECONDS // HISTORY_STEP + 1)
    ]


def resample(
    points: list[tuple[float, float]], ticks: list[float]
) -> list[float | None]:
    """The last value at or before each tick, if it is recent enough to trust."""
    points = sorted(points)
    out: list[float | None] = []
    i = -1
    for t in ticks:
        while i + 1 < len(points) and points[i + 1][0] <= t:
            i += 1
        out.append(
            points[i][1] if i >= 0 and t - points[i][0] <= 2 * HISTORY_STEP else None
        )
    return out


def iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bk_metric(cfg: Config, name: str, start: float, end: float, align: str) -> dict:
    """Per-queue series of one buildkite-agent-metrics metric."""
    params = {
        "filter": f'metric.type="custom.googleapis.com/buildkite/{cfg.org}/{name}"',
        "interval.startTime": iso(start),
        "interval.endTime": iso(end),
        "aggregation.alignmentPeriod": f"{HISTORY_STEP}s",
        "aggregation.perSeriesAligner": align,
    }
    url = (
        f"https://monitoring.googleapis.com/v3/projects/{cfg.project}/timeSeries?"
        + urllib.parse.urlencode(params)
    )
    out: dict[str, list[tuple[float, float]]] = {}
    for ts in google(url).get("timeSeries", []):
        queue = ts["metric"]["labels"].get("Queue", "")
        out[queue] = [
            (
                parse_time(p["interval"]["endTime"]),
                float(p["value"].get("doubleValue", p["value"].get("int64Value", 0))),
            )
            for p in ts["points"]
        ]
    return out


def promql(cfg: Config, query: str, start: float, end: float) -> dict:
    params = {"query": query, "start": start, "end": end, "step": HISTORY_STEP}
    url = (
        f"https://monitoring.googleapis.com/v1/projects/{cfg.project}/location/global/"
        "prometheus/api/v1/query_range?" + urllib.parse.urlencode(params)
    )
    return {
        r["metric"].get("cluster_queue", ""): [
            (float(t), float(v)) for t, v in r["values"]
        ]
        for r in google(url)["data"]["result"]
    }


def fetch_monitoring(cfg: Config) -> dict:
    now = time.time()
    start = now - HISTORY_SECONDS - HISTORY_STEP
    sel = f'cluster="{cfg.metrics_cluster}"'
    calls = {
        "bare_total": lambda: bk_metric(
            cfg, "TotalAgentCount", start, now, "ALIGN_MAX"
        ),
        "bare_busy": lambda: bk_metric(cfg, "BusyAgentCount", start, now, "ALIGN_MEAN"),
        "bare_waiting": lambda: bk_metric(
            cfg, "ScheduledJobsCount", start, now, "ALIGN_MEAN"
        ),
        # max before sum: a rolled Kueue controller leaves the old pod's series
        # inside the lookback, and a plain sum counts both. See "Reading the
        # metrics" in ../README.md.
        "kube_pending": lambda: promql(
            cfg,
            "sum by (cluster_queue) (max by (cluster_queue, status) "
            f"(kueue_pending_workloads{{{sel}}}))",
            start,
            now,
        ),
        "kube_used": lambda: promql(
            cfg,
            "sum by (cluster_queue) (max by (cluster_queue, flavor, resource) "
            f'(kueue_cluster_queue_resource_usage{{{sel},resource="{TPU}"}}))',
            start,
            now,
        ),
    }
    with concurrent.futures.ThreadPoolExecutor(len(calls)) as pool:
        futures = {k: pool.submit(f) for k, f in calls.items()}
        return {k: f.result() for k, f in futures.items()}


# --------------------------------------------------------------------------
# Snapshot


class Source:
    """One upstream, its last good result, and why the latest fetch failed."""

    def __init__(self, name: str, fetch) -> None:
        self.name = name
        self.fetch = fetch
        self.data: dict | None = None
        self.at = 0.0
        self.error = ""

    def refresh(self) -> None:
        try:
            self.data, self.at, self.error = self.fetch(), time.time(), ""
        except Exception as e:  # noqa: BLE001 - shown on the page, not raised
            self.error = f"{type(e).__name__}: {e}"[:300]


class Dashboard:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        queues = Hourly(
            lambda: {
                q["id"]: q["key"]
                for q in buildkite(cfg, f"/clusters/{cfg.cluster_id}/queues")
            }
        )
        pipelines = Hourly(
            lambda: {
                p["slug"]
                for p in buildkite_pages(cfg, "/pipelines")
                if p.get("cluster_id") == cfg.cluster_id
            }
        )
        self.sources = {
            "buildkite": Source(
                "Buildkite", lambda: fetch_buildkite(cfg, queues, pipelines)
            ),
            "kueue": Source("Kueue", lambda: fetch_kueue(cfg)),
            "monitoring": Source("Cloud Monitoring", lambda: fetch_monitoring(cfg)),
        }
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._refreshing = False
        self._snapshot: dict | None = None
        self._at = 0.0

    def _stale(self) -> bool:
        return time.time() - self._at > self.cfg.cache_seconds

    def _refresh(self) -> None:
        with self._refresh_lock:
            try:
                # A first page view that queued behind another finds the
                # snapshot that one built.
                if self._snapshot is not None and not self._stale():
                    return
                with concurrent.futures.ThreadPoolExecutor(len(self.sources)) as pool:
                    list(pool.map(Source.refresh, self.sources.values()))
                snapshot = build_snapshot(self.cfg, self.sources)
                with self._lock:
                    self._snapshot, self._at = snapshot, time.time()
            finally:
                with self._lock:
                    self._refreshing = False

    def snapshot(self) -> dict:
        # A refresh takes seconds - most of it Buildkite - so a page view is
        # served the last snapshot and a stale one is refreshed behind it. Only
        # the first view after a cold start waits. The page states its own age.
        with self._lock:
            snapshot = self._snapshot
            if snapshot is not None and self._stale() and not self._refreshing:
                self._refreshing = True
                threading.Thread(target=self._refresh, daemon=True).start()
        if snapshot is None:
            self._refresh()
            snapshot = self._snapshot
        return snapshot


def latest(series: dict, key: str) -> float | None:
    points = series.get(key) or []
    return max(points)[1] if points else None


def build_snapshot(cfg: Config, sources: dict[str, Source]) -> dict:
    now = time.time()
    bk = sources["buildkite"].data or {"jobs": []}
    kq = sources["kueue"].data or {"queues": [], "workloads": []}
    mon = sources["monitoring"].data or {}
    ticks = grid(now)

    jobs_by_id = {j["id"]: j for j in bk["jobs"]}
    builds_meta = {(j["pipeline"], j["number"]): j for j in bk["jobs"]}
    cqs = {q["name"]: q for q in kq["queues"]}
    cohorts: dict[str, dict] = {}
    for q in kq["queues"]:
        c = cohorts.setdefault(q["cohort"], {"nominal": 0, "used": 0})
        c["nominal"] += q["nominal"]
        c["used"] += q["used"]

    def build_row(pipeline: str, number: int) -> dict:
        meta = builds_meta.get((pipeline, number), {})
        return {
            "pipeline": pipeline,
            "number": number,
            "url": meta.get("url")
            or f"https://buildkite.com/{cfg.org}/{pipeline}/builds/{number}",
            "branch": meta.get("branch", ""),
            "source": meta.get("source", ""),
            "message": meta.get("message", ""),
            "priority": None,
            "bare_waiting": 0,
            "bare_running": 0,
            "kube_pending": 0,
            "kube_running": 0,
            "oldest_wait": None,
        }

    def bump(
        rows: dict, pipeline: str, number: int, field: str, priority: int, waiting_since
    ):
        row = rows.setdefault((pipeline, number), build_row(pipeline, number))
        row[field] += 1
        row["priority"] = max(
            row["priority"] if row["priority"] is not None else priority, priority
        )
        if waiting_since is not None:
            wait = now - waiting_since
            row["oldest_wait"] = max(row["oldest_wait"] or 0, wait)

    def side_history(series_key: str, queue: str, scale: float = 1.0) -> list:
        values = resample(mon.get(series_key, {}).get(queue, []), ticks)
        return [None if v is None else round(v * scale, 1) for v in values]

    topologies = []
    mapped_bare, mapped_kube = set(), set()
    for topo in cfg.topologies:
        bq, kqn, chips = topo["bare_queue"], topo["kube_queue"], topo["chips"]
        mapped_bare.add(bq)
        mapped_kube.add(kqn)
        rows: dict = {}
        bare_jobs = [j for j in bk["jobs"] if j["queue"] == bq]
        for j in bare_jobs:
            if j["state"] in BK_RUNNING:
                bump(
                    rows,
                    j["pipeline"],
                    j["number"],
                    "bare_running",
                    j["priority"],
                    None,
                )
            else:
                bump(
                    rows,
                    j["pipeline"],
                    j["number"],
                    "bare_waiting",
                    j["priority"],
                    j["runnable_at"],
                )
        wls = [w for w in kq["workloads"] if w["queue"] == kqn]
        reasons: dict[str, int] = {}
        for w in wls:
            prio = jobs_by_id.get(w["job_id"], {}).get("priority", w["priority"])
            if w["state"] == "pending":
                bump(rows, w["pipeline"], w["number"], "kube_pending", prio, w["since"])
                # "3 more needed" differs per workload; the cause does not.
                reason = (
                    re.sub(r", \d+ more needed", "", w["reason"])
                    or "queued behind the head of the queue"
                )
                reasons[reason] = reasons.get(reason, 0) + 1
            else:
                bump(rows, w["pipeline"], w["number"], "kube_running", prio, None)
        cq = cqs.get(kqn, {})
        bare_waiting = [j for j in bare_jobs if j["state"] not in BK_RUNNING]
        kube_pending = [w for w in wls if w["state"] == "pending"]
        topologies.append(
            {
                **topo,
                "bare": {
                    "agents_total": latest(mon.get("bare_total", {}), bq),
                    "agents_busy": sum(
                        1 for j in bare_jobs if j["state"] in BK_RUNNING
                    ),
                    "waiting": len(bare_waiting),
                    "oldest_wait": max(
                        (
                            now - j["runnable_at"]
                            for j in bare_waiting
                            if j["runnable_at"]
                        ),
                        default=None,
                    ),
                },
                "kube": {
                    "nominal": cq.get("nominal"),
                    "borrowing_limit": cq.get("borrowing_limit"),
                    "used": cq.get("used"),
                    "cohort": cq.get("cohort", ""),
                    "cohort_nominal": cohorts.get(cq.get("cohort", ""), {}).get(
                        "nominal"
                    ),
                    "cohort_used": cohorts.get(cq.get("cohort", ""), {}).get("used"),
                    "admitted": sum(1 for w in wls if w["state"] == "admitted"),
                    "dispatching": sum(1 for w in wls if w["state"] == "dispatching"),
                    "pending": len(kube_pending),
                    "oldest_wait": max(
                        (now - w["since"] for w in kube_pending), default=None
                    ),
                    "reasons": sorted(reasons.items(), key=lambda kv: -kv[1]),
                },
                "builds": sorted(
                    rows.values(),
                    key=lambda r: (
                        -(r["bare_waiting"] + r["kube_pending"] > 0),
                        -(r["priority"] or 0),
                        -(r["oldest_wait"] or 0),
                    ),
                ),
                "history": {
                    "bare_waiting": side_history("bare_waiting", bq),
                    "kube_pending": side_history("kube_pending", kqn),
                    "bare_chips": side_history("bare_busy", bq, chips),
                    "kube_chips": side_history("kube_used", kqn),
                },
            }
        )

    # Kube steps Buildkite has not handed to the cluster. Their topology is not
    # known yet - the step's command does not name it; the launcher decides.
    unplaced: dict = {}
    for j in bk["jobs"]:
        if j["queue"] != "kube" or j["state"] in BK_RUNNING:
            continue
        field = "limited" if j["state"] in BK_LIMITED else "waiting_agent"
        row = unplaced.setdefault(
            (j["pipeline"], j["number"]),
            {**build_row(j["pipeline"], j["number"]), "limited": 0, "waiting_agent": 0},
        )
        row[field] += 1
        row["priority"] = max(row["priority"] or 0, j["priority"])
        if j["runnable_at"]:
            row["oldest_wait"] = max(row["oldest_wait"] or 0, now - j["runnable_at"])

    # "Total" is the exporter's sum over every queue, not a queue.
    other_bare = sorted(
        ({j["queue"] for j in bk["jobs"]} | set(mon.get("bare_total", {})))
        - mapped_bare
        - {"kube", "Total"}
    )
    other = [
        {
            "fleet": "bare",
            "queue": q,
            "agents_total": latest(mon.get("bare_total", {}), q),
            "busy": sum(
                1 for j in bk["jobs"] if j["queue"] == q and j["state"] in BK_RUNNING
            ),
            "waiting": sum(
                1
                for j in bk["jobs"]
                if j["queue"] == q and j["state"] not in BK_RUNNING
            ),
        }
        for q in other_bare
    ] + [
        {
            "fleet": "kube",
            "queue": name,
            "busy": sum(
                1
                for w in kq["workloads"]
                if w["queue"] == name and w["state"] != "pending"
            ),
            "waiting": sum(
                1
                for w in kq["workloads"]
                if w["queue"] == name and w["state"] == "pending"
            ),
        }
        for name in sorted(set(cqs) - mapped_kube)
    ]

    return {
        "generated_at": now,
        "history_ticks": ticks,
        "sources": {
            k: {"name": s.name, "at": s.at, "error": s.error}
            for k, s in sources.items()
        },
        "topologies": topologies,
        "unplaced": sorted(
            unplaced.values(),
            key=lambda r: (-(r["priority"] or 0), -(r["oldest_wait"] or 0)),
        ),
        "other": other,
    }


# --------------------------------------------------------------------------
# Rendering

E = html.escape


def ago(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d{seconds % 86400 // 3600}h"


def num(value) -> str:
    return "-" if value is None else f"{int(value):,}"


def sparkline(
    title: str, ticks: list[float], series: list[tuple[str, str, list]]
) -> str:
    """A 24-hour line chart: one y-axis, one line per fleet, hover readout."""
    w, h, left, right, top, bottom = 300, 84, 30, 34, 8, 18
    values = [v for _, _, vs in series for v in vs if v is not None]
    peak = max(values, default=0)
    ymax = max(1, peak)
    # Round the top tick to a number worth reading.
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000):
        if ymax <= step * 4:
            ymax = -(-ymax // step) * step
            break
    n = len(ticks) - 1
    x = lambda i: left + (w - left - right) * i / n  # noqa: E731
    y = lambda v: top + (h - top - bottom) * (1 - v / ymax)  # noqa: E731
    parts = [
        f'<line class="grid" x1="{left}" x2="{w - right}" y1="{y(ymax):.1f}" y2="{y(ymax):.1f}"/>',
        f'<line class="axis" x1="{left}" x2="{w - right}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>',
        f'<text class="tick" x="{left - 4}" y="{y(ymax) + 3:.1f}" text-anchor="end">{num(ymax)}</text>',
        f'<text class="tick" x="{left - 4}" y="{y(0) + 3:.1f}" text-anchor="end">0</text>',
        f'<text class="tick" x="{left}" y="{h - 4}">-24h</text>',
        f'<text class="tick" x="{x(n / 2):.1f}" y="{h - 4}" text-anchor="middle">-12h</text>',
        f'<text class="tick" x="{w - right}" y="{h - 4}" text-anchor="end">now</text>',
    ]
    for cls, _, vs in series:
        d, pen = [], "M"
        for i, v in enumerate(vs):
            if v is None:
                pen = "M"
                continue
            d.append(f"{pen}{x(i):.1f},{y(v):.1f}")
            pen = "L"
        if d:
            parts.append(f'<path class="line {cls}" d="{" ".join(d)}"/>')
        last = next(
            ((i, v) for i, v in reversed(list(enumerate(vs))) if v is not None), None
        )
        if last:
            parts.append(
                f'<circle class="dot {cls}" cx="{x(last[0]):.1f}" cy="{y(last[1]):.1f}" r="4"/>'
            )
    data = json.dumps(
        {
            "ticks": ticks,
            "series": [{"cls": c, "name": nm, "values": vs} for c, nm, vs in series],
        }
    )
    legend = "".join(
        f'<span class="key"><i class="k {c}"></i>{E(nm)}</span>' for c, nm, _ in series
    )
    return (
        f'<figure class="chart"><figcaption><span>{E(title)}</span>{legend}</figcaption>'
        f'<div class="plot" data-chart="{E(data)}" data-geom="{left},{right},{w}">'
        f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{E(title)}, last 24 hours">'
        f'{"".join(parts)}<line class="crosshair" x1="0" x2="0" y1="{top}" y2="{h - bottom}"/></svg>'
        '<div class="tip" hidden></div></div></figure>'
    )


def history_table(snap: dict, topo: dict) -> str:
    ticks, hist = snap["history_ticks"], topo["history"]
    rows = "".join(
        f'<tr><td><time data-ts="{ticks[i]}"></time></td>'
        f"<td>{num(hist['bare_waiting'][i])}</td><td>{num(hist['kube_pending'][i])}</td>"
        f"<td>{num(hist['bare_chips'][i])}</td><td>{num(hist['kube_chips'][i])}</td></tr>"
        for i in range(len(ticks) - 1, -1, -4)
    )
    return (
        '<details><summary>Hourly values</summary><table class="num"><thead><tr><th>Time</th>'
        "<th>Bare waiting</th><th>Kube pending</th><th>Bare chips busy</th><th>Kube chips used</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></details>"
    )


def build_link(r: dict) -> str:
    title = E(
        f"{r['branch']} - {r['message']}" if r.get("message") else r.get("branch", "")
    )
    return (
        f'<a href="{E(r["url"])}" title="{title}">{E(r["pipeline"])} #{r["number"]}</a>'
    )


def render_topology(snap: dict, t: dict) -> str:
    b, k = t["bare"], t["kube"]
    hist = t["history"]
    builds = (
        "".join(
            "<tr>"
            f'<td>{build_link(r)}</td><td class="branch">{E(r["branch"])}</td><td>{E(r["source"])}</td>'
            f"<td>{num(r['priority'])}</td>"
            f"<td>{num(r['bare_waiting']) if r['bare_waiting'] else ''}</td>"
            f"<td>{num(r['bare_running']) if r['bare_running'] else ''}</td>"
            f"<td>{num(r['kube_pending']) if r['kube_pending'] else ''}</td>"
            f"<td>{num(r['kube_running']) if r['kube_running'] else ''}</td>"
            f"<td>{ago(r['oldest_wait'])}</td></tr>"
            for r in t["builds"]
        )
        or '<tr><td colspan="9" class="empty">Nothing waiting or running.</td></tr>'
    )
    reasons = "".join(f"<li><b>{n}</b> {E(msg)}</li>" for msg, n in k["reasons"])
    quota = (
        f"{num(k['used'])} chips in use; nominal {num(k['nominal'])}"
        + (
            f", may borrow {num(k['borrowing_limit'])} more"
            if k["borrowing_limit"]
            else ""
        )
        if k["nominal"] is not None
        else "queue not found on the manager"
    )
    cohort = (
        f"Cohort {E(k['cohort'])}: {num(k['cohort_used'])} of {num(k['cohort_nominal'])} chips in use"
        if k["cohort"]
        else ""
    )
    return f"""
<section id="{E(t["bare_queue"])}">
  <h2>{E(t["label"])}</h2>
  <div class="sides">
    <div class="side"><h3><i class="k bare"></i>Bare metal <code>{E(t["bare_queue"])}</code></h3>
      <p class="stat"><b>{num(b["waiting"])}</b> waiting <span>oldest {ago(b["oldest_wait"])}</span></p>
      <p>{num(b["agents_busy"])} of {num(b["agents_total"])} agents busy</p></div>
    <div class="side"><h3><i class="k kube"></i>Kube <code>{E(t["kube_queue"])}</code></h3>
      <p class="stat"><b>{num(k["pending"])}</b> pending <span>oldest {ago(k["oldest_wait"])}</span></p>
      <p>{num(k["admitted"])} admitted{f", {k['dispatching']} dispatching" if k["dispatching"] else ""};
         {quota}</p><p class="muted">{cohort}</p></div>
  </div>
  <div class="charts">
    {sparkline("Steps waiting", snap["history_ticks"], [("bare", "bare", hist["bare_waiting"]), ("kube", "kube", hist["kube_pending"])])}
    {sparkline("Chips in use", snap["history_ticks"], [("bare", "bare", hist["bare_chips"]), ("kube", "kube", hist["kube_chips"])])}
  </div>
  {f'<div class="reasons"><h4>Why kube is pending</h4><ul>{reasons}</ul></div>' if reasons else ""}
  <table class="num builds"><thead><tr><th>Build</th><th>Branch</th><th>Source</th><th>Priority</th>
    <th>Bare waiting</th><th>Bare running</th><th>Kube pending</th><th>Kube running</th><th>Oldest wait</th>
  </tr></thead><tbody>{builds}</tbody></table>
  {history_table(snap, t)}
</section>"""


def render(snap: dict) -> str:
    summary = "".join(
        f'<tr><td><a href="#{E(t["bare_queue"])}">{E(t["label"])}</a></td>'
        f"<td>{num(t['bare']['agents_busy'])}/{num(t['bare']['agents_total'])}</td>"
        f"<td>{num(t['bare']['waiting'])}</td><td>{ago(t['bare']['oldest_wait'])}</td>"
        f"<td>{num(t['kube']['used'])}/{num(t['kube']['nominal'])}</td>"
        f"<td>{num(t['kube']['admitted'])}</td><td>{num(t['kube']['pending'])}</td>"
        f"<td>{ago(t['kube']['oldest_wait'])}</td></tr>"
        for t in snap["topologies"]
    )
    errors = "".join(
        f'<p class="error">{E(s["name"])} failed: {E(s["error"])}'
        + (f' - showing data from <time data-ts="{s["at"]}"></time>' if s["at"] else "")
        + "</p>"
        for s in snap["sources"].values()
        if s["error"]
    )
    unplaced = (
        "".join(
            f'<tr><td>{build_link(r)}</td><td class="branch">{E(r["branch"])}</td><td>{E(r["source"])}</td>'
            f"<td>{num(r['priority'])}</td><td>{num(r['limited']) if r['limited'] else ''}</td>"
            f"<td>{num(r['waiting_agent']) if r['waiting_agent'] else ''}</td><td>{ago(r['oldest_wait'])}</td></tr>"
            for r in snap["unplaced"]
        )
        or '<tr><td colspan="7" class="empty">None.</td></tr>'
    )
    other = "".join(
        f"<tr><td>{E(o['fleet'])}</td><td><code>{E(o['queue'])}</code></td>"
        f"<td>{num(o['busy'])}{'/' + num(o['agents_total']) if o.get('agents_total') is not None else ''}</td>"
        f"<td>{num(o['waiting'])}</td></tr>"
        for o in snap["other"]
    )
    sections = "".join(render_topology(snap, t) for t in snap["topologies"])
    return PAGE.format(
        generated=snap["generated_at"],
        errors=errors,
        summary=summary,
        sections=sections,
        unplaced=unplaced,
        other=other,
    )


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>TPU CI queues</title>
<style>
.viz-root {{
  color-scheme: light;
  --surface: #fcfcfb; --page: #f9f9f7; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10); --critical: #d03b3b;
  --bare: #2a78d6; --kube: #eb6834;
}}
@media (prefers-color-scheme: dark) {{
  .viz-root {{
    color-scheme: dark;
    --surface: #1a1a19; --page: #0d0d0d; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --bare: #3987e5; --kube: #d95926;
  }}
}}
body {{ margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width: 1180px; margin: 0 auto; padding: 20px; }}
h1 {{ font-size: 20px; margin: 0 0 4px; }} h2 {{ font-size: 17px; margin: 0 0 10px; }}
h3 {{ font-size: 13px; font-weight: 600; margin: 0 0 4px; color: var(--ink-2); }}
h4 {{ font-size: 13px; margin: 0 0 4px; }}
a {{ color: inherit; }} code {{ font-size: 12px; }}
.muted, .tick, figcaption {{ color: var(--muted); }}
header p {{ margin: 0; color: var(--ink-2); }}
section, .card {{ background: var(--surface); border: 1px solid var(--ring); border-radius: 8px;
  padding: 16px; margin: 16px 0; }}
.error {{ color: var(--critical); font-weight: 600; }}
.error::before {{ content: "! "; }}
.sides {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
.side p {{ margin: 2px 0; }}
.stat b {{ font-size: 26px; font-weight: 600; }} .stat span {{ color: var(--ink-2); margin-left: 6px; }}
.charts {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin: 12px 0; }}
.chart {{ margin: 0; }}
figcaption {{ display: flex; gap: 12px; font-size: 12px; margin-bottom: 2px; }}
figcaption span:first-child {{ color: var(--ink-2); font-weight: 600; margin-right: auto; }}
.key {{ display: inline-flex; align-items: center; gap: 4px; }}
i.k {{ display: inline-block; width: 12px; height: 2px; border-radius: 1px; vertical-align: middle;
  margin-right: 4px; }}
i.k.bare {{ background: var(--bare); }} i.k.kube {{ background: var(--kube); }}
.plot {{ position: relative; }}
svg {{ width: 100%; height: auto; display: block; overflow: visible; }}
.grid {{ stroke: var(--grid); stroke-width: 1; }} .axis {{ stroke: var(--axis); stroke-width: 1; }}
.tick {{ font-size: 9px; fill: var(--muted); font-variant-numeric: tabular-nums; }}
.line {{ fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }}
.line.bare {{ stroke: var(--bare); }} .line.kube {{ stroke: var(--kube); }}
.dot {{ stroke: var(--surface); stroke-width: 2; }}
.dot.bare {{ fill: var(--bare); }} .dot.kube {{ fill: var(--kube); }}
.crosshair {{ stroke: var(--axis); stroke-width: 1; visibility: hidden; }}
.tip {{ position: absolute; top: 0; pointer-events: none; background: var(--surface);
  border: 1px solid var(--ring); border-radius: 6px; padding: 6px 8px; font-size: 12px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.12); white-space: nowrap; z-index: 1; }}
.tip .row {{ display: flex; align-items: center; gap: 6px; }} .tip b {{ min-width: 2.5em; }}
.tip .when {{ color: var(--muted); margin-bottom: 2px; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 8px; }}
th, td {{ text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }}
th {{ font-size: 12px; font-weight: 600; color: var(--ink-2); }}
table.num td {{ font-variant-numeric: tabular-nums; }}
td.branch {{ max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
td.empty {{ color: var(--muted); }}
.reasons ul {{ margin: 0; padding-left: 18px; }} .reasons li {{ color: var(--ink-2); }}
.reasons li b {{ color: var(--ink); margin-right: 4px; }}
details {{ margin-top: 8px; }} summary {{ cursor: pointer; color: var(--ink-2); font-size: 12px; }}
@media (max-width: 760px) {{ .sides, .charts {{ grid-template-columns: 1fr; }} }}
</style></head>
<body class="viz-root"><main>
<header><h1>TPU CI queues</h1>
<p>Bare-metal Buildkite queues beside the Kueue queues replacing them, per topology.
Updated <time data-ts="{generated}"></time>; the page reloads every minute.</p>
<p class="muted">Bare <i>waiting</i> = Buildkite jobs scheduled with no agent.
Kube <i>pending</i> = Kueue workloads not yet admitted; steps Buildkite still holds are listed separately below.
Hover a chart for values. Data as JSON at <a href="api/snapshot">api/snapshot</a>.</p>
{errors}</header>
<div class="card"><table class="num"><thead><tr><th>Topology</th>
<th>Bare agents busy</th><th>Bare waiting</th><th>Bare oldest</th>
<th>Kube chips used/nominal</th><th>Kube admitted</th><th>Kube pending</th><th>Kube oldest</th>
</tr></thead><tbody>{summary}</tbody></table></div>
{sections}
<section><h2>Kube steps not yet in Kueue</h2>
<p class="muted">Steps on the <code>kube</code> queue that Buildkite has not handed to the cluster:
held by a concurrency group, or waiting for an agent pod. Their topology is decided by the launcher, so it is not known yet.</p>
<table class="num"><thead><tr><th>Build</th><th>Branch</th><th>Source</th><th>Priority</th>
<th>Concurrency-limited</th><th>Waiting for agent</th><th>Oldest wait</th></tr></thead>
<tbody>{unplaced}</tbody></table></section>
<section><h2>Other queues</h2><table class="num"><thead><tr><th>Fleet</th><th>Queue</th>
<th>Busy</th><th>Waiting</th></tr></thead><tbody>{other}</tbody></table></section>
</main>
<script>
for (const el of document.querySelectorAll("time[data-ts]")) {{
  const d = new Date(Number(el.dataset.ts) * 1000);
  el.dateTime = d.toISOString();
  el.textContent = d.toLocaleString([], {{month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}});
}}
for (const plot of document.querySelectorAll(".plot")) {{
  const data = JSON.parse(plot.dataset.chart);
  const [left, right, width] = plot.dataset.geom.split(",").map(Number);
  const svg = plot.querySelector("svg"), tip = plot.querySelector(".tip");
  const hair = svg.querySelector(".crosshair"), n = data.ticks.length - 1;
  const hide = () => {{ tip.hidden = true; hair.style.visibility = "hidden"; }};
  svg.addEventListener("pointerleave", hide);
  svg.addEventListener("pointermove", (ev) => {{
    const box = svg.getBoundingClientRect(), scale = width / box.width;
    const sx = (ev.clientX - box.left) * scale;
    const i = Math.max(0, Math.min(n, Math.round((sx - left) / (width - left - right) * n)));
    const x = left + (width - left - right) * i / n;
    hair.setAttribute("x1", x); hair.setAttribute("x2", x); hair.style.visibility = "visible";
    tip.replaceChildren();
    const when = document.createElement("div"); when.className = "when";
    when.textContent = new Date(data.ticks[i] * 1000).toLocaleString([], {{hour: "2-digit", minute: "2-digit"}});
    tip.append(when);
    for (const s of data.series) {{
      const row = document.createElement("div"); row.className = "row";
      const key = document.createElement("i"); key.className = "k " + s.cls;
      const val = document.createElement("b"); val.textContent = s.values[i] ?? "-";
      const name = document.createElement("span"); name.textContent = s.name;
      row.append(key, val, name); tip.append(row);
    }}
    tip.hidden = false;
    const px = x / scale, tw = tip.offsetWidth;
    tip.style.left = Math.max(0, Math.min(box.width - tw, px + 10)) + "px";
  }});
}}
</script></body></html>
"""


# --------------------------------------------------------------------------
# Server


def serve(dashboard: Dashboard, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
            path = urllib.parse.urlparse(self.path).path
            if path == "/healthz":
                return self.reply(200, "text/plain", b"ok")
            if path == "/api/snapshot":
                body = json.dumps(dashboard.snapshot(), indent=1).encode()
                return self.reply(200, "application/json", body)
            if path == "/":
                return self.reply(
                    200,
                    "text/html; charset=utf-8",
                    render(dashboard.snapshot()).encode(),
                )
            return self.reply(404, "text/plain", b"not found")

        def reply(self, code: int, kind: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            # One line per request on stderr, which Cloud Run collects.
            print(f"{self.command} {self.path} {fmt % args}", flush=True)

    ThreadingHTTPServer(("", port), Handler).serve_forever()


def main() -> None:
    global LOCAL
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--local",
        action="store_true",
        help="use gcloud and the bk CLI for credentials instead of the metadata server",
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args()
    LOCAL = args.local
    serve(Dashboard(Config()), args.port)


if __name__ == "__main__":
    main()
