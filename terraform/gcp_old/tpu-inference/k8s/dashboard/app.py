#!/usr/bin/env python3
"""Health of the kube TPU CI fleet: quota, borrowing, waits, failures, events.

Buildkite's queue page cannot show the kube fleet by topology. Every kube step
sits on the one `kube` queue, and its topology is chosen later, by the
launcher, as a Kueue queue. This page is the view an on-call, a maintainer and
an owner each need, top to bottom:

- Health checks: is each part of the path from Buildkite to a TPU pod working
  right now, and is anything waiting or running past the budgets that will
  kill it.
- Quota now: each cohort's nominal chips, what is in use, and per queue what it
  borrows or leaves idle.
- The last 24 hours or 7 days: chip utilization, outcomes split into test and
  infrastructure failures, and how long workloads waited, started and ran.
- Per ClusterQueue: workloads admitted and pending, the builds they belong to,
  and why the pending ones are pending.
- Cluster events, and kube steps Buildkite has not handed to the cluster yet.

Sources, each for what only it has:

- Kueue and the Kubernetes API on every cluster, read live through Connect
  Gateway: queues, quota, workloads and the build each belongs to (the launcher
  labels every Job and JobSet it submits), worker connections, agent pods,
  events.
- Buildkite's REST API: kube steps not yet in Kueue, and each build's branch,
  source and priority.
- Cloud Monitoring: Kueue's and the Buildkite controller's metrics, and the
  TPU duty cycle GKE reports per node.
- BigQuery: kube_workload_timing, one row per finished workload, written by
  the launcher - outcomes and phase timings.

Nothing polls in the background of an idle instance. A page view is served the
last snapshot and a stale one is refreshed behind it, so a dashboard nobody is
looking at makes no API calls - which matters for Buildkite, whose rate limit
the whole org shares.

Run locally against the live fleet, with your own credentials:

    ./app.py --local     # gcloud for Google APIs, `bk api` for Buildkite
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import math
import os
import re
import string
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TPU = "google.com/tpu"

# Display names and order for the TPU families the fleet's queue names start
# with, and the `model` label GKE's duty-cycle metric gives each. A family not
# listed still shows, after these, without a busy line.
GENERATIONS = {"tpu7x": "v7x", "ct6e": "v6e"}
DUTY_MODELS = {"tpu7x": "tpu7x", "ct6e": "tpu-v6e-slice"}

# Buildkite job states, by what they mean for a kube step that has not reached
# Kueue. "waiting" (dependencies not met) and "blocked" are left out: those jobs
# are not asking for anything yet.
BK_WAITING = {"scheduled", "assigned", "accepted", "reserved"}
BK_LIMITED = {"limited", "limiting"}
BK_ACTIVE_BUILDS = "state[]=running&state[]=scheduled&state[]=failing&state[]=canceling"

# kube_workload_timing outcomes that are the fleet's fault rather than the
# test's. "failed" is the test exiting non-zero; "cancelled" is Buildkite.
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
# Normal events worth showing beside the warnings.
NOTABLE_NORMAL = ("TriggeredScaleUp", "NotTriggerScaleUp", "Preempted", "Evicted")

RANGES = {
    "24h": {
        "seconds": 86400,
        "step": 900,
        "every": 4,
        "label": "24 hours",
        "ticks": ("-24h", "-12h", "now"),
    },
    "7d": {
        "seconds": 7 * 86400,
        "step": 3600,
        "every": 6,
        "label": "7 days",
        "ticks": ("-7d", "-3.5d", "now"),
    },
}

# Thresholds for the health checks, as a share of the budgets the launcher
# enforces - a workload past them is about to be killed by the fleet itself.
WARN_SHARE, FAIL_SHARE = 0.25, 0.8
DISPATCH_STALL_SECONDS = 300
STUCK_POD_SECONDS = 600

PAGE = string.Template((Path(__file__).parent / "page.html").read_text())
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
        # which is not what the quota numbers report.
        self.metrics_cluster = env("KUEUE_METRICS_CLUSTER")
        # Every cluster in the fleet, manager included, for events and health.
        self.clusters = json.loads(
            env(
                "CLUSTERS",
                json.dumps([{"name": self.metrics_cluster, "gateway": self.gateway}]),
            )
        )
        self.org = env("BUILDKITE_ORG")
        self.cluster_id = env("BUILDKITE_CLUSTER_ID")
        self.queue = env("BUILDKITE_QUEUE", "kube")
        self.token = os.environ.get("BUILDKITE_API_TOKEN", "")
        self.timing_table = env("TIMING_TABLE", "")
        self.queue_budget = int(env("QUEUE_BUDGET_SECONDS", "43200"))
        self.test_budget = int(env("TEST_BUDGET_SECONDS", "28800"))
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


def request_json(url: str, headers: dict[str, str], body: dict | None = None) -> object:
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers = {**headers, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        # The status alone says nothing; the body says which query was wrong.
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(
            f"HTTP {e.code} from {url.split('?', 1)[0]}: {detail}"
        ) from None


def google(url: str, body: dict | None = None) -> dict:
    return request_json(url, {"Authorization": f"Bearer {GOOGLE_TOKEN.get()}"}, body)


def buildkite(cfg: Config, path: str) -> list | dict:
    """GET an org-relative REST path, e.g. /builds?state=running."""
    if LOCAL:
        out = subprocess.run(
            ["bk", "api", path], capture_output=True, text=True, check=True
        )
        return json.loads(out.stdout)
    url = f"https://api.buildkite.com/v2/organizations/{cfg.org}{path}"
    return request_json(url, {"Authorization": f"Bearer {cfg.token}"})


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


def quantity(value: object) -> float:
    """A Kubernetes quantity of chips or cores: "4", 8, "500m"."""
    text = str(value or 0)
    if text.endswith("m"):
        return float(text[:-1]) / 1000
    return float(text)


JOB_ID = re.compile(
    r"(?:bk|buildkite)-([0-9a-f]{8})-?([0-9a-f]{4})-?([0-9a-f]{4})-?([0-9a-f]{4})-?([0-9a-f]{12})"
)


def job_hex(text: str) -> str:
    """The Buildkite job id inside a bk-<id> or buildkite-<uuid> object name."""
    m = JOB_ID.search(text or "")
    return "".join(m.groups()) if m else ""


# --------------------------------------------------------------------------
# Buildkite


class Cached:
    """A value that changes rarely - the cluster's pipelines."""

    def __init__(self, fetch, ttl: float) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._value = None
        self._at = 0.0

    def get(self):
        if self._value is None or time.time() - self._at > self._ttl:
            self._value, self._at = self._fetch(), time.time()
        return self._value


def fetch_buildkite(cfg: Config, pipelines: Cached) -> dict:
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

    meta, priorities, job_builds, held = {}, {}, {}, []
    rule = f"queue={cfg.queue}"
    for b in builds:
        kube = [
            j
            for j in b.get("jobs") or []
            if j.get("type") == "script" and rule in (j.get("agent_query_rules") or [])
        ]
        if not kube:
            continue
        pipeline, number = b["pipeline"]["slug"], b["number"]
        meta[f"{pipeline}#{number}"] = {
            "url": b["web_url"],
            "branch": b.get("branch") or "",
            "source": b.get("source") or "",
            "message": (b.get("message") or "").split("\n", 1)[0][:120],
        }
        for j in kube:
            priority = (j.get("priority") or {}).get("number", 0)
            priorities[j["id"]] = priority
            job_builds[j["id"].replace("-", "")] = [pipeline, number]
            if j.get("state") in BK_WAITING | BK_LIMITED:
                held.append(
                    {
                        "pipeline": pipeline,
                        "number": number,
                        "limited": j["state"] in BK_LIMITED,
                        "priority": priority,
                        "runnable_at": parse_time(j.get("runnable_at")),
                    }
                )
    return {
        "meta": meta,
        "priorities": priorities,
        "job_builds": job_builds,
        "held": held,
    }


# --------------------------------------------------------------------------
# Kueue and the manager


def queue_resource(cq: dict) -> str:
    """What a ClusterQueue meters: chips for a TPU queue, cores for the CPU one."""
    covered = [
        r for g in cq["spec"].get("resourceGroups", []) for r in g["coveredResources"]
    ]
    return TPU if TPU in covered else (covered[0] if covered else "")


def cq_summary(cq: dict) -> dict:
    resource = queue_resource(cq)
    nominal = borrowing = 0.0
    for group in cq["spec"].get("resourceGroups", []):
        for flavor in group["flavors"]:
            for r in flavor["resources"]:
                if r["name"] == resource:
                    nominal += quantity(r.get("nominalQuota"))
                    borrowing += quantity(r.get("borrowingLimit"))
    used = borrowed = 0.0
    for flavor in cq.get("status", {}).get("flavorsUsage", []):
        for r in flavor["resources"]:
            if r["name"] == resource:
                used += quantity(r.get("total"))
                borrowed += quantity(r.get("borrowed"))
    preemption = cq["spec"].get("preemption") or {}
    return {
        "name": cq["metadata"]["name"],
        "resource": resource,
        "cohort": cq["spec"].get("cohortName") or cq["spec"].get("cohort") or "",
        "nominal": nominal,
        "borrowing_limit": borrowing,
        "used": used,
        "borrowed": borrowed,
        # Any: this queue takes lent quota back by evicting whoever borrowed it.
        "reclaim": preemption.get("reclaimWithinCohort", "Never"),
    }


def workload_amount(wl: dict, resource: str) -> float:
    admission = wl.get("status", {}).get("admission")
    if admission:
        return sum(
            quantity(a.get("resourceUsage", {}).get(resource))
            for a in admission.get("podSetAssignments", [])
        )
    total = 0.0
    for ps in wl["spec"].get("podSets", []):
        per_pod = 0.0
        for c in ps["template"]["spec"].get("containers", []):
            res = c.get("resources", {})
            per_pod += quantity(
                (res.get("requests") or {}).get(resource)
                or (res.get("limits") or {}).get(resource)
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


def conditions_true(obj: dict, kind: str) -> tuple[bool, str]:
    for c in obj.get("status", {}).get("conditions", []):
        if c["type"] == kind:
            return c["status"] == "True", c.get("message") or c.get("reason") or ""
    return False, "no status"


def fetch_kueue(cfg: Config) -> dict:
    kueue = f"{cfg.gateway}/apis/kueue.x-k8s.io/v1beta2"
    ns = cfg.namespace
    by_build = "labelSelector=buildkite.com%2Fpipeline"
    urls = {
        "cqs": f"{kueue}/clusterqueues",
        "lqs": f"{kueue}/namespaces/{ns}/localqueues",
        "wls": f"{kueue}/namespaces/{ns}/workloads",
        "mkcs": f"{kueue}/multikueueclusters",
        "checks": f"{kueue}/admissionchecks",
        "jobs": f"{cfg.gateway}/apis/batch/v1/namespaces/{ns}/jobs?{by_build}",
        "jobsets": f"{cfg.gateway}/apis/jobset.x-k8s.io/v1alpha2/namespaces/{ns}/jobsets?{by_build}",
        # The agent pods agent-stack-k8s creates, one per kube step.
        "pods": f"{cfg.gateway}/api/v1/namespaces/{ns}/pods?labelSelector=buildkite.com%2Fjob-uuid",
    }
    with concurrent.futures.ThreadPoolExecutor(len(urls)) as pool:
        got = dict(zip(urls, pool.map(lambda u: google(u)["items"], urls.values())))

    queues = [cq_summary(cq) for cq in got["cqs"]]
    resources = {q["name"]: q["resource"] for q in queues}
    lq_to_cq = {lq["metadata"]["name"]: lq["spec"]["clusterQueue"] for lq in got["lqs"]}
    owners = {
        (kind, o["metadata"]["name"]): o["metadata"].get("labels", {})
        for kind, key in (("Job", "jobs"), ("JobSet", "jobsets"))
        for o in got[key]
    }
    job_builds = {
        labels["buildkite.com/job-id"].replace("-", ""): [
            labels.get("buildkite.com/pipeline", ""),
            int(labels.get("buildkite.com/build-number") or 0),
        ]
        for labels in owners.values()
        if labels.get("buildkite.com/job-id")
    }

    now = time.time()
    workloads = []
    for wl in got["wls"]:
        state = workload_state(wl, now)
        if state is None:
            continue
        admission = wl.get("status", {}).get("admission") or {}
        queue = admission.get("clusterQueue") or lq_to_cq.get(
            wl["spec"].get("queueName"), wl["spec"].get("queueName")
        )
        labels = {}
        for ref in wl["metadata"].get("ownerReferences", []):
            labels = owners.get((ref["kind"], ref["name"]), labels)
        workloads.append(
            {
                **state,
                "queue": queue,
                "amount": workload_amount(wl, resources.get(queue, TPU)),
                "priority": wl["spec"].get("priority", 0),
                "pipeline": labels.get("buildkite.com/pipeline", ""),
                "number": int(labels.get("buildkite.com/build-number") or 0),
                "job_id": labels.get("buildkite.com/job-id", ""),
            }
        )

    workers = []
    for m in got["mkcs"]:
        active, message = conditions_true(m, "Active")
        workers.append(
            {"name": m["metadata"]["name"], "active": active, "message": message}
        )
    checks = []
    for c in got["checks"]:
        active, message = conditions_true(c, "Active")
        checks.append(
            {"name": c["metadata"]["name"], "active": active, "message": message}
        )
    pods = []
    for p in got["pods"]:
        scheduled = next(
            (
                c
                for c in p["status"].get("conditions", [])
                if c["type"] == "PodScheduled"
            ),
            {},
        )
        pods.append(
            {
                "phase": p["status"].get("phase", ""),
                "created": parse_time(p["metadata"]["creationTimestamp"]),
                "job": p["metadata"]
                .get("labels", {})
                .get("buildkite.com/job-uuid", "")
                .replace("-", ""),
                "unscheduled": scheduled.get("status") == "False",
                "message": scheduled.get("message") or "",
            }
        )
    return {
        "queues": queues,
        "workloads": workloads,
        "workers": workers,
        "checks": checks,
        "pods": pods,
        "job_builds": job_builds,
    }


# --------------------------------------------------------------------------
# Events


def fetch_events(cfg: Config) -> dict:
    """Warnings and a few notable normal events, from every cluster's namespace."""
    selectors = ["type=Warning"] + [f"type=Normal,reason={r}" for r in NOTABLE_NORMAL]
    calls = {
        (c["name"], sel): (
            f"{c['gateway']}/api/v1/namespaces/{cfg.namespace}/events?"
            + urllib.parse.urlencode({"fieldSelector": sel})
        )
        for c in cfg.clusters
        for sel in selectors
    }
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        futures = {key: pool.submit(google, url) for key, url in calls.items()}
    events, errors, seen = [], {}, set()
    cutoff = time.time() - 3600
    for (cluster, _), future in futures.items():
        try:
            items = future.result()["items"]
        except Exception as e:  # noqa: BLE001 - one cluster's failure is reported, not fatal
            errors[cluster] = f"{type(e).__name__}: {e}"[:200]
            continue
        for e in items:
            if e["metadata"]["uid"] in seen:
                continue
            seen.add(e["metadata"]["uid"])
            last = parse_time(
                e.get("lastTimestamp")
                or (e.get("series") or {}).get("lastObservedTime")
                or e.get("eventTime")
                or e["metadata"]["creationTimestamp"]
            )
            if last is None or last < cutoff:
                continue
            obj = e.get("involvedObject", {})
            events.append(
                {
                    "cluster": cluster,
                    "type": e.get("type", ""),
                    "reason": e.get("reason", ""),
                    "kind": obj.get("kind", ""),
                    "name": obj.get("name", ""),
                    "message": e.get("message") or "",
                    "count": e.get("count")
                    or (e.get("series") or {}).get("count")
                    or 1,
                    "last": last,
                }
            )
    return {"events": events, "errors": errors}


# --------------------------------------------------------------------------
# Cloud Monitoring


def grid(now: float, seconds: int, step: int) -> list[float]:
    end = now - now % step
    return [end - seconds + i * step for i in range(seconds // step + 1)]


def resample(
    points: list[tuple[float, float]], ticks: list[float], step: int
) -> list[float | None]:
    """The last value at or before each tick, if it is recent enough to trust."""
    points = sorted(points)
    out: list[float | None] = []
    i = -1
    for t in ticks:
        while i + 1 < len(points) and points[i + 1][0] <= t:
            i += 1
        out.append(points[i][1] if i >= 0 and t - points[i][0] <= 2 * step else None)
    return out


def prom_url(cfg: Config, kind: str, params: dict) -> str:
    return (
        f"https://monitoring.googleapis.com/v1/projects/{cfg.project}/location/global/"
        f"prometheus/api/v1/{kind}?" + urllib.parse.urlencode(params)
    )


def prom_range(
    cfg: Config, query: str, by: tuple[str, ...], seconds: int, step: int
) -> dict:
    end = time.time()
    url = prom_url(
        cfg,
        "query_range",
        {"query": query, "start": end - seconds - step, "end": end, "step": step},
    )
    return {
        "|".join(r["metric"].get(label, "") for label in by): [
            (float(t), float(v)) for t, v in r["values"]
        ]
        for r in google(url)["data"]["result"]
    }


def prom_now(cfg: Config, query: str, by: tuple[str, ...]) -> dict:
    url = prom_url(cfg, "query", {"query": query})
    return {
        "|".join(r["metric"].get(label, "") for label in by): float(r["value"][1])
        for r in google(url)["data"]["result"]
    }


def kueue_selector(cfg: Config) -> str:
    return f'cluster="{cfg.metrics_cluster}"'


def duty_selector(cfg: Config) -> str:
    # Plain alternation: cluster names are [a-z0-9-], and a backslash escape
    # is not valid inside a PromQL string.
    names = "|".join(c["name"] for c in cfg.clusters)
    return f'project_id="{cfg.project}",cluster_name=~"{names}"'


def fetch_health(cfg: Config) -> dict:
    sel = kueue_selector(cfg)
    queries = {
        # The Buildkite controller: is it polling, and are its creates landing.
        "monitor_up": ("max(buildkite_monitor_monitor_up)", ()),
        "creates": (
            "sum(increase(buildkite_scheduler_job_create_success_total[1h]))",
            (),
        ),
        "create_calls": (
            "sum(increase(buildkite_scheduler_job_create_calls_total[1h]))",
            (),
        ),
        "query_errors": (
            "sum(increase(buildkite_monitor_job_query_errors_total[1h]))",
            (),
        ),
        # Every cluster's Kueue controller, as Managed Prometheus last scraped it.
        "kueue_up": ('max by (cluster) (up{job=~".*kueue.*"})', ("cluster",)),
        "evictions": (
            "sum by (cluster_queue, reason) (max by (cluster_queue, reason) "
            f"(increase(kueue_evicted_workloads_total{{{sel}}}[24h])))",
            ("cluster_queue", "reason"),
        ),
    }
    with concurrent.futures.ThreadPoolExecutor(len(queries)) as pool:
        futures = {
            k: pool.submit(prom_now, cfg, q, by) for k, (q, by) in queries.items()
        }
        return {k: f.result() for k, f in futures.items()}


def fetch_history(cfg: Config, span: dict) -> dict:
    seconds, step = span["seconds"], span["step"]
    sel = kueue_selector(cfg)
    # max before sum: a rolled Kueue controller leaves the old pod's series
    # inside the lookback, and a plain sum counts both. See "Reading the
    # metrics" in ../README.md.
    per_resource = ("cluster_queue", "resource")
    queries = {
        "pending": (
            "sum by (cluster_queue) (max by (cluster_queue, status) "
            f"(kueue_pending_workloads{{{sel}}}))",
            ("cluster_queue",),
        ),
        "used": (
            "sum by (cluster_queue, resource) (max by (cluster_queue, flavor, resource) "
            f"(kueue_cluster_queue_resource_usage{{{sel}}}))",
            per_resource,
        ),
        "nominal": (
            "sum by (cluster_queue, resource) (max by (cluster_queue, flavor, resource) "
            f"(kueue_cluster_queue_nominal_quota{{{sel}}}))",
            per_resource,
        ),
        # Busy chips: TensorCore duty cycle, percent per chip, summed. Nodes that
        # do not report it - the disagg multi-host pods' - count as idle.
        "busy": (
            f"sum by (model) (kubernetes_io:node_accelerator_duty_cycle{{{duty_selector(cfg)}}}) / 100",
            ("model",),
        ),
        "present": (
            f"count by (model) (kubernetes_io:node_accelerator_duty_cycle{{{duty_selector(cfg)}}})",
            ("model",),
        ),
    }
    with concurrent.futures.ThreadPoolExecutor(len(queries) + 1) as pool:
        futures = {
            k: pool.submit(prom_range, cfg, q, by, seconds, step)
            for k, (q, by) in queries.items()
        }
        evictions = pool.submit(
            prom_now,
            cfg,
            "sum by (cluster_queue) (max by (cluster_queue, reason) "
            f"(increase(kueue_evicted_workloads_total{{{sel}}}[{seconds}s])))",
            ("cluster_queue",),
        )
        out = {k: f.result() for k, f in futures.items()}
        out["evictions"] = evictions.result()
        return out


# --------------------------------------------------------------------------
# BigQuery


def bigquery(cfg: Config, sql: str) -> list[dict]:
    url = f"https://bigquery.googleapis.com/bigquery/v2/projects/{cfg.project}/queries"
    body = google(url, {"query": sql, "useLegacySql": False, "timeoutMs": 30000})
    if not body.get("jobComplete"):
        raise TimeoutError("BigQuery query did not finish in 30 s")
    names = [f["name"] for f in body["schema"]["fields"]]
    return [
        dict(zip(names, (cell["v"] for cell in row["f"])))
        for row in body.get("rows", [])
    ]


def fetch_stats(cfg: Config, span: dict) -> dict:
    if not cfg.timing_table:
        return {"queues": [], "outcomes": []}
    start = int(time.time()) - span["seconds"]
    # ended_at is when the launcher wrote the row; the submitted_at bound is for
    # partition pruning, wide enough for the longest wait plus run.
    where = (
        f"ended_at >= TIMESTAMP_SECONDS({start}) "
        f"AND submitted_at >= TIMESTAMP_SECONDS({start - 2 * 86400})"
    )
    chip_seconds = (
        "chips * TIMESTAMP_DIFF(IFNULL(finished_at, ended_at), admitted_at, SECOND)"
    )

    def quantiles(expr: str, name: str) -> str:
        q = f"APPROX_QUANTILES({expr}, 100 IGNORE NULLS)"
        return f"{q}[SAFE_OFFSET(50)] AS {name}_p50, {q}[SAFE_OFFSET(90)] AS {name}_p90"

    per_queue = f"""
SELECT queue, COUNT(*) AS finished,
  SUM(IFNULL(requeues, 0)) AS requeues, SUM(IFNULL(redispatches, 0)) AS redispatches,
  {quantiles("TIMESTAMP_DIFF(quota_reserved_at, submitted_at, SECOND)", "wait")},
  {quantiles("TIMESTAMP_DIFF(first_pod_started_at, admitted_at, SECOND)", "startup")},
  {quantiles("TIMESTAMP_DIFF(finished_at, first_pod_started_at, SECOND)", "run")}
FROM `{cfg.timing_table}` WHERE {where} GROUP BY queue"""
    outcomes = f"""
SELECT queue, pipeline, outcome, COUNT(*) AS n, SUM({chip_seconds}) / 3600 AS chip_hours
FROM `{cfg.timing_table}` WHERE {where} GROUP BY queue, pipeline, outcome"""
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        a, b = (
            pool.submit(bigquery, cfg, per_queue),
            pool.submit(bigquery, cfg, outcomes),
        )
        return {"queues": a.result(), "outcomes": b.result()}


# --------------------------------------------------------------------------
# Snapshot


class Source:
    """One upstream, its last good result, and why the latest fetch failed."""

    def __init__(self, name: str, fetch, ttl: float) -> None:
        self.name = name
        self.fetch = fetch
        self.ttl = ttl
        self.data: dict | None = None
        self.at = 0.0
        self.error = ""
        self._lock = threading.Lock()

    def refresh_if_stale(self) -> None:
        with self._lock:
            if self.data is not None and time.time() - self.at < self.ttl:
                return
            try:
                self.data, self.at, self.error = self.fetch(), time.time(), ""
            except Exception as e:  # noqa: BLE001 - shown on the page, not raised
                self.error = f"{type(e).__name__}: {e}"[:300]
                # Retry on the next view rather than waiting out the ttl.
                self.at = 0.0 if self.data is None else self.at


class Dashboard:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        pipelines = Cached(
            lambda: {
                p["slug"]
                for p in buildkite_pages(cfg, "/pipelines")
                if p.get("cluster_id") == cfg.cluster_id
            },
            3600,
        )
        live = cfg.cache_seconds
        self.live = {
            "kueue": Source("Kueue", lambda: fetch_kueue(cfg), live),
            "buildkite": Source(
                "Buildkite", lambda: fetch_buildkite(cfg, pipelines), live
            ),
            "events": Source("Events", lambda: fetch_events(cfg), live),
            "health": Source("Metrics", lambda: fetch_health(cfg), live),
        }
        self.ranged = {
            name: {
                "history": Source(
                    "Metrics history",
                    lambda span=span: fetch_history(cfg, span),
                    live if span["seconds"] <= 86400 else 300,
                ),
                "stats": Source(
                    "BigQuery", lambda span=span: fetch_stats(cfg, span), 300
                ),
            }
            for name, span in RANGES.items()
        }
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[dict, float]] = {}
        self._refreshing: set[str] = set()

    def _sources(self, span: str) -> list[Source]:
        # The health checks always read the last 24 hours, whatever range the
        # page shows.
        return [
            *self.live.values(),
            *self.ranged[span].values(),
            self.ranged["24h"]["stats"],
        ]

    def _refresh(self, span: str) -> None:
        try:
            with concurrent.futures.ThreadPoolExecutor(8) as pool:
                list(pool.map(Source.refresh_if_stale, set(self._sources(span))))
            snapshot = build_snapshot(self.cfg, self.live, self.ranged, span)
            with self._lock:
                self._cache[span] = (snapshot, time.time())
        finally:
            with self._lock:
                self._refreshing.discard(span)

    def snapshot(self, span: str) -> dict:
        # A refresh takes seconds - most of it Buildkite - so a page view is
        # served the last snapshot and a stale one is refreshed behind it. Only
        # the first view of a range after a cold start waits. The page states
        # its own age.
        with self._lock:
            cached = self._cache.get(span)
            stale = cached is None or time.time() - cached[1] > self.cfg.cache_seconds
            start = stale and span not in self._refreshing
            if start:
                self._refreshing.add(span)
        if cached is None:
            if start:
                self._refresh(span)
            else:
                while span in self._refreshing:
                    time.sleep(0.2)
            return self._cache[span][0]
        if start:
            threading.Thread(target=self._refresh, args=(span,), daemon=True).start()
        return cached[0]


def shape(name: str) -> dict:
    """What a TPU queue name says: tpu7x-standard-4t-2x2x2 is a 2-host v7x 2x2x2."""
    m = re.fullmatch(
        r"(?P<family>[a-z0-9]+)-standard-(?P<per_vm>\d+)t-(?P<topo>[\dx]+)", name
    )
    if not m:
        return {"family": "", "generation": "", "topology": "", "chips": 0, "hosts": 0}
    chips = math.prod(int(d) for d in m["topo"].split("x"))
    return {
        "family": m["family"],
        "generation": GENERATIONS.get(m["family"], m["family"]),
        "topology": m["topo"],
        "chips": chips,
        "hosts": max(1, chips // int(m["per_vm"])),
    }


def num_or_none(value) -> float | None:
    return None if value in (None, "") else float(value)


def mean(values: list) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def add_series(*series: list) -> list:
    out = []
    for values in zip(*series):
        present = [v for v in values if v is not None]
        out.append(round(sum(present), 1) if present else None)
    return out


def build_snapshot(cfg: Config, live: dict, ranged: dict, span_name: str) -> dict:
    now = time.time()
    span = RANGES[span_name]
    kq = live["kueue"].data or {
        "queues": [],
        "workloads": [],
        "workers": [],
        "checks": [],
        "pods": [],
        "job_builds": {},
    }
    bk = live["buildkite"].data or {
        "meta": {},
        "priorities": {},
        "job_builds": {},
        "held": [],
    }
    ev = live["events"].data or {"events": [], "errors": {}}
    health = live["health"].data or {}
    hist = ranged[span_name]["history"].data or {}
    stats = ranged[span_name]["stats"].data or {"queues": [], "outcomes": []}
    stats24 = ranged["24h"]["stats"].data or {"queues": [], "outcomes": []}
    ticks = grid(now, span["seconds"], span["step"])
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

    def history(key: str, *labels: str) -> list:
        values = resample(
            hist.get(key, {}).get("|".join(labels), []), ticks, span["step"]
        )
        return [None if v is None else round(v, 1) for v in values]

    # Outcomes over the shown range, per queue and per pipeline.
    by_queue_outcome: dict[str, dict] = {}
    by_pipeline: dict[str, dict] = {}
    for row in stats["outcomes"]:
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
    timings = {r["queue"]: r for r in stats["queues"]}

    def outcome_summary(entry: dict | None) -> dict:
        entry = entry or {"finished": 0, "chip_hours": 0.0, "outcomes": {}}
        outcomes = entry["outcomes"]
        infra = {k: v for k, v in outcomes.items() if k not in TEST_OUTCOMES}
        return {
            "finished": entry["finished"],
            "succeeded": outcomes.get("succeeded", 0),
            "failed": outcomes.get("failed", 0),
            "cancelled": outcomes.get("cancelled", 0),
            "infra": sum(infra.values()),
            "infra_outcomes": sorted(infra.items(), key=lambda kv: -kv[1]),
            "chip_hours": entry["chip_hours"],
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
        used_hist = history("used", q["name"], q["resource"])
        nominal_hist = history("nominal", q["name"], q["resource"])
        t = timings.get(q["name"], {})
        queues.append(
            {
                **q,
                **shape(q["name"]),
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
                "history": {
                    "used": used_hist,
                    "nominal": nominal_hist,
                    "pending": history("pending", q["name"]),
                },
                # Utilization is a cohort number: against the queue's own nominal,
                # a queue that lives on borrowed chips reads hundreds of percent.
                "mean_used": mean(used_hist),
                "range": {
                    **outcome_summary(by_queue_outcome.get(q["name"])),
                    "evictions": hist.get("evictions", {}).get(q["name"], 0.0),
                    "requeues": int(t.get("requeues") or 0),
                    "redispatches": int(t.get("redispatches") or 0),
                    **{
                        k: num_or_none(t.get(k))
                        for k in (
                            "wait_p50",
                            "wait_p90",
                            "startup_p50",
                            "startup_p90",
                            "run_p50",
                            "run_p90",
                        )
                    },
                },
            }
        )

    family_order = list(GENERATIONS)
    queues.sort(
        key=lambda q: (
            q["resource"] != TPU,
            family_order.index(q["family"]) if q["family"] in family_order else 99,
            q["chips"],
            q["name"],
        )
    )

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
                "idle": 0.0,
                "pending": 0,
                "pending_amount": 0.0,
            },
        )
        c["queues"].append(q["name"])
        for key in ("nominal", "used", "idle", "pending", "pending_amount"):
            c[key] += q[key]
    for c in cohorts.values():
        members = [q for q in queues if q["cohort"] == c["name"]]
        c["free"] = max(0.0, c["nominal"] - c["used"])
        c["admitted_history"] = add_series(*(q["history"]["used"] for q in members))
        c["nominal_history"] = add_series(*(q["history"]["nominal"] for q in members))
        model = DUTY_MODELS.get(c["family"], "")
        c["busy_history"] = history("busy", model) if model else [None] * len(ticks)
        c["present_history"] = (
            history("present", model) if model else [None] * len(ticks)
        )
        admitted, nominal, busy = (
            mean(c["admitted_history"]),
            mean(c["nominal_history"]),
            mean(c["busy_history"]),
        )
        c["utilization"] = (
            admitted / nominal if admitted is not None and nominal else None
        )
        c["busy_share"] = busy / nominal if busy is not None and nominal else None
        c["admitted_chip_hours"] = (admitted or 0) * span["seconds"] / 3600

    events = group_events(ev["events"], job_builds, cfg.org)
    held = held_rows(bk["held"], build_row, now)
    pipelines = sorted(
        (
            {"pipeline": name, **outcome_summary(entry)}
            for name, entry in by_pipeline.items()
        ),
        key=lambda p: -p["chip_hours"],
    )

    snapshot = {
        "generated_at": now,
        "range": span_name,
        "history_ticks": ticks,
        "sources": {
            k: {"name": s.name, "at": s.at, "error": s.error}
            for k, s in {
                **live,
                **{f"{k}_{span_name}": s for k, s in ranged[span_name].items()},
            }.items()
        },
        "event_errors": ev["errors"],
        "cohorts": list(cohorts.values()),
        "queues": queues,
        "pipelines": pipelines,
        "events": events,
        "held": held,
    }
    snapshot["checks"] = health_checks(cfg, snapshot, kq, bk, ev, health, stats24, now)
    return snapshot


def held_rows(jobs: list, build_row, now: float) -> list:
    """Kube steps Buildkite has not handed to the cluster, by build. Their queue
    is not known yet - the step's command does not name it; the launcher does."""
    rows: dict = {}
    for j in jobs:
        row = rows.setdefault(
            (j["pipeline"], j["number"]),
            {
                **build_row(j["pipeline"], j["number"]),
                "limited": 0,
                "waiting_agent": 0,
                "oldest_agent_wait": None,
            },
        )
        row["limited" if j["limited"] else "waiting_agent"] += 1
        row["priority"] = max(row["priority"] or 0, j["priority"])
        if j["runnable_at"]:
            wait = now - j["runnable_at"]
            row["oldest_wait"] = max(row["oldest_wait"] or 0, wait)
            if not j["limited"]:
                row["oldest_agent_wait"] = max(row["oldest_agent_wait"] or 0, wait)
    return sorted(
        rows.values(), key=lambda r: (-(r["priority"] or 0), -(r["oldest_wait"] or 0))
    )


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
        g["fleet"] = g["type"] == "Warning" and g["reason"] in FLEET_WARNINGS
        out.append(g)
    return sorted(out, key=lambda g: (-g["fleet"], g["type"] != "Warning", -g["last"]))


def health_checks(cfg, snap, kq, bk, ev, health, stats24, now) -> list:
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
            f"{len(dispatching)} dispatching, none stalled; {redispatches} redispatches in the last 24 hours.",
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
    total = {"finished": 0, "infra": 0, "failed": 0, "outcomes": {}}
    for row in stats24["outcomes"]:
        n = int(row["n"])
        total["finished"] += n
        if row["outcome"] == "failed":
            total["failed"] += n
        elif row["outcome"] not in TEST_OUTCOMES:
            total["infra"] += n
            total["outcomes"][row["outcome"]] = (
                total["outcomes"].get(row["outcome"], 0) + n
            )
    if not cfg.timing_table or not total["finished"]:
        add(
            "unknown",
            "Infrastructure failures (24h)",
            "No finished workloads recorded.",
        )
    else:
        rate = total["infra"] / total["finished"]
        status = "fail" if rate > 0.05 else "warn" if total["infra"] else "ok"
        breakdown = ", ".join(
            f"{k} {v}"
            for k, v in sorted(total["outcomes"].items(), key=lambda kv: -kv[1])
        )
        add(
            status,
            "Infrastructure failures (24h)",
            f"{total['infra']} of {total['finished']} workloads"
            + (f" ({breakdown})" if breakdown else "")
            + f"; {total['failed']} test failures besides.",
        )

    # Evictions: preemption is reclaim working as designed; anything else is not.
    evictions = health.get("evictions", {})
    by_reason: dict[str, float] = {}
    for key, n in evictions.items():
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
            "Cluster warnings (1h)",
            "Could not read events from " + ", ".join(ev["errors"]) + ".",
        )
    elif fleet:
        add(
            "warn",
            "Cluster warnings (1h)",
            "; ".join(
                f"{g['reason']} ×{g['count']} on {g['cluster']}" for g in fleet[:4]
            )
            + ". See Cluster events.",
        )
    else:
        add(
            "ok",
            "Cluster warnings (1h)",
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
        r for r in snap["held"] if (r["oldest_agent_wait"] or 0) > STUCK_POD_SECONDS
    ]
    limited = sum(r["limited"] for r in snap["held"])
    if waiting:
        add(
            "warn",
            "Steps waiting for an agent",
            f"{sum(r['waiting_agent'] for r in waiting)} kube steps scheduled over "
            f"{STUCK_POD_SECONDS // 60} minutes with no agent pod.",
        )
    else:
        add(
            "ok",
            "Steps waiting for an agent",
            f"None stuck; {limited} held by concurrency groups, as their pipelines ask.",
        )
    return checks


# --------------------------------------------------------------------------
# Rendering

E = html.escape


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


def render_sources(snap: dict) -> str:
    out = []
    for s in snap["sources"].values():
        state = "fail" if s["error"] else "ok"
        title = E(s["error"]) if s["error"] else "ok"
        label = s["name"] + (" failed" if s["error"] else "")
        out.append(
            f'<span class="pill {state}" title="{title}"><span class="dot"></span>{E(label)}</span>'
        )
    return "".join(out)


def render_nav(snap: dict) -> str:
    links = [
        f'<a class="pill{" active" if snap["range"] == name else ""}" href="?range={name}">'
        f"{'● ' if snap['range'] == name else ''}{E(span['label'])}</a>"
        for name, span in RANGES.items()
    ]
    for q in snap["queues"]:
        if q["resource"] != TPU:
            continue
        cls = "pill attn" if q["pending"] else "pill"
        links.append(
            f'<a class="{cls}" href="#{E(q["name"])}">{E(q["generation"])} {E(q["topology"])}'
            f" · {num(q['pending'])} pending</a>"
        )
    return "".join(links)


ICONS = {
    "ok": ("✓", "OK"),
    "warn": ("!", "Warning"),
    "fail": ("✕", "Failing"),
    "info": ("i", "Note"),
    "unknown": ("?", "Unknown"),
}


def render_checks(snap: dict) -> str:
    return "".join(
        f'<div class="check {c["status"]}"><span class="icon" aria-hidden="true">{ICONS[c["status"]][0]}</span>'
        f'<span class="title">{E(c["title"])}<span class="state">{ICONS[c["status"]][1]}</span></span>'
        f'<span class="detail">{E(c["detail"])}</span></div>'
        for c in snap["checks"]
    )


def render_summary(snap: dict) -> str:
    out = []
    by_name = {q["name"]: q for q in snap["queues"]}
    for c in snap["cohorts"]:
        if c["resource"] != TPU:
            continue
        rows = "".join(
            f"""<tr><td><a href="#{E(q["name"])}" title="{E(q["name"])}">{E(queue_title(q))}</a></td>
<td class="n">{num(q["nominal"])}</td><td class="n"><b>{num(q["used"])}</b></td>
<td>{borrow_cell(q)}</td>
<td class="n">{num(q["borrowing_limit"]) if q["borrowing_limit"] else "-"}</td>
<td>{"evicts borrowers" if q["reclaim"] != "Never" else '<span class="muted">never</span>'}</td>
<td class="n">{num(q["admitted"] + q["dispatching"])}</td>
<td class="n">{num(q["pending"])}{f' <span class="muted">({num(q["pending_amount"])} chips)</span>' if q["pending"] else ""}</td>
<td class="barcell">{quota_bar(q, c["nominal"])}</td></tr>"""
            for q in (by_name[n] for n in c["queues"])
        )
        out.append(f"""
<div class="cohort">
  <h3>{E(c["generation"] or c["name"])} <span class="muted">· cohort <code>{E(c["name"])}</code></span></h3>
  <div class="tiles">
    <div class="tile"><span>Chips in use</span><b>{num(c["used"])}</b><small>of {num(c["nominal"])} nominal ({pct(c["used"] / c["nominal"] if c["nominal"] else None)})</small></div>
    <div class="tile"><span>Free</span><b>{num(c["free"])}</b><small>chips no queue is using</small></div>
    <div class="tile"><span>Pending</span><b>{num(c["pending"])}</b><small>workloads, {num(c["pending_amount"])} chips</small></div>
    <div class="tile"><span>Utilization, {E(RANGES[snap["range"]]["label"])}</span><b>{pct(c["utilization"])}</b><small>admitted; {pct(c["busy_share"])} busy, of nominal</small></div>
  </div>
  <div class="table-wrap"><table><thead><tr><th>Queue</th><th class="n">Nominal</th><th class="n">In use</th><th>Borrowing</th>
  <th class="n">May borrow</th><th>Reclaim</th><th class="n">Running</th><th class="n">Pending</th>
  <th class="barcell"><span class="key"><i class="sw own"></i>own</span><span class="key"><i class="sw borrowed"></i>borrowed</span><span class="key"><i class="sw idle"></i>idle</span></th>
  </tr></thead><tbody>{rows}</tbody></table></div>
</div>""")
    return "".join(out)


def sparkline(
    title: str, ticks: list[float], series: list, labels: tuple, ref=None, extra=None
) -> str:
    """A line chart with a hover readout. ref is (label, values), drawn as a
    stepped hairline - the quota a line is measured against. extra rows appear
    only in the readout."""
    w, h, left, right, top, bottom = 300, 84, 30, 44, 8, 18
    values = [v for _, _, vs in series for v in vs if v is not None]
    if ref:
        values += [v for v in ref[1] if v is not None]
    ymax = max(1, max(values, default=0))
    # Round the top tick to a number worth reading.
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000):
        if ymax <= step * 4:
            ymax = -(-ymax // step) * step
            break
    n = len(ticks) - 1

    def x(i: float) -> float:
        return left + (w - left - right) * i / n

    def y(v: float) -> float:
        return top + (h - top - bottom) * (1 - v / ymax)

    def path(vs: list) -> str:
        d, pen = [], "M"
        for i, v in enumerate(vs):
            if v is None:
                pen = "M"
                continue
            d.append(f"{pen}{x(i):.1f},{y(v):.1f}")
            pen = "L"
        return " ".join(d)

    parts = [
        f'<line class="grid" x1="{left}" x2="{w - right}" y1="{y(ymax):.1f}" y2="{y(ymax):.1f}"/>',
        f'<line class="axis" x1="{left}" x2="{w - right}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>',
        f'<text class="tick" x="{left - 4}" y="{y(ymax) + 3:.1f}" text-anchor="end">{num(ymax)}</text>',
        f'<text class="tick" x="{left - 4}" y="{y(0) + 3:.1f}" text-anchor="end">0</text>',
        f'<text class="tick" x="{left}" y="{h - 4}">{labels[0]}</text>',
        f'<text class="tick" x="{x(n / 2):.1f}" y="{h - 4}" text-anchor="middle">{labels[1]}</text>',
        f'<text class="tick" x="{w - right}" y="{h - 4}" text-anchor="end">{labels[2]}</text>',
    ]
    if ref and path(ref[1]):
        last = next((v for v in reversed(ref[1]) if v is not None), None)
        parts.append(f'<path class="ref" d="{path(ref[1])}"/>')
        if last is not None:
            parts.append(
                f'<text class="tick" x="{w - right + 4}" y="{y(last) + 3:.1f}">{E(ref[0])}</text>'
            )
    for cls, _, vs in series:
        d = path(vs)
        if d:
            parts.append(f'<path class="line {cls}" d="{d}"/>')
        last = next(
            ((i, v) for i, v in reversed(list(enumerate(vs))) if v is not None), None
        )
        if last:
            parts.append(
                f'<circle class="dot {cls}" cx="{x(last[0]):.1f}" cy="{y(last[1]):.1f}" r="4"/>'
            )
    readout = [{"cls": c, "name": nm, "values": vs} for c, nm, vs in series]
    if ref:
        readout.append({"cls": "ref", "name": ref[0], "values": ref[1]})
    for nm, vs in extra or []:
        readout.append({"cls": "", "name": nm, "values": vs})
    legend = (
        "".join(
            f'<span class="key"><i class="k {c}"></i>{E(nm)}</span>'
            for c, nm, _ in series
        )
        if len(series) > 1
        else ""
    )
    data = json.dumps({"ticks": ticks, "series": readout})
    return (
        f'<figure class="chart"><figcaption>{E(title)} <span style="font-weight:400">{legend}</span></figcaption>'
        f'<div class="plot" data-chart="{E(data)}" data-geom="{left},{right},{w}">'
        f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{E(title)}">'
        f'{"".join(parts)}<line class="crosshair" x1="0" x2="0" y1="{top}" y2="{h - bottom}"/></svg>'
        '<div class="tip" hidden></div></div></figure>'
    )


def values_table(
    snap: dict, columns: list[tuple[str, list]], what: str = "Values"
) -> str:
    ticks = snap["history_ticks"]
    every = RANGES[snap["range"]]["every"]
    head = "".join(f'<th class="n">{E(name)}</th>' for name, _ in columns)
    rows = "".join(
        f'<tr><td><time data-ts="{ticks[i]}"></time></td>'
        + "".join(f'<td class="n">{num(vs[i])}</td>' for _, vs in columns)
        + "</tr>"
        for i in range(len(ticks) - 1, -1, -every)
    )
    return (
        f"<details><summary>{E(what)} over the last {E(RANGES[snap['range']]['label'])}</summary>"
        f'<div class="table-wrap"><table><thead><tr><th>Time</th>{head}</tr></thead><tbody>{rows}</tbody></table></div></details>'
    )


def render_daily(snap: dict) -> str:
    span = RANGES[snap["range"]]
    charts = "".join(
        sparkline(
            f"{c['generation']} chips: admitted and busy",
            snap["history_ticks"],
            [
                ("used", "admitted", c["admitted_history"]),
                ("busy", "busy", c["busy_history"]),
            ],
            span["ticks"],
            ref=("nominal", c["nominal_history"]),
            extra=[("chips on nodes", c["present_history"])],
        )
        for c in snap["cohorts"]
        if c["resource"] == TPU
    )
    tables = "".join(
        values_table(
            snap,
            [
                (f"{c['generation']} admitted", c["admitted_history"]),
                (f"{c['generation']} busy", c["busy_history"]),
                (f"{c['generation']} nominal", c["nominal_history"]),
            ],
            what=f"{c['generation']} chip values",
        )
        for c in snap["cohorts"]
        if c["resource"] == TPU
    )

    def infra_cell(r: dict) -> str:
        if not r["infra"]:
            return '<td class="n">0</td>'
        detail = ", ".join(f"{k} {v}" for k, v in r["infra_outcomes"])
        return f'<td class="n"><b>{num(r["infra"])}</b><div class="sub-row">{E(detail)}</div></td>'

    def rate(r: dict) -> str:
        decided = r["succeeded"] + r["failed"] + r["infra"]
        return pct(r["succeeded"] / decided) if decided else "-"

    queue_rows = "".join(
        f"""<tr><td class="nowrap"><a href="#{E(q["name"])}" title="{E(q["name"])}">{E(queue_title(q))}</a></td>
<td class="n">{num(q["range"]["finished"])}</td><td class="n">{rate(q["range"])}</td>
<td class="n">{num(q["range"]["failed"])}</td>{infra_cell(q["range"])}
<td class="n">{num(q["range"]["evictions"])}</td><td class="n">{num(q["range"]["requeues"])}</td>
<td class="n">{num(q["range"]["redispatches"])}</td>
<td class="n">{ago(q["range"]["wait_p50"])} / {ago(q["range"]["wait_p90"])}</td>
<td class="n">{ago(q["range"]["startup_p50"])} / {ago(q["range"]["startup_p90"])}</td>
<td class="n">{ago(q["range"]["run_p50"])}</td>
<td class="n">{num(q["range"]["chip_hours"])}</td><td class="n">{num(q["mean_used"])}</td></tr>"""
        for q in snap["queues"]
        if q["resource"] == TPU
    )
    pipeline_rows = (
        "".join(
            f"""<tr><td>{E(p["pipeline"])}</td><td class="n">{num(p["finished"])}</td><td class="n">{rate(p)}</td>
<td class="n">{num(p["failed"])}</td>{infra_cell(p)}<td class="n">{num(p["chip_hours"])}</td></tr>"""
            for p in snap["pipelines"][:15]
        )
        or '<tr><td colspan="6" class="empty">No finished workloads.</td></tr>'
    )
    return f"""
<div class="charts">{charts}</div>
<p class="muted">Admitted = chips Kueue has given to workloads. Busy = TensorCore duty cycle summed over the nodes
that report it, in chips; the gap between them is time a workload holds chips without computing - startup, compile,
model load, idle disagg roles.</p>
{tables}
<h4 style="margin-top:20px">By queue</h4>
<div class="table-wrap"><table class="dense"><thead><tr><th>Queue</th><th class="n">Finished</th><th class="n">Success</th>
<th class="n">Test failures</th><th class="n">Infra failures</th><th class="n">Evictions</th><th class="n">Requeues</th>
<th class="n">Redispatches</th><th class="n">Wait p50 / p90</th><th class="n">Startup p50 / p90</th>
<th class="n">Run p50</th><th class="n">Chip-hours</th><th class="n">Avg in use</th></tr></thead>
<tbody>{queue_rows}</tbody></table></div>
<p class="muted">Success is over workloads that finished either way: cancelled ones are left out. Wait is submitted to
quota reserved; startup is admitted to the first workload container running.</p>
<h4 style="margin-top:20px">By pipeline</h4>
<div class="table-wrap"><table><thead><tr><th>Pipeline</th><th class="n">Finished</th><th class="n">Success</th>
<th class="n">Test failures</th><th class="n">Infra failures</th><th class="n">Chip-hours</th></tr></thead>
<tbody>{pipeline_rows}</tbody></table></div>"""


def build_link(r: dict) -> str:
    title = E(
        f"{r['branch']} - {r['message']}" if r.get("message") else r.get("branch", "")
    )
    return (
        f'<a href="{E(r["url"])}" title="{title}">{E(r["pipeline"])} #{r["number"]}</a>'
    )


def render_queue(snap: dict, q: dict) -> str:
    span = RANGES[snap["range"]]
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
    charts = sparkline(
        f"{u.capitalize()} in use",
        snap["history_ticks"],
        [("used", "in use", q["history"]["used"])],
        span["ticks"],
        ref=("nominal", q["history"]["nominal"]) if tpu else None,
    ) + sparkline(
        "Workloads pending",
        snap["history_ticks"],
        [("pending", "pending", q["history"]["pending"])],
        span["ticks"],
    )
    return f"""
<div class="card queue{"" if tpu else " other"}" id="{E(q["name"])}">
  <div class="card-head"><h2 title="{E(q["name"])}">{E(queue_title(q))}</h2></div>
  <div class="stats">
    <p class="stat"><b>{num(q["pending"])}</b><span>pending · oldest {ago(q["oldest_wait"])}</span></p>
    <p class="stat"><b>{num(q["admitted"])}</b><span>admitted{f" · {q['dispatching']} dispatching" if q["dispatching"] else ""}</span></p>
    <p class="stat"><b>{num(q["used"])}</b><span>{u} in use of {num(q["nominal"])} nominal</span></p>
  </div>
  <div class="pills">{"".join(pills)}</div>
  <div class="charts">{charts}</div>
  {f'<div class="reasons"><h4>Why workloads are pending</h4><ul>{reasons}</ul></div>' if reasons else ""}
  <div class="table-wrap"><table><thead><tr><th>Build</th><th>Branch</th><th>Source</th><th class="n">Priority</th>
    <th class="n">Pending</th><th class="n">Running</th><th class="n">{u.capitalize()} held</th><th class="n">Oldest wait</th>
  </tr></thead><tbody>{builds}</tbody></table></div>
  {values_table(snap, [(f"{u.capitalize()} in use", q["history"]["used"]), ("Nominal", q["history"]["nominal"]), ("Pending", q["history"]["pending"])])}
</div>"""


def render_events(snap: dict) -> str:
    if not snap["events"]:
        body = '<tr><td colspan="6" class="empty">No warnings or notable events in the last hour.</td></tr>'
    else:
        body = "".join(
            f"""<tr><td><time data-ts="{g["last"]}"></time></td><td>{E(g["cluster"])}</td>
<td>{'<span class="pill attn">' + E(g["reason"]) + "</span>" if g["fleet"] else E(g["reason"])}<div class="sub-row">{E(g["kind"])}{" · Warning" if g["type"] == "Warning" else ""}</div></td>
<td class="n">{num(g["count"])}{f'<div class="sub-row">{g["objects"]} objects</div>' if g["objects"] > 1 else ""}</td>
<td class="msg">{E(g["message"][:300])}</td>
<td>{"<br>".join(f'<a href="{E(url)}">{E(name)}</a>' for name, url in g["builds"][:3])}{f'<div class="sub-row">+{len(g["builds"]) - 3} more</div>' if len(g["builds"]) > 3 else ""}</td></tr>"""
            for g in snap["events"][:60]
        )
    errors = "".join(
        f'<p class="error">Events from {E(c)} failed: {E(e)}</p>'
        for c, e in snap["event_errors"].items()
    )
    return f"""{errors}<p class="muted" style="margin-bottom:8px">Warnings and scale-up, preemption and eviction events
from the <code>buildkite</code> namespace on every cluster, repeats grouped. Highlighted reasons are the fleet's
problems rather than a test's; Kueue's <i>Pending</i> warnings are the backlog the queue sections already show.</p>
<div class="table-wrap"><table><thead><tr><th>Last seen</th><th>Cluster</th><th>Reason</th><th class="n">Count</th>
<th>Message</th><th>Builds</th></tr></thead><tbody>{body}</tbody></table></div>"""


def render_held(snap: dict) -> str:
    rows = (
        "".join(
            f'<tr><td>{build_link(r)}</td><td class="branch">{E(r["branch"])}</td><td>{E(r["source"])}</td>'
            f'<td class="n">{num(r["priority"])}</td><td class="n">{num(r["limited"]) if r["limited"] else ""}</td>'
            f'<td class="n">{num(r["waiting_agent"]) if r["waiting_agent"] else ""}</td><td class="n">{ago(r["oldest_wait"])}</td></tr>'
            for r in snap["held"]
        )
        or '<tr><td colspan="7" class="empty">None.</td></tr>'
    )
    return (
        '<div class="table-wrap"><table><thead><tr><th>Build</th><th>Branch</th><th>Source</th><th class="n">Priority</th>'
        '<th class="n">Concurrency-limited</th><th class="n">Waiting for agent</th><th class="n">Oldest wait</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
    )


def render(snap: dict) -> str:
    errors = "".join(
        f'<p class="error">{E(s["name"])} failed: {E(s["error"])}'
        + (f' - showing data from <time data-ts="{s["at"]}"></time>' if s["at"] else "")
        + "</p>"
        for s in snap["sources"].values()
        if s["error"]
    )
    other_queues = [q for q in snap["queues"] if q["resource"] != TPU]
    cpu_line = "; ".join(
        f"<code>{E(q['name'])}</code> {num(q['used'])} {unit(q)} in use, "
        f"{num(q['admitted'] + q['dispatching'])} running, {num(q['pending'])} pending"
        for q in other_queues
    )
    return PAGE.substitute(
        generated=snap["generated_at"],
        sources=render_sources(snap),
        nav=render_nav(snap),
        errors=f'<div class="card errors">{errors}</div>' if errors else "",
        checks=render_checks(snap),
        summary=render_summary(snap),
        cpu_line=cpu_line or "none",
        range_label=E(RANGES[snap["range"]]["label"]),
        daily=render_daily(snap),
        tpu="".join(
            render_queue(snap, q) for q in snap["queues"] if q["resource"] == TPU
        ),
        events=render_events(snap),
        held=render_held(snap),
        other="".join(render_queue(snap, q) for q in other_queues),
    )


# --------------------------------------------------------------------------
# Server


def serve(dashboard: Dashboard, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
            url = urllib.parse.urlparse(self.path)
            span = urllib.parse.parse_qs(url.query).get("range", ["24h"])[0]
            if span not in RANGES:
                span = "24h"
            if url.path == "/healthz":
                return self.reply(200, "text/plain", b"ok")
            if url.path == "/api/snapshot":
                body = json.dumps(dashboard.snapshot(span), indent=1).encode()
                return self.reply(200, "application/json", body)
            if url.path == "/":
                page = render(dashboard.snapshot(span)).encode()
                return self.reply(200, "text/html; charset=utf-8", page)
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
