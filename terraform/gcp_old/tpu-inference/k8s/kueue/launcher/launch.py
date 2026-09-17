#!/usr/bin/env python3
"""Submit a TPU workload on behalf of a Buildkite job, then own its lifecycle.

Runs as the command container of an agent-stack-k8s Job in the manager cluster.

    launch --machine-type ct6e-standard-8t --topology 2x4 -- pytest tests/e2e
    launch --manifest .buildkite/kubernetes/manifests/1p1d.yaml

Every TPU step goes through here, single pod or not. agent-stack-k8s can only
create a batch/v1 Job and a Job cannot span hosts, so multi-host work needs
something to create a JobSet; keeping the agent outside the Kueue workload also
means the Buildkite job is acquired before admission and that preemption pauses
a run rather than failing it.

Both forms are the same path with different manifests: the launcher ships the
single-pod Job, and anything else comes as a manifest from the repo under test,
which states its own hardware and its own per-role commands. Placement is
therefore read back out of the manifest, where it can be checked against the
profile registry rather than queueing forever against quota that does not exist.

A repo manifest is untrusted. PodSecurity `baseline` on the workload namespace
already rejects privileged containers, hostPath volumes and host networking, so
validation here covers only what admission cannot know.
"""

import argparse
import copy
import json
import os
import re
import shlex
import shutil
import signal
import string
import subprocess
import sys
import time
import urllib.request

try:
    import yaml
except ImportError:
    raise SystemExit(
        "no PyYAML: launcher_image is not built from kueue/launcher/Dockerfile"
    )

NAMESPACE = os.environ.get("LAUNCHER_NAMESPACE", "buildkite")
PROFILES_PATH = os.environ.get(
    "LAUNCHER_PROFILES", "/opt/launcher/profiles/profiles.yaml"
)

# The Job a step gets when it names hardware and nothing else. Deployed with
# the launcher rather than kept in a repo: everything in it (which caches
# exist, what mounts them, which identity may write them) is cluster state.
DEFAULT_JOB = os.environ.get("LAUNCHER_DEFAULT_JOB", "/opt/launcher/manifests/job.yaml")
POD_DEFAULTS = os.environ.get(
    "LAUNCHER_POD_DEFAULTS", "/opt/launcher/manifests/pod_defaults.yaml")

ACCELERATOR_KEY = "cloud.google.com/gke-tpu-accelerator"
TOPOLOGY_KEY = "cloud.google.com/gke-tpu-topology"
TPU_RESOURCE = "google.com/tpu"

# Kueue reads this on the top-level object for both Job and JobSet, never on
# the inner pods.
QUEUE_LABEL = "kueue.x-k8s.io/queue-name"

# GKE's name, not ours: the gcsfuse sidecar looks for an emptyDir called this
# and uses it as its file cache.
FUSE_CACHE_VOLUME = "gke-gcsfuse-cache"

# The pod's /dev/shm. Too small does not present as a full filesystem - it
# presents as a worker that stops answering, then as the slice failing around
# it.
SHM_VOLUME = "dshm"

# Memory-backed emptyDirs the launcher sizes from the host, and the profile key
# holding each ceiling.
HOST_SIZED_VOLUMES = {
    FUSE_CACHE_VOLUME: "fuse_cache_size",
    SHM_VOLUME: "shm_size",
}

# kubectl --timestamps prefixes each entry with RFC3339. Anything else on a
# line is a fragment of the entry above it, not a new one.
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.]+Z$")

WORKLOAD_CONTAINER = "workload"

# Held back from the BUILDKITE_* sweep in forward_env. BUILDKITE_COMMAND is the
# step's own command line, which would have the workload re-run the launcher
# that started it; BUILDKITE_PLUGINS can bloat the pod spec; the rest are the
# agent's per-job API credentials, which belong to the agent rather than to a
# workload pod.
BUILDKITE_DENY = frozenset({
    "BUILDKITE_COMMAND",
    "BUILDKITE_PLUGINS",
    "BUILDKITE_AGENT_JOB_API_SOCKET",
    "BUILDKITE_AGENT_JOB_API_TOKEN",
    "BUILDKITE_OIDC_TOKEN_PATH",
})

# What a manifest sets to be given the fleet's pod setup instead of restating
# it. See inherit_defaults().
DEFAULTS_ANNOTATION = "tpu-ci.google.com/defaults"
DEFAULTS_STANDARD = "standard"


POLL_SECONDS = 5
# Used only until the first log line arrives. MultiKueue deletes the remote Job
# when the workload finishes and the pods go with it, so a short step on a warm
# node can be created, run and removed between two ordinary polls, leaving
# nothing to read.
FIRST_LOG_POLL_SECONDS = 2

# Worker credentials are kept out of the default kubeconfig; see worker_env().
WORKER_KUBECONFIG = "/tmp/worker.kubeconfig"

SUPPORTED_KINDS = {"Job": "job", "JobSet": "jobset"}

# Every kubectl and gcloud call here is a single API request. Generous on
# purpose: a call cut off early turns a slow control plane into a failed test,
# while one that waits two minutes only delays a poll.
CLI_TIMEOUT_SECONDS = 120

# How long to go without an answer from the manager's API server before giving
# up on the workload. A GKE control plane stops answering for a few seconds
# whenever it is upgraded or repaired, and the workload runs on a worker
# cluster throughout. Bounded because past this the launcher can no longer say
# what it is watching.
API_GRACE_SECONDS = 600

# Deadline for the signal handler's delete, which races the pod's termination
# grace period (SIGKILL 30s after SIGTERM) rather than the step timeout: a
# delete still in flight then is a delete that never happened, and the workload
# holds its chips until the ownerReference collects it.
CLEANUP_TIMEOUT_SECONDS = 15

# Shorter than CLI_TIMEOUT_SECONDS: these are two plain HTTP requests with no
# control plane behind them, and a registry that has stopped answering should
# not hold up every pod in the workload before the caller falls back to the tag.
REGISTRY_TIMEOUT_SECONDS = 20

# Ask for the manifest itself rather than the schema the registry assumes for a
# client that named none. All four, because the digest to pin is the one the
# pull will ask for: for a multi-architecture image that is the index, not the
# single manifest inside it.
REGISTRY_MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])

METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)


def log(msg):
    print(f"~~~ launcher: {msg}", flush=True)


# The controller copies the agent binary onto the shared workspace volume
# rather than onto this container's PATH.
AGENT_CLI = shutil.which("buildkite-agent") or "/workspace/buildkite-agent"


def agent(*args):
    """Run one buildkite-agent subcommand, best effort.

    Everything this is used for is commentary on a run that is happening either
    way, so a failure is worth a log line and nothing more. Credentials come
    from the step's own environment.
    """
    try:
        proc = subprocess.run(
            [AGENT_CLI, *args], capture_output=True, text=True,
            timeout=CLI_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"warning: buildkite-agent {args[0]} failed: {exc}")
        return False
    if proc.returncode != 0:
        log(f"warning: buildkite-agent {' '.join(args[:2])} failed: "
            f"{proc.stderr.strip()[:200]}")
    return proc.returncode == 0


def waiting_context():
    """The annotation context for this step's "waiting for hardware" notice.

    Per job rather than per build: several steps of one build queue at once and
    a shared context would have them overwrite each other. A context is also
    what lets the notice be withdrawn on admission.
    """
    job_id = os.environ.get("BUILDKITE_JOB_ID")
    return f"kueue-waiting-{job_id}" if job_id else None


def announce_waiting(context, queue, note):
    """Say on the build page that this step is queued, not running.

    Buildkite has no state for "the agent has the job but the hardware does
    not": the step is `running` from the moment the launcher starts. An
    annotation is the one surface visible without opening the job, and it is
    withdrawn on admission.
    """
    label = os.environ.get("BUILDKITE_LABEL", "this step")
    agent("annotate", "--context", context, "--style", "info",
          f":hourglass: **{label}** is waiting for hardware, not running: "
          f"{note} (queue `{queue}`).")


def withdraw_waiting(context):
    agent("annotation", "remove", "--context", context)


def kubectl(*args, check=True, timeout=CLI_TIMEOUT_SECONDS):
    """Run one kubectl call against the manager.

    A call that never returns holds the admitted workload's chips past the step
    timeout, since the step is waiting on this process.

    With check=False a timeout is reported as a failed call rather than raised,
    so it reaches kubectl_json's classification with every other failed call
    and is retried on the same terms.
    """
    try:
        return subprocess.run(
            ["kubectl", "-n", NAMESPACE, *args],
            check=check, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        if check:
            raise
        return subprocess.CompletedProcess(args, 1, "", f"timed out after {timeout}s")


# The API server's considered answer rather than a failure to reach it.
# Retrying these waits out the whole grace period to arrive at the same
# refusal, while the workload holds its chips throughout.
FATAL_API_ERRORS = ("(Forbidden)", "(Unauthorized)")


class ApiUnreachable(Exception):
    """A kubectl call that failed without telling us anything about the object.

    Kept apart from NotFound because the two call for opposite responses: an
    object the API server says is gone will not come back, while an API server
    that could not be reached has said nothing about a workload that is on a
    worker cluster and carries on regardless.
    """


def kubectl_json(*args):
    """The object, or None if the API server says there is no such object.

    Raises ApiUnreachable for every other failure, so that a caller treating
    absence as terminal cannot silently treat an unanswered call the same way.

    --ignore-not-found rather than reading kubectl's message: a control plane
    still registering its CRDs also says "the server could not find the
    requested resource", which is about a URL path and not about our object.
    With the flag a missing object exits 0 with empty stdout.
    """
    proc = kubectl(*args, "--ignore-not-found", "-o", "json", check=False)
    if proc.returncode == 0:
        return json.loads(proc.stdout) if proc.stdout.strip() else None
    detail = proc.stderr.strip()[:300]
    if any(marker in proc.stderr for marker in FATAL_API_ERRORS):
        raise SystemExit(f"kubectl {' '.join(args)}: {detail}")
    raise ApiUnreachable(detail or f"kubectl {' '.join(args)} exited {proc.returncode}")


def delete_workload(kind, name, timeout=CLI_TIMEOUT_SECONDS):
    """Delete the workload and say whether it worked.

    On cancellation the launcher is about to exit, so a failed delete leaves
    chips running with nobody watching. The caller chooses the deadline: the
    signal handler has the pod's termination grace period to meet.
    """
    proc = kubectl("delete", kind, name, "--wait=false", check=False,
                   timeout=timeout)
    if proc.returncode == 0:
        log(f"deleted {kind}/{name}")
    elif "NotFound" in proc.stderr:
        log(f"{kind}/{name} already gone")
    else:
        log(f"WARNING: could not delete {kind}/{name}: {proc.stderr.strip()[:200]}")
    return proc.returncode == 0


def load_registry():
    with open(PROFILES_PATH) as fh:
        return yaml.safe_load(fh) or {}


def available(registry):
    return sorted(
        f"{p.get('machine_type')} {p.get('topology')}"
        for p in registry.get("profiles", {}).values()
    )


def load_profile(registry, machine_type, topology):
    """The shape a step asked for, by machine type and topology.

    Matched on the registry's fields rather than by composing the profile name
    from them, so that naming convention stays in the generator that writes it.
    """
    for profile in registry.get("profiles", {}).values():
        if (profile.get("machine_type") == machine_type
                and profile.get("topology") == topology):
            return profile
    raise SystemExit(
        f"the fleet runs no {machine_type} at {topology}. Available: "
        + "; ".join(available(registry))
    )


def pod_chips(spec):
    """The chip counts one pod's containers ask for, as distinct values.

    Requests as well as limits: the API accepts an extended resource stated
    under either and leaves the other unset, so reading one field would let a
    pod that wrote only requests pass for a role that holds no chips.
    """
    return {
        str(field[TPU_RESOURCE])
        for c in spec.get("containers", [])
        for field in (
            (c.get("resources") or {}).get("limits") or {},
            (c.get("resources") or {}).get("requests") or {},
        )
        if field.get(TPU_RESOURCE) is not None
    }


def pod_shape(spec):
    """The hardware one pod asks for: what it selects, and how many chips.

    Chips as well as the two labels, because those do not identify a shape on
    their own: a 2x4 slice of v6e is eight chips either as one ct6e-standard-8t
    or as two ct6e-standard-4t, which differ in host, pod count and quota.
    """
    selector = spec.get("nodeSelector") or {}
    chips = pod_chips(spec)
    return (
        selector.get(ACCELERATOR_KEY),
        selector.get(TOPOLOGY_KEY),
        chips.pop() if len(chips) == 1 else None,
    )


def resolve_shape(doc, registry, where):
    """The profile the manifest's TPU pods describe.

    Only the roles holding chips decide it: a disaggregated workload is servers
    plus a client that drives them over HTTP and wants no accelerator, and the
    queues put google.com/tpu alone under quota, so a role asking for none is
    admitted with the rest and scheduled wherever the worker has room.
    """
    asked = {pod_shape(spec) for spec in pod_specs(doc)}
    # Nothing of the three, rather than "no chips": a role that names an
    # accelerator but forgets its limit has made a mistake, and should reach
    # the message below rather than be read as CPU-only and ignored.
    asked.discard((None, None, None))
    if len(asked) > 1:
        raise SystemExit(
            f"{where}: names more than one shape "
            + "; ".join(sorted(f"{a} {t} x{c}" for a, t, c in asked))
            + ".\nA workload is admitted against one queue, and a queue is one "
            "shape, so every pod holding chips has to ask for the same hardware."
        )
    accelerator, topology, chips = asked.pop() if asked else (None, None, None)
    if not all((accelerator, topology, chips)):
        raise SystemExit(
            f"{where}: does not say what hardware it needs. A pod that holds "
            f"chips wants nodeSelector {ACCELERATOR_KEY} and {TOPOLOGY_KEY}, "
            f"and a {TPU_RESOURCE} limit on the container holding them."
        )
    for profile in registry.get("profiles", {}).values():
        if (profile["accelerator_label"] == accelerator
                and profile["topology"] == topology
                and str(profile["chips"]) == chips):
            return profile
    raise SystemExit(
        f"{where}: the fleet has no node pool of {accelerator} at {topology} "
        f"with {chips} chips a host. Available: " + "; ".join(available(registry))
    )


def quota_reserved(workload):
    """Whether Kueue has committed chips to this workload."""
    quota = condition(workload, "QuotaReserved")
    return bool(quota) and quota.get("status") == "True"


def admission_timeout(registry):
    """How long to wait in line for chips.

    Queueing is capacity rather than a fault in the step, so this is generous
    and flat - independent of what the workload asked to run for. The tight
    bound is admission_max_seconds, which starts when quota is reserved: a
    queue is someone else using the chips, an undispatched reservation is
    nobody using them.
    """
    return int(registry["queue_max_seconds"])


def resolve_image(registry):
    """The workload image, checked against the cluster-side allowlist.

    CI images are built per commit, so the image has to be the pipeline's
    choice - which in a public repo means a PR's choice. Hence the check.
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
    return pin_digest(image)


def access_token():
    """A token for the launcher's own identity, from the node's metadata server.

    The same source the Cloud CLI and the kubelet read, so it carries the
    workload identity this pod already runs as, with nothing to mount.
    """
    request = urllib.request.Request(
        METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}
    )
    with urllib.request.urlopen(
        request, timeout=REGISTRY_TIMEOUT_SECONDS
    ) as response:
        return json.load(response)["access_token"]


def pin_digest(image):
    """Resolve a tag to the digest it points at right now.

    A run is many pulls - a pod per role, and another on every restart - spread
    over hours. A moving tag can be republished between two of them, which puts
    two builds in one benchmark and invalidates a compile cache keyed on the
    image. Resolving once here is what makes every pod the same bytes.

    Best effort: a tag that cannot be resolved is passed through, because the
    registry being briefly unreachable is a worse reason to fail a step than an
    unpinned pull is to run one.
    """
    if "@" in image:
        return image
    # Split the last path segment off first: a colon earlier in the string is a
    # registry port, not a tag.
    repo, sep, tail = image.rpartition("/")
    name, _, tag = tail.partition(":")
    host, _, path = repo.partition("/")
    try:
        if not path:
            raise ValueError("names no repository under a registry host")
        request = urllib.request.Request(
            f"https://{host}/v2/{path}/{name}/manifests/{tag or 'latest'}",
            method="HEAD",
            headers={
                "Authorization": f"Bearer {access_token()}",
                "Accept": REGISTRY_MANIFEST_ACCEPT,
            },
        )
        with urllib.request.urlopen(
            request, timeout=REGISTRY_TIMEOUT_SECONDS
        ) as response:
            digest = response.headers.get("Docker-Content-Digest", "").strip()
    except (OSError, ValueError, KeyError) as err:
        log(f"warning: could not resolve {image} to a digest ({err}); using the tag")
        return image
    if not digest:
        log(f"warning: {image} resolved to no digest header; using the tag")
        return image
    pinned = f"{repo}{sep}{name}@{digest}"
    log(f"image {image} -> {pinned}")
    return pinned


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
    """Own the workload from this pod, so GC removes it when the pod goes.

    The pod, not its Job: agent-stack sets backoffLimit=0, so an evicted pod
    leaves a Failed Job around until job-ttl expires. Owning from the pod fires
    at once and still covers Job deletion, since that deletes the pods too.
    Exactly one owner, since a dependent is collected only once every owner is
    gone.
    """
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "name": os.environ["LAUNCHER_POD_NAME"],
        "uid": os.environ["LAUNCHER_POD_UID"],
        "blockOwnerDeletion": False,
    }


def correlation_labels():
    """Labels tying the workload back to its Buildkite job.

    Also stamped on pod templates: that is the selector the log stream follows
    in the worker cluster.
    """
    pairs = {
        "buildkite.com/job-id": os.environ.get("BUILDKITE_JOB_ID", ""),
        "buildkite.com/build-number": os.environ.get("BUILDKITE_BUILD_NUMBER", ""),
        "buildkite.com/pipeline": os.environ.get("BUILDKITE_PIPELINE_SLUG", ""),
    }
    return {k: v for k, v in pairs.items() if v}


def pod_templates(doc):
    """Every pod template in the document, whatever the kind."""
    if doc["kind"] == "Job":
        return [doc["spec"]["template"]]
    return [
        rj["template"]["spec"]["template"]
        for rj in doc["spec"].get("replicatedJobs", [])
    ]


def pod_specs(doc):
    """Every PodSpec in the document, whatever the kind."""
    return [t["spec"] for t in pod_templates(doc)]


def job_specs(doc):
    """Every JobSpec in the document: one for a Job, one per replicatedJob."""
    if doc["kind"] == "Job":
        return [doc["spec"]]
    return [rj["template"]["spec"] for rj in doc["spec"].get("replicatedJobs", [])]


def pod_metadatas(doc):
    return [t.setdefault("metadata", {}) for t in pod_templates(doc)]


def forward_env(doc, names, registry):
    """Copy named step variables onto the workload container.

    Named explicitly, except for BUILDKITE_*: a workload needs the agent that
    started it to publish an artifact or report a test result, and every step
    wanted the same set, so the launcher supplies it rather than each pipeline
    restating it. BUILDKITE_DENY holds back the agent's own job credentials.
    Everything else a workload wants, including its secrets, the step names.

    A name the step asked for but does not have may be a fleet-wide one, and is
    then supplied as a secretKeyRef into the Secret each worker's sync writes.
    A reference rather than a value because a value would sit in plaintext in
    five objects: the Job, the Workload, MultiKueue's copies of both on the
    worker, and the pod.
    """
    fleet = registry.get("env_secrets") or {}
    swept = sorted(k for k in os.environ
                   if k.startswith("BUILDKITE_") and k not in BUILDKITE_DENY)
    names = list(dict.fromkeys(list(names) + swept))
    entries = []
    for name in names:
        value = os.environ.get(name, "")
        if value:
            entries.append({"name": name, "value": value})
        elif name in fleet:
            entries.append({
                "name": name,
                "valueFrom": {"secretKeyRef": {
                    "name": fleet[name]["secret"], "key": fleet[name]["key"],
                }},
            })
        # Anything else falls through to the manifest, which may state a
        # default; writing "" over it would be worse than not forwarding.
    if not entries:
        return []
    named = {e["name"] for e in entries}
    for spec in pod_specs(doc):
        for container in spec.get("containers", []):
            if container.get("name") != WORKLOAD_CONTAINER:
                continue
            env = container.setdefault("env", [])
            # --env wins over the manifest, which holds only the default, so a
            # step can override a cluster secret with one it fetched itself.
            env[:] = [e for e in env if e["name"] not in named]
            # Copies, so a JobSet's roles do not share one dict: editing one
            # role's env would otherwise edit every role's.
            env.extend(copy.deepcopy(e) for e in entries)
    return [e["name"] for e in entries]


# Substituted into the built-in Job only; a repo manifest states its hardware
# itself, so render() gives these names a specific error there.
SHAPE_NAMES = (
    "CHIPS",
    "TOPOLOGY",
    "ACCELERATOR_LABEL",
    "MEMORY_REQUEST",
)


def render(path, image, name, shape):
    """The manifest as a document: names substituted, kind checked, ints fixed."""
    if not os.path.exists(path):
        raise SystemExit(
            f"manifest {path!r} not found in the checkout. Point --manifest at "
            f"a Job or JobSet in the calling repo."
        )

    required = {"WORKLOAD_NAME": name, "IMAGE": image, **shape}
    # Whatever the step exports, so a manifest can pin ${BUILDKITE_COMMIT} or a
    # size its own pipeline sets.
    optional = {k: v for k, v in os.environ.items() if k not in required}

    # One substitution pass over the two merged, not a pass each: with two
    # passes the escape has to survive both, so a literal dollar in a
    # container's shell would need $$$$ instead of the usual $$.
    try:
        doc = yaml.safe_load(
            string.Template(open(path).read()).substitute({**optional, **required})
        )
    except KeyError as e:
        missing = e.args[0]
        hint = (
            "Hardware is stated, not asked for: write the accelerator label, "
            "the topology and the chip count into the pod itself."
            if missing in SHAPE_NAMES else
            f"The launcher supplies {', '.join(sorted(required))}; every other "
            "name has to come from the step's environment."
        )
        raise SystemExit(f"{path}: nothing provides ${{{missing}}}.\n{hint}")
    except ValueError as e:
        # A lone `$` that is not a placeholder. Write `$$` for a literal one.
        raise SystemExit(f"{path}: {e}")
    except yaml.YAMLError as e:
        raise SystemExit(f"{path}: not valid YAML once substituted: {e}")

    if not isinstance(doc, dict):
        raise SystemExit(f"{path}: not a Kubernetes object, but {type(doc).__name__}")
    if doc.get("kind") not in SUPPORTED_KINDS:
        raise SystemExit(
            f"{path}: kind {doc.get('kind')!r} is not one of {sorted(SUPPORTED_KINDS)}"
        )
    try:
        specs = pod_specs(doc)
    except (KeyError, TypeError):
        specs = []
    if not specs:
        raise SystemExit(
            f"{path}: a {doc['kind']} with no pods in it. A Job needs "
            "spec.template.spec; a JobSet needs spec.replicatedJobs, each with "
            "a template.spec.template.spec."
        )
    return coerce_ints(doc)


def finalise(doc, profile, registry, name, labels, owner, command, where):
    """Everything the launcher decides rather than the manifest."""
    meta = doc.setdefault("metadata", {})
    meta["name"] = name
    meta["namespace"] = NAMESPACE
    meta.setdefault("labels", {})[QUEUE_LABEL] = profile["queue"]
    meta["labels"].update(labels)
    if owner:
        meta["ownerReferences"] = [owner]

    for pod_meta in pod_metadatas(doc):
        pod_meta.setdefault("labels", {}).update(labels)

    # Required with or without a command of our own: it is also where step
    # environment is forwarded and whose output is streamed back.
    workload = [c for spec in pod_specs(doc)
                for c in spec.get("containers", [])
                if c.get("name") == WORKLOAD_CONTAINER]
    if not workload:
        raise SystemExit(
            f"{where}: no container named {WORKLOAD_CONTAINER!r}. That is the "
            "one the launcher forwards step environment into and reads logs "
            "from; name the container running the test."
        )
    # Set as a list element rather than interpolated into YAML, so a command
    # containing quotes or newlines cannot corrupt the manifest.
    if command is not None:
        for container in workload:
            container["args"] = [command]

    cap_runtime(doc, profile, registry)
    inherit_defaults(doc, profile)
    size_host_volumes(doc, profile)
    return doc


def requested_runtime():
    """What the step asked to run for, if it asked.

    A step sharing a manifest with others has nowhere else to put a deadline of
    its own, and the same four chips serve a smoke test and a day-long eval.
    """
    raw = os.environ.get("TPU_MAX_RUNTIME_SECONDS", "").strip()
    if not raw:
        return None
    try:
        seconds = int(raw)
    except ValueError:
        raise SystemExit(
            f"TPU_MAX_RUNTIME_SECONDS={raw!r} is not a whole number of seconds"
        )
    if seconds <= 0:
        raise SystemExit(
            f"TPU_MAX_RUNTIME_SECONDS={raw!r} must be greater than zero"
        )
    return seconds


def cap_runtime(doc, profile, registry):
    """Bound how long the workload may hold its chips.

    Narrowest opinion wins: TPU_MAX_RUNTIME_SECONDS from the step, else the
    manifest's activeDeadlineSeconds, else the shape's default. The registry
    ceiling applies to all three - a workload must not outlast the step
    watching it, or it holds a reservation nothing will clean up.
    """
    default = int(profile["max_runtime_seconds"])
    ceiling = int(registry["runtime_max_seconds"])
    asked = requested_runtime()
    if asked is not None:
        # Name what the step overrode: a manifest and a step disagreeing about
        # the deadline is baffling anywhere but the log.
        stated = sorted(
            {
                int(spec["activeDeadlineSeconds"])
                for spec in job_specs(doc)
                if spec.get("activeDeadlineSeconds") is not None
            }
        )
        over = f", over the manifest's {', '.join(f'{s}s' for s in stated)}"
        log(
            f"runtime {min(asked, ceiling)}s, from TPU_MAX_RUNTIME_SECONDS"
            f"{over if stated else ''}"
        )
    for spec in job_specs(doc):
        # `is None` rather than falsy, so a manifest stating 0 keeps it rather
        # than silently getting the shape default.
        current = spec.get("activeDeadlineSeconds")
        if asked is not None:
            current = asked
        elif current is None:
            current = default
        spec["activeDeadlineSeconds"] = min(int(current), ceiling)


def pod_defaults():
    """What pod_defaults.yaml gives a workload, split by what it depends on.

    One document rather than a pod to read them off: the set is the fleet's
    contract with every repo, and a Job that also defined it could not be
    edited for its own sake without changing every manifest too.
    """
    with open(POD_DEFAULTS) as fh:
        doc = yaml.safe_load(fh) or {}
    tpu = doc.get("tpuPods") or {}
    missing = {"volumes", "volumeMounts", "annotations", "spec", "env"} - set(tpu)
    if missing or not (doc.get("everyPod") or {}).get("annotations"):
        raise SystemExit(
            f"{POD_DEFAULTS} is missing tpuPods.{'/'.join(sorted(missing))} or "
            "everyPod.annotations. A workload inheriting it would come up "
            "without its caches."
        )
    return doc


def inherit_defaults(doc, profile):
    """Give a manifest the fleet's pod setup without it restating it.

    The sidecar annotations come with the caches: without gke-gcsfuse/volumes
    GKE injects no sidecar and the claims silently never mount.

    Additive throughout. Anything the manifest already sets - a volume, a mount
    path, an annotation, an env name, a field of the pod or Job spec - is left
    alone, so a role can add its own or override one it needs to differ on.

    Only the chip-holding parts are held back from a chipless role, because
    only they depend on the hardware: the caches are sized from a TPU host's
    memory, and the retry rules are about TPU nodes being repaired.
    """
    annotations = (doc.get("metadata") or {}).get("annotations") or {}
    if annotations.get(DEFAULTS_ANNOTATION) != DEFAULTS_STANDARD:
        return
    defaults = pod_defaults()
    every = defaults["everyPod"]["annotations"]
    tpu = defaults["tpuPods"]

    for key, value in (defaults.get("workload") or {}).items():
        doc["spec"].setdefault(key, value)

    for template in pod_templates(doc):
        on_pod = template.setdefault("metadata", {}).setdefault(
            "annotations", {})
        for key, value in every.items():
            on_pod.setdefault(key, value)

        spec = template["spec"]
        if not pod_chips(spec):
            continue

        for key, value in tpu["annotations"].items():
            on_pod.setdefault(key, value)
        for key, value in tpu["spec"].items():
            spec.setdefault(key, copy.deepcopy(value))

        have = {v.get("name") for v in spec.setdefault("volumes", [])}
        spec["volumes"].extend(
            copy.deepcopy(v) for v in tpu["volumes"]
            if v.get("name") not in have
        )

        for container in spec.get("containers", []):
            if container.get("name") != WORKLOAD_CONTAINER:
                continue
            at = {m.get("mountPath")
                  for m in container.setdefault("volumeMounts", [])}
            # Ahead of the manifest's own, so a mount nested inside an
            # inherited one - the profile cache under the jax cache - is
            # applied after the mount it sits in rather than under it.
            container["volumeMounts"][:0] = [
                copy.deepcopy(m) for m in tpu["volumeMounts"]
                if m.get("mountPath") not in at
            ]
            named = {e.get("name")
                     for e in container.setdefault("env", [])}
            container["env"].extend(
                copy.deepcopy(e) for e in tpu["env"]
                if e.get("name") not in named
            )
            # Sized from the host like the volumes above, and for the same
            # reason: what the pod needs is a fraction of the machine it
            # landed on, which the manifest cannot know.
            requests = (container.setdefault("resources", {})
                        .setdefault("requests", {}))
            requests.setdefault("memory", profile["memory_request"])

    for job in job_specs(doc):
        if not pod_chips(job["template"]["spec"]):
            continue
        for key, value in (defaults.get("tpuJobs") or {}).items():
            job.setdefault(key, copy.deepcopy(value))


def size_host_volumes(doc, profile):
    """How large this host lets the memory-backed volumes grow.

    Per machine type, since host memory runs from 176 GB to 1440 GB across the
    shapes we run. Set only where the manifest left it open.

    Chip-holding roles only: the figures are fractions of a TPU host's memory,
    and a role holding no chips runs on a worker-cpu node that cannot honour
    them. validate() makes such a role state its own sizeLimit instead.

    Tested on the chips alone, not on the shape, which is also None for a pod
    that names an accelerator and misstates its count - that pod is on a TPU
    host and does want sizing; resolve_shape rejects it on its own terms.
    """
    for spec in pod_specs(doc):
        if not pod_chips(spec):
            continue
        for volume in spec.get("volumes", []):
            name = volume.get("name")
            key = HOST_SIZED_VOLUMES.get(name)
            if key is None or "emptyDir" not in volume:
                continue
            size = profile.get(key)
            if size is None:
                # A deploy applies this program and the shape registry as two
                # ConfigMaps in turn, so a launcher starting between the two can
                # read a registry older than itself.
                raise SystemExit(
                    f"the shape registry has no {key} for "
                    f"{profile.get('queue', 'this shape')}, so {name} would be "
                    "created with no ceiling. Regenerate and redeploy the "
                    "manifests; if a deploy is in flight, retry the step."
                )
            volume["emptyDir"].setdefault("sizeLimit", size)


# Fields the Kubernetes API declares as integers. Substitution turns them into
# strings, and the API rejects `activeDeadlineSeconds: "10800"` with a type
# error a long way from the cause.
INT_FIELDS = frozenset({
    "activeDeadlineSeconds",
    "backoffLimit",
    "completions",
    "parallelism",
    "replicas",
    "startupPolicyOrder",
    "terminationGracePeriodSeconds",
    "ttlSecondsAfterFinished",
})


def coerce_ints(node):
    """Recursively turn numeric strings into ints for known integer fields."""
    if isinstance(node, dict):
        for key, value in node.items():
            if (key in INT_FIELDS and isinstance(value, str)
                    and value.strip().lstrip("-").isdigit()):
                node[key] = int(value)
            else:
                coerce_ints(value)
    elif isinstance(node, list):
        for item in node:
            coerce_ints(item)
    return node


def validate(doc, registry, where):
    """Reject a manifest the cluster should not run.

    Deliberately narrow: PodSecurity `baseline` on the namespace already
    rejects privileged containers, hostPath volumes and host networking. These
    are the things admission cannot judge.
    """
    # Rejected rather than overwritten: the queue follows from the hardware the
    # pods ask for, so a label here reads as though the manifest chose it.
    if QUEUE_LABEL in doc.get("metadata", {}).get("labels", {}):
        raise SystemExit(
            f"{where}: sets {QUEUE_LABEL}. Remove it - the launcher sets the "
            "queue from the shape the pods select."
        )

    # The launcher's own account can create JobSets, so a workload running as
    # it could submit further work outside any quota.
    allowed = set(registry.get("workload_service_accounts") or ["default"])
    for spec in pod_specs(doc):
        sa = spec.get("serviceAccountName")
        if sa and sa not in allowed:
            raise SystemExit(
                f"serviceAccountName {sa!r} is not allowed on a workload pod; "
                f"the cluster permits {', '.join(sorted(allowed))}"
            )

    # The one case size_host_volumes cannot size. An unbounded memory-backed
    # emptyDir is as large as the node, so the node hits memory pressure before
    # the volume hits a limit and the kubelet evicts a victim of its own
    # choosing rather than the pod that overran.
    for spec in pod_specs(doc):
        if pod_chips(spec):
            continue
        for volume in spec.get("volumes", []):
            name = volume.get("name")
            if name not in HOST_SIZED_VOLUMES:
                continue
            empty_dir = volume.get("emptyDir")
            if empty_dir is not None and "sizeLimit" not in empty_dir:
                raise SystemExit(
                    f"{where}: {name} has no sizeLimit on a pod that asks for "
                    "no chips. The launcher sizes that volume from the TPU "
                    "host's memory and this pod is not on one, so the manifest "
                    "has to name a figure the node it did ask for can hold."
                )
    return doc


def find_workload(uid):
    """Kueue's Workload for our object, or None if it has not made one yet.

    None only ever means that: kubectl_json raises rather than returning None
    when it could not ask, which keeps "Kueue has not got to it" apart from
    "the manager did not answer".
    """
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


def startup_note(env, items):
    """Where a pod is between admission and running, in a few words.

    Admission only means the chips are reserved: the pod still has to be
    scheduled, the node possibly created and the image pulled, which runs to
    tens of minutes. Reported, not enforced. None once a pod is up and its own
    output takes over.
    """
    if not items:
        return "waiting for a pod to be created"
    pod = items[0]
    status = pod.get("status", {})
    phase = status.get("phase")
    if phase in ("Running", "Succeeded", "Failed"):
        return None
    for cond in status.get("conditions", []) or []:
        if cond.get("type") == "PodScheduled" and cond.get("status") != "True":
            return f"pod not scheduled yet: {cond.get('reason') or ''}".strip()
    for cs in status.get("containerStatuses", []) or []:
        waiting = (cs.get("state") or {}).get("waiting") or {}
        if waiting.get("reason"):
            return f"container waiting: {waiting['reason']}"

    # The collector reads only the workload container, so without this a bucket
    # the pod cannot authenticate to looks exactly like a slow node for as long
    # as the deadline allows.
    init = [cs for cs in (status.get("initContainerStatuses") or [])
            if not (cs.get("state") or {}).get("terminated")]
    for cs in init:
        name = cs.get("name") or ""
        if "gcsfuse" not in name:
            continue
        tail = subprocess.run(
            ["kubectl", "-n", NAMESPACE, "logs", pod["metadata"]["name"],
             "-c", name, "--tail=3"],
            env=env, capture_output=True, text=True, check=False, timeout=60,
        )
        last = " / ".join(l.strip() for l in tail.stdout.splitlines() if l.strip())
        if last:
            return f"{name} still starting: {last[:300]}"
    return "pod scheduled, container starting"


def termination_reasons(items):
    """Why the containers ended, from the pod rather than from the output.

    A process killed by the kernel prints no traceback, so an OOM and a test
    returning 1 look identical in the log. The pod records which it was.
    """
    out = []
    for pod in items:
        for cs in pod.get("status", {}).get("containerStatuses", []) or []:
            term = (cs.get("state") or {}).get("terminated") or {}
            if not term:
                term = (cs.get("lastState") or {}).get("terminated") or {}
            if term and term.get("reason") not in (None, "Completed"):
                out.append(f"{pod['metadata']['name']}/{cs.get('name')}: "
                           f"{term.get('reason')} (exit {term.get('exitCode')})")
    return out


def workload_exit_code(items):
    """The status the workload's own command returned, or None.

    A step's result is the workload's result, and a step with a retry rule
    needs the specific code rather than a flat 1.

    Only when every terminated workload container agrees: disagreement means
    several roles failed for different reasons and no single number describes
    the run, so the caller's 1 is the honest answer.
    """
    codes = set()
    for pod in items:
        for cs in pod.get("status", {}).get("containerStatuses", []) or []:
            if cs.get("name") != "workload":
                continue
            term = (cs.get("state") or {}).get("terminated") or {}
            if not term:
                term = (cs.get("lastState") or {}).get("terminated") or {}
            code = term.get("exitCode")
            if code:
                codes.add(code)
    if len(codes) != 1:
        return None
    code = codes.pop()
    # Beyond a byte the shell truncates, and 0 would report a failure as a pass.
    return code if 0 < code < 256 else None


def describe_admission(workload):
    if workload is None:
        return "waiting for Kueue to create the workload"
    status = workload.get("status", {})
    # Eviction first: a workload keeps status.clusterName once admitted, so
    # testing that first would report "admitted to X" for the rest of the run
    # and a preemption would never reach the log.
    evicted = condition(workload, "Evicted")
    if evicted and evicted.get("status") == "True":
        return f"evicted ({evicted.get('reason')}), waiting for re-admission"
    if status.get("clusterName"):
        return f"admitted to worker cluster {status['clusterName']}"
    if status.get("nominatedClusterNames"):
        return f"dispatching to {', '.join(status['nominatedClusterNames'])}"
    quota = condition(workload, "QuotaReserved")
    if quota and quota.get("status") != "True":
        return f"waiting for quota: {quota.get('message', quota.get('reason', ''))}"
    return "waiting for admission"


def worker_env(cluster_name, registry):
    """Connect Gateway credentials for a worker, in an isolated kubeconfig.

    Isolated deliberately: gcloud rewrites the current context, and the
    launcher's calls to the manager rely on having no kubeconfig at all.
    """
    worker = (registry.get("workers") or {}).get(cluster_name)
    if worker is None:
        log(f"no gateway mapping for worker {cluster_name!r}; logs unavailable")
        return None
    env = {**os.environ, "KUBECONFIG": WORKER_KUBECONFIG}
    # Called from inside the watch loop with the workload on the chips, and the
    # caller retries every poll, so a timeout costs one interval while no
    # timeout costs the run.
    try:
        proc = subprocess.run(
            [
                "gcloud", "container", "fleet", "memberships", "get-credentials",
                worker["membership"], "--project", worker["project"],
                # Without this gcloud searches every fleet location and fails
                # the lookup when any one of them is unreachable, even though
                # the location we want answered.
                "--location", worker["location"],
            ],
            env=env, capture_output=True, text=True, check=False,
            timeout=CLI_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        log(f"gateway credentials timed out for {cluster_name} after "
            f"{CLI_TIMEOUT_SECONDS}s; retrying on the next poll")
        return None
    if proc.returncode != 0:
        log(f"gateway credentials failed for {cluster_name}: "
            f"{proc.stderr.strip()[:300]}")
        return None
    return env


def worker_pods(env, job_id):
    """This workload's pods on the worker, or None if they could not be read.

    One read per turn of the loop, shared by every reader: fetching per reader
    would cost three Connect Gateway round trips for one answer.

    None rather than an empty list, because "no pods yet" and "could not ask"
    read differently in a step log.
    """
    proc = subprocess.run(
        ["kubectl", "-n", NAMESPACE, "get", "pods",
         "-l", f"buildkite.com/job-id={job_id}", "-o", "json"],
        env=env, capture_output=True, text=True, check=False, timeout=60,
    )
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout).get("items", [])
    except json.JSONDecodeError:
        return None


def pod_groups(items):
    """The pods tagged with the Job that owns each, for log attribution.

    Grouped by owning Job rather than by pod, because a pod does not survive
    preemption and the Job name does.
    """
    pods = []
    for pod in items:
        meta = pod.get("metadata", {})
        labels = meta.get("labels", {})
        group = labels.get("batch.kubernetes.io/job-name") or meta.get("name", "pod")
        index = labels.get("batch.kubernetes.io/job-completion-index")
        if index is not None:
            group = f"{group}-{index}"
        pods.append({
            "name": meta.get("name", ""),
            "group": group,
            "phase": pod.get("status", {}).get("phase", "Unknown"),
        })
    return pods


class LogCollector:
    """Streams the workload pods' output into this step's log.

    Polled rather than followed because Connect Gateway resets the long-lived
    HTTP/2 stream `kubectl logs -f` needs. Short requests through the same
    gateway are reliable.
    """

    def __init__(self, env, job_id):
        self.env = env
        self.job_id = job_id
        self.cursor = {}        # pod name -> last log timestamp seen
        self.emitted = 0        # lines printed, over every pod
        self.last_group = None  # whose output the log is currently under

    def _fetch(self, pod):
        # The workload container only: --all-containers interleaves containers
        # whose timestamps advance independently, and one cursor per pod cannot
        # represent that - a chatty sidecar drags the cursor forward and the
        # workload's own lines are discarded as already seen.
        cmd = ["kubectl", "-n", NAMESPACE, "logs", pod["name"],
               "--container", WORKLOAD_CONTAINER, "--timestamps=true"]
        since = self.cursor.get(pod["name"])
        if since:
            cmd += ["--since-time", since]
        proc = subprocess.run(cmd, env=self.env, capture_output=True,
                              text=True, check=False, timeout=120)
        # A pod that is Pending, or already deleted, is normal - not an error.
        return proc.stdout if proc.returncode == 0 else ""

    def sweep(self):
        """Poll off a read of our own, for the two moments outside the loop."""
        return self.poll(worker_pods(self.env, self.job_id))

    def poll(self, items):
        """Emit whatever is new in the pods it is handed."""
        pods = pod_groups(items or [])
        if not pods:
            return 0
        emitted = 0
        for pod in sorted(pods, key=lambda p: p["group"]):
            out = self._fetch(pod)
            if not out:
                continue
            fresh = []
            keeping = False
            for line in out.splitlines():
                stamp, _, text = line.partition(" ")
                if not TIMESTAMP.match(stamp):
                    # A continuation: kubectl stamps an entry, but splitlines()
                    # also breaks on the carriage returns inside one. Taking a
                    # fragment's first word as a timestamp would poison the
                    # cursor and silence the pod from its first progress bar on.
                    if keeping:
                        fresh.append(line)
                    continue
                # --since-time is inclusive to the second, so the cursor has to
                # drop what was already shown rather than trust the server.
                keeping = self.cursor.get(pod["name"], "") < stamp
                if not keeping:
                    continue
                self.cursor[pod["name"]] = stamp
                fresh.append(text)
            if not fresh:
                continue
            if pod["group"] != self.last_group:
                print(f"--- {pod['group']}", flush=True)
                self.last_group = pod["group"]
            for text in fresh:
                print(text, flush=True)
            emitted += len(fresh)
        self.emitted += emitted
        return emitted


def main():
    log(f"python {sys.version.split()[0]} at {sys.executable}")

    parser = argparse.ArgumentParser(prog="launch")
    parser.add_argument(
        "--machine-type",
        help="TPU machine type for the built-in Job, e.g. ct6e-standard-8t. "
             "The last field is chips per host, so this fixes the host as well "
             "as the generation.",
    )
    parser.add_argument(
        "--topology",
        help="Slice topology asked of that machine type, e.g. 2x4. Together "
             "with --machine-type it names a shape the fleet has node pools "
             "and quota for; everything else about placement follows.",
    )
    parser.add_argument(
        "--env", action="append", default=[], metavar="NAME",
        help="forward this environment variable from the step into the "
             "workload container; repeatable. Names only - the value is read "
             "here, so nothing secret has to appear in the pipeline.",
    )
    parser.add_argument(
        "--manifest",
        help="Job or JobSet to submit, relative to the checkout, for work the "
             "built-in Job cannot express. It states its own hardware and its "
             "own commands, so it takes the place of --machine-type, "
             "--topology and the command rather than adding to them.",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = args.command
    if command and command[0] == "--":
        command = command[1:]

    registry = load_registry()
    if args.manifest and command:
        raise SystemExit(
            "--manifest describes what to run as well as where, so there is no "
            f"command to pass here. Put {shlex.join(command)!r} in the "
            f"{WORKLOAD_CONTAINER!r} container of {args.manifest}."
        )
    if not args.manifest and not command:
        raise SystemExit(
            "no command given; use: launch --machine-type M --topology T "
            "-- <command>"
        )
    if args.manifest and (args.machine_type or args.topology):
        raise SystemExit(
            "--manifest states its own hardware, so --machine-type and "
            "--topology do not apply to it. Put the accelerator label, the "
            "topology and the chip count in the pods that need them."
        )
    if not args.manifest and not (args.machine_type and args.topology):
        raise SystemExit(
            "say what hardware to run on: --machine-type and --topology for "
            "the built-in Job, or --manifest for a workload that states its "
            "own. Available: " + "; ".join(available(registry))
        )

    shape = {}
    manifest = args.manifest or DEFAULT_JOB
    if not args.manifest:
        profile = load_profile(registry, args.machine_type, args.topology)
        if profile["hosts"] > 1:
            raise SystemExit(
                f"{args.machine_type} at {args.topology} is "
                f"{profile['hosts']} hosts, and the built-in Job is one pod. "
                "Write a JobSet and pass --manifest."
            )
        shape = {
            "CHIPS": str(profile["chips"]),
            "TOPOLOGY": profile["topology"],
            "ACCELERATOR_LABEL": profile["accelerator_label"],
            "MEMORY_REQUEST": profile["memory_request"],
        }

    image = resolve_image(registry)
    name = workload_name()
    labels = correlation_labels()
    doc = render(manifest, image, name, shape)
    # Read back even when the flags chose it, so the pods are the one answer to
    # what shape a workload is.
    profile = resolve_shape(doc, registry, manifest)
    validate(doc, registry, manifest)
    forwarded = forward_env(doc, args.env, registry)
    if forwarded:
        log(f"forwarding step env: {', '.join(forwarded)}")
    # shlex.join, not " ".join: argv has already been through the step's shell
    # and the built-in Job runs the result through another one, so joining
    # plainly loses every quote the step wrote.
    finalise(doc, profile, registry, name, labels, owner_reference(),
             shlex.join(command) if command else None, manifest)
    kind = SUPPORTED_KINDS[doc["kind"]]

    deleted = False
    collector = None

    def cleanup(signum, _frame):
        # agent-stack deletes the pod on Buildkite cancellation, so SIGTERM is
        # how the launcher learns the build is gone.
        nonlocal deleted
        if not deleted:
            deleted = True
            log(f"signal {signum}, deleting {kind}/{name}")
            if collector:
                collector.sweep()
            delete_workload(kind, name, timeout=CLEANUP_TIMEOUT_SECONDS)
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    log(f"submitting {doc['kind']} {name} to {profile['queue']} "
        f"({profile['hosts']} x {profile['chips']} chips, from {manifest})")
    subprocess.run(
        ["kubectl", "-n", NAMESPACE, "apply", "-f", "-"],
        input=json.dumps(doc), text=True, check=True,
        timeout=CLI_TIMEOUT_SECONDS,
    )

    # Everything below holds an admitted workload, so the except clauses delete
    # it: the ownerReference only collects it once the launcher's own pod
    # object is removed, and a pod that exited non-zero stays for minutes to
    # hours while the chips sit unavailable.
    #
    # Not a `finally`: on the ordinary paths the workload has already reached a
    # terminal state and the ownerReference is the right thing to remove it,
    # after Kueue has read the conditions this loop just logged.
    #
    # Outside the try because the handler withdraws the notice, and the first
    # statement inside can raise.
    waiting = waiting_context()
    announced = False

    def stop_announcing():
        nonlocal announced
        if announced:
            announced = False
            withdraw_waiting(waiting)

    # Every read of the manager below goes through with_grace; the worker-side
    # reads that stream the pods' logs have their own failure handling.
    blind_since = None
    # Kept across spells, because the grace period is per contiguous outage - a
    # manager answering one poll in twenty resets it - while the admission
    # timeout wants to report the total.
    blind_total = 0.0

    # While blind the step's log is otherwise silent, and ten silent minutes
    # look the same as a hang.
    blind_notice_seconds = 60

    def with_grace(fn, *fn_args):
        nonlocal blind_since, blind_total
        announced_at = None
        while True:
            try:
                value = fn(*fn_args)
            except ApiUnreachable as err:
                now = time.monotonic()
                if blind_since is None:
                    blind_since = now
                if announced_at is None or now - announced_at >= blind_notice_seconds:
                    announced_at = now
                    blind = now - blind_since
                    log(f"manager API unreachable for {blind:.0f}s of "
                        f"{API_GRACE_SECONDS}s, still waiting on {kind}/{name}: "
                        f"{err}")
                if now - blind_since > API_GRACE_SECONDS:
                    raise
                time.sleep(POLL_SECONDS)
                continue
            if blind_since is not None:
                waited = time.monotonic() - blind_since
                blind_total += waited
                log(f"manager API answering again after {waited:.0f}s")
                blind_since = None
            return value

    try:
        created = with_grace(kubectl_json, "get", kind, name)
        if created is None:
            # The apply above succeeded, so something removed it since.
            log(f"{kind}/{name} was removed immediately after being created")
            stop_announcing()
            return 1
        uid = created["metadata"]["uid"]
        admission_limit = admission_timeout(registry)
        dispatch_limit = int(registry["admission_max_seconds"])
        started = time.monotonic()
        reserved = None
        admitted = False
        running = False
        last_startup = None
        last_note = None
        genv = None
        job_id = labels.get("buildkite.com/job-id")

        while True:
            obj = with_grace(kubectl_json, "get", kind, name)
            if obj is None:
                log(f"{kind}/{name} disappeared")
                stop_announcing()
                return 1

            # Watched for the whole run, not just until admission: preemption
            # happens after it.
            workload = with_grace(find_workload, uid)
            note = describe_admission(workload)
            cluster = (workload or {}).get("status", {}).get("clusterName")

            # Retried on any poll where the cluster is known and there are no
            # credentials yet: one attempt at first admission would cost a whole
            # run's logs whenever it failed.
            if cluster and genv is None:
                genv = worker_env(cluster, registry)
                if genv:
                    collector = LogCollector(genv, job_id)
            if cluster and not admitted:
                admitted = True
                stop_announcing()
            # Two different waits: before quota is reserved the workload is in
            # line behind other work and gets the whole budget, after it only
            # dispatch is left and a workload past the shorter cap is stuck
            # while holding a reservation.
            #
            # Cleared as well as set, because preemption and a lost worker both
            # send an admitted workload back to the queue with QuotaReserved
            # false; latched, the dispatch cap would kill it for being queued.
            if quota_reserved(workload):
                if reserved is None:
                    reserved = time.monotonic()
            else:
                reserved = None
            if reserved is not None:
                limit, since, what = dispatch_limit, reserved, "dispatched"
            else:
                limit, since, what = admission_limit, started, "admitted"
            if not admitted and time.monotonic() - since > limit:
                # Time spent unable to reach the manager counts against this on
                # purpose: the budget is carved so the run still fits inside the
                # step's own deadline, which a blind spell does not move.
                blind = f", {blind_total:.0f}s of it blind" if blind_total else ""
                log(f"not {what} within {limit}s{blind} - capacity, not the test")
                stop_announcing()
                delete_workload(kind, name)
                return 1
            if note != last_note:
                log(note)
                last_note = note
                # Kept current while the wait goes on, but withdrawn rather
                # than corrected once the workload lands.
                if waiting and not admitted:
                    announce_waiting(waiting, profile["queue"], note)
                    announced = True

            # One read of the workload's pods, for both readers below.
            items = worker_pods(genv, job_id) if genv else None

            # A failed read is not a started pod: leave it to the next turn.
            if admitted and items is not None and not running:
                s = startup_note(genv, items)
                if s is None:
                    running = True
                elif s != last_startup:
                    log(s)
                    last_startup = s

            if collector:
                collector.poll(items)

            # Under MultiKueue the local object is a shadow of one that ran on a
            # worker, so its own status may never be filled in. The Workload's
            # Finished condition is authoritative.
            finished = condition(workload, "Finished")
            wl_done = bool(finished and finished.get("status") == "True")
            wl_failed = bool(wl_done and "Failed" in finished.get("reason", ""))
            wl_succeeded = wl_done and not wl_failed

            if doc["kind"] == "Job":
                status = obj.get("status", {})
                done = status.get("succeeded", 0) >= 1 or wl_succeeded
                failed_cond = condition(obj, "Failed")
                failed = ((failed_cond and failed_cond.get("status") == "True")
                          or status.get("failed", 0) >= 1 or wl_failed)
            else:
                completed = condition(obj, "Completed")
                done = bool(completed and completed.get("status") == "True") or wl_succeeded
                failed_cond = condition(obj, "Failed")
                failed = bool(failed_cond and failed_cond.get("status") == "True") or wl_failed

            if done or failed:
                if collector:
                    collector.sweep()     # last look before the pods are removed
                    if not collector.emitted:
                        log("no workload logs captured: the pods were removed "
                            "before anything could be read from them.")
                elif genv is None:
                    log("no workload logs captured: the workload never reported "
                        "a cluster to fetch gateway credentials for, so none were "
                        "requested. Not a Connect Gateway permission problem.")
                pods = worker_pods(genv, job_id) or [] if (failed and genv) else []
                for why in termination_reasons(pods):
                    log(f"container terminated: {why}")

                # Read while the Workload still exists: Kueue collects it soon
                # after the run, and once it is gone a preemption and a test
                # returning 1 read the same.
                if failed:
                    for c in (workload or {}).get("status", {}).get("conditions", []):
                        if c.get("status") != "True":
                            continue
                        detail = " ".join(x for x in (c.get("reason"),
                                                      c.get("message")) if x)
                        log(f"workload {c.get('type')}: {detail}"[:300])

                    # Expand the last section on the build page.
                    print("^^^ +++", flush=True)
                    log(f"{kind}/{name} failed")
                    return workload_exit_code(pods) or 1
                log(f"{kind}/{name} completed")
                return 0

            if collector and not collector.emitted:
                time.sleep(FIRST_LOG_POLL_SECONDS)
            else:
                time.sleep(POLL_SECONDS)
    except ApiUnreachable as err:
        # Handled apart from the traceback below because the delete it is about
        # to attempt goes to the same API server that just stopped answering,
        # so the chips may need reclaiming by hand.
        stop_announcing()
        log(f"no answer from the manager's API server for {API_GRACE_SECONDS}s: "
            f"{err}")
        if not deleted:
            deleted = True
            if not delete_workload(kind, name):
                log(f"WARNING: {kind}/{name} could not be deleted either, so it "
                    "may still be running on its worker cluster and holding its "
                    "chips. It is collected when this pod's object is removed.")
        print("^^^ +++", flush=True)
        return 1
    except BaseException:
        stop_announcing()
        if not deleted:
            deleted = True
            delete_workload(kind, name)
        raise


if __name__ == "__main__":
    sys.exit(main())
