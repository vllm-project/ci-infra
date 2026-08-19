locals {
  common_labels = merge(var.labels, {
    managed_by = "terraform"
    component  = "buildkite-tpu-ci"
  })

  # No Fleet roles: Kueue, External Secrets and the launcher used to run as
  # this account by impersonation and now hold their own grants, so nothing
  # reaches the Fleet through it. What is left is what a node needs to pull
  # images and write its own telemetry.
  manager_node_service_account_roles = toset([
    "roles/artifactregistry.reader",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
    "roles/stackdriver.resourceMetadata.writer",
  ])

  worker_node_service_account_roles = toset([
    "roles/artifactregistry.reader",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
    "roles/stackdriver.resourceMetadata.writer",
  ])

  worker_node_role_bindings = {
    for item in flatten([
      for worker_name, worker in var.worker_clusters : [
        for role in local.worker_node_service_account_roles : {
          key         = "${worker_name}/${role}"
          worker_name = worker_name
          project     = worker.project
          role        = role
        }
      ]
    ]) : item.key => item
  }

  tpu_node_pools = {
    for item in flatten([
      for worker_name, worker in var.worker_clusters : [
        for profile_name, pool in worker.tpu_pools : {
          key            = "${worker_name}/${profile_name}"
          worker_name    = worker_name
          project        = worker.project
          location       = worker.location
          profile_name   = profile_name
          machine_type   = pool.machine_type
          topology       = try(pool.topology, null)
          chips_per_node = pool.chips_per_node
          min_nodes      = pool.min_nodes
          max_nodes      = pool.max_nodes
          # Hosts in one slice. 1 is a single-host pool; more makes this a
          # multi-host TPU slice pool, which GKE creates with a placement
          # policy carrying the topology and scales atomically - every host
          # or none - so its node counts are whole slices (see clusters.tf).
          hosts                = try(pool.hosts, 1)
          is_multi_host        = try(pool.hosts, 1) > 1
          reservation_name     = try(pool.reservation_name, null)
          node_service_account = try(worker.node_service_account, null)
        }
      ]
    ]) : item.key => item
  }
}
