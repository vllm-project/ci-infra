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

  # Every project a worker runs in. A pod authenticates in the Workload
  # Identity pool of the cluster it runs on, and that pool is the project's, so
  # a grant to a workload's Kubernetes service account is made once per
  # project.
  worker_projects = distinct([for w in values(var.workers) : w.project])

  # The workloads' tpu-workload account, once per worker project.
  workload_principals = {
    for project in local.worker_projects :
    project => "serviceAccount:${project}.svc.id.goog[${var.namespace}/tpu-workload]"
  }

  # Keyed by project/location, which is how the grants below and the rendered
  # tree name a worker. cluster_name is also the Fleet membership ID.
  workers = {
    for name, w in var.workers : "${w.project}/${w.location}" => merge(w, {
      name         = name
      cluster_name = "${var.name_prefix}-${name}"
      # modules/worker creates the account under this name, in a root this one
      # cannot read, so it is derived here rather than referenced.
      node_service_account = "${var.name_prefix}-wkr-${name}@${w.project}.iam.gserviceaccount.com"
    })
  }

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
