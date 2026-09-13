#!/usr/bin/env python3
"""Render the Kueue objects for every cluster from prod.auto.tfvars.

Kueue's queues have to agree with the node pools Terraform builds: a quota that
does not match the reservation either strands chips or admits work that can
never be scheduled. Reading the same tfvars Terraform reads keeps the two from
drifting, at the cost of parsing HCL here.

The output goes to kueue/generated/, which is committed. Reviewing a queue
change means reading the diff of the YAML that will be applied, not inferring
it from a template, and deploy_manifests.py refuses to run if the committed
tree does not match a fresh render.

generate() also returns the fleet it just rendered - the versions to install and
the clusters to install them on - which is how deploy_manifests.py knows where
to go, rather than from an index file that would be a third thing to keep in
step.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import shutil
import sys
from pathlib import Path

import hcl2
import yaml

ROOT = Path(__file__).resolve().parent.parent
TFVARS = ROOT / "prod.auto.tfvars"
TEMPLATES = ROOT / "kueue" / "templates"
DEFAULT_OUT = ROOT / "kueue" / "generated"

# The namespace is read from the tfvars rather than set here, because the cache
# bucket IAM in cache.tf names the workload's service account by namespace: the
# two have to agree, and one of them has to be the source. variables.tf carries
# the reasoning for there being only the one namespace.

# What the SecretSync calls the Secret it writes, and what the agent-stack-k8s
# chart is told to read. Here rather than in either template because it is the
# join between them: the controller looks the Secret up by name, so a rename on
# one side and not the other leaves it crash-looping on a Secret that is not
# there. Nothing outside this fleet refers to it, so the value itself is
# arbitrary.
AGENT_TOKEN_SECRET_NAME = "buildkite-agent-token"

# The same join, for the Secret holding the git SSH key: the SecretSync writes
# it and the chart values name it as envFrom on the checkout container.
GIT_CREDENTIALS_SECRET_NAME = "git-ssh-credentials"

# The environment variable the agent looks the key up under, which encodes the
# key's algorithm - see git_credentials.yaml.tpl. Change it with the key.
GIT_SSH_KEY_ENV = "SSH_PRIVATE_ED25519_KEY"


def fleet_secret_name(env_name: str) -> str:
    """What a fleet credential is called once it is a Kubernetes Secret.

    Derived from the variable rather than configured, because this is the join
    between two generated things - the SecretSync on a worker writes it, the
    launcher's registry tells the launcher to point a secretKeyRef at it - and
    a join nobody can misspell is better than one more name to keep in step.
    """
    name = "fleet-" + env_name.lower().replace("_", "-")
    # Checked here rather than left to kubectl, which would reject it partway
    # through a deploy with some clusters already updated. Underscores fold to
    # dashes, so two names can also arrive at one Secret; the caller checks
    # that, since only it can see the whole set.
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name):
        raise SystemExit(
            f"env_secrets key {env_name!r} does not make a Kubernetes name: "
            f"got {name!r}, which must be lowercase alphanumerics and dashes"
        )
    return name


# The ComputeClass the manager's nodes are created from, and the name the
# manager's namespace points at to make it the default for everything in it.
# Here rather than in either template because it is the join between them: a
# namespace whose default names a class that does not exist leaves every pod in
# it pending.
MANAGER_COMPUTE_CLASS = "manager-system"

# The ComputeClass a worker's chip-less workload roles select. Named here rather
# than only in the template because a manifest in the tpu-inference repo names
# it too, in the nodeSelector of any role that holds no chips - so the string is
# fleet-wide API, not an implementation detail of this file.
#
# No namespace default goes with it, unlike the manager's: see the template.
WORKER_COMPUTE_CLASS = "worker-cpu"

# The launcher's program, and the ConfigMap deploy_manifests.py builds out of
# it. Not rendered into the generated tree: a program indented into YAML is not
# a diff anyone reads. Named here because launcher.yaml.tpl mounts it.
LAUNCHER_SCRIPT = ROOT / "kueue" / "launcher" / "launch.py"
LAUNCHER_SCRIPT_CONFIGMAP = "tpu-launcher-scripts"
# The key is the file name under the mount, and the step's command is that
# path: /opt/launcher/launch, not `python /opt/launcher/launch.py`.
LAUNCHER_SCRIPT_KEY = "launch"

# The Job a step gets when it names hardware and nothing else, deployed the
# same way and for the same reason.
LAUNCHER_DEFAULT_JOB = ROOT / "kueue" / "launcher" / "job.yaml"
LAUNCHER_MANIFEST_CONFIGMAP = "tpu-launcher-manifests"
LAUNCHER_DEFAULT_JOB_KEY = "job.yaml"

# The node label GKE puts on a TPU node, by machine family. Not derivable from
# the machine type - a ct6e-standard-8t is `tpu-v6e-slice`, a tpu7x-standard-4t
# is plain `tpu7x` - so these are read off live nodes.
#
# An unlisted family is an error rather than a guess: this is what pins a pod
# to the pool its profile promised, and neither way of getting it wrong is
# reported. A label no node carries waits forever; another family's lands on
# the wrong chips.
ACCELERATOR_LABELS = {
    "ct6e": "tpu-v6e-slice",
    "tpu7x": "tpu7x",
}

# Host memory per TPU machine type, from the accelerator-optimized machine
# family documentation. Only the shapes we run are listed.
MACHINE_MEMORY_GB = {
    "ct6e-standard-1t": 176,
    "ct6e-standard-4t": 720,
    "ct6e-standard-8t": 1440,
    "tpu7x-standard-1t": 240,
    "tpu7x-standard-4t": 960,
}

# How much of the host a workload's gcsfuse file cache may take. Memory, not
# disk: the volume behind it is a `medium: Memory` emptyDir, so a node's
# ephemeral storage does not bound it and too high shows up as an OOM.
#
# Per machine type, since host memory runs from 176 GB to 1440 GB across the
# shapes we run. Only the pod's volume can vary that way; the per-mount
# fileCacheCapacity in cache_volumes.yaml.tpl is one object per cluster and so
# is sized for the smallest shape.
FUSE_VOLUME_RATIO = 0.50


# hcl2 defaults to output you can write back out as HCL, which is not what we
# want to read: a string keeps the quotes it was written with, so
# `project_id = "x"` arrives as the five characters `"x"`. Comments come back
# too, under a __comments__ key beside the variables.
TFVARS_OPTIONS = hcl2.SerializationOptions(
    strip_string_quotes=True, with_comments=False
)


def load_tfvars(path: Path) -> dict:
    with path.open() as f:
        return hcl2.load(f, serialization_options=TFVARS_OPTIONS)


def bucket_name(prefix: str, project: str, location: str, purpose: str) -> str:
    """Where a worker's cache lives, derived rather than configured.

    locals.tf derives it identically and creates the bucket; this writes the
    same string into a PersistentVolume's volumeHandle. Two derivations of one
    name can drift, and the failure is quiet - a volume pointing at a bucket
    that was never created - so deploy_manifests.py checks that every bucket a
    volume names exists before it applies anything. Change one side and the
    other has to move with it.

    The hash covers project and region, which is what identifies a cluster; the
    purpose sits beside it in the clear, so a cluster's two buckets share a
    suffix and read as a pair. The project is in the hash because a bucket name
    is globally unique across the whole of GCP.
    """
    digest = hashlib.sha256(f"{project}/{location}".encode()).hexdigest()[:8]
    return f"{prefix}-{purpose}-{digest}"


def worker_node_service_account(prefix: str, project: str, location: str) -> str:
    """The identity a worker's nodes run as, derived rather than configured.

    iam.tf builds the same string and creates the account; a ComputeClass names
    it so that the nodes GKE auto-creates run as it too, rather than falling
    through to the Compute Engine default account. Two derivations of one name
    can drift, and getting it wrong is not loud: GKE accepts an account that
    does not exist and the node pool fails to register, or - worse, if the name
    happens to resolve - the nodes come up with more authority than intended.
    The suffix is the location because that is a worker's short name; see
    locals.tf.
    """
    return f"{prefix}-wkr-{location}@{project}.iam.gserviceaccount.com"


def render(name: str, **values) -> str:
    text = (TEMPLATES / f"{name}.yaml.tpl").read_text()
    for key, value in values.items():
        placeholder = "${" + key + "}"
        if placeholder not in text:
            raise KeyError(f"{name}.yaml.tpl has no {placeholder}")
        text = text.replace(placeholder, str(value))
    left = [line for line in text.splitlines() if "${" in line]
    if left:
        raise KeyError(f"{name}.yaml.tpl left unsubstituted: {left}")
    return text


def indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line else "" for line in text.rstrip().splitlines())


def cohort(queue: str) -> str:
    """The machine family, which names the ResourceFlavor and the cohort."""
    return queue.split("-")[0]


def fuse_min_cache_gib() -> int:
    """The smallest gke-gcsfuse-cache volume a pod can mount the caches with.

    The sum of the fileCacheCapacity figures rather than the largest of them:
    every gcsfuse mount in a pod shares one gke-gcsfuse-cache volume, so what
    has to fit is all of them at once. gcsfuse fills to those figures whatever
    the volume behind them holds, and overrunning a memory-backed emptyDir's
    sizeLimit is the kubelet evicting the pod mid-run, so no smaller number
    degrades gracefully.

    Read out of the template instead of restated here, which would be a second
    copy of a number that moves.
    """
    text = (TEMPLATES / "cache_volumes.yaml.tpl").read_text()
    # Comment lines dropped first: the comments around these fields quote the
    # figures they explain, and a quoted one would be counted twice.
    body = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    # Matched loosely and checked strictly, so that a figure this cannot read is
    # an error rather than a mount silently left out of the sum. Out of the sum
    # is the direction that hurts: the floor comes back too low, a shape that
    # should have been refused is generated, and the kubelet evicts the pod when
    # the cache fills. gcsfuse's own "-1" is the case worth naming - it is legal,
    # it means fill the volume, and no total bounds it.
    caps = re.findall(r"fileCacheCapacity:\s*(\S+)", body)
    if not caps:
        raise ValueError("cache_volumes.yaml.tpl declares no fileCacheCapacity")
    total = 0
    for cap in caps:
        match = re.fullmatch(r'"(\d+)Gi"', cap)
        if not match:
            raise ValueError(
                f"cache_volumes.yaml.tpl states fileCacheCapacity: {cap}, which "
                "is not a quoted whole number of Gi. The gcsfuse cache floor is "
                "the sum of these, and a figure this cannot add is a floor too "
                "low rather than a missing one."
            )
        total += int(match.group(1))
    return total


def fuse_cache_size(machine_type: str) -> str:
    """The workload's gcsfuse file cache on this machine type, as a GiB string.

    GB to GiB as well as the ratio: the machine family documentation quotes
    memory in decimal gigabytes and a Kubernetes quantity written Gi is binary,
    so taking the number across unconverted would ask for 7% more of the host
    than intended.

    An unlisted machine type is an error rather than a conservative guess, for
    the reason on fuse_min_cache_gib: a number too small is not slower, it is a
    pod the kubelet evicts once the cache fills.
    """
    gb = MACHINE_MEMORY_GB.get(machine_type)
    if gb is None:
        raise KeyError(
            f"no host memory known for machine type {machine_type!r}. Read it "
            "off the accelerator-optimized machine family documentation and "
            "add it to MACHINE_MEMORY_GB; the gcsfuse file cache is sized from "
            "it, and a wrong number is an eviction rather than a slow mount."
        )
    gib = int(gb * FUSE_VOLUME_RATIO * 1000**3 / 1024**3)
    floor = fuse_min_cache_gib()
    if gib < floor:
        raise ValueError(
            f"{machine_type} has {gb} GB of host memory, so {FUSE_VOLUME_RATIO:.0%} "
            f"of it is {gib}Gi - under the {floor}Gi of fileCacheCapacity that "
            "cache_volumes.yaml.tpl asks for across the mounts sharing one "
            "gke-gcsfuse-cache volume. gcsfuse would fill past the emptyDir's "
            "sizeLimit and the kubelet would evict the pod."
        )
    return f"{gib}Gi"


def shapes(worker: dict) -> dict[str, dict]:
    """Every TPU shape a worker cluster can run, keyed by queue name.

    A queue is named for its node pool - <machine type>-<topology>, e.g.
    ct6e-standard-8t-2x4, joined the same way locals.tf joins it - rather than
    for the cluster's copy of that pool, because two regions running the same
    shape are one queue on the manager and MultiKueue picks the region.

    Everything here but the quota is a fact about the hardware, and it is the
    same dict the launcher's profile registry is built from. So the queue that
    admits a workload and the pod that lands on a node cannot disagree about
    what the shape is: they are the same three lines of tfvars, read once.
    """
    out: dict[str, dict] = {}
    for pool in worker.get("tpu_node_pools", []):
        machine_type = pool["machine_type"]
        topology = pool["topology"]
        name = f"{machine_type}-{topology}"

        # The machine type's last field is chips per VM - unlike the Buildkite
        # queue names, where tpu7x-8 counts TensorCores and means a four-chip
        # tpu7x-standard-4t.
        chips = int(machine_type.rsplit("-", 1)[-1].removesuffix("t"))
        slice_chips = math.prod(int(d) for d in topology.split("x"))
        hosts, remainder = divmod(slice_chips, chips)
        if remainder or not hosts:
            raise ValueError(
                f"{name}: a {topology} slice is {slice_chips} chips, which is "
                f"not a whole number of {machine_type} hosts at {chips} chips "
                "each"
            )

        family = machine_type.split("-")[0]
        if family not in ACCELERATOR_LABELS:
            raise KeyError(
                f"{name}: no accelerator node label known for machine family "
                f"{family!r}. Read cloud.google.com/gke-tpu-accelerator off a "
                "node of that family and add it to ACCELERATOR_LABELS; it "
                "cannot be derived from the machine type."
            )

        # A multi-host slice is admitted and built whole, so quota that is not
        # a multiple of hosts is quota this shape can never use, and a floor or
        # ceiling that is not one is a node pool GKE cannot build. Fail here
        # rather than as a workload that queues forever.
        for field in ("min_nodes", "nominal_nodes", "max_nodes"):
            if hosts > 1 and int(pool[field]) % hosts:
                raise ValueError(
                    f"{name}: {field}={pool[field]} is not a multiple of the "
                    f"{hosts} hosts in a {topology} slice; every count for a "
                    "multi-host shape has to be whole slices"
                )

        out[name] = {
            "queue": name,
            # How a step names this shape when it asks for the built-in Job.
            # Stated rather than left implicit in the queue name, so the lookup
            # reads the registry instead of re-splitting a string.
            "machine_type": machine_type,
            "chips": chips,
            "hosts": hosts,
            "topology": topology,
            "accelerator_label": ACCELERATOR_LABELS[family],
            "fuse_cache_size": fuse_cache_size(machine_type),
            # The one number here that is a policy rather than a fact, and the
            # only one Kueue reads: this shape's share of the reservation.
            "quota": int(pool["nominal_nodes"]) * chips,
        }
    return out


def queues(shapes: dict[str, int], namespace: str, checks: bool) -> str:
    """A flavor per machine family, then a queue per shape sharing it.

    checks is what separates the manager from a worker: on the manager every
    queue carries a MultiKueue AdmissionCheck, so passing quota there means the
    workload is dispatched rather than run.
    """
    out = [
        render("resource_flavor", ACCELERATOR=family)
        for family in sorted({cohort(name) for name in shapes})
    ]
    for name, chips in sorted(shapes.items()):
        out.append(
            render(
                "queue_group",
                QUEUE_NAME=name,
                ACCELERATOR=cohort(name),
                NAMESPACE=namespace,
                NOMINAL_QUOTA=chips,
                ADMISSION_CHECKS=(
                    "\n  admissionChecksStrategy:\n"
                    "    admissionChecks:\n"
                    f"      - name: {name}-multikueue-dispatch"
                    if checks
                    else ""
                ),
            )
        )
    return "".join(out)


def fleet_secret_docs(tfvars: dict, namespace: str) -> str:
    """A SecretProviderClass and a SecretSync per fleet credential.

    `path` is internal - it names the value for the SecretSync below, and is
    never a file anywhere, since nothing mounts these as a CSI volume.

    `versions/latest` so a rotation is picked up without a deploy. A container
    resolves a secretKeyRef once, when it is created, so a running workload
    keeps the version it started with - but a workload is one job long, and the
    next job gets whatever the sync last wrote.
    """
    docs = []
    seen = {}
    for env_name, spec in sorted(tfvars["env_secrets"].items()):
        name = fleet_secret_name(env_name)
        if name in seen:
            raise SystemExit(
                f"env_secrets keys {seen[name]!r} and {env_name!r} both name "
                f"the Secret {name!r}; one of them would silently win"
            )
        seen[name] = env_name
        docs.append(
            f"""---
apiVersion: secrets-store.csi.x-k8s.io/v1
kind: SecretProviderClass
metadata:
  name: {name}
  namespace: {namespace}
spec:
  provider: gke
  parameters:
    secrets: |
      - resourceName: "projects/{spec['project']}/secrets/{spec['secret']}/versions/latest"
        path: "value"
---
apiVersion: secret-sync.gke.io/v1
kind: SecretSync
metadata:
  name: {name}
  namespace: {namespace}
spec:
  serviceAccountName: secret-sync
  secretProviderClassName: {name}
  secretObject:
    type: Opaque
    data:
      - sourcePath: value
        targetKey: {env_name}"""
        )
    return "\n".join(docs)


def launcher_profiles(fleet: dict, workers: list[str], tfvars: dict) -> str:
    """The registry the launcher resolves a workload's hardware against.

    One profile per shape, named for the shape, which is also the name of the
    Kueue queue that admits it - so a workload asks for hardware that exists in
    the fleet, or is refused before it is submitted. The manager's fleet-wide
    queues are what a profile points at; which region actually runs it is
    MultiKueue's to decide.

    Everything a workload manifest is not trusted to state itself is here,
    because it is here that it can be checked: the registries an image may come
    from, the identities a pod may run as, and the budget the launcher derives
    its admission deadline from.
    """
    return yaml.safe_dump(
        {
            # A step sets WORKLOAD_IMAGE and tpu-inference builds fork pull
            # requests, so it is attacker-controlled. Empty means unrestricted.
            "allowed_image_repos": tfvars["allowed_image_repos"],
            # Identities a workload may run as: tpu-workload is the one the
            # cache buckets authorise, default is for a manifest needing no
            # cloud access. Neither can create JobSets, unlike the launcher's
            # own account, which is the point.
            "workload_service_accounts": ["default", "tpu-workload"],
            # Names the launcher can supply itself when a step forwards one it
            # does not have. Fleet-wide credentials, and still only reaching a
            # workload that asked by name.
            #
            # Where the sync put it on the worker, not where it came from in
            # Secret Manager: the launcher points a secretKeyRef at it and
            # never reads the value. Same tfvars list the syncs are generated
            # from, so a name here is a Secret that exists.
            "env_secrets": {
                name: {"secret": fleet_secret_name(name), "key": name}
                for name in sorted(tfvars["env_secrets"])
            },
            "total_max_seconds": int(tfvars["tpu_total_max_seconds"]),
            # How the launcher gets from an admitted workload to the pod logs.
            # Kueue reports the cluster it dispatched to by MultiKueueCluster
            # name, which is also the Fleet membership ID; memberships live in
            # the manager's project whatever project the worker runs in.
            "workers": {
                name: {"membership": name, "project": tfvars["project_id"]}
                for name in sorted(workers)
            },
            "profiles": {
                name: {
                    **{
                        key: value
                        for key, value in entry["shape"].items()
                        # Kueue's, not the launcher's: what a shape's queue may
                        # hold says nothing about where one workload runs.
                        if key != "quota"
                    },
                    "max_runtime_seconds": int(tfvars["tpu_test_max_seconds"]),
                }
                for name, entry in sorted(fleet.items())
            },
        },
        sort_keys=False,
    )


# On every generated file, because the tree is committed and so reads like
# something you could edit in place. The last sentence is the operative one: an
# edit here is not just overwritten on the next render, it stops the deploy.
HEADER = """\
# Generated by scripts/generate_manifests.py from prod.auto.tfvars. Do not edit.
#
# Change the tfvars or kueue/templates/ and re-run the generator. An edit made
# here is never applied: deploy_manifests.py re-renders and refuses to deploy a
# tree that does not match.
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + text.rstrip() + "\n")


def generate(tfvars: dict, out_dir: Path) -> dict:
    project = tfvars["project_id"]
    prefix = tfvars["name_prefix"]
    namespace = tfvars["namespace"]

    # Sorted by the pair that identifies a cluster, so the render is stable
    # whatever order the tfvars lists them in - the tree is committed, and a
    # reordering that changed no configuration would still show up as a diff.
    workers = sorted(
        tfvars.get("worker_clusters", []),
        key=lambda w: (w["project"], w["location"]),
    )

    manager_name = f"{prefix}-manager"
    manager_dir = "manager"

    # Per shape, the workers that can run it and the chips they add up to. The
    # manager's quota is the fleet's, because it admits on behalf of all of
    # them; each worker then admits again against its own.
    fleet: dict[str, dict] = {}
    clusters = [
        {
            "dir": manager_dir,
            "role": "manager",
            "project": project,
            "location": tfvars["manager_region"],
            "name": manager_name,
        }
    ]

    for worker in workers:
        # Nested rather than one flattened name, so the tree groups by project
        # the way the fleet does and a region is a leaf under it. Both parts are
        # needed: two projects may each run a us-east5.
        worker_dir = f"workers/{worker['project']}/{worker['location']}"
        cluster_name = f"{prefix}-{worker['location']}"
        clusters.append(
            {
                "dir": worker_dir,
                "role": "worker",
                "project": worker["project"],
                "location": worker["location"],
                "name": cluster_name,
            }
        )

        local = shapes(worker)
        for name, shape in local.items():
            # The shape is stored once, not summed: two clusters running it
            # run the same hardware. Only the quota adds up.
            entry = fleet.setdefault(name, {"quota": 0, "workers": [], "shape": shape})
            entry["quota"] += shape["quota"]
            entry["workers"].append(cluster_name)

        base = out_dir / worker_dir
        write(base / "system" / "10-kueue-config.yaml", kueue_config("worker"))
        # Under system/, alongside the rest of what a worker has to hold before
        # it can run anything. Nothing in this repo names the class; the roles
        # that do arrive later, as workloads MultiKueue dispatches here, and a
        # pod naming a class the cluster does not have stays pending.
        write(
            base / "system" / "00-compute-class.yaml",
            render(
                "compute_class_worker",
                NAME=WORKER_COMPUTE_CLASS,
                NODE_SERVICE_ACCOUNT=worker_node_service_account(
                    prefix, worker["project"], worker["location"]
                ),
            ),
        )
        write(
            base / "queues" / "00-namespace.yaml",
            render("namespace", NAMESPACE=namespace, EXTRA_LABELS=""),
        )
        write(
            base / "queues" / "10-queues.yaml",
            queues(
                {name: shape["quota"] for name, shape in local.items()},
                namespace,
                checks=False,
            ),
        )
        write(
            base / "queues" / "20-multikueue-rbac.yaml",
            render("multikueue_rbac_worker", PROJECT_ID=project),
        )

        # Workers only: the TPU pods are the only thing that mounts a cache, and
        # a PersistentVolume naming the gcsfuse driver is not satisfiable on the
        # manager, which does not have it enabled.
        write(
            base / "workload" / "00-service-account.yaml",
            render("workload_sa", NAMESPACE=namespace, PROJECT_ID=worker["project"]),
        )
        write(
            base / "workload" / "05-fleet-secrets.yaml",
            render(
                "fleet_secrets",
                NAMESPACE=namespace,
                SECRET_DOCS=fleet_secret_docs(tfvars, namespace),
            ),
        )
        write(
            base / "workload" / "10-cache-volumes.yaml",
            render(
                "cache_volumes",
                NAMESPACE=namespace,
                CACHE_BUCKET=bucket_name(
                    prefix, worker["project"], worker["location"], "cache"
                ),
                MODELS_BUCKET=bucket_name(
                    prefix, worker["project"], worker["location"], "models"
                ),
            ),
        )
        # The launcher runs on the manager; all it wants here is to read the
        # logs of the pods MultiKueue put in this cluster. PROJECT_ID is the
        # manager's, because that is the Workload Identity pool the launcher's
        # service account is federated through.
        write(
            base / "workload" / "20-launcher-rbac.yaml",
            render("launcher_rbac_worker", NAMESPACE=namespace, PROJECT_ID=project),
        )

    base = out_dir / manager_dir
    write(base / "system" / "10-kueue-config.yaml", kueue_config("manager"))
    # Manager only. A worker's TPU pods do call buildkite-agent, but with the
    # per-job access token the launcher forwards, not with this one - see the
    # template.
    write(
        base / "workload" / "00-agent-token.yaml",
        render(
            "secret_sync",
            NAMESPACE=namespace,
            PROJECT_ID=project,
            SECRET_NAME=AGENT_TOKEN_SECRET_NAME,
            AGENT_TOKEN_SECRET_ID=tfvars["agent_token_secret_id"],
        ),
    )
    # Manager only, and beside the agent token rather than after it: both are
    # read by the same service account and both have to exist before the
    # controller that mounts them starts.
    write(
        base / "workload" / "01-git-credentials.yaml",
        render(
            "git_credentials",
            NAMESPACE=namespace,
            PROJECT_ID=project,
            SECRET_NAME=GIT_CREDENTIALS_SECRET_NAME,
            GIT_SSH_KEY_SECRET_ID=tfvars["git_ssh_key_secret_id"],
            GIT_SSH_KEY_ENV=GIT_SSH_KEY_ENV,
        ),
    )
    write(
        base / "system" / "20-auth-plugin.yaml",
        render(
            "auth_plugin_overlay",
            AUTH_PLUGIN_IMAGE=tfvars["auth_plugin_image"],
            AUTH_PLUGIN_SRC=tfvars["auth_plugin_source_path"],
        ),
    )
    # Manager only, and after the agent token: the launcher pods are created by
    # the controller that reads it, and every TPU step in the fleet goes through
    # them. A worker gets only the log-reading grant above.
    write(
        base / "workload" / "10-launcher.yaml",
        render(
            "launcher",
            NAMESPACE=namespace,
            LAUNCHER_IMAGE=tfvars["launcher_image"],
            LAUNCHER_PROFILES=indent(
                launcher_profiles(
                    fleet,
                    [c["name"] for c in clusters if c["role"] == "worker"],
                    tfvars,
                ),
                4,
            ),
        ),
    )
    # Under system/, which deploy_manifests.py applies before queues/: the
    # namespace below names this class as its default, and a default that is not
    # there yet is a namespace nothing can schedule into.
    write(
        base / "system" / "00-compute-class.yaml",
        render("compute_class", NAME=MANAGER_COMPUTE_CLASS),
    )
    # Everything the fleet runs on the manager lives in this namespace, so
    # setting the class here covers the launcher pods and the agent pods without
    # either of their templates having to name a node. The controllers in
    # kueue-system and jobset-system are upstream's and stay on plain
    # auto-provisioning, which falls back across families of its own accord.
    write(
        base / "queues" / "00-namespace.yaml",
        render(
            "namespace",
            NAMESPACE=namespace,
            EXTRA_LABELS=f"\n    cloud.google.com/default-compute-class: {MANAGER_COMPUTE_CLASS}",
        ),
    )
    write(
        base / "queues" / "10-queues.yaml",
        queues(
            {name: entry["quota"] for name, entry in fleet.items()},
            namespace,
            checks=True,
        ),
    )
    write(
        base / "queues" / "20-multikueue.yaml",
        "".join(
            render("multikueue_cluster", WORKER_NAME=name, CLUSTER_PROFILE_NAME=profile)
            for name, profile in sorted(
                {
                    c["name"]: f"{c['name']}-{c['location']}"
                    for c in clusters
                    if c["role"] == "worker"
                }.items()
            )
        )
        + "".join(
            render("admission_check", QUEUE_NAME=queue)
            + render(
                "multikueue_config",
                QUEUE_NAME=queue,
                WORKER_LIST="\n".join(
                    f"    - {name}" for name in sorted(entry["workers"])
                ),
            )
            for queue, entry in sorted(fleet.items())
        ),
    )

    # Under charts/ rather than beside the manifests, because it is not one.
    # deploy_manifests.py applies system/, queues/ and workload/; this is an
    # input to a helm render, and applying it would be an error.
    write(
        base / "charts" / "agent-stack-k8s.yaml",
        render(
            "agent_stack_values",
            AGENT_TOKEN_SECRET_NAME=AGENT_TOKEN_SECRET_NAME,
            GIT_CREDENTIALS_SECRET_NAME=GIT_CREDENTIALS_SECRET_NAME,
            BUILDKITE_QUEUE=tfvars["buildkite_queue"],
        ),
    )

    return {
        "kueue_version": tfvars["kueue_version"],
        "jobset_version": tfvars["jobset_version"],
        "agent_stack_version": tfvars["agent_stack_version"],
        # For the chart render, which is the one thing deployed by flag rather
        # than by manifest and so cannot read its namespace off the object.
        "namespace": namespace,
        "clusters": clusters,
    }


def top_level_keys(text: str) -> set[str]:
    """The keys a YAML document sets at column zero."""
    return {
        line.split(":", 1)[0]
        for line in text.splitlines()
        if ":" in line and line[:1].isalpha()
    }


def kueue_config(role: str) -> str:
    """The controller ConfigMap: the shared configuration, plus the manager's.

    The two sides admit the same workload, so most of the configuration has to
    be the same on both and lives in one file rather than in two that a comment
    asks you to keep in step. The manager's extra keys are appended, which is
    only sound while the two files set disjoint top-level keys, so that is
    checked rather than trusted.
    """
    text = (ROOT / "kueue" / "common-config.yaml").read_text()
    if role == "manager":
        extra = (ROOT / "kueue" / "manager-config.yaml").read_text()
        clash = top_level_keys(text) & top_level_keys(extra)
        if clash:
            raise KeyError(
                f"manager-config.yaml also sets {sorted(clash)}; appending it to "
                f"common-config.yaml would leave the key twice in one document"
            )
        text = f"{text.rstrip()}\n{extra}"
    return render("kueue_config", CONFIG=indent(text, 4))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    tfvars = load_tfvars(TFVARS)

    # Rendered into a fresh tree so a cluster or a generation that goes away
    # leaves no file behind for deploy_manifests.py to keep applying.
    if args.out_dir.exists():
        shutil.rmtree(args.out_dir)
    generate(tfvars, args.out_dir)

    print(f"wrote {args.out_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
