# Referenced from the cluster's auto_provisioning_defaults; see manager.tf for
# what happens if nodes are left on the project default instead.
resource "google_service_account" "manager_nodes" {
  project      = var.project_id
  account_id   = "${var.name_prefix}-mgr-node"
  display_name = "Manager GKE Node SA"
}

# Every pod scheduled to a node can reach that node's identity, so a node gets
# nothing beyond container.defaultNodeServiceAccount - GKE's maintained
# definition of what it takes to boot, log and report metrics. Anything a
# workload needs goes on the workload's own account through Workload Identity.
#
# It does not cover pulling private images; that is granted per repository
# below, to both node accounts.
resource "google_project_iam_member" "manager_nodes" {
  project = var.project_id
  role    = "roles/container.defaultNodeServiceAccount"
  member  = "serviceAccount:${google_service_account.manager_nodes.email}"
}

resource "google_artifact_registry_repository_iam_member" "manager_nodes" {
  for_each = local.manager_repository_bindings

  project    = var.project_id
  location   = each.value.location
  repository = each.value.repository
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.manager_nodes.email}"
}

# One per worker cluster rather than one shared account, so that revoking a
# worker's access is deleting a principal and not editing a policy.
resource "google_service_account" "worker_nodes" {
  for_each = local.workers

  project      = each.value.project
  account_id   = "${var.name_prefix}-wkr-${each.value.short_name}"
  display_name = "Worker GKE Node SA (${each.value.short_name})"
}

resource "google_project_iam_member" "worker_nodes" {
  for_each = local.workers

  project = each.value.project
  role    = "roles/container.defaultNodeServiceAccount"
  member  = "serviceAccount:${google_service_account.worker_nodes[each.key].email}"
}

resource "google_artifact_registry_repository_iam_member" "worker_nodes" {
  for_each = local.worker_node_repository_bindings

  project    = var.project_id
  location   = each.value.location
  repository = each.value.repository
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.worker_nodes[each.value.worker_name].email}"
}

# Lets the manager's Kueue controller reach a worker's API server through
# Connect Gateway. It authenticates as its Kubernetes service account directly,
# with no Google service account in between, so there is no key and nothing to
# rotate; gke-gcloud-auth-plugin turns that identity into the gateway's token.
#
# On the project - the one grant here not scoped to the resource it is about.
# Connect Gateway checks gkehub.gateway.get against its own
# projects/<project>/gkeMemberships/<id> resource, which a binding on the fleet
# membership does not cover: the same principal holding gatewayEditor on the
# membership is refused with PERMISSION_DENIED.
#
# So what bounds the controller inside a worker is not IAM but the
# kueue-multikueue-remote ClusterRole the generated manifests bind it to there.
resource "google_project_iam_member" "kueue_manager_gateway" {
  for_each = toset([for w in var.worker_clusters : w.project])

  project = each.value
  role    = "roles/gkehub.gatewayEditor"
  member  = local.kueue_controller_principal
}

# The same gateway, for the launcher, which follows a workload into whichever
# worker MultiKueue dispatched it to and streams the pod logs back. Project
# scope for the reason above and no other: in a worker the launcher can do
# nothing beyond the tpu-launcher-log-reader ClusterRole bound to it there.
#
# gatewayReader rather than gatewayEditor, since reading logs is get, list and
# watch; gkehub.viewer alongside it because resolving the membership reads it.
#
# On var.project_id whatever project a worker runs in - workers.tf registers
# every cluster into this project's fleet, so that is where the gkeMemberships
# resource the gateway checks lives.
resource "google_project_iam_member" "launcher_gateway" {
  for_each = toset(["roles/gkehub.gatewayReader", "roles/gkehub.viewer"])

  project = var.project_id
  role    = each.value
  member  = local.launcher_principal
}

# Read of the registry metadata, not of the layers: the launcher never pulls an
# image, it asks which digest a tag currently points at so that every pod in a
# workload is pinned to one set of bytes. The pull itself is the node service
# accounts' above.
resource "google_artifact_registry_repository_iam_member" "launcher" {
  for_each = local.manager_repository_bindings

  project    = var.project_id
  location   = each.value.location
  repository = each.value.repository
  role       = "roles/artifactregistry.reader"
  member     = local.launcher_principal
}
