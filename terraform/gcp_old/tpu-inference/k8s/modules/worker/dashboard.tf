# What the fleet's dashboard reads about this cluster outside Kubernetes, all
# of it kept in the cluster's own project: its node pools from the GKE API,
# Kueue's and GKE's TPU metrics from Managed Prometheus, and its events and
# container logs from Cloud Logging. These are the reads manager/dashboard.tf
# grants in the fleet's project, so a worker there declares none. Inside the
# cluster the dashboard goes through Connect Gateway, under the RBAC the
# deploy installs from kueue/templates/dashboard_rbac_worker.yaml.tpl.
#
# Per cluster although the bindings are per project, so a second worker in the
# same project declares the same bindings, and destroying either revokes them
# for both.
resource "google_project_iam_member" "dashboard" {
  for_each = var.project == var.fleet.project_id ? toset([]) : toset([
    "roles/container.clusterViewer",
    "roles/monitoring.viewer",
  ])

  project = var.project
  role    = each.value
  member  = "serviceAccount:${local.dashboard}"
}

# Logs through the _Default bucket's _AllLogs view rather than the whole
# project, as manager/dashboard.tf grants them.
resource "google_logging_log_view_iam_member" "dashboard" {
  count = var.project == var.fleet.project_id ? 0 : 1

  parent   = "projects/${var.project}"
  location = "global"
  bucket   = "_Default"
  name     = "_AllLogs"
  role     = "roles/logging.viewAccessor"
  member   = "serviceAccount:${local.dashboard}"
}
