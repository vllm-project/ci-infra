# One per worker cluster rather than one shared account, so that revoking a
# worker's access is deleting a principal and not editing a policy. manager/
# grants it pulls from the fleet's Artifact Registry by its derived email,
# since the account is created in a root manager/ cannot read.
resource "google_service_account" "nodes" {
  project      = var.project
  account_id   = "${var.fleet.name_prefix}-wkr-${var.name}"
  display_name = "Worker GKE Node SA (${var.name})"

  lifecycle {
    precondition {
      condition     = length("${var.fleet.name_prefix}-wkr-${var.name}") <= 30
      error_message = "${var.fleet.name_prefix}-wkr-${var.name} is longer than the 30 characters a service account ID may be. Give the worker a shorter name."
    }
  }
}

# Every pod scheduled to a node can reach that node's identity, so a node gets
# nothing beyond container.defaultNodeServiceAccount - GKE's maintained
# definition of what it takes to boot, log and report metrics. Anything a
# workload needs goes on the workload's own account through Workload Identity.
resource "google_project_iam_member" "nodes" {
  project = var.project
  role    = "roles/container.defaultNodeServiceAccount"
  member  = "serviceAccount:${google_service_account.nodes.email}"
}

# Only for a worker in another project from the fleet. When the worker shares
# the fleet's project, enabling the GKE Hub API already grants the service agent
# this role on it; declaring it here would make Terraform the owner of a binding
# Google manages, and destroy would revoke it.
#
# Per cluster although the binding is per project, so a second worker in the
# same project declares the same binding, and destroying either revokes it for
# both.
resource "google_project_iam_member" "gkehub_service_agent" {
  count = var.project == var.fleet.project_id ? 0 : 1

  project = var.project
  role    = "roles/gkehub.serviceAgent"
  member  = "serviceAccount:service-${var.fleet.project_number}@gcp-sa-gkehub.iam.gserviceaccount.com"
}
