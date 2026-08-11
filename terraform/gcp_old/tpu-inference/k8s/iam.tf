resource "google_service_account" "manager_nodes" {
  count        = var.create_service_accounts && var.manager_node_service_account == null ? 1 : 0
  project      = var.project_id
  account_id   = "${var.name_prefix}-mgr-node"
  display_name = "Manager GKE Node SA"
}

resource "google_project_iam_member" "manager_nodes" {
  for_each = var.create_service_accounts && var.manager_node_service_account == null ? local.node_service_account_roles : toset([])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.manager_nodes[0].email}"
}

# Worker Node Service Account
resource "google_service_account" "worker_nodes" {
  for_each = {
    for k, v in var.worker_clusters : k => v
    if var.create_service_accounts && try(v.node_service_account, null) == null
  }

  project      = each.value.project
  account_id   = "${var.name_prefix}-wkr-node"
  display_name = "Worker GKE Node SA (${each.key})"
}

resource "google_project_iam_member" "worker_nodes" {
  for_each = {
    for k, v in local.worker_node_role_bindings : k => v
    if var.create_service_accounts && try(var.worker_clusters[v.worker_name].node_service_account, null) == null
  }

  project = each.value.project
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.worker_nodes[each.value.worker_name].email}"
}

# Worker Node Access on Manager Project (Artifact Registry Reader for pulling images)
resource "google_project_iam_member" "worker_nodes_manager_project" {
  for_each = {
    for k, v in var.worker_clusters : k => v
    if var.create_service_accounts && try(v.node_service_account, null) == null
  }

  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.worker_nodes[each.key].email}"
}

# Manager Node Access on Worker Projects (e.g. Artifact Registry reader & GKE Hub Gateway Admin)
resource "google_project_iam_member" "manager_nodes_worker_project" {
  for_each = {
    for item in flatten([
      for worker_key, worker in var.worker_clusters : [
        for role in ["roles/artifactregistry.reader", "roles/gkehub.gatewayAdmin"] : {
          key        = "${worker_key}/${role}"
          worker_key = worker_key
          project    = worker.project
          role       = role
        }
      ]
    ]) : item.key => item
    if var.create_service_accounts && var.manager_node_service_account == null
  }

  project = each.value.project
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.manager_nodes[0].email}"
}

# Secret Manager Accessor for External Secrets Operator on Secret Project
resource "google_project_iam_member" "secret_accessor_manager" {
  count   = var.create_service_accounts && var.manager_node_service_account == null ? 1 : 0
  project = var.buildkite_secret_project
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.manager_nodes[0].email}"
}

resource "google_project_iam_member" "secret_accessor_worker" {
  for_each = {
    for k, v in var.worker_clusters : k => v
    if var.create_service_accounts && try(v.node_service_account, null) == null
  }

  project = var.buildkite_secret_project
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.worker_nodes[each.key].email}"
}

# GKE Hub Service Agent IAM Binding
resource "google_project_iam_member" "gkehub_service_agent" {
  for_each = var.worker_clusters

  project = each.value.project
  role    = "roles/gkehub.serviceAgent"
  member  = "serviceAccount:service-${data.google_project.manager.number}@gcp-sa-gkehub.iam.gserviceaccount.com"
}

data "google_project" "manager" {
  project_id = var.project_id
}

# Workload Identity binding for Kueue controller manager
resource "google_service_account_iam_member" "kueue_workload_identity" {
  for_each = var.create_service_accounts && var.manager_node_service_account == null ? toset([
    "roles/iam.workloadIdentityUser",
    "roles/iam.serviceAccountTokenCreator",
  ]) : []

  service_account_id = google_service_account.manager_nodes[0].name
  role               = each.value
  member             = "serviceAccount:${var.project_id}.svc.id.goog[kueue-system/kueue-controller-manager]"
}

# Connect Gateway IAM permissions for Kueue controller manager to access Fleet worker clusters via Connect Gateway
resource "google_project_iam_member" "connect_gateway_manager" {
  count   = var.create_service_accounts && var.manager_node_service_account == null ? 1 : 0
  project = var.project_id
  role    = "roles/gkehub.gatewayAdmin"
  member  = "serviceAccount:${google_service_account.manager_nodes[0].email}"
}

# Listing Fleet memberships, which resolving a Connect Gateway target requires.
resource "google_project_iam_member" "gkehub_viewer_manager" {
  count   = var.create_service_accounts && var.manager_node_service_account == null ? 1 : 0
  project = var.project_id
  role    = "roles/gkehub.viewer"
  member  = "serviceAccount:${google_service_account.manager_nodes[0].email}"
}

resource "google_project_iam_member" "connect_gateway_kueue_wi" {
  project = var.project_id
  role    = "roles/gkehub.gatewayAdmin"
  member  = "serviceAccount:${var.project_id}.svc.id.goog[kueue-system/kueue-controller-manager]"
}

# Workload Identity binding for External Secrets Operator controller
resource "google_service_account_iam_member" "external_secrets_workload_identity_manager" {
  for_each = var.create_service_accounts && var.manager_node_service_account == null ? toset([
    "roles/iam.workloadIdentityUser",
    "roles/iam.serviceAccountTokenCreator",
  ]) : []

  service_account_id = google_service_account.manager_nodes[0].name
  role               = each.value
  member             = "serviceAccount:${var.project_id}.svc.id.goog[external-secrets/external-secrets]"
}

resource "google_service_account_iam_member" "external_secrets_workload_identity_worker" {
  for_each = {
    for item in flatten([
      for worker_key, worker in var.worker_clusters : [
        for role in ["roles/iam.workloadIdentityUser", "roles/iam.serviceAccountTokenCreator"] : {
          key        = "${worker_key}/${role}"
          worker_key = worker_key
          project    = worker.project
          role       = role
        }
      ]
    ]) : item.key => item
    if var.create_service_accounts && try(var.worker_clusters[item.worker_key].node_service_account, null) == null
  }

  service_account_id = google_service_account.worker_nodes[each.value.worker_key].name
  role               = each.value.role
  member             = "serviceAccount:${each.value.project}.svc.id.goog[external-secrets/external-secrets]"
}
