#!/usr/bin/env python3
import argparse
import hashlib
import os
import sys
import shutil
from pathlib import Path
from string import Template
import hcl2
import yaml


def clean_val(val):
    """Clean hcl2 string quotes."""
    if isinstance(val, str):
        return val.strip('"\'')
    return val

def parse_tfvars(file_path):
    """Parse HCL tfvars file using official hcl2 library."""
    with open(file_path, "r") as f:
        data = hcl2.load(f)

    project_id = clean_val(data["project_id"])
    name_prefix = clean_val(data["name_prefix"])
    manager_region = clean_val(data["manager_region"])
    secret_project = clean_val(data.get("buildkite_secret_project", project_id))
    secret_id = clean_val(data.get("buildkite_secret_id", "buildkite-tpu-ci-agent-dev-token"))
    
    worker_clusters = {}
    # hcl2 keeps the surrounding quotes on object keys that are written quoted
    # in tfvars, and these keys become Kubernetes object names. Without this a
    # quoted key yields a ClusterQueue literally named "v6e-1-1x1", which fails
    # far from its cause.
    for raw_key, wdata in data["worker_clusters"].items():
        key = clean_val(raw_key)
        pools = {}
        for raw_pk, pdata in wdata["tpu_pools"].items():
            pk = clean_val(raw_pk)
            chips = int(pdata["chips_per_node"])
            nominal_nodes = int(pdata.get("nominal_nodes", pdata["max_nodes"]))
            max_nodes = int(pdata["max_nodes"])
            accelerator = clean_val(pdata["accelerator"])
            hosts = int(pdata.get("hosts", 1))
            # A multi-host slice is admitted whole and built whole: a JobSet
            # of `hosts` pods, on a node pool that scales by whole slices. A
            # nominal that is not a multiple of hosts would leave quota this
            # lane can never use, and a max that is not one is a node pool GKE
            # cannot build. Fail here rather than as a workload that queues
            # forever.
            for field, value in (("nominal_nodes", nominal_nodes), ("max_nodes", max_nodes)):
                if hosts > 1 and value % hosts:
                    raise SystemExit(
                        f"{key}/{pk}: {field}={value} is not a multiple of hosts={hosts}; "
                        "a multi-host lane's quota must be whole slices"
                    )

            pools[pk] = {
                "chips": chips,
                "nominal_nodes": nominal_nodes,
                "max_nodes": max_nodes,
                "quota": chips * nominal_nodes,
                "accelerator": accelerator,
                # Consumed by the launcher: what a pipeline naming this profile
                # actually gets placed on.
                "topology": clean_val(pdata["topology"]),
                "accelerator_label": clean_val(pdata["accelerator_label"]),
                # Hosts in the slice. Single-host today; a multi-host topology
                # sets this so the JobSet parallelism matches the node count.
                "hosts": hosts,
                # Only used to size the file cache volume, which scales with
                # host memory rather than with chips.
                "machine_type": clean_val(pdata["machine_type"]),
            }
            
        project = clean_val(wdata["project"])
        worker_clusters[key] = {
            "project": project,
            "location": clean_val(wdata["location"]),
            "cluster_name": f"{name_prefix}-{key}",
            "profile_name": f"{name_prefix}-{key}-global",
            # Must match cache.tf exactly - same inputs, same hash, same name.
            # If these two ever disagree the PersistentVolume points at a bucket
            # that does not exist, and every pod fails to mount.
            "cache_bucket": bucket_name(name_prefix, project, key, "cache"),
            "models_bucket": bucket_name(name_prefix, project, key, "models"),
            "pools": pools
        }
        
    return {
        "project_id": project_id,
        "name_prefix": name_prefix,
        "manager_region": manager_region,
        "manager_cluster": f"{name_prefix}-manager",
        "secret_project": secret_project,
        "secret_id": secret_id,
        "namespace": "buildkite",
        "launcher_image": clean_val(data.get(
            "launcher_image", "gcr.io/google.com/cloudsdktool/google-cloud-cli:debian_component_based")),
        # Mirrors var.tpu_test_max_seconds; both the launcher Job and the
        # submitted workload are bounded by it so the deadlines cannot drift.
        "max_runtime_seconds": int(clean_val(data.get("tpu_test_max_seconds", 10800))),
        # The launcher derives how long to wait for admission from these two:
        # whatever is left of the total once a full-length run is allowed for.
        "total_max_seconds": int(clean_val(data.get("tpu_total_max_seconds", 28800))),
        # Registry prefixes a workload image may come from. WORKLOAD_IMAGE is
        # set by the pipeline, which in a public repo means a PR can name any
        # image; this is the boundary. Empty means unrestricted - populate it
        # before opening the queue to fork PRs.
        "allowed_image_repos": [
            clean_val(r) for r in data.get("allowed_image_repos", [])
        ],
        "worker_clusters": worker_clusters
    }

# Host memory per TPU machine type, from the accelerator-optimized machine
# family documentation. Only the shapes we actually run are listed; an unknown
# type falls back to the smallest, which is safe because the value only ever
# caps a cache.
MACHINE_MEMORY_GB = {
    "ct6e-standard-1t": 176,
    "ct6e-standard-4t": 720,
    "ct6e-standard-8t": 1440,
    "tpu7x-standard-4t": 960,
}


def _gib(gb, ratio):
    """`ratio` of `gb` gigabytes, as a whole-GiB string.

    Not rounded to anything coarser: at the compilation cache's 0.05 a 5GiB
    step would take ct6e-standard-1t's 8Gi down to 5Gi, below the figure that
    was measured sufficient.
    """
    return f"{int(gb * 1000 ** 3 / 1024 ** 3 * ratio)}Gi"


# How the gcsfuse file cache is divided. Fractions of host memory.
#
# The volume is half. 0.6 was tried once, on the grounds that nothing had
# looked above 56Gi of models; build 242 then loaded checkpoints in 9.5m at
# 73Gi against 8.8m at 56Gi, so the working set already fits and the extra
# capacity was headroom rather than speed. Half leaves ~57 GiB of a 176 GB
# node to the tests, against ~40 at 0.6.
#
# GKE's own gcsfuse profiles allow 0.7, but they are not also running the
# workload that reads the cache.
#
# The two mounts sum to 0.45, deliberately under the volume. If they summed to
# it, the tmpfs would hit its sizeLimit before either mount reached its own
# eviction threshold, and gcsfuse would fail writes instead of evicting - which
# is what build 241 appears to have done at fileCacheCapacity: -1, taking 26.6m
# over checkpoint loading against build 234's 8.8m with explicit capacities.
#
# Models take eight times the compilation cache because the misses are not
# comparable: a model miss is a multi-gigabyte download, a compilation miss is
# a ~36ms same-region round trip. 8Gi of compilation cache measurably sufficed
# on ct6e-standard-1t, and the whole namespace is ~34GB of which a run reads a
# fraction, so the capacity is worth more to the models.
FUSE_VOLUME_RATIO = 0.50
# The per-mount shares are applied by hand in cache_volumes.yaml.tpl, not from
# here: a PersistentVolume is one object per cluster and every profile binds the
# same claim, so its fileCacheCapacity has to hold on the smallest shape and
# cannot vary by machine type. Only the pod's cache volume, which the launcher
# renders per profile, can. They are recorded here because the figures in that
# template are derived from them.
FUSE_MODELS_RATIO = 0.40
FUSE_CACHE_RATIO = 0.05
# Never more than this to the compilation cache however large the machine. The
# whole namespace is ~34GB and a run reads a fraction of it, so past this the
# capacity does nothing for compilation and is denied to the models, whose
# misses cost a multi-gigabyte download rather than a ~36ms round trip.
FUSE_CACHE_MAX_GIB = 30


def fuse_sizes(machine_type):
    """Cache volume and per-mount capacities for this machine type.

    Sized from the machine rather than fixed because host memory ranges from
    176 GB on ct6e-standard-1t to 1440 GB on ct6e-standard-8t, and one figure
    is either unsafe on the smallest or leaves most of the largest idle.

    An unlisted type gets figures small enough to be safe anywhere: too low
    only costs read speed, too high risks an OOM.
    """
    gb = MACHINE_MEMORY_GB.get(machine_type)
    if gb is None:
        return {"fuse_cache_size": "20Gi"}
    return {"fuse_cache_size": _gib(gb, FUSE_VOLUME_RATIO)}


def bucket_name(name_prefix, project, cluster_key, purpose):
    """A regional bucket for one worker cluster.

    Mirrors locals.cache_bucket in cache.tf. Bucket names are globally unique
    and cannot be renamed, so the name carries a hash of project, cluster and
    purpose - inputs that never change for a given bucket. Terraform creates it;
    this only has to arrive at the same string.
    """
    region = "-".join(cluster_key.split("-")[:2])
    digest = hashlib.sha256(f"{project}/{cluster_key}/{purpose}".encode()).hexdigest()[:6]
    return f"{name_prefix}-{purpose}-{region}-{digest}"


def clean_doc(doc_str):
    """Strip leading/trailing document separators and whitespace."""
    s = doc_str.strip()
    while s.startswith("---"):
        s = s[3:].strip()
    while s.endswith("---"):
        s = s[:-3].strip()
    return s

def _indent(text, spaces):
    pad = " " * spaces
    return "\n".join((pad + line) if line.strip() else "" for line in text.splitlines())

def render_launcher(template, k8s_dir, config):
    """Launcher ServiceAccount/RBAC/ConfigMaps/PodTemplate.

    The script is kept as a real file under kueue/launcher/ so it can be
    linted and tested, and is indented into a ConfigMap here. It must not go
    through Template.substitute, being full of shell and Python $VAR.

    Workload manifests are deliberately not here: they live in the repo under
    test, so the shape of a job sits with the people writing tests. Only the
    profile registry is cluster-side, because chips, topology and queue names
    are facts about the hardware rather than about any one test.
    """
    launcher_dir = k8s_dir / "kueue" / "launcher"
    script = (launcher_dir / "launch.py").read_text()

    # profiles.yaml: what a pipeline may ask for, and what it resolves to.
    profiles = {}
    for wconf in config["worker_clusters"].values():
        for name, pool in wconf["pools"].items():
            profiles[name] = {
                "queue": name,
                "chips": pool["chips"],
                "hosts": pool.get("hosts", 1),
                "topology": pool["topology"],
                "accelerator_label": pool["accelerator_label"],
                "max_runtime_seconds": config["max_runtime_seconds"],
                **fuse_sizes(pool["machine_type"]),
            }
    # Wrapped rather than a bare profile map: the launcher also needs the
    # image allowlist, and a step's WORKLOAD_IMAGE is repo-controlled.
    # Kueue reports the admitted cluster as Workload.status.clusterName, which
    # is the MultiKueueCluster name and also the Fleet membership id. The
    # memberships live in the manager (fleet host) project, not the worker's.
    workers = {
        wconf["cluster_name"]: {
            "membership": wconf["cluster_name"],
            "project": config["project_id"],
        }
        for wconf in config["worker_clusters"].values()
    }
    profiles_yaml = yaml.safe_dump(
        {
            "allowed_image_repos": config["allowed_image_repos"],
            # Identities a workload may run as. tpu-workload is what the cache
            # buckets authorise; default remains for manifests that need no
            # cloud access. Neither can create JobSets, which is the point -
            # the launcher's own account can, and a workload running as it
            # could submit work outside any quota.
            "workload_service_accounts": ["default", "tpu-workload"],
            "total_max_seconds": config["total_max_seconds"],
            "workers": dict(sorted(workers.items())),
            "profiles": dict(sorted(profiles.items())),
        },
        sort_keys=False,
    )

    return Template(template).substitute(
        NAMESPACE=config["namespace"],
        LAUNCHER_IMAGE=config["launcher_image"],
        LAUNCHER_SCRIPT=_indent(script, 4),
        LAUNCHER_PROFILES=_indent(profiles_yaml, 4),
    )

def format_admission_checks(accelerator):
    """MultiKueue dispatch block appended to a ClusterQueue spec.

    Only the manager dispatches; worker ClusterQueues admit locally and render
    this empty. This is the sole difference between the manager and worker
    forms of queue_group.yaml.tpl.
    """
    if not accelerator:
        return ""
    return (
        "\n  admissionChecksStrategy:"
        "\n    admissionChecks:"
        f"\n      - name: {accelerator}-multikueue-dispatch"
    )

def render_queue_group(template, pools, namespace, with_admission_checks):
    """One ClusterQueue + LocalQueue per pool, sorted for stable output.

    pools maps queue name -> {"quota", "accelerator"}.
    """
    tmpl = Template(template)
    return [
        clean_doc(tmpl.substitute(
            QUEUE_NAME=name,
            ACCELERATOR=info["accelerator"],
            NAMESPACE=namespace,
            NOMINAL_QUOTA=info["quota"],
            ADMISSION_CHECKS=format_admission_checks(
                info["accelerator"] if with_admission_checks else None
            ),
        ))
        for name, info in sorted(pools.items())
    ]

def generate_manager_parts(config, templates, k8s_dir):
    parts = {}
    
    # 1. Base (Namespace, ClusterSecretStore, ExternalSecret)
    base_tmpl = Template(templates["base"])
    parts["01-base.yaml"] = base_tmpl.substitute(
        NAMESPACE=config["namespace"],
        SECRET_PROJECT=config["secret_project"],
        SECRET_ID=config["secret_id"],
        CLUSTER_LOCATION=config["manager_region"],
        CLUSTER_NAME=config["manager_cluster"]
    ).strip() + "\n"
    
    # 2. MultiKueueCluster per worker
    mk_cluster_tmpl = Template(templates["multikueue_cluster"])
    fleet_docs = []
    accel_workers = {}
    
    for wk, wconf in config["worker_clusters"].items():
        wname = wconf["cluster_name"]
        pname = wconf["profile_name"]
        fleet_docs.append(clean_doc(mk_cluster_tmpl.substitute(
            WORKER_NAME=wname,
            CLUSTER_PROFILE_NAME=pname
        )))
        
        for pk, pconf in wconf["pools"].items():
            accel = pconf["accelerator"]
            if accel not in accel_workers:
                accel_workers[accel] = set()
            accel_workers[accel].add(wname)
    parts["02-multikueue-fleet.yaml"] = "\n---\n".join(fleet_docs) + "\n"

    # 3. MultiKueueConfig & AdmissionCheck per accelerator cohort
    mk_config_tmpl = Template(templates["multikueue_config"])
    adm_check_tmpl = Template(templates["admission_check"])
    cohort_docs = []
    
    for accel, workers in sorted(accel_workers.items()):
        w_list_str = "\n".join(f"    - {w}" for w in sorted(workers))
        cohort_docs.append(clean_doc(mk_config_tmpl.substitute(
            ACCELERATOR=accel,
            WORKER_LIST=w_list_str
        )))
        cohort_docs.append(clean_doc(adm_check_tmpl.substitute(
            ACCELERATOR=accel
        )))
    parts["03-cohorts.yaml"] = "\n---\n".join(cohort_docs) + "\n"
        
    # 4. ResourceFlavor per unique accelerator
    rf_tmpl = Template(templates["resource_flavor"])
    rf_docs = []
    for accel in sorted(accel_workers.keys()):
        rf_docs.append(clean_doc(rf_tmpl.substitute(
            ACCELERATOR=accel
        )))
    parts["04-resource-flavors.yaml"] = "\n---\n".join(rf_docs) + "\n"

    # 5. ClusterQueue, LocalQueue per pool across all workers
    pool_data = {}
    for wconf in config["worker_clusters"].values():
        for pk, pconf in wconf["pools"].items():
            if pk not in pool_data:
                pool_data[pk] = {"quota": 0, "accelerator": pconf["accelerator"]}
            pool_data[pk]["quota"] += pconf["quota"]

    queue_docs = render_queue_group(
        templates["queue_group"], pool_data, config["namespace"],
        with_admission_checks=True,
    )
    parts["05-queues.yaml"] = "\n---\n".join(queue_docs) + "\n"

    # 6. Launcher (manager only; the workload it submits is mirrored onto a
    #    worker by MultiKueue).
    parts["06-launcher.yaml"] = render_launcher(
        templates["launcher"], k8s_dir, config
    ).strip() + "\n"

    return parts

def generate_worker_parts(config, worker_key, templates):
    wconf = config["worker_clusters"][worker_key]
    parts = {}
    
    # 1. Base (Namespace, ClusterSecretStore, ExternalSecret)
    base_tmpl = Template(templates["base"])
    parts["01-base.yaml"] = base_tmpl.substitute(
        NAMESPACE=config["namespace"],
        SECRET_PROJECT=config["secret_project"],
        SECRET_ID=config["secret_id"],
        CLUSTER_LOCATION=wconf["location"],
        CLUSTER_NAME=wconf["cluster_name"]
    ).strip() + "\n"
    
    # 2. ResourceFlavor per unique accelerator in worker
    rf_tmpl = Template(templates["resource_flavor"])
    accelerators = sorted({pconf["accelerator"] for pconf in wconf["pools"].values()})
    rf_docs = [clean_doc(rf_tmpl.substitute(ACCELERATOR=accel)) for accel in accelerators]
    parts["02-resource-flavors.yaml"] = "\n---\n".join(rf_docs) + "\n"

    # 3. ClusterQueue, LocalQueue per pool profile in worker
    queue_docs = render_queue_group(
        templates["queue_group"], wconf["pools"], config["namespace"],
        with_admission_checks=False,
    )
    parts["03-queues.yaml"] = "\n---\n".join(queue_docs) + "\n"

    # 5. The identity workloads run as, which the cache buckets authorise.
    # 4. What the manager's Kueue controller may do here. Scoped to mirroring
    #    workloads; it used to hold cluster-admin.
    parts["04-multikueue-rbac.yaml"] = Template(
        templates["multikueue_rbac_worker"]
    ).substitute(PROJECT_ID=config["project_id"]).strip() + "\n"

    # 5. The identity workloads run as, which the cache buckets authorise.
    parts["05-workload-sa.yaml"] = Template(
        templates["workload_sa"]
    ).substitute(
        NAMESPACE=config["namespace"],
        PROJECT=wconf["project"],
    ).strip() + "\n"

    # 6. The caches, as claims. Named identically in every region so a workload
    #    manifest never carries a bucket name; the region binding lives here.
    parts["06-cache-volumes.yaml"] = Template(
        templates["cache_volumes"]
    ).substitute(
        NAMESPACE=config["namespace"],
        CACHE_BUCKET=wconf["cache_bucket"],
        MODELS_BUCKET=wconf["models_bucket"],
    ).strip() + "\n"

    # 7. Let the manager-side launcher read pod logs here over Connect Gateway.
    #    Numbered after the infra manifests so this PR adds a file rather than
    #    renumbering theirs.
    parts["07-launcher-rbac.yaml"] = Template(
        templates["launcher_rbac_worker"]
    ).substitute(
        PROJECT_ID=config["project_id"],
        NAMESPACE=config["namespace"],
    ).strip() + "\n"

    return parts

def load_templates(templates_dir):
    templates = {}
    templates["base"] = (templates_dir / "base.yaml.tpl").read_text()
    templates["multikueue_cluster"] = (templates_dir / "multikueue_cluster.yaml.tpl").read_text()
    templates["multikueue_config"] = (templates_dir / "multikueue_config.yaml.tpl").read_text()
    templates["admission_check"] = (templates_dir / "admission_check.yaml.tpl").read_text()
    templates["resource_flavor"] = (templates_dir / "resource_flavor.yaml.tpl").read_text()
    templates["queue_group"] = (templates_dir / "queue_group.yaml.tpl").read_text()
    templates["launcher"] = (templates_dir / "launcher.yaml.tpl").read_text()
    templates["launcher_rbac_worker"] = (templates_dir / "launcher_rbac_worker.yaml.tpl").read_text()
    templates["cache_volumes"] = (templates_dir / "cache_volumes.yaml.tpl").read_text()
    templates["workload_sa"] = (templates_dir / "workload_sa.yaml.tpl").read_text()
    templates["multikueue_rbac_worker"] = (templates_dir / "multikueue_rbac_worker.yaml.tpl").read_text()
    return templates

def main():
    k8s_dir = Path(__file__).resolve().parent.parent

    parser = argparse.ArgumentParser(
        description="Render the Kueue manifests for the manager and every worker "
                    "cluster from prod.auto.tfvars.")
    parser.add_argument(
        "--out-dir", type=Path, default=k8s_dir / "generated",
        # deploy_manifests.sh renders into a scratch directory and diffs it
        # against the committed one, which is how it tells stale manifests from
        # fresh without writing over what is under review.
        help="Where to write the manifests; emptied first. "
             "Default: the committed generated/.")
    args = parser.parse_args()

    tfvars_file = k8s_dir / "prod.auto.tfvars"
    templates_dir = k8s_dir / "kueue" / "templates"
    out_dir = args.out_dir

    if not tfvars_file.exists():
        print(f"Error: {tfvars_file} not found.")
        sys.exit(1)

    print(f"Reading configuration from: {tfvars_file}")
    config = parse_tfvars(tfvars_file)

    print(f"Loading template files from: {templates_dir}")
    templates = load_templates(templates_dir)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Generate Manager Manifests (Modular directory + Consolidated file)
    mgr_parts = generate_manager_parts(config, templates, k8s_dir)
    mgr_dir = out_dir / "manager"
    mgr_dir.mkdir(parents=True, exist_ok=True)
    
    for filename in sorted(mgr_parts.keys()):
        (mgr_dir / filename).write_text(mgr_parts[filename])
        print(f"Generated: manager/{filename}")

    # 2. Generate Worker Manifests (Modular directory + Consolidated file)
    for worker_key in config["worker_clusters"].keys():
        wkr_parts = generate_worker_parts(config, worker_key, templates)
        wkr_dir = out_dir / f"worker-{worker_key}"
        wkr_dir.mkdir(parents=True, exist_ok=True)
        
        for filename in sorted(wkr_parts.keys()):
            (wkr_dir / filename).write_text(wkr_parts[filename])
            print(f"Generated: worker-{worker_key}/{filename}")

    print("\nGeneration Complete! Apply a whole directory with kubectl apply -f, or\nfile by file - the numeric prefixes are the order kubectl uses either way.")

if __name__ == "__main__":
    main()
