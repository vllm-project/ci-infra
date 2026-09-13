locals {
  common_labels = merge(var.labels, {
    managed_by = "terraform"
    component  = "tpu-ci"
  })

  # Both service account names are fixed elsewhere and matched here: Kueue's by
  # its release manifests, the launcher's by kueue/templates/launcher.yaml.tpl.
  kueue_controller_principal = join("", [
    "principal://iam.googleapis.com/projects/${data.google_project.manager.number}",
    "/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog",
    "/subject/ns/kueue-system/sa/kueue-controller-manager",
  ])

  launcher_principal = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/tpu-launcher]"

  # Keyed by project/location rather than by list index, which would renumber -
  # and so rebuild - every cluster after one that was removed.
  #
  # short_name names the things that are project scoped (cluster, service
  # account, router); fleet_name names the things that are fleet-wide (the
  # Terraform address, the rendered directory, the MultiKueueCluster).
  workers = {
    for w in var.worker_clusters : "${w.project}/${w.location}" => merge(w, {
      short_name = w.location
      fleet_name = "${w.project}-${w.location}"
    })
  }

  # Every cluster's pools in one map, since one resource creates them all. The
  # pool name is derived from the shape rather than given in the tfvars, so it
  # cannot describe hardware the pool does not have.
  tpu_node_pools = {
    for pool in flatten([
      for worker_name, worker in local.workers : [
        for pool in worker.tpu_node_pools : [
          # The topology's dimensions padded to three with 1s, so the product
          # below is one expression: Terraform has no product(), and v7x
          # topologies are 3D where v6e's are 2D.
          for dims in [concat([for d in split("x", pool.topology) : parseint(d, 10)], [1, 1])] :
          merge(pool, {
            name = "${pool.machine_type}-${pool.topology}"

            # short_name is carried alongside the key because a Kubernetes
            # label value may not hold the slash that key has in it.
            worker     = worker_name
            short_name = worker.short_name

            # A slice wider than one VM is placed as a unit and needs a
            # placement policy. The machine type's suffix is chips per VM -
            # unlike the Buildkite queue names, where tpu7x-8 counts
            # TensorCores and is a four-chip tpu7x-standard-4t.
            is_multi_host = dims[0] * dims[1] * dims[2] > parseint(
              trimsuffix(reverse(split("-", pool.machine_type))[0], "t"), 10
            )

            family = split("-", pool.machine_type)[0]
          })
        ]
      ]
    ]) : "${pool.worker}/${pool.name}" => pool
  }

  # scripts/generate_manifests.py derives these same names for the
  # PersistentVolume volumeHandles, so the two must stay in step; a mismatch is
  # a volume pointing at a bucket that was never created, which
  # deploy_manifests.py checks for before applying.
  #
  # The hash covers project and region because bucket names are globally
  # unique, so two organisations running this would otherwise collide.
  workload_buckets = merge([
    for worker in var.worker_clusters : {
      for purpose, retention_days in {
        cache  = var.cache_lifecycle_age_days
        models = var.models_lifecycle_age_days
        } : "${worker.project}/${worker.location}/${purpose}" => {
        purpose        = purpose
        project        = worker.project
        location       = worker.location
        retention_days = retention_days
        name = join("-", [
          var.name_prefix,
          purpose,
          substr(sha256("${worker.project}/${worker.location}"), 0, 8),
        ])
      }
    }
  ]...)

  manager_repository_bindings = {
    for repo in var.image_repositories :
    "${repo.location}/${repo.repository}" => repo
  }

  worker_node_repository_bindings = {
    for item in flatten([
      for worker_name, worker in local.workers : [
        for repo in var.image_repositories : {
          key         = "${worker_name}/${repo.location}/${repo.repository}"
          worker_name = worker_name
          location    = repo.location
          repository  = repo.repository
        }
      ]
    ]) : item.key => item
  }
}
