#!/usr/bin/env python3
"""Submit a TPU workload on behalf of a Buildkite job, then own its lifecycle.

Runs as the command container of an agent-stack-k8s Job in the manager
cluster. Every TPU step goes through here, single pod or not, so there is one
code path and one place where policy lives.

    launch --machine-type ct6e-standard-8t --topology 2x4 -- pytest tests/e2e
    launch --manifest .buildkite/kubernetes/manifests/1p1d.yaml

Why a launcher rather than running the test in this pod: agent-stack-k8s can
only create a batch/v1 Job, and a Job cannot span hosts, so multi-host work
needs something to create a JobSet. Routing the single-pod case through it too
costs one cheap CPU pod and buys two properties that only hold when the agent
is outside the Kueue workload: the agent acquires its Buildkite job in seconds
rather than after admission and node scale-up, so the reservation cannot lapse
and be claimed twice; and preemption evicts the workload without killing the
agent, so a preempted run is a pause in the log rather than a failed build.

The two forms above are the same path with different manifests. Most steps run
one pod on one host, which is the same YAML every time, so the launcher ships
that Job itself and a step gives only the hardware and the command. A step that
needs another arrangement - roles that talk to each other, hosts of one slice -
brings a manifest instead, and that manifest says everything: the hardware,
because a JobSet can hold roles that want chips beside a client that wants none
and no flag here could express that, and the commands, because a JobSet has one
per role and no reading of one command line says which of them it replaces.

So placement is read back out of the manifest rather than taken from a flag.
That is also the only reading that can be checked: a shape the fleet has no
node pool for is refused here, where the profile registry says what the pools
are, instead of queueing forever against quota that does not exist.

A manifest comes from the repo being tested, and so is untrusted. PodSecurity
`baseline` on the workload namespace already rejects privileged containers,
hostPath volumes and host networking, so validation here covers only what
admission cannot know: that the image is from an allowed registry, and that the
workload does not run as the launcher's own identity.
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

# Comes from the launcher image, which exists to add it: the Cloud CLI image it
# is built on ships no YAML importable from Python 3. Installed there rather
# than fetched here, so PyPI is not in the path of every TPU step.
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

# The Job a step gets when it names hardware and nothing else. Deployed beside
# this program rather than kept in a repo, because everything in it is a fact
# about the cluster - which caches exist, what mounts them, which identity may
# write them - and a repo that copied it would be copying those.
DEFAULT_JOB = os.environ.get("LAUNCHER_DEFAULT_JOB", "/opt/launcher/manifests/job.yaml")

# Where a pod says what hardware it wants. Read back to find the profile, so
# these are the launcher's names for them too.
ACCELERATOR_KEY = "cloud.google.com/gke-tpu-accelerator"
TOPOLOGY_KEY = "cloud.google.com/gke-tpu-topology"
TPU_RESOURCE = "google.com/tpu"

# Where Kueue reads the queue, on the top-level object for both Job and JobSet.
QUEUE_LABEL = "kueue.x-k8s.io/queue-name"

# GKE's name, not ours: the gcsfuse sidecar looks for an emptyDir called this
# and uses it as the file cache. Sized here because the ceiling is a fact about
# the host, and the manifest does not know which host it landed on.
FUSE_CACHE_VOLUME = "gke-gcsfuse-cache"

# kubectl --timestamps prefixes each entry with RFC3339. Anything else on a
# line is a fragment of the entry above it, not a new one.
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.]+Z$")

# The container a manifest must name for its test. Already assumed by env
# forwarding, which only injects into this one.
WORKLOAD_CONTAINER = "workload"

POLL_SECONDS = 5
# From admission until the first line reaches us, and only there. When the
# workload finishes MultiKueue deletes the remote Job and the pods go with it,
# so a short step on a warm node can be created, run and removed between two
# ordinary polls and leave nothing to read at all. Once output is arriving the
# pod is demonstrably still there and the ordinary interval is enough.
FIRST_LOG_POLL_SECONDS = 2

# Worker credentials are kept out of the default kubeconfig; see worker_env().
WORKER_KUBECONFIG = "/tmp/worker.kubeconfig"

# Kinds the launcher will submit. Anything else in a template is a mistake, and
# catching it here is cheaper than a confusing RBAC denial.
SUPPORTED_KINDS = {"Job": "job", "JobSet": "jobset"}

# Every kubectl and gcloud call the launcher makes is a single API request, so
# this is generous for all of them. Generous on purpose: the cost of being
# wrong is not symmetric. A call cut off early turns a slow control plane into
# a failed test, while one that waits two minutes only delays a poll that runs
# every few seconds anyway.
CLI_TIMEOUT_SECONDS = 120


def log(msg):
    print(f"~~~ launcher: {msg}", flush=True)


# The agent binary, which the controller copies onto the shared workspace
# volume rather than onto this container's PATH. Looked up anyway, so an
# invocation that does put it on PATH keeps working.
AGENT_CLI = shutil.which("buildkite-agent") or "/workspace/buildkite-agent"


def agent(*args):
    """Run one buildkite-agent subcommand, best effort.

    Everything the launcher uses this for is commentary on a run that is
    happening either way, so a failure here is worth a line in the log and
    nothing more. The credentials come from the step's own environment, which
    is why there is nothing to pass.
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

    Per job rather than per build: several steps of one build queue at once,
    each for its own shape, and a shared context would have them overwrite each
    other. A context is also what lets the notice be withdrawn on admission -
    an appended line could only ever be added to.
    """
    job_id = os.environ.get("BUILDKITE_JOB_ID")
    return f"kueue-waiting-{job_id}" if job_id else None


def announce_waiting(context, queue, note):
    """Say on the build page that this step is queued, not running.

    Buildkite has no state for "the agent has the job but the hardware does
    not". The step is `running` from the moment the launcher starts, so a build
    where every TPU step is waiting on one busy reservation looks exactly like
    a build where every step is executing. The log says which it is, but only
    if someone opens the job.

    An annotation is the one surface that is visible from the build page
    without opening anything, and it is withdrawn on admission, so what is left
    on screen is the set of steps still waiting.
    """
    label = os.environ.get("BUILDKITE_LABEL", "this step")
    agent("annotate", "--context", context, "--style", "info",
          f":hourglass: **{label}** is waiting for hardware, not running: "
          f"{note} (queue `{queue}`).")


def withdraw_waiting(context):
    agent("annotation", "remove", "--context", context)


def kubectl(*args, check=True, timeout=CLI_TIMEOUT_SECONDS):
    """Run one kubectl call against the manager.

    Every call here is a single API request, so a deadline costs nothing when
    the API server is answering and is the difference between a slow poll and
    a stopped launcher when it is not. The launcher holds an admitted workload
    for as long as it runs, and a call that never returns holds the chips with
    it - past the step timeout, since the step is waiting on this process.

    A timeout reads as a failed call rather than an exception for the callers
    that already tolerate one: a poll that cannot reach the API server has the
    same nothing to report as a poll that was refused, and the loop's next
    attempt is a better answer than a traceback.
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


def kubectl_json(*args):
    proc = kubectl(*args, "-o", "json", check=False)
    return json.loads(proc.stdout) if proc.returncode == 0 else None


def delete_workload(kind, name):
    """Delete the workload and say whether it worked.

    On cancellation the launcher is about to exit, so a failed delete leaves
    chips running with nobody watching; logging the intent alone would make
    that indistinguishable from success.
    """
    proc = kubectl("delete", kind, name, "--wait=false", check=False)
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
    """The shape a step asked for, by the two names it is already known by.

    A machine type and a topology rather than the profile's own name: those are
    what a node pool, a reservation and `gcloud` all call a shape, and the
    profile name is only the two of them joined. Matched on the registry's
    fields rather than by composing that name, so the convention lives in one
    place - the generator that writes it.
    """
    for profile in registry.get("profiles", {}).values():
        if (profile.get("machine_type") == machine_type
                and profile.get("topology") == topology):
            return profile
    raise SystemExit(
        f"the fleet runs no {machine_type} at {topology}. Available: "
        + "; ".join(available(registry))
    )


def pod_shape(spec):
    """The hardware one pod asks for: what it selects, and how many chips.

    Chips as well as the two labels, because those do not identify a shape on
    their own: a 2x4 slice of v6e is eight chips either as one ct6e-standard-8t
    or as two ct6e-standard-4t, and which it is decides the host, the pod count
    and the quota.
    """
    selector = spec.get("nodeSelector") or {}
    chips = {
        str((c.get("resources") or {}).get("limits", {}).get(TPU_RESOURCE))
        for c in spec.get("containers", [])
        if (c.get("resources") or {}).get("limits", {}).get(TPU_RESOURCE) is not None
    }
    return (
        selector.get(ACCELERATOR_KEY),
        selector.get(TOPOLOGY_KEY),
        chips.pop() if len(chips) == 1 else None,
    )


def resolve_shape(doc, registry, where):
    """The profile the manifest's TPU pods describe.

    Read rather than passed in, so the hardware is written once. What follows
    from it - the queue, the deadline, the file cache size - is cluster policy,
    and the manifest states none of it.

    Only the roles holding chips decide it. A disaggregated workload is servers
    plus a client that drives them over HTTP, and the client wants no
    accelerator at all; the queues put google.com/tpu alone under quota, so a
    role asking for none is admitted with the rest and scheduled wherever the
    worker has room.
    """
    asked = {pod_shape(spec) for spec in pod_specs(doc)}
    # Nothing of the three, rather than "no chips": a role that names an
    # accelerator but forgets its limit has made a mistake, and falls through to
    # the message below rather than being read as CPU-only and ignored.
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


def admission_timeout(registry, doc):
    """How long to wait for chips: the whole budget, less this run.

    Derived rather than configured, from the only two numbers worth choosing.
    Longer leaves no room to run; shorter fails steps that were queueing. Read
    off the deadline cap_runtime settled on rather than the shape's default, so
    a workload that asked to serve for ten hours is not also given eight to
    queue in.
    """
    runtime = max(int(spec["activeDeadlineSeconds"]) for spec in job_specs(doc))
    return max(int(registry["total_max_seconds"]) - runtime, 300)


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


def pin_digest(image):
    """Resolve a tag to the digest it points at right now.

    A run is many pulls - a pod per role, and another on every restart - spread
    over hours. A moving tag can be republished between two of them, which puts
    two builds in one benchmark and invalidates a compile cache keyed on the
    image. Resolving once, here, is what makes every pod in a workload the same
    bytes.

    Best effort: a tag that cannot be resolved is passed through, because the
    registry being briefly unreachable is a worse reason to fail a step than an
    unpinned pull is to run one.
    """
    if "@" in image:
        return image
    proc = subprocess.run(
        ["gcloud", "artifacts", "docker", "images", "describe", image,
         "--format=value(image_summary.digest)"],
        capture_output=True, text=True, timeout=CLI_TIMEOUT_SECONDS,
    )
    digest = proc.stdout.strip()
    if proc.returncode != 0 or not digest:
        log(f"warning: could not resolve {image} to a digest; using the tag")
        return image
    # Strip the tag off the last path segment only: a colon earlier in the
    # string is a registry port, not a tag separator.
    head, sep, tail = image.rpartition("/")
    pinned = f"{head}{sep}{tail.split(':', 1)[0]}@{digest}"
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

    Exactly one owner: a dependent is collected only once every owner is gone,
    so naming both would be weaker than naming either.
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


def pod_specs(doc):
    """Every PodSpec in the document, whatever the kind."""
    if doc["kind"] == "Job":
        return [doc["spec"]["template"]["spec"]]
    return [
        rj["template"]["spec"]["template"]["spec"]
        for rj in doc["spec"].get("replicatedJobs", [])
    ]


def job_specs(doc):
    """Every JobSpec in the document: one for a Job, one per replicatedJob."""
    if doc["kind"] == "Job":
        return [doc["spec"]]
    return [rj["template"]["spec"] for rj in doc["spec"].get("replicatedJobs", [])]


def pod_metadatas(doc):
    if doc["kind"] == "Job":
        return [doc["spec"]["template"].setdefault("metadata", {})]
    return [
        rj["template"]["spec"]["template"].setdefault("metadata", {})
        for rj in doc["spec"].get("replicatedJobs", [])
    ]


def forward_env(doc, names, registry):
    """Copy named step variables onto the workload container.

    Named explicitly rather than forwarded wholesale. There is no allowlist -
    the launcher's environment holds the agent's per-job credentials, and a step
    that needs its workload to talk back to Buildkite may forward them - but it
    has to say so, one name at a time, so what crosses into a workload pod is
    written down in the step rather than inherited by default.

    Some names are the fleet's rather than any one pipeline's, and the launcher
    supplies those for a step that asked and does not have one, by pointing at
    the Secret each worker's sync writes. A reference rather than a value
    because the pod is on another cluster: a value would be plaintext in the
    Job, in the Workload Kueue admits it through, in the copies MultiKueue makes
    of both on the worker, and in the pod - five objects, readable by anything
    with get on any of them. A secretKeyRef is behind whatever guards the
    Secret, which is the boundary a credential should have.
    """
    fleet = registry.get("env_secrets") or {}
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
            # Copies, so a JobSet's roles do not share one dict between them:
            # anything that later edits a role's env would be editing every
            # role's, and finding that out means reading two functions that
            # look independent.
            env.extend(copy.deepcopy(e) for e in entries)
    return [e["name"] for e in entries]


# What the built-in Job is rendered with, and the only names a manifest cannot
# supply for itself. A repo manifest states its hardware instead, so asking for
# one of these there is a mistake worth naming.
SHAPE_NAMES = ("CHIPS", "TOPOLOGY", "ACCELERATOR_LABEL")


def render(path, image, name, shape):
    """The manifest as a document: names substituted, kind checked, ints fixed."""
    if not os.path.exists(path):
        raise SystemExit(
            f"manifest {path!r} not found in the checkout. Point --manifest at "
            f"a Job or JobSet in the calling repo."
        )

    # Required: supplied on every render, so a ${NAME} still unresolved once
    # these are applied is a typo, and substitute() raises rather than leaving
    # it as literal text.
    required = {"WORKLOAD_NAME": name, "IMAGE": image, **shape}
    # Optional: whatever the step exports, so a manifest can pin
    # ${BUILDKITE_COMMIT} or a size its own pipeline sets.
    optional = {k: v for k, v in os.environ.items() if k not in required}

    # One pass over the two merged, not a pass each. A manifest's containers are
    # mostly shell, and shell is full of dollars that are not ours - $HOSTNAME,
    # $(date), a loop variable. Substituting twice means the escape has to
    # survive twice too: $$ collapses to $ in the first pass and is read as a
    # placeholder in the second, so writing a single literal dollar took $$$$.
    # Merged, the manifest keeps the ordinary convention - $$ is a literal $ -
    # and the errors below are unchanged, since an unknown name still raises.
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
        # After substitution, so the line it points at is the rendered text -
        # which is the one that has to parse, and where a value carrying a
        # colon or a newline shows up.
        raise SystemExit(f"{path}: not valid YAML once substituted: {e}")

    if not isinstance(doc, dict):
        raise SystemExit(f"{path}: not a Kubernetes object, but {type(doc).__name__}")
    if doc.get("kind") not in SUPPORTED_KINDS:
        raise SystemExit(
            f"{path}: kind {doc.get('kind')!r} is not one of {sorted(SUPPORTED_KINDS)}"
        )
    # Checked before anything walks it, so a Job with no template or a JobSet
    # with no replicatedJobs is a sentence rather than a KeyError traceback.
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
    # The queue label must be on the top-level object; Kueue reads it there for
    # both Job and JobSet, never off the inner pods.
    meta.setdefault("labels", {})[QUEUE_LABEL] = profile["queue"]
    meta["labels"].update(labels)
    if owner:
        meta["ownerReferences"] = [owner]

    for pod_meta in pod_metadatas(doc):
        pod_meta.setdefault("labels", {}).update(labels)

    # Required with or without a command of our own: it is also where step
    # environment is forwarded and whose output is streamed back, so a workload
    # without one runs unattributed.
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
    size_fuse_cache(doc, profile)
    return doc


def cap_runtime(doc, profile, registry):
    """Bound how long the workload may hold its chips.

    A default rather than a setting, because most steps have no opinion and the
    number would otherwise have to be right in every copy of every manifest.
    One that does have an opinion states activeDeadlineSeconds and is believed:
    how long a workload runs is a property of the work, not of the hardware,
    and a benchmark that serves for ten hours has no shape-derived number that
    could know that.

    The ceiling is the step's whole budget, which is the part that is always
    true - a workload must not outlast the step watching it, or it is holding a
    reservation nothing will clean up.
    """
    default = int(profile["max_runtime_seconds"])
    ceiling = int(registry["total_max_seconds"])
    for spec in job_specs(doc):
        current = spec.get("activeDeadlineSeconds")
        spec["activeDeadlineSeconds"] = (
            min(int(current), ceiling) if current else default
        )


def size_fuse_cache(doc, profile):
    """How large the gcsfuse file cache may grow on this host.

    Per machine type, since host memory runs from 176 GB to 1440 GB across the
    shapes we run and one figure is either unsafe on the smallest or wasteful on
    the largest. Set only where the manifest left it open, so a pod that needs
    the memory for itself can say so.

    Chip-holding roles only. The figure is a fraction of a TPU host's memory,
    and a role that holds no chips is not on one - it is on a worker-cpu node
    sized to its own requests, where a tmpfs the size of a TPU host's cache is
    an eviction as soon as it fills. Such a role has to state its own sizeLimit
    to mount the caches at all.
    """
    for spec in pod_specs(doc):
        if pod_shape(spec) == (None, None, None):
            continue
        for volume in spec.get("volumes", []):
            if volume.get("name") == FUSE_CACHE_VOLUME and "emptyDir" in volume:
                volume["emptyDir"].setdefault(
                    "sizeLimit", profile["fuse_cache_size"]
                )


# Fields the Kubernetes API declares as integers. A manifest is text with
# ${...} substituted in, so whether a value survives as an int comes down to
# quoting, and the API rejects `activeDeadlineSeconds: "10800"` with a type
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
    # pods ask for, so a label here is either the same thing said twice or a
    # disagreement, and both read as though the manifest chose the queue.
    if QUEUE_LABEL in doc.get("metadata", {}).get("labels", {}):
        raise SystemExit(
            f"{where}: sets {QUEUE_LABEL}. Remove it - the launcher sets the "
            "queue from the shape the pods select."
        )

    # Which identities a workload may run as, published by the cluster and not
    # extensible by a repo: the launcher's own account can create JobSets, so a
    # workload running as it could submit further work outside any quota.
    allowed = set(registry.get("workload_service_accounts") or ["default"])
    for spec in pod_specs(doc):
        sa = spec.get("serviceAccountName")
        if sa and sa not in allowed:
            raise SystemExit(
                f"serviceAccountName {sa!r} is not allowed on a workload pod; "
                f"the cluster permits {', '.join(sorted(allowed))}"
            )
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


def startup_note(env, items):
    """Where a pod is between admission and running, in a few words.

    Admission only means the chips are reserved: the pod still has to be
    scheduled, the node possibly created, and the image pulled. That gap runs
    to tens of minutes, and unreported it is time that can be seen but not
    attributed. Reported, not enforced - a slow node pool is not a failure.

    None once a pod is up and its own output takes over as the better signal.
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

    # Still initialising means a sidecar has not finished, and here that is
    # almost always the gcsfuse mount. The collector reads only the workload
    # container, so without this a bucket the pod cannot authenticate to looks
    # exactly like a slow node for as long as the deadline allows.
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


def describe_admission(workload):
    if workload is None:
        return "waiting for Kueue to create the workload"
    status = workload.get("status", {})
    # Eviction first: a workload keeps status.clusterName once admitted, so
    # testing that first would answer "admitted to X" for the rest of the run
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
    # A timeout is the same answer as a refusal here - no credentials this turn
    # - and the caller already retries on every poll, so it costs one interval
    # rather than the run. Without one it costs the run: this is called from
    # inside the watch loop, with the workload admitted and on the chips.
    try:
        proc = subprocess.run(
            [
                "gcloud", "container", "fleet", "memberships", "get-credentials",
                worker["membership"], "--project", worker["project"],
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

    One read per turn of the loop, shared by everything that wants them: what
    the pods are doing, what they printed, and why they stopped. Fetching per
    reader would cost three Connect Gateway round trips for one answer, and the
    polling ahead of the first log line is deliberately quick.

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

    Grouped by owning Job rather than pod, because a pod does not survive
    preemption and the Job name does - so one continuous stream per unit of
    work rather than a fresh one per attempt.
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
        # The workload container only. --all-containers interleaves containers
        # whose timestamps advance independently, which one cursor per pod
        # cannot represent: a chatty sidecar drags the cursor forward and the
        # workload's own lines are then discarded as already seen. Sidecar
        # output is still in `kubectl logs` for anyone debugging a mount.
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
                    # A continuation: kubectl stamps an entry, but
                    # splitlines() also breaks on the carriage returns inside
                    # one, so a progress bar yields untimestamped fragments.
                    # Taking one as a timestamp would poison the cursor and
                    # silence the pod from its first progress bar onward.
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
            # A section header rather than a 36-character prefix on every
            # line; a JobSet gets a new one each time the output switches pod.
            if pod["group"] != self.last_group:
                print(f"--- {pod['group']}", flush=True)
                self.last_group = pod["group"]
            for text in fresh:
                print(text, flush=True)
            emitted += len(fresh)
        self.emitted += emitted
        return emitted


def main():
    # So a change of base image is visible in the log rather than inferred.
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
    # A manifest describes the whole workload, command included: a JobSet has a
    # command per role, and there is no reading of one command line that says
    # which of them it replaces.
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

    # The built-in Job is one pod, and only the flags can select a shape that
    # is more than one - a manifest saying so has already written the pods.
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
        }

    image = resolve_image(registry)
    name = workload_name()
    labels = correlation_labels()
    doc = render(manifest, image, name, shape)
    # Read back even when the flags chose it, so there is one answer to what
    # shape a workload is: the pods'.
    profile = resolve_shape(doc, registry, manifest)
    validate(doc, registry, manifest)
    forwarded = forward_env(doc, args.env, registry)
    if forwarded:
        log(f"forwarding step env: {', '.join(forwarded)}")
    # shlex.join, not " ".join: the step's own shell has already parsed the
    # command into arguments, and the built-in Job runs the result through a
    # shell again, so joining plainly loses every quote the step wrote. `python
    # -c 'import jax; print(jax.devices())'` arrives as three arguments and
    # would go back out as an unquoted one-liner the second shell breaks on.
    finalise(doc, profile, registry, name, labels, owner_reference(),
             shlex.join(command) if command else None, manifest)
    kind = SUPPORTED_KINDS[doc["kind"]]

    deleted = False
    collector = None

    def cleanup(signum, _frame):
        # agent-stack deletes the pod on Buildkite cancellation, so SIGTERM is
        # how the launcher learns the build is gone. The ownerReference covers
        # the cases that never deliver one.
        nonlocal deleted
        if not deleted:
            deleted = True
            log(f"signal {signum}, deleting {kind}/{name}")
            if collector:
                collector.sweep()
            delete_workload(kind, name)
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

    # Everything from here holds an admitted workload, so an exception on the
    # way out is not just a failed step: the slice keeps running with nobody
    # watching it. The pod ownerReference does collect it, but only once the
    # launcher's own pod object is removed, and a pod that exited non-zero
    # stays until something else cleans it up - minutes to hours during which
    # the chips are unavailable and the queue behind them does not move.
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

    try:
        uid = kubectl_json("get", kind, name)["metadata"]["uid"]
        admission_limit = admission_timeout(registry, doc)
        started = time.monotonic()
        admitted = False
        running = False
        last_startup = None
        last_note = None
        genv = None
        job_id = labels.get("buildkite.com/job-id")

        while True:
            obj = kubectl_json("get", kind, name)
            if obj is None:
                log(f"{kind}/{name} disappeared")
                stop_announcing()
                return 1

            # Watched for the whole run, not just until admission: preemption
            # happens after it, and a step that goes silent for minutes waiting to
            # be re-admitted reads as a hang.
            workload = find_workload(uid)
            note = describe_admission(workload)
            cluster = (workload or {}).get("status", {}).get("clusterName")

            # On any poll where the cluster is known and we have no credentials,
            # not only on the first admission: tying the one attempt to that one
            # moment costs a whole run's logs whenever anything perturbs it, and
            # reports it as missing gateway access rather than as a failed fetch.
            if cluster and genv is None:
                genv = worker_env(cluster, registry)
                if genv:
                    collector = LogCollector(genv, job_id)
            if cluster and not admitted:
                admitted = True
                stop_announcing()
            if not admitted and time.monotonic() - started > admission_limit:
                log(f"not admitted within {admission_limit}s - capacity, not the test")
                stop_announcing()
                delete_workload(kind, name)
                return 1
            if note != last_note:
                log(note)
                last_note = note
                # Only while it is still true. Kueue's reason changes as the
                # wait goes on - reserved, then preempted, then reserved again -
                # and the notice is worth keeping current, but it is withdrawn
                # the moment the workload lands rather than corrected.
                if waiting and not admitted:
                    announce_waiting(waiting, profile["queue"], note)
                    announced = True

            # One read of the workload's pods, for both readers below.
            items = worker_pods(genv, job_id) if genv else None

            # Until the first pod is up, say where it is stuck. After that the pod's
            # own output is the better signal and this goes quiet. A failed read is
            # neither: leave it to the next turn rather than calling it started.
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
                if failed and genv:
                    for why in termination_reasons(worker_pods(genv, job_id) or []):
                        log(f"container terminated: {why}")

                # What the Workload thought, while it still exists - Kueue
                # collects it soon after the run, and once it is gone a preemption
                # and a test returning 1 read the same. termination_reasons()
                # covers the pod that exited; this covers the one taken away.
                if failed:
                    for c in (workload or {}).get("status", {}).get("conditions", []):
                        if c.get("status") != "True":
                            continue
                        detail = " ".join(x for x in (c.get("reason"),
                                                      c.get("message")) if x)
                        log(f"workload {c.get('type')}: {detail}"[:300])

                    # Expand the last section: collapsed output keeps a green
                    # build readable, but on a failure it is what anyone wants.
                    print("^^^ +++", flush=True)
                    log(f"{kind}/{name} failed")
                    return 1
                log(f"{kind}/{name} completed")
                return 0

            if collector and not collector.emitted:
                time.sleep(FIRST_LOG_POLL_SECONDS)
            else:
                time.sleep(POLL_SECONDS)
    except BaseException:
        stop_announcing()
        if not deleted:
            deleted = True
            delete_workload(kind, name)
        raise


if __name__ == "__main__":
    sys.exit(main())
