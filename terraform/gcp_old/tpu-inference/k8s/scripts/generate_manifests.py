#!/usr/bin/env python3
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
                "hosts": int(pdata.get("hosts", 1)),
            }
            
        worker_clusters[key] = {
            "project": clean_val(wdata["project"]),
            "location": clean_val(wdata["location"]),
            "cluster_name": f"{name_prefix}-{key}",
            "profile_name": f"{name_prefix}-{key}-global",
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
        # The launcher impersonates the manager node SA (see iam.tf), which
        # holds gkehub.gatewayEditor on the fleet host project and is the
        # identity worker clusters authorise for pod log reads.
        "launcher_gsa": clean_val(data.get(
            "launcher_gsa", f"{name_prefix}-mgr-node@{project_id}.iam.gserviceaccount.com")),
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
            "total_max_seconds": config["total_max_seconds"],
            "workers": dict(sorted(workers.items())),
            "profiles": dict(sorted(profiles.items())),
        },
        sort_keys=False,
    )

    return Template(template).substitute(
        NAMESPACE=config["namespace"],
        LAUNCHER_GSA=config["launcher_gsa"],
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

    # 4. Let the manager-side launcher read pod logs here over Connect Gateway.
    parts["04-launcher-rbac.yaml"] = Template(
        templates["launcher_rbac_worker"]
    ).substitute(LAUNCHER_GSA=config["launcher_gsa"]).strip() + "\n"

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
    return templates

def main():
    k8s_dir = Path(__file__).resolve().parent.parent
    tfvars_file = k8s_dir / "prod.auto.tfvars"
    templates_dir = k8s_dir / "kueue" / "templates"
    out_dir = k8s_dir / "generated"

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
