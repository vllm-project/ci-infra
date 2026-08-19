resource "google_service_account" "manager_nodes" {
  count        = var.create_service_accounts && var.manager_node_service_account == null ? 1 : 0
  project      = var.project_id
  account_id   = "${var.name_prefix}-mgr-node"
  display_name = "Manager GKE Node SA"
}

resource "google_project_iam_member" "manager_nodes" {
  for_each = var.create_service_accounts && var.manager_node_service_account == null ? local.manager_node_service_account_roles : toset([])

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
  for_each = var.worker_clusters

  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${coalesce(try(each.value.node_service_account, null), try(google_service_account.worker_nodes[each.key].email, null))}"
}

# Manager Node Access on Worker Projects (Artifact Registry Reader for gcp-auth-plugin and tooling)
resource "google_project_iam_member" "manager_nodes_worker_project" {
  for_each = {
    for worker_key, worker in var.worker_clusters : worker_key => worker
    if var.create_service_accounts && var.manager_node_service_account == null
  }

  project = each.value.project
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.manager_nodes[0].email}"
}

# Secret Manager Accessor for External Secrets Operator via direct Workload Identity
resource "google_project_iam_member" "external_secrets_manager_secret_accessor" {
  project = var.buildkite_secret_project
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${var.project_id}.svc.id.goog[external-secrets/external-secrets]"
}

resource "google_project_iam_member" "external_secrets_worker_secret_accessor" {
  for_each = var.worker_clusters

  project = var.buildkite_secret_project
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${each.value.project}.svc.id.goog[external-secrets/external-secrets]"
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

# The manager node service account no longer holds gkehub.viewer or
# gkehub.gatewayEditor.
#
# Both existed for the impersonation model: Kueue, External Secrets and the
# launcher all ran as Kubernetes service accounts annotated onto this account,
# so its roles were theirs. Each of them now holds its own grant directly, and
# nothing impersonates this account, so its Fleet permissions are reachable by
# nobody.
#
# Kept: artifactregistry.reader, logging, monitoring and
# stackdriver.resourceMetadata.writer, which the nodes themselves use. Removing
# artifactregistry.reader on the worker projects broke a build and was restored
# in 64ca84f; that is the shape of mistake this comment exists to prevent
# repeating.

# Connect Gateway for the Kueue controller: it creates, updates and deletes
# workloads on the worker clusters, so it needs write. gatewayEditor is that;
# gatewayAdmin additionally carries impersonation and policy verbs it never
# uses.
#
# gkehub.viewer for the same reason the launcher has it - resolving a Connect
# Gateway target means listing Fleet memberships first. Kueue worked without it
# only because the manager node SA happened to hold it; that grant is going
# away, so make the dependency explicit rather than inherited.
resource "google_project_iam_member" "connect_gateway_kueue_wi" {
  for_each = toset(["roles/gkehub.gatewayEditor", "roles/gkehub.viewer"])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${var.project_id}.svc.id.goog[kueue-system/kueue-controller-manager]"
}

resource "google_project_iam_member" "connect_gateway_kueue_worker_project" {
  for_each = var.worker_clusters

  project = each.value.project
  role    = "roles/gkehub.gatewayEditor"
  member  = "serviceAccount:${var.project_id}.svc.id.goog[kueue-system/kueue-controller-manager]"
}
