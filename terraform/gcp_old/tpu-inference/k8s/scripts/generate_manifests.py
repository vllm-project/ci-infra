#!/usr/bin/env python3
import os
import sys
import shutil
from pathlib import Path
from string import Template
import hcl2

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

def generate_manager_parts(config, templates):
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

    return parts

def load_templates(templates_dir):
    templates = {}
    templates["base"] = (templates_dir / "base.yaml.tpl").read_text()
    templates["multikueue_cluster"] = (templates_dir / "multikueue_cluster.yaml.tpl").read_text()
    templates["multikueue_config"] = (templates_dir / "multikueue_config.yaml.tpl").read_text()
    templates["admission_check"] = (templates_dir / "admission_check.yaml.tpl").read_text()
    templates["resource_flavor"] = (templates_dir / "resource_flavor.yaml.tpl").read_text()
    templates["queue_group"] = (templates_dir / "queue_group.yaml.tpl").read_text()
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
    mgr_parts = generate_manager_parts(config, templates)
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
