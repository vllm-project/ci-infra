#!/usr/bin/env python3
"""Submit a TPU workload on behalf of a Buildkite job, then own its lifecycle.

Runs as the command container of an agent-stack-k8s Job in the manager
cluster. Every TPU step goes through here, single pod or not, so there is one
code path and one place where policy lives.

    launch --profile v6e-8-2x4 -- pytest tests/e2e
    launch --profile v6e-8-2x4 --template jobset-multihost -- bash bench.sh

Why a launcher rather than running the test in this pod: agent-stack-k8s can
only create a batch/v1 Job, and a Job cannot span hosts, so multi-host slices
and prefill/decode disagg need something to create a JobSet. Routing the
single-pod case through the same path costs one cheap CPU pod and buys two
properties that only hold when the agent is *outside* the Kueue workload:

  * The agent acquires its Buildkite job in seconds instead of after TPU
    admission and node pool scale-up, so the job is never held reserved long
    enough for Buildkite's reservation to lapse and be claimed twice.
  * Kueue preemption evicts the workload without killing the agent, so a
    preempted run is a pause in the step log rather than a failed build
    needing `retry: automatic` in every pipeline.

Profiles and workload templates are mounted from ConfigMaps generated from the
same tfvars as the Kueue objects, so a profile cannot exist in one and be
missing from the other, and a pipeline cannot invent placement.
"""

import argparse
import json
import os
import re
import signal
import string
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import yaml

NAMESPACE = os.environ.get("LAUNCHER_NAMESPACE", "buildkite")
PROFILES_PATH = os.environ.get("LAUNCHER_PROFILES", "/opt/launcher/profiles/profiles.yaml")
TEMPLATES_DIR = os.environ.get("LAUNCHER_TEMPLATES", "/opt/launcher/templates")
POLL_SECONDS = 5

# Deadline for the workload to be admitted, separate from the deadline for it
# to finish. A capacity shortfall and a hung test should not look the same, nor
# both consume the whole Buildkite step timeout.
ADMISSION_TIMEOUT_SECONDS = int(os.environ.get("LAUNCHER_ADMISSION_TIMEOUT", "1800"))

# Kinds the launcher will submit. Anything else in a template is a mistake, and
# catching it here is cheaper than a confusing RBAC denial.
SUPPORTED_KINDS = {"Job": "job", "JobSet": "jobset"}


def log(msg):
    print(f"~~~ launcher: {msg}", flush=True)


def kubectl(*args, check=True):
    return subprocess.run(
        ["kubectl", "-n", NAMESPACE, *args], check=check, capture_output=True, text=True
    )


def kubectl_json(*args):
    proc = kubectl(*args, "-o", "json", check=False)
    return json.loads(proc.stdout) if proc.returncode == 0 else None


def load_registry():
    with open(PROFILES_PATH) as fh:
        return yaml.safe_load(fh) or {}

def load_profile(registry, name):
    profiles = registry.get("profiles", {})
    if name not in profiles:
        raise SystemExit(
            f"unknown profile {name!r}. Available: {', '.join(sorted(profiles))}"
        )
    return profiles[name]

def resolve_image(registry):
    """The workload image, checked against the cluster-side allowlist.

    The image is deliberately the pipeline's choice - CI images are built per
    commit, so the cluster cannot know it. That makes it repo-controlled, and
    in a public repo repo-controlled means PR-controlled, so the registry it
    comes from is checked here rather than trusted.
    """
    image = os.environ.get("WORKLOAD_IMAGE", "").strip()
    if not image:
        raise SystemExit(
            "no workload image. Set WORKLOAD_IMAGE in the pipeline env, e.g.\n"
            "  env:\n"
            "    WORKLOAD_IMAGE: \"$${REGISTRY}/vllm:$${BUILDKITE_COMMIT}\""
        )
    allowed = registry.get("allowed_image_repos") or []
    if not allowed:
        log("warning: no allowed_image_repos configured; any image is accepted")
    elif not any(image.startswith(prefix) for prefix in allowed):
        raise SystemExit(
            f"image {image!r} is not from an allowed registry. Allowed prefixes: "
            + ", ".join(allowed)
        )
    return image


def workload_name():
    """DNS-safe name from the Buildkite job UUID.

    JobSet appends -<replicatedJob>-<jobIndex>-<podIndex> to build child names,
    so the parent has to leave room inside the 63 character limit. Bare UUID
    hex is 32 chars, leaving ~25 for the suffix.
    """
    uuid = os.environ.get("BUILDKITE_JOB_ID", "")
    slug = re.sub(r"[^a-z0-9]", "", uuid.lower())
    if not slug:
        raise SystemExit("BUILDKITE_JOB_ID is not set; refusing to guess a name")
    return f"bk-{slug}"


def owner_reference():
    """Point the workload at the k8s Job running this pod.

    Kubernetes GC then removes the workload whenever that Job goes away, which
    covers what the SIGTERM handler cannot: OOM kill, node loss, TTL cleanup.
    """
    pod = kubectl_json("get", "pod", os.environ["LAUNCHER_POD_NAME"])
    owners = (pod or {}).get("metadata", {}).get("ownerReferences", [])
    job = next((o for o in owners if o.get("kind") == "Job"), None)
    if job is None:
        log("warning: launcher pod has no owning Job; cleanup relies on SIGTERM only")
        return None
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "name": job["name"],
        "uid": job["uid"],
        "blockOwnerDeletion": False,
    }


def correlation_labels():
    """Labels tying the workload back to its Buildkite job.

    Also stamped on pod templates: that is what reaches Cloud Logging and what
    an orphan sweep in the worker cluster would match on.
    """
    pairs = {
        "buildkite.com/job-id": os.environ.get("BUILDKITE_JOB_ID", ""),
        "buildkite.com/build-number": os.environ.get("BUILDKITE_BUILD_NUMBER", ""),
        "buildkite.com/pipeline": os.environ.get("BUILDKITE_PIPELINE_SLUG", ""),
    }
    return {k: v for k, v in pairs.items() if v}


def pod_specs(doc):
    """Every PodSpec in the document, whatever the kind."""
    if doc["kind"] == "Job":
        return [doc["spec"]["template"]["spec"]]
    return [
        rj["template"]["spec"]["template"]["spec"]
        for rj in doc["spec"].get("replicatedJobs", [])
    ]


def pod_metadatas(doc):
    if doc["kind"] == "Job":
        return [doc["spec"]["template"].setdefault("metadata", {})]
    return [
        rj["template"]["spec"]["template"].setdefault("metadata", {})
        for rj in doc["spec"].get("replicatedJobs", [])
    ]


def render(template_name, profile, image, command, name, labels, owner):
    path = os.path.join(TEMPLATES_DIR, f"{template_name}.yaml")
    if not os.path.exists(path):
        available = sorted(
            f[:-5] for f in os.listdir(TEMPLATES_DIR) if f.endswith(".yaml")
        )
        raise SystemExit(
            f"unknown template {template_name!r}. Available: {', '.join(available)}"
        )

    subs = {
        "WORKLOAD_NAME": name,
        "NUM_HOSTS": str(profile.get("hosts", 1)),
        "KUEUE_QUEUE": profile["queue"],
        "CHIPS": str(profile["chips"]),
        "TOPOLOGY": profile["topology"],
        "ACCELERATOR_LABEL": profile["accelerator_label"],
        "MAX_RUNTIME": str(profile.get("max_runtime_seconds", 10800)),
        "IMAGE": image,
    }
    # Step env is available too, so a template can pin ${BUILDKITE_COMMIT}.
    subs.update({k: v for k, v in os.environ.items() if k not in subs})
    doc = yaml.safe_load(string.Template(open(path).read()).safe_substitute(subs))

    if doc.get("kind") not in SUPPORTED_KINDS:
        raise SystemExit(
            f"{path}: kind {doc.get('kind')!r} is not one of {sorted(SUPPORTED_KINDS)}"
        )

    meta = doc.setdefault("metadata", {})
    meta["name"] = name
    meta["namespace"] = NAMESPACE
    # The queue label must be on the top-level object; Kueue reads it there for
    # both Job and JobSet, never off the inner pods.
    meta.setdefault("labels", {})["kueue.x-k8s.io/queue-name"] = profile["queue"]
    meta["labels"].update(labels)
    if owner:
        meta["ownerReferences"] = [owner]

    for pod_meta in pod_metadatas(doc):
        pod_meta.setdefault("labels", {}).update(labels)

    # Set the command as a list element rather than interpolating it into YAML,
    # so a command containing quotes or newlines cannot corrupt the manifest.
    placed = False
    for spec in pod_specs(doc):
        for container in spec.get("containers", []):
            if container.get("name") == "workload":
                container["args"] = [command]
                placed = True
    if not placed:
        raise SystemExit(f"{path}: no container named 'workload' to run the command in")

    return doc


def find_workload(uid):
    workloads = kubectl_json("get", "workloads")
    for item in (workloads or {}).get("items", []):
        for owner in item.get("metadata", {}).get("ownerReferences", []):
            if owner.get("uid") == uid:
                return item
    return None


def condition(obj, cond_type):
    for cond in (obj or {}).get("status", {}).get("conditions", []):
        if cond.get("type") == cond_type:
            return cond
    return None


def describe_admission(workload):
    if workload is None:
        return "waiting for Kueue to create the workload"
    status = workload.get("status", {})
    if status.get("clusterName"):
        return f"admitted to worker cluster {status['clusterName']}"
    if status.get("nominatedClusterNames"):
        return f"dispatching to {', '.join(status['nominatedClusterNames'])}"
    quota = condition(workload, "QuotaReserved")
    if quota and quota.get("status") != "True":
        return f"waiting for quota: {quota.get('message', quota.get('reason', ''))}"
    evicted = condition(workload, "Evicted")
    if evicted and evicted.get("status") == "True":
        return f"evicted ({evicted.get('reason')}), waiting for re-admission"
    return "waiting for admission"


def stream_logs(labels, since):
    """Poll Cloud Logging for workload pod output.

    The pods run in a worker cluster; MultiKueue syncs status back to the
    manager but not logs, so kubectl logs here returns nothing. Querying by
    label means no worker-cluster credentials and no need to know which worker
    Kueue picked.
    """
    job_id = labels.get("buildkite.com/job-id")
    if not job_id:
        return since
    query = (
        'resource.type="k8s_container" '
        f'labels."k8s-pod/buildkite_com/job-id"="{job_id}" '
        f'timestamp>"{since.isoformat().replace("+00:00", "Z")}"'
    )
    proc = subprocess.run(
        ["gcloud", "logging", "read", query, "--format=json", "--order=asc", "--limit=1000"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return since
    try:
        entries = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return since
    for entry in entries:
        payload = entry.get("textPayload") or json.dumps(entry.get("jsonPayload", ""))
        pod = entry.get("resource", {}).get("labels", {}).get("pod_name", "")
        print(f"[{pod}] {payload}", flush=True)
        if entry.get("timestamp"):
            since = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
    return since


def main():
    parser = argparse.ArgumentParser(prog="launch")
    parser.add_argument("--profile", required=True, help="TPU profile, e.g. v6e-8-2x4")
    parser.add_argument("--template", default="job", help="workload template (default: job)")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("no command given; use: launch --profile P -- <command>")

    registry = load_registry()
    profile = load_profile(registry, args.profile)
    image = resolve_image(registry)
    name = workload_name()
    labels = correlation_labels()
    doc = render(args.template, profile, image, " ".join(command), name, labels, owner_reference())
    kind = SUPPORTED_KINDS[doc["kind"]]

    deleted = False

    def cleanup(signum, _frame):
        # agent-stack deletes the pod on Buildkite cancellation, so SIGTERM is
        # how the launcher learns the build is gone. The ownerReference covers
        # the cases that never deliver one.
        nonlocal deleted
        if not deleted:
            deleted = True
            log(f"signal {signum}, deleting {kind}/{name}")
            kubectl("delete", kind, name, "--wait=false", check=False)
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    log(f"submitting {doc['kind']} {name} (profile {args.profile}, template {args.template})")
    subprocess.run(
        ["kubectl", "-n", NAMESPACE, "apply", "-f", "-"],
        input=json.dumps(doc), text=True, check=True,
    )

    uid = kubectl_json("get", kind, name)["metadata"]["uid"]
    since = datetime.now(timezone.utc) - timedelta(seconds=30)
    started = time.monotonic()
    admitted = False
    last_note = None

    while True:
        obj = kubectl_json("get", kind, name)
        if obj is None:
            log(f"{kind}/{name} disappeared")
            return 1

        if not admitted:
            workload = find_workload(uid)
            note = describe_admission(workload)
            if note != last_note:
                log(note)
                last_note = note
            if workload and workload.get("status", {}).get("clusterName"):
                admitted = True
            elif time.monotonic() - started > ADMISSION_TIMEOUT_SECONDS:
                log(f"not admitted within {ADMISSION_TIMEOUT_SECONDS}s - capacity, not the test")
                kubectl("delete", kind, name, "--wait=false", check=False)
                return 1

        since = stream_logs(labels, since)

        # Job reports succeeded/failed counts; JobSet reports conditions.
        if doc["kind"] == "Job":
            status = obj.get("status", {})
            done = status.get("succeeded", 0) >= 1
            failed = condition(obj, "Failed")
            failed = (failed and failed.get("status") == "True") or status.get("failed", 0) >= 1
        else:
            completed = condition(obj, "Completed")
            done = bool(completed and completed.get("status") == "True")
            failed_cond = condition(obj, "Failed")
            failed = bool(failed_cond and failed_cond.get("status") == "True")

        if done:
            stream_logs(labels, since)
            log(f"{kind}/{name} completed")
            return 0
        if failed:
            stream_logs(labels, since)
            log(f"{kind}/{name} failed")
            return 1

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
