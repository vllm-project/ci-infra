"""Reads the fleet: Kueue and Kubernetes through Connect Gateway, Buildkite,
Cloud Monitoring and BigQuery. Nothing here renders; see views.py."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import math
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

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
BK_RUNNING = {"running", "canceling", "timing_out"}
BK_ACTIVE_BUILDS = "state[]=running&state[]=scheduled&state[]=failing&state[]=canceling"

# How many of a queue's pending workloads to read in Kueue's order; the page
# shows the first few and keeps the rest behind a button.
NEXT_UP = 50

# Normal events worth showing beside the warnings.
NOTABLE_NORMAL = ("TriggeredScaleUp", "NotTriggerScaleUp", "Preempted", "Evicted")

# Set by app.py: use gcloud and the bk CLI rather than the metadata server and
# a token, for running against the live fleet from a workstation.
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
        self.token_secret = os.environ.get("BUILDKITE_TOKEN_SECRET", "")
        # Cloud Run names the service in K_SERVICE; the default is for a local run.
        self.service = os.environ.get("K_SERVICE", "tpu-ci-queue-dashboard")
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


# --------------------------------------------------------------------------
# Buildkite


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

    meta, job_builds, jobs = {}, {}, []
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
            job_builds[j["id"].replace("-", "")] = [pipeline, number]
            if j.get("state") in BK_WAITING | BK_LIMITED | BK_RUNNING:
                jobs.append(
                    {
                        "id": j["id"].replace("-", ""),
                        "pipeline": pipeline,
                        "number": number,
                        "label": j.get("name") or j.get("step_key") or "",
                        "url": j.get("web_url") or b["web_url"],
                        "state": j["state"],
                        # A concurrency-held job has no runnable_at until it is
                        # released; scheduled_at is when it joined the line.
                        "runnable_at": parse_time(
                            j.get("runnable_at") or j.get("scheduled_at")
                        ),
                        "started_at": parse_time(j.get("started_at")),
                    }
                )
    return {
        "meta": meta,
        "job_builds": job_builds,
        "jobs": jobs,
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
        **shape(cq["metadata"]["name"]),
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
            # Admitted is quota; PodsReady is every pod of it actually up.
            "pods_ready": true("PodsReady"),
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
                "name": wl["metadata"]["name"],
                "created": parse_time(wl["metadata"]["creationTimestamp"]),
                # The worker MultiKueue placed it on, once one admitted it.
                "cluster": wl.get("status", {}).get("clusterName"),
                "queue": queue,
                "amount": workload_amount(wl, resources.get(queue, TPU)),
                # The WorkloadPriorityClass the launcher set, and the value Kueue
                # copied from it: what orders the queue. Buildkite's own job
                # priority is a different scale and orders nothing here. None
                # and 0 for a workload that names no class.
                "priority": wl["spec"].get("priority", 0),
                "priority_class": (wl["spec"].get("priorityClassRef") or {}).get(
                    "name"
                ),
                "pipeline": labels.get("buildkite.com/pipeline", ""),
                "number": int(labels.get("buildkite.com/build-number") or 0),
                "job_id": labels.get("buildkite.com/job-id", ""),
            }
        )

    # Kueue's order for each queue with anything pending: who it considers next,
    # from its visibility API rather than re-derived here, so the page shows
    # what Kueue does and not what it should do.
    visibility = f"{cfg.gateway}/apis/visibility.kueue.x-k8s.io/v1beta2/clusterqueues"

    def order(name: str) -> dict:
        try:
            items = google(f"{visibility}/{name}/pendingworkloads?limit={NEXT_UP}")[
                "items"
            ]
        except Exception as e:  # noqa: BLE001 - shown on the page, not raised
            return {"items": [], "error": f"{type(e).__name__}: {e}"[:200]}
        return {
            "items": [
                {
                    "workload": i["metadata"]["name"],
                    "position": i["positionInClusterQueue"],
                    "priority": i.get("priority", 0),
                }
                for i in items
            ],
            "error": "",
        }

    pending = sorted({w["queue"] for w in workloads if w["state"] == "pending"})
    with concurrent.futures.ThreadPoolExecutor(max(1, len(pending))) as pool:
        next_up = dict(zip(pending, pool.map(order, pending)))

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
                "job": p["metadata"]
                .get("labels", {})
                .get("buildkite.com/job-uuid", "")
                .replace("-", ""),
                "phase": p["status"].get("phase", ""),
                "created": parse_time(p["metadata"]["creationTimestamp"]),
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
        "next_up": next_up,
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


def prom(cfg: Config, kind: str, params: dict) -> list:
    url = (
        f"https://monitoring.googleapis.com/v1/projects/{cfg.project}/location/global/"
        f"prometheus/api/v1/{kind}?" + urllib.parse.urlencode(params)
    )
    return google(url)["data"]["result"]


def prom_range(
    cfg: Config, query: str, by: tuple, start: int, end: int, step: int
) -> dict:
    result = prom(
        cfg, "query_range", {"query": query, "start": start, "end": end, "step": step}
    )
    return {
        "|".join(r["metric"].get(label, "") for label in by): [
            (float(t), float(v)) for t, v in r["values"]
        ]
        for r in result
    }


def prom_now(cfg: Config, query: str, by: tuple, at: float | None = None) -> dict:
    params = {"query": query} | ({"time": at} if at else {})
    return {
        "|".join(r["metric"].get(label, "") for label in by): float(r["value"][1])
        for r in prom(cfg, "query", params)
    }


def kueue_selector(cfg: Config) -> str:
    return f'cluster="{cfg.metrics_cluster}"'


def duty_selector(cfg: Config) -> str:
    # Plain alternation: cluster names are [a-z0-9-], and a backslash escape is
    # not valid inside a PromQL string.
    names = "|".join(c["name"] for c in cfg.clusters)
    return f'project_id="{cfg.project}",cluster_name=~"{names}"'


def node_selector(cfg: Config) -> str:
    """TPU nodes only. GKE names a TPU node gke-tpu-<hash>-<id>, the hash being its
    node pool's instance group; CPU nodes carry the cluster name there instead."""
    return f'{duty_selector(cfg)},node_name=~"gke-tpu-[0-9a-f]{{8}}-[a-z0-9]+"'


def node_presence(cfg: Config) -> str:
    """1 per TPU node while it exists. allocatable_cores is reported for every
    node, unlike the duty cycle, which some multi-host nodes never report."""
    return (
        "count by (cluster_name, node_name) (last_over_time("
        f"kubernetes_io:node_cpu_allocatable_cores{{{node_selector(cfg)}}}[2m]))"
    )


def current(metric: str, by: str) -> str:
    """One value per `by` group, from whichever controller pod reported last.

    A rolled Kueue controller leaves the old pod's series behind for the
    five-minute lookback, so a plain sum across pods counts both (see
    "Reading the metrics" in ../README.md), and a max can pair one pod's
    reading of a queue with another pod's reading of the next. Only series
    sampled in the last two minutes are taken, which is the new pod alone
    within two minutes of a roll.
    """
    return f"max by ({by}) (last_over_time({metric}[2m]))"


def fetch_health(cfg: Config) -> dict:
    sel = kueue_selector(cfg)
    busy_duty = f"kubernetes_io:node_accelerator_duty_cycle{{{node_selector(cfg)}}}"
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
        # A counter per controller pod, and only the leader counts, so the
        # pods' increases add up to the fleet's.
        "evictions": (
            f"sum by (cluster_queue, reason) (increase(kueue_evicted_workloads_total{{{sel}}}[24h]))",
            ("cluster_queue", "reason"),
        ),
        # Chips computing right now, per node, for views.py to sum by node pool
        # as it does node presence: per queue, and per worker on the overview.
        "busy": (
            f"sum by (cluster_name, node_name) (last_over_time({busy_duty}[2m])) / 100",
            ("cluster_name", "node_name"),
        ),
        "nodes": (node_presence(cfg), ("cluster_name", "node_name")),
    }
    with concurrent.futures.ThreadPoolExecutor(len(queries)) as pool:
        futures = {
            k: pool.submit(prom_now, cfg, q, by) for k, (q, by) in queries.items()
        }
        return {k: f.result() for k, f in futures.items()}


def fetch_history(cfg: Config, start: int, end: int, step: int) -> dict:
    """Per-step series for [start, end].

    Each point is the step's average, not a sample at its end, so a short spike
    does not stand for a whole step. The average is taken over a fine
    subquery of the fleet-wide value - collapsed to one controller pod, and
    summed over nodes - at each instant. Averaging each series first and
    combining after would weigh a pod or node that existed for ten minutes of
    an hour as if it had been there all hour.
    """
    sel = kueue_selector(cfg)
    duty = f"kubernetes_io:node_accelerator_duty_cycle{{{duty_selector(cfg)}}}"
    # Fifteen samples a step: enough to average a step fairly, few enough that
    # a week of hourly points answers in a couple of seconds.
    res = max(60, step // 15)
    window = f"[{step}s:{res}s]"
    # A node or chip that is absent has no sample rather than a zero, so
    # avg_over_time would average over the minutes it existed and count a node
    # up for ten minutes of an hour as up all hour. Summing the samples and
    # dividing by the step counts it for the ten minutes.
    share = f"* {res} / {step}"
    per_queue = "cluster_queue, flavor, resource"
    queries = {
        "pending": (
            f"sum by (cluster_queue) (avg_over_time(({current(f'kueue_pending_workloads{{{sel}}}', 'cluster_queue, status')}){window}))",
            ("cluster_queue",),
        ),
        "used": (
            f"sum by (cluster_queue, resource) (avg_over_time(({current(f'kueue_cluster_queue_resource_usage{{{sel}}}', per_queue)}){window}))",
            ("cluster_queue", "resource"),
        ),
        # The step's largest, so a quota change mid-step shows the quota the
        # usage could reach.
        "nominal": (
            f"sum by (cluster_queue, resource) (max_over_time(({current(f'kueue_cluster_queue_nominal_quota{{{sel}}}', per_queue)}){window}))",
            ("cluster_queue", "resource"),
        ),
        # Busy chips: TensorCore duty cycle, percent per chip, summed. Nodes that
        # do not report it - the disagg multi-host pods' - count as idle.
        "busy": (
            f"sum_over_time((sum by (model) (last_over_time({duty}[2m])) / 100){window}) {share}",
            ("model",),
        ),
        # Each TPU node's share of each step, by name, for views.py to sum by
        # node pool: a week is a few thousand node lifetimes and a few hundred
        # KiB.
        "nodes": (
            f"sum_over_time(({node_presence(cfg)}){window}) {share}",
            ("cluster_name", "node_name"),
        ),
    }
    with concurrent.futures.ThreadPoolExecutor(len(queries) + 1) as pool:
        futures = {
            k: pool.submit(prom_range, cfg, q, by, start, end, step)
            for k, (q, by) in queries.items()
        }
        evictions = pool.submit(
            prom_now,
            cfg,
            f"sum by (cluster_queue) (increase(kueue_evicted_workloads_total{{{sel}}}[{end - start}s]))",
            ("cluster_queue",),
            end,
        )
        out = {k: f.result() for k, f in futures.items()}
        out["evictions"] = evictions.result()
        return out


def fetch_node_pools(cfg: Config) -> list:
    """Every TPU node pool in the fleet, from the GKE API: its shape, autoscaling
    bounds, and the instance-group hashes its nodes are named by.

    The node metrics carry a node's name and nothing else, and a pool's instance
    group outlives every node it creates, so the hash ties a node seen in the
    metrics a week ago to its pool - even a pool scaled to zero since.
    """
    pools = []
    for c in cfg.clusters:
        m = re.search(r"/locations/([^/]+)/gkeMemberships/([^/]+)$", c["gateway"])
        if not m:
            continue
        location, name = m.groups()
        url = (
            f"https://container.googleapis.com/v1/projects/{cfg.project}/locations/{location}"
            f"/clusters/{name}/nodePools"
        )
        for p in google(url).get("nodePools", []):
            chips = re.search(r"-(\d+)t$", p["config"]["machineType"])
            if not chips:
                continue
            auto = p.get("autoscaling", {})
            multi_host = bool(p.get("placementPolicy", {}).get("tpuTopology"))
            pools.append(
                {
                    "cluster": name,
                    "name": p["name"],
                    # The Kueue queue it serves: a multi-host pool is one slice,
                    # named <shape>-<slice>.
                    "queue": re.sub(r"-\d+$", "", p["name"])
                    if multi_host
                    else p["name"],
                    "multi_host": multi_host,
                    "chips_per_node": int(chips[1]),
                    "min_nodes": int(
                        auto.get("minNodeCount") or auto.get("totalMinNodeCount") or 0
                    ),
                    "max_nodes": int(
                        auto.get("maxNodeCount") or auto.get("totalMaxNodeCount") or 0
                    ),
                    "hashes": [
                        h[1]
                        for u in p.get("instanceGroupUrls", [])
                        if (h := re.search(r"-([0-9a-f]{8})-grp$", u))
                    ],
                }
            )
    return pools


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


def fetch_stats(cfg: Config, start: int, end: int) -> dict:
    """Outcomes and phase timings of the workloads that finished in [start, end)."""
    if not cfg.timing_table:
        return {"queues": [], "outcomes": []}
    # ended_at is when the launcher wrote the row; the submitted_at bound is for
    # partition pruning, wide enough for the longest wait plus run.
    where = (
        f"ended_at >= TIMESTAMP_SECONDS({start}) AND ended_at < TIMESTAMP_SECONDS({end}) "
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
