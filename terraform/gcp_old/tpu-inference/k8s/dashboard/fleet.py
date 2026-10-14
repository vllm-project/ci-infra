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
                # gcloud hands back its own cached token without saying when it
                # expires, which can be in minutes, so ask again often: the call
                # is cheap, and a token held past its expiry is a 401 per read.
                self._value, self._expiry = out.stdout.strip(), time.time() + 300
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


# A Spot ResourceFlavor is named <generation>-spot (generate_manifests.py). As a
# PromQL matcher: the reserved flavors' series, and the Spot ones'.
RESERVED_FLAVOR = 'flavor!~".*-spot"'
SPOT_FLAVOR = 'flavor=~".*-spot"'


def is_spot(flavor: str) -> bool:
    return flavor.endswith("-spot")


# The node selector Kueue writes into a pod it admits on a Spot flavor
# (resource_flavor_spot.yaml.tpl); a reserved flavor writes none.
SPOT_LABEL = "cloud.google.com/gke-spot"


def placement(pods: list) -> dict:
    """Per Buildkite job, its pods on a worker: how many there are, on a node
    and running - every pod, the chip-less ones included, since Kueue calls a
    workload ready only once all are - and the TPU chips of them on a Spot
    node, on a reserved node, and admitted onto Spot with no node yet.

    The worker's Kueue admits a workload again after the manager's and picks
    its own flavor, and that choice is the one the pods follow; the manager's
    can say Spot for pods on reserved nodes or the other way round. A
    multi-pod job can land on both.
    """
    out: dict = {}
    for pod in pods:
        meta, spec, status = pod["metadata"], pod.get("spec", {}), pod.get("status", {})
        if meta.get("deletionTimestamp") or status.get("phase") in ("Succeeded", "Failed"):
            continue
        job = (meta.get("labels") or {}).get("buildkite.com/job-id", "").replace("-", "")
        if not job:
            continue
        row = out.setdefault(
            job,
            {"spot": 0.0, "reserved": 0.0, "spot_waiting": 0.0, "reserved_waiting": 0.0,
             "pods": 0, "pods_on_node": 0, "pods_running": 0},
        )
        row["pods"] += 1
        row["pods_on_node"] += 1 if spec.get("nodeName") else 0
        row["pods_running"] += 1 if status.get("phase") == "Running" else 0
        chips = sum(
            quantity((c.get("resources", {}).get("requests") or {}).get(TPU))
            for c in spec.get("containers", [])
        )
        if not chips:
            continue
        spot = (spec.get("nodeSelector") or {}).get(SPOT_LABEL) == "true"
        key = ("spot" if spot else "reserved") if spec.get("nodeName") else (
            "spot_waiting" if spot else "reserved_waiting"
        )
        row[key] += chips
    return out


def cq_summary(cq: dict) -> dict:
    """A ClusterQueue's quota and usage. Nominal, borrowing and usage are the
    reserved flavor's: a Spot flavor's quota is chips GCE may not have, never
    lent or borrowed, so it is counted apart rather than as capacity the queue
    is guaranteed."""
    resource = queue_resource(cq)
    nominal = borrowing = spot_quota = 0.0
    # What it may lend, when it is capped below its nominal; None lends all.
    lending = None
    for group in cq["spec"].get("resourceGroups", []):
        for flavor in group["flavors"]:
            for r in flavor["resources"]:
                if r["name"] != resource:
                    continue
                if is_spot(flavor["name"]):
                    spot_quota += quantity(r.get("nominalQuota"))
                else:
                    nominal += quantity(r.get("nominalQuota"))
                    borrowing += quantity(r.get("borrowingLimit"))
                    if r.get("lendingLimit") is not None:
                        lending = (lending or 0.0) + quantity(r.get("lendingLimit"))
    used = borrowed = spot_used = 0.0
    # The quota held - what Kueue admits against - not flavorsUsage, which
    # counts admitted workloads only: on the manager a workload holds its quota
    # from reservation while MultiKueue hands it to a worker, and leaving those
    # out showed chips free that Kueue would not give anyone.
    status = cq.get("status", {})
    for flavor in status.get("flavorsReservation") or status.get("flavorsUsage") or []:
        for r in flavor["resources"]:
            if r["name"] != resource:
                continue
            if is_spot(flavor["name"]):
                spot_used += quantity(r.get("total"))
            else:
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
        "lending_limit": lending,
        "spot_quota": spot_quota,
        "spot_used": spot_used,
        # How the queue preempts, in Kueue's words: taking lent quota back from
        # borrowers (Never, LowerPriority, Any), within its own queue (Never,
        # LowerPriority, LowerOrNewerEqualPriority), and in order to borrow.
        "reclaim": preemption.get("reclaimWithinCohort", "Never"),
        "within_queue": preemption.get("withinClusterQueue", "Never"),
        "borrow_preempt": (preemption.get("borrowWithinCohort") or {}).get(
            "policy", "Never"
        ),
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
    # Where the pods are, from each worker: an unreachable worker costs the
    # Spot figures for its jobs, not the page.
    workers = {
        c["name"]: f"{c['gateway']}/api/v1/namespaces/{ns}/pods?labelSelector=buildkite.com%2Fjob-id"
        for c in cfg.clusters
        if c["name"] != cfg.metrics_cluster
    }

    def worker_pods(url: str) -> tuple[list, str]:
        try:
            return google(url)["items"], ""
        except Exception as e:  # noqa: BLE001 - shown on the page, not raised
            return [], f"{type(e).__name__}: {e}"[:200]

    with concurrent.futures.ThreadPoolExecutor(len(urls) + len(workers)) as pool:
        placed = dict(zip(workers, pool.map(worker_pods, workers.values())))
        got = dict(zip(urls, pool.map(lambda u: google(u)["items"], urls.values())))
    places = placement([pod for items, _ in placed.values() for pod in items])
    placement_errors = {name: error for name, (_, error) in placed.items() if error}

    queues = [cq_summary(cq) for cq in got["cqs"]]
    resources = {q["name"]: q["resource"] for q in queues}
    lq_to_cq = {lq["metadata"]["name"]: lq["spec"]["clusterQueue"] for lq in got["lqs"]}
    owners = {
        (kind, o["metadata"]["name"]): o["metadata"].get("labels", {})
        for kind, key in (("Job", "jobs"), ("JobSet", "jobsets"))
        for o in got[key]
    }
    # The Job or JobSet behind each uid, for the job a preemption names.
    job_uids = {
        o["metadata"]["uid"]: (o["metadata"].get("labels") or {}).get("buildkite.com/job-id", "").replace("-", "")
        for key in ("jobs", "jobsets")
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
        place = places.get(labels.get("buildkite.com/job-id", "").replace("-", ""), {})
        workloads.append(
            {
                **state,
                "name": wl["metadata"]["name"],
                "created": parse_time(wl["metadata"]["creationTimestamp"]),
                # The worker MultiKueue placed it on, once one admitted it.
                "cluster": wl.get("status", {}).get("clusterName"),
                "queue": queue,
                "amount": workload_amount(wl, resources.get(queue, TPU)),
                # Its pods' chips on the worker: on Spot nodes, and admitted
                # onto Spot with no node yet (see placement()).
                "spot_chips": place.get("spot", 0.0),
                "spot_waiting_chips": place.get("spot_waiting", 0.0),
                "spot": bool(place.get("spot") or place.get("spot_waiting")),
                # Its pods on the worker, for a step part of which runs while
                # the rest waits for a node.
                "pods_total": place.get("pods", 0),
                "pods_on_node": place.get("pods_on_node", 0),
                "pods_running": place.get("pods_running", 0),
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
        "placement_errors": placement_errors,
        "job_uids": job_uids,
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
    # A query no series matches answers null rather than an empty list.
    return google(url)["data"]["result"] or []


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


def worker_kueue_selector(cfg: Config) -> str:
    """The workers' Kueue series. Each worker admits a workload again and picks
    its own flavor, which is where the pods go, so Spot usage is theirs."""
    names = "|".join(c["name"] for c in cfg.clusters if c["name"] != cfg.metrics_cluster)
    return f'cluster=~"{names}"'


def duty_selector(cfg: Config) -> str:
    # Plain alternation: cluster names are [a-z0-9-], and a backslash escape is
    # not valid inside a PromQL string.
    names = "|".join(c["name"] for c in cfg.clusters)
    return f'project_id="{cfg.project}",cluster_name=~"{names}"'


def node_selector(cfg: Config) -> str:
    """TPU nodes only. GKE names a TPU node gke-tpu-<hash>-<id>, the hash being its
    node pool's instance group; CPU nodes carry the cluster name there instead."""
    return f'{duty_selector(cfg)},node_name=~"gke-tpu-[0-9a-f]{{8}}-[a-z0-9]+"'


# GKE's node metrics reach Managed Prometheus minutes after they are sampled:
# measured 2026-10-08, a node's newest sample was a median 170 s and at most
# 271 s old. Asked about "now" with a 2-minute lookback, two thirds of the
# nodes had nothing to show (22 of 71 v7x chips). So the live page reads them
# as of NODE_LAG seconds ago, with a lookback that spans one sample interval
# and a late arrival; a past range is complete and only needs the lookback.
NODE_LAG = 300
NODE_LOOKBACK = "3m"


def node_presence(cfg: Config) -> str:
    """1 per TPU node while it exists. allocatable_cores is reported for every
    node, unlike the duty cycle, which some multi-host nodes never report."""
    return (
        "count by (cluster_name, node_name) (last_over_time("
        f"kubernetes_io:node_cpu_allocatable_cores{{{node_selector(cfg)}}}[{NODE_LOOKBACK}]))"
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


def reserved_agreement(cfg: Config) -> dict:
    """fetch_health's queries for the manager's and the workers' reserved
    chips per generation, and the gap between them that has lasted."""
    # The quota held, which on the manager includes workloads still being
    # handed to a worker: one held there that no worker admits is a gap too.
    usage = f'kueue_cluster_queue_resource_reservation{{{{{{sel}},resource="{TPU}",{RESERVED_FLAVOR}}}}}'
    manager = (
        f"sum by (flavor) ("
        f"{current(usage.format(sel=kueue_selector(cfg)), 'cluster_queue, flavor, resource')})"
    )
    workers = (
        f"sum by (flavor) ("
        f"{current(usage.format(sel=worker_kueue_selector(cfg)), 'cluster, cluster_queue, flavor, resource')})"
    )
    return {
        "reserved_manager": (manager, ("flavor",)),
        "reserved_workers": (workers, ("flavor",)),
        "reserved_gap": (f"min_over_time((abs({workers} - {manager}))[10m:1m])", ("flavor",)),
    }


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
        # Chips computing, per node, for views.py to sum by node pool as it
        # does node presence: per queue, and per worker on the overview.
        "busy": (
            f"sum by (cluster_name, node_name) (last_over_time({busy_duty}[{NODE_LOOKBACK}])) / 100",
            ("cluster_name", "node_name"),
        ),
        "nodes": (node_presence(cfg), ("cluster_name", "node_name")),
        # Reserved chips per generation as the manager's Kueue counts them and
        # as the workers' do: each admits a workload and picks its flavor on
        # its own, so the two can part. The gap is the smallest over ten
        # minutes, since every dispatch opens a short one.
        **reserved_agreement(cfg),
    }
    # The node metrics as of NODE_LAG ago, when every node has reported.
    lagged = {"busy", "nodes"}
    at = time.time() - NODE_LAG
    with concurrent.futures.ThreadPoolExecutor(len(queries)) as pool:
        futures = {
            k: pool.submit(prom_now, cfg, q, by, at if k in lagged else None)
            for k, (q, by) in queries.items()
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
            f"sum by (cluster_queue, resource) (avg_over_time(({current(f'kueue_cluster_queue_resource_usage{{{sel},{RESERVED_FLAVOR}}}', per_queue)}){window}))",
            ("cluster_queue", "resource"),
        ),
        # Admitted onto Spot by the workers, whose choice the pods follow;
        # summed over workers, each collapsed to one controller pod.
        "spot_used": (
            f"sum by (cluster_queue, resource) (avg_over_time(({current(f'kueue_cluster_queue_resource_usage{{{worker_kueue_selector(cfg)},{SPOT_FLAVOR}}}', 'cluster, ' + per_queue)}){window}))",
            ("cluster_queue", "resource"),
        ),
        # The step's largest, so a quota change mid-step shows the quota the
        # usage could reach.
        "nominal": (
            f"sum by (cluster_queue, resource) (max_over_time(({current(f'kueue_cluster_queue_nominal_quota{{{sel},{RESERVED_FLAVOR}}}', per_queue)}){window}))",
            ("cluster_queue", "resource"),
        ),
        "spot_nominal": (
            f"sum by (cluster_queue, resource) (max_over_time(({current(f'kueue_cluster_queue_nominal_quota{{{sel},{SPOT_FLAVOR}}}', per_queue)}){window}))",
            ("cluster_queue", "resource"),
        ),
        # Evictions in each step, by queue: preemption to reclaim lent quota, a
        # node gone, a pod that never became ready.
        "evicted": (
            f"sum by (cluster_queue) (increase(kueue_evicted_workloads_total{{{sel}}}[{step}s]))",
            ("cluster_queue",),
        ),
        # Busy chips: TensorCore duty cycle, percent per chip, summed. Nodes that
        # do not report it - the disagg multi-host pods' - count as idle.
        "busy": (
            f"sum_over_time((sum by (model) (last_over_time({duty}[{NODE_LOOKBACK}])) / 100){window}) {share}",
            ("model",),
        ),
        # Busy chips per node, for views.py to sum by node pool as it does
        # presence: the model label cannot tell a Spot node from a reserved one.
        "busy_nodes": (
            f"sum_over_time((sum by (cluster_name, node_name) (last_over_time({duty}[{NODE_LOOKBACK}])) / 100){window}) {share}",
            ("cluster_name", "node_name"),
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
            spot = bool(p["config"].get("spot"))
            # The Kueue queue it serves: a multi-host pool is one slice, named
            # <shape>-<slice>. A Spot pool, <shape>-spot, keeps its own name, so
            # its nodes are never counted as the shape's reserved ones.
            queue = re.sub(r"-\d+$", "", p["name"]) if multi_host else p["name"]
            pools.append(
                {
                    "cluster": name,
                    "name": p["name"],
                    "queue": queue,
                    # The shape a Spot pool overflows from; its own otherwise.
                    "shape": re.sub(r"-spot$", "", queue),
                    "multi_host": multi_host,
                    "spot": spot,
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


# The bare-metal TPU queues and the chips a job on each holds: Buildkite queue
# names count TensorCores, two a chip.
BARE_QUEUES = {
    "tpu_v7x_2_queue": 1,
    "tpu_v7x_8_queue": 4,
    "tpu_v7x_16_queue": 8,
    "tpu_v7x_32_queue": 16,
    "tpu_v6e_queue": 1,
    "tpu_v6e_8_queue": 8,
}


# --------------------------------------------------------------------------
# Logs


def log_view(cfg: Config) -> str:
    """Read through the _Default bucket's _AllLogs view, which the dashboard is
    granted on, rather than the project."""
    return f"projects/{cfg.project}/locations/global/buckets/_Default/views/_AllLogs"


def log_filters(cfg: Config, fleet_reasons=(), routine=()) -> dict[str, str]:
    """The Cloud Logging filter behind each health check: the component's own
    warnings and errors, and the dashboard's record of the checks.
    fleet_reasons are the event reasons Cluster problems counts; routine, the
    messages among them it leaves out."""
    workers = " OR ".join(
        f'"{c["name"]}"' for c in cfg.clusters if c["name"] != cfg.metrics_cluster
    )
    clusters = " OR ".join(f'"{c["name"]}"' for c in cfg.clusters)
    events = f'logName="projects/{cfg.project}/logs/events" AND jsonPayload.type="Warning"'
    reasons = " OR ".join(f'"{r}"' for r in sorted(fleet_reasons)) or '""'
    quiet = "".join(f' AND NOT jsonPayload.message:"{m}"' for m in routine)
    return {
        # agent-stack-k8s logs text, its level a word in the line.
        "controller": (
            f'resource.type="k8s_container" AND resource.labels.cluster_name="{cfg.metrics_cluster}" '
            'AND resource.labels.namespace_name="buildkite" AND resource.labels.container_name="controller" '
            'AND (textPayload:" ERR " OR textPayload:" WRN ")'
        ),
        # A standby Kueue replica logs its cache missing queues it does not
        # serve; only the leader's errors say anything.
        "kueue": (
            f'resource.type="k8s_container" AND resource.labels.cluster_name=({clusters}) '
            'AND resource.labels.namespace_name="kueue-system" AND severity>=ERROR '
            'AND NOT jsonPayload."replica-role"="follower"'
        ),
        # An agent pod the manager could not schedule or create.
        "agent_events": (
            f'{events} AND resource.labels.cluster_name="{cfg.metrics_cluster}" '
            'AND jsonPayload.involvedObject.namespace="buildkite" '
            'AND jsonPayload.reason=("FailedScheduling" OR "FailedCreate" OR "FailedMount")'
        ),
        "cluster_events": (
            f"{events} AND resource.labels.cluster_name=({workers}) "
            f"AND jsonPayload.reason=({reasons}){quiet}"
        ),
        "health": (
            f'resource.type="cloud_run_revision" AND resource.labels.service_name="{cfg.service}" '
            "AND jsonPayload.health_check.title:*"
        ),
    }


def read_logs(cfg: Config, filter_: str, hours: float, limit: int) -> list[dict]:
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
    body = {
        "resourceNames": [log_view(cfg)],
        "filter": f'({filter_}) AND timestamp>="{since}"',
        "orderBy": "timestamp desc",
        "pageSize": limit,
    }
    return google("https://logging.googleapis.com/v2/entries:list", body).get("entries", [])


def log_time(value: str) -> float:
    # Cloud Logging gives nanoseconds; fromisoformat takes six digits at most.
    head, _, frac = value.rstrip("Z").partition(".")
    return dt.datetime.fromisoformat(f"{head}.{(frac + '000000')[:6]}+00:00").timestamp()


CONTROLLER_LINE = re.compile(r" (ERR|WRN) \S+ (.*?)(?: component=.*?)?(?: error=\"(.*)\")?$")
VOLATILE = [
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), "…"),
    (re.compile(r"\b[0-9a-f]{16,}\b"), "…"),
    (re.compile(r"-[a-z0-9]{5}(?=\b|$)"), "-…"),
    (re.compile(r"\b\d{4,}\b"), "N"),
]


def log_entry(entry: dict) -> dict:
    """A log entry as a health check shows it: when, where, how bad, what -
    and a key with the IDs taken out, so a hundred alike count as one line."""
    labels = entry.get("resource", {}).get("labels", {})
    payload = entry.get("jsonPayload") or {}
    out = {"t": log_time(entry["timestamp"]), "cluster": labels.get("cluster_name", "")}
    if "health_check" in payload:
        return {**out, **payload["health_check"]}
    if "reason" in payload and "involvedObject" in payload:
        obj = payload["involvedObject"]
        text = f"{payload['reason']}: {payload.get('message', '')}"
        out.update(level="warning", text=text, object=f"{obj.get('kind', '')}/{obj.get('name', '')}")
    elif payload:
        text = payload.get("msg") or payload.get("message") or ""
        if payload.get("error"):
            text += f": {payload['error']}"
        out.update(level=payload.get("level", "error"), text=text)
    else:
        line = entry.get("textPayload", "")
        m = CONTROLLER_LINE.search(line)
        if m:
            text = m.group(2) + (f": {m.group(3)}" if m.group(3) else "")
            out.update(level="error" if m.group(1) == "ERR" else "warning", text=text)
        else:
            out.update(level="warning", text=line[:300])
    key = out["text"]
    for pattern, sub in VOLATILE:
        key = pattern.sub(sub, key)
    out["key"] = key[:240]
    return out


def fetch_logs(cfg: Config, fleet_reasons=(), routine=()) -> dict:
    """The last day of each component's warnings and errors, and the last week
    of the dashboard's own record of its checks."""
    filters = log_filters(cfg, fleet_reasons, routine)
    spans = {k: (168, 1000) if k == "health" else (24, 300) for k in filters}
    with concurrent.futures.ThreadPoolExecutor(len(filters)) as pool:
        futures = {
            k: pool.submit(read_logs, cfg, f, *spans[k]) for k, f in filters.items()
        }
        return {
            "project": cfg.project,
            "filters": filters,
            **{k: [log_entry(e) for e in f.result()] for k, f in futures.items()},
        }


# What took a running or admitted step's chips, from two records: Kueue's
# events on the manager's workloads, and the launcher's lines (launch.py) when a
# Spot node went away under a step or never came. Fetched for the whole fleet
# in one query each, not per job.
PREEMPTION_HOURS = 24
SPOT_RECLAIMED = "Spot node reclaimed under"
SPOT_NO_NODE = "admitted onto Spot but no Spot node"
# Kueue's Preempted message names why; its words, and what the page calls each.
KUEUE_PREEMPTION = (
    ("reclamation within the cohort", "reclaim"),
    ("prioritization in the ClusterQueue", "priority"),
    ("fair sharing within the cohort", "fair_share"),
)
PREEMPTION_PATHS = re.compile(r"preemptor path: [^;]*?([\w-]+); preemptee path: [^;]*?([\w-]+);")
PREEMPTOR_JOB = re.compile(r"JobUID: ([0-9a-f-]{36})")


def fetch_preemptions(cfg: Config) -> list[dict]:
    """The last PREEMPTION_HOURS of preemptions and Spot fallbacks, one row
    each, with the Buildkite job it hit: Kueue preempting a workload to give
    its chips back to a queue that lent them (or to a higher priority in the
    same queue), naming the queue and job that took them; a Spot node
    reclaimed under a step; a step admitted onto Spot that never got a node;
    a workload whose pods were not ready in time."""
    events = (
        f'logName="projects/{cfg.project}/logs/events" '
        f'AND resource.labels.cluster_name="{cfg.metrics_cluster}" '
        'AND jsonPayload.involvedObject.kind="Workload" '
        'AND jsonPayload.reason=("Preempted" OR "Evicted")'
    )
    lines = (
        f'resource.type="k8s_container" AND resource.labels.cluster_name="{cfg.metrics_cluster}" '
        f'AND resource.labels.namespace_name="{cfg.namespace}" '
        f'AND (textPayload:"{SPOT_RECLAIMED}" OR textPayload:"{SPOT_NO_NODE}")'
    )
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        a = pool.submit(read_logs, cfg, events, PREEMPTION_HOURS, 1000)
        b = pool.submit(read_logs, cfg, lines, PREEMPTION_HOURS, 1000)
        event_entries, line_entries = a.result(), b.result()
    out, seen = [], set()

    def add(row: dict) -> None:
        # The event exporter can write one event more than once.
        key = (row["job"], row["cause"], int(row["t"]))
        if row["job"] and key not in seen:
            seen.add(key)
            out.append(row)

    for e in event_entries:
        payload = e.get("jsonPayload") or {}
        message = payload.get("message") or ""
        row = {
            "t": log_time(e["timestamp"]),
            "job": job_hex(payload.get("involvedObject", {}).get("name", "")),
            "queue": "",
            "by_queue": "",
            "by_job_uid": "",
        }
        if payload.get("reason") == "Preempted":
            cause = next((c for words, c in KUEUE_PREEMPTION if words in message), "preempted")
            paths = PREEMPTION_PATHS.search(message)
            by = PREEMPTOR_JOB.search(message)
            row.update(
                cause=cause,
                by_queue=paths.group(1) if paths else "",
                queue=paths.group(2) if paths else "",
                by_job_uid=by.group(1) if by else "",
            )
        elif message.startswith("Preempted"):
            continue  # its Preempted event says the same, with more
        else:
            row["cause"] = "pods_ready" if "PodsReady" in message else "evicted"
        add(row)
    for e in line_entries:
        text = e.get("textPayload") or ""
        add(
            {
                "t": log_time(e["timestamp"]),
                "job": job_hex(e.get("resource", {}).get("labels", {}).get("pod_name", "")),
                "cause": "spot_reclaim" if SPOT_RECLAIMED in text else "spot_no_node",
                "queue": "",
                "by_queue": "",
                "by_job_uid": "",
            }
        )
    return sorted(out, key=lambda r: r["t"])


def fetch_waits(cfg: Config, start: int, end: int) -> dict:
    """Per queue over [start, end), a point a minute where Prometheus allows:
    pending workloads, admitted TPU chips and nominal TPU quota, for Migration's
    idle-while-jobs-waited. Not Trends' steps: an hour full and queueing in
    its first half and idle in its second would average to idle chips beside
    waiting jobs."""
    sel = kueue_selector(cfg)
    tpu = f'{sel},resource="{TPU}",{RESERVED_FLAVOR}'
    per_queue = "cluster_queue, flavor, resource"
    # Prometheus answers at most 11,000 points a series.
    step = max(60, -(-(end - start) // 11000 // 60) * 60)
    queries = {
        "pending": f"sum by (cluster_queue) ({current(f'kueue_pending_workloads{{{sel}}}', 'cluster_queue, status')})",
        "used": f"sum by (cluster_queue) ({current(f'kueue_cluster_queue_resource_usage{{{tpu}}}', per_queue)})",
        "nominal": f"sum by (cluster_queue) ({current(f'kueue_cluster_queue_nominal_quota{{{tpu}}}', per_queue)})",
    }
    with concurrent.futures.ThreadPoolExecutor(len(queries)) as pool:
        futures = {
            k: pool.submit(prom_range, cfg, q, ("cluster_queue",), start, end, step)
            for k, q in queries.items()
        }
        return {"step": step, **{k: f.result() for k, f in futures.items()}}


def fetch_wait_series(cfg: Config, start: int, end: int) -> dict:
    """Each kube queue's median and p90 wait - submitted to quota reserved, as
    History's tables count it - of the workloads submitted in each stretch of
    [start, end): an hour for a day, four hours for a week, twelve beyond. A
    workload enters the table when it ends, so the last stretches miss what is
    still running."""
    if not cfg.timing_table:
        return {}
    seconds = end - start
    bucket = 3600 if seconds <= 2 * 86400 else 4 * 3600 if seconds <= 8 * 86400 else 12 * 3600
    wait = "TIMESTAMP_DIFF(quota_reserved_at, submitted_at, SECOND)"
    rows = bigquery(cfg, f"""
SELECT queue, DIV(UNIX_SECONDS(submitted_at) - {start}, {bucket}) AS bucket,
  APPROX_QUANTILES({wait}, 100)[OFFSET(50)] AS p50,
  APPROX_QUANTILES({wait}, 100)[OFFSET(90)] AS p90, COUNT(*) AS n
FROM `{cfg.timing_table}`
WHERE submitted_at >= TIMESTAMP_SECONDS({start}) AND submitted_at < TIMESTAMP_SECONDS({end})
  AND quota_reserved_at IS NOT NULL
GROUP BY queue, bucket""")
    # And each cohort's across the window, for the page's headline.
    overall = bigquery(cfg, f"""
SELECT SPLIT(queue, '-')[OFFSET(0)] AS prefix,
  APPROX_QUANTILES({wait}, 100)[OFFSET(50)] AS p50, APPROX_QUANTILES({wait}, 100)[OFFSET(90)] AS p90
FROM `{cfg.timing_table}`
WHERE submitted_at >= TIMESTAMP_SECONDS({start}) AND submitted_at < TIMESTAMP_SECONDS({end})
  AND quota_reserved_at IS NOT NULL
GROUP BY prefix""")
    return {
        "start": start,
        "bucket": bucket,
        "count": -(-seconds // bucket),
        "rows": rows,
        "overall": {r["prefix"]: {"p50": float(r["p50"]), "p90": float(r["p90"])} for r in overall},
    }


def fetch_jobs(cfg: Config, start: int, end: int) -> list[dict]:
    """Every kube workload that ended in [start, end), newest first, from the
    launcher's timing table: the per-queue job history Buildkite cannot show,
    since every kube step runs on its one kube queue. A row is written when the
    workload ends, so what is still in flight comes from the live snapshot."""
    if not cfg.timing_table:
        return []
    ended = "COALESCE(ended_at, finished_at)"
    sql = f"""
SELECT queue, chips, pipeline, build_number, job_id, label, branch, outcome,
  exit_code, requeues, redispatches,
  UNIX_SECONDS(submitted_at) AS submitted, UNIX_SECONDS(quota_reserved_at) AS reserved,
  UNIX_SECONDS(admitted_at) AS admitted, UNIX_SECONDS(first_pod_started_at) AS started,
  UNIX_SECONDS(finished_at) AS finished, UNIX_SECONDS({ended}) AS ended
FROM `{cfg.timing_table}`
WHERE {ended} >= TIMESTAMP_SECONDS({start}) AND {ended} < TIMESTAMP_SECONDS({end})
  AND created_at >= TIMESTAMP_SECONDS({start - 2 * 86400})
ORDER BY ended DESC
LIMIT 20000"""
    return bigquery(cfg, sql)


def fetch_bare(cfg: Config, start: int, end: int) -> list[dict]:
    """What the bare-metal TPU queues ran in [start, end), from the step log
    table beside the kube timing table: how much of the work kube has not taken
    yet. The table records a step's queue from 2026-10-01."""
    if not cfg.timing_table:
        return []
    table = cfg.timing_table.rsplit(".", 1)[0] + ".step_execution_logs"
    queues = ", ".join(f"'{q}'" for q in BARE_QUEUES)
    chips = (
        "CASE queue "
        + " ".join(f"WHEN '{q}' THEN {c}" for q, c in BARE_QUEUES.items())
        + " END"
    )
    lo, hi = f"TIMESTAMP_SECONDS({start})", f"TIMESTAMP_SECONDS({end})"

    def quantile(q: int) -> str:
        return (
            "APPROX_QUANTILES(TIMESTAMP_DIFF(started_at, runnable_at, SECOND), 100 IGNORE NULLS)"
            f"[SAFE_OFFSET({q})]"
        )

    # One row per job: the table can hold a step more than once.
    sql = f"""
WITH jobs AS (
  SELECT job_id, ANY_VALUE(queue) AS queue, MIN(runnable_at) AS runnable_at,
    MIN(started_at) AS started_at, MAX(finished_at) AS finished_at
  FROM `{table}`
  WHERE queue IN ({queues}) AND started_at < {hi} AND finished_at > {lo}
    AND created_at >= TIMESTAMP_SECONDS({start - 2 * 86400})
    AND NOT ENDS_WITH(IFNULL(pipeline_slug, ''), '-kube')
  GROUP BY job_id)
SELECT queue, COUNT(*) AS jobs,
  SUM({chips} * TIMESTAMP_DIFF(LEAST(finished_at, {hi}), GREATEST(started_at, {lo}), SECOND)) / 3600
    AS chip_hours,
  {quantile(50)} AS wait_p50, {quantile(90)} AS wait_p90
FROM jobs GROUP BY queue"""
    return bigquery(cfg, sql)


# Steps that wait for the rest of their build rather than doing work, so they
# always finish last and say nothing about which step held the build up.
WATCHER_STEPS = ("E2E_SUMMARY",)
# Shorter steps are gates, notifications and uploads, never a build's long pole.
MIN_STEP_SECONDS = 120


def fetch_steps(cfg: Config, start: int, end: int) -> dict:
    """The kube builds created in [start, end) and their steps, from the step
    log beside the kube timing table: per build, the step that finished last
    and the one before it; per step, its time, shards, failures and
    chip-hours, with the kube phases of the jobs that ran on kube."""
    if not cfg.timing_table:
        return {"builds": [], "steps": []}
    table = cfg.timing_table.rsplit(".", 1)[0] + ".step_execution_logs"
    watchers = ", ".join(f"'{s}'" for s in WATCHER_STEPS)
    # One row per job: the table can hold a job more than once. A build's kind
    # is a property of the build, so every job of it agrees. A kube build is
    # one with any job on kube, as a build's TPU steps all run on one fleet.
    jobs = f"""
WITH job_rows AS (
  SELECT pipeline_slug AS pipeline, build_number AS build, step_name AS step, job_id,
    ANY_VALUE(CASE WHEN build_source = 'schedule' THEN 'nightly'
      WHEN branch = 'main' THEN 'main' ELSE 'pr' END) AS kind,
    MIN(build_created_at) AS created, ANY_VALUE(queue) AS queue,
    MIN(started_at) AS started, MAX(finished_at) AS finished,
    ANY_VALUE(state HAVING MAX finished_at) AS state,
    LOGICAL_OR(IFNULL(retried, FALSE)) AS retried,
    LOGICAL_OR(IFNULL(soft_failed, FALSE)) AS soft_failed,
    ANY_VALUE(exit_status HAVING MAX finished_at) AS exit_status
  FROM `{table}`
  WHERE created_at >= TIMESTAMP_SECONDS({start - 86400})
    AND build_created_at >= TIMESTAMP_SECONDS({start}) AND build_created_at < TIMESTAMP_SECONDS({end})
    AND started_at IS NOT NULL AND finished_at IS NOT NULL
  GROUP BY pipeline, build, step, job_id),
jobs AS (
  SELECT * FROM job_rows
  QUALIFY LOGICAL_OR(queue = 'kube') OVER (PARTITION BY pipeline, build))"""
    # A build is clean when every job it did not retry passed; only those say
    # how long a build takes. A step's time in a build runs from its first job
    # starting to its last finishing.
    builds = f"""{jobs},
per_step AS (
  SELECT pipeline, build, step, MIN(started) AS started, MAX(finished) AS finished
  FROM jobs
  WHERE step NOT IN ({watchers}) AND TIMESTAMP_DIFF(finished, started, SECOND) >= {MIN_STEP_SECONDS}
  GROUP BY pipeline, build, step),
ranked AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY pipeline, build ORDER BY finished DESC) AS place
  FROM per_step),
builds AS (
  SELECT pipeline, build, ANY_VALUE(kind) AS kind, MIN(created) AS created, COUNT(*) AS jobs,
    COUNTIF(retried) AS retries,
    COUNTIF(state != 'passed' AND NOT retried AND NOT soft_failed) AS unfinished
  FROM jobs GROUP BY pipeline, build)
SELECT b.pipeline, b.build, b.kind, UNIX_SECONDS(b.created) AS created, b.jobs, b.retries,
  b.unfinished = 0 AS clean, p.step AS pole, UNIX_SECONDS(p.started) AS pole_started,
  UNIX_SECONDS(p.finished) AS pole_finished, n.step AS next, UNIX_SECONDS(n.finished) AS next_finished
FROM builds b
LEFT JOIN ranked p ON p.pipeline = b.pipeline AND p.build = b.build AND p.place = 1
LEFT JOIN ranked n ON n.pipeline = b.pipeline AND n.build = b.build AND n.place = 2"""
    # The timing table has a row per workload, and a job more than one when it
    # was requeued or resubmitted. A kube job's run splits into the launcher's
    # setup up to its first submission, the wait to its last admission (earlier
    # attempts included), its last workload's startup and test, and the
    # launcher's teardown - logs and artifacts - after. Its chip-hours are
    # every workload's.
    last = "HAVING MAX COALESCE(ended_at, finished_at)"

    def quantile(expr: str, q: int) -> str:
        return f"APPROX_QUANTILES({expr}, 100 IGNORE NULLS)[SAFE_OFFSET({q})]"

    def passed(expr: str) -> str:
        return f"IF(state = 'passed', {expr}, NULL)"

    steps = f"""{jobs},
timing AS (
  SELECT job_id, MIN(submitted_at) AS submitted,
    ANY_VALUE(admitted_at {last}) AS admitted,
    ANY_VALUE(first_pod_started_at {last}) AS pod_started,
    ANY_VALUE(finished_at {last}) AS pod_finished,
    SUM(chips * TIMESTAMP_DIFF(IFNULL(finished_at, ended_at), admitted_at, SECOND)) AS chip_s
  FROM `{cfg.timing_table}`
  WHERE created_at >= TIMESTAMP_SECONDS({start - 86400}) AND job_id IS NOT NULL
  GROUP BY job_id),
j AS (
  SELECT jobs.*, TIMESTAMP_DIFF(finished, started, SECOND) AS ran_s,
    TIMESTAMP_DIFF(started, created, SECOND) AS start_s,
    TIMESTAMP_DIFF(timing.submitted, started, SECOND) AS setup_s,
    TIMESTAMP_DIFF(timing.admitted, timing.submitted, SECOND) AS wait_s,
    TIMESTAMP_DIFF(timing.pod_started, timing.admitted, SECOND) AS startup_s,
    TIMESTAMP_DIFF(timing.pod_finished, timing.pod_started, SECOND) AS test_s,
    TIMESTAMP_DIFF(finished, timing.pod_finished, SECOND) AS teardown_s,
    timing.chip_s
  FROM jobs LEFT JOIN timing USING (job_id)),
shards AS (
  SELECT pipeline, kind, step, COUNT(*) AS n, MIN(ran_s) AS shortest, MAX(ran_s) AS longest
  FROM j WHERE state = 'passed' GROUP BY pipeline, kind, step, build),
spread AS (
  SELECT pipeline, kind, step, APPROX_TOP_COUNT(n, 1)[OFFSET(0)].value AS shards,
    {quantile("shortest", 50)} AS shortest_p50, {quantile("longest", 50)} AS longest_p50
  FROM shards GROUP BY pipeline, kind, step),
exits AS (
  SELECT pipeline, kind, step, STRING_AGG(CONCAT(code, ' ×', CAST(n AS STRING)), ', ' ORDER BY n DESC LIMIT 3) AS exits
  FROM (
    SELECT pipeline, kind, step, IFNULL(CAST(exit_status AS STRING), 'none') AS code, COUNT(*) AS n
    FROM j WHERE state = 'failed' AND NOT soft_failed GROUP BY pipeline, kind, step, code)
  GROUP BY pipeline, kind, step)
SELECT pipeline, kind, step, COUNT(*) AS jobs, COUNT(DISTINCT build) AS builds,
  COUNTIF(state = 'failed' AND NOT soft_failed) AS failed, COUNTIF(retried) AS retried,
  ANY_VALUE(queue) AS queue,
  {quantile(passed("ran_s"), 50)} AS ran_p50, {quantile(passed("ran_s"), 90)} AS ran_p90,
  {quantile(passed("start_s"), 50)} AS start_p50,
  {quantile(passed("setup_s"), 50)} AS setup_p50, {quantile(passed("wait_s"), 50)} AS wait_p50,
  {quantile(passed("startup_s"), 50)} AS startup_p50, {quantile(passed("test_s"), 50)} AS test_p50,
  {quantile(passed("test_s"), 90)} AS test_p90, {quantile(passed("teardown_s"), 50)} AS teardown_p50,
  SUM(chip_s) / 3600 AS chip_hours,
  ANY_VALUE(spread.shards) AS shards, ANY_VALUE(spread.shortest_p50) AS shortest_p50,
  ANY_VALUE(spread.longest_p50) AS longest_p50, ANY_VALUE(exits.exits) AS exits
FROM j
LEFT JOIN spread USING (pipeline, kind, step)
LEFT JOIN exits USING (pipeline, kind, step)
GROUP BY pipeline, kind, step"""
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(bigquery, cfg, builds), pool.submit(bigquery, cfg, steps)
        return {"builds": a.result(), "steps": b.result()}


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
