locals {
  common_labels = merge(var.labels, {
    managed_by = "terraform"
    component  = "buildkite-tpu-ci"
  })

  node_service_account_roles = toset([
    "roles/artifactregistry.reader",
    "roles/gkehub.gatewayEditor",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
    "roles/stackdriver.resourceMetadata.writer",
  ])

  worker_node_role_bindings = {
    for item in flatten([
      for worker_name, worker in var.worker_clusters : [
        for role in local.node_service_account_roles : {
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
          key                  = "${worker_name}/${profile_name}"
          worker_name          = worker_name
          project              = worker.project
          location             = worker.location
          profile_name         = profile_name
          machine_type         = pool.machine_type
          topology             = try(pool.topology, null)
          chips_per_node       = pool.chips_per_node
          min_nodes            = pool.min_nodes
          max_nodes            = pool.max_nodes
          reservation_name     = try(pool.reservation_name, null)
          node_service_account = try(worker.node_service_account, null)
        }
      ]
    ]) : item.key => item
  }

  buildkite_queues = merge([
    for worker_name, worker in var.worker_clusters : {
      for profile_name, pool in worker.tpu_pools : profile_name => {
        queue             = profile_name
        topology          = pool.topology
        machine_type      = pool.machine_type
        accelerator       = pool.accelerator
        accelerator_label = pool.accelerator_label
      }
    }
  ]...)
}
