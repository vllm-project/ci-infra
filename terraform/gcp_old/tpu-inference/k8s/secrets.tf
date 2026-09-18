# Every secret here is a data source, not a resource: all of them predate this
# fleet and are owned elsewhere, and a name that is wrong should fail at plan
# time rather than quietly create an empty second secret.
#
# The grants are all _member and all scoped to one secret. These projects hold
# credentials nothing here has any business reading - REST API tokens, a webhook
# signing key - and two of the secrets are another team's.
#
# The copying itself is generated YAML rather than Terraform; see
# kueue/templates/secret_sync.yaml.tpl.

# The Buildkite agent token. The bare-metal agents read the same one.
data "google_secret_manager_secret" "agent_token" {
  project   = var.project_id
  secret_id = var.agent_token_secret_id
}

resource "google_secret_manager_secret_iam_member" "agent_token_sync" {
  project   = var.project_id
  secret_id = data.google_secret_manager_secret.agent_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/secret-sync]"
}

# The credentials a workload reads from its own environment, each in the project
# that owns it - which is never this one.
data "google_secret_manager_secret" "env_secret" {
  for_each = var.env_secrets

  project   = each.value.project
  secret_id = each.value.secret
}

# The Workload Identity pool is the project's rather than a cluster's, so this
# one principal covers the manager and both workers, and a new worker inherits
# the grant by existing.
resource "google_secret_manager_secret_iam_member" "env_secret_sync" {
  for_each = var.env_secrets

  project   = each.value.project
  secret_id = data.google_secret_manager_secret.env_secret[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/secret-sync]"
}

# The GitHub deploy key, which the agent's checkout container takes from its
# environment.
data "google_secret_manager_secret" "git_ssh_key" {
  project   = var.project_id
  secret_id = var.git_ssh_key_secret_id
}

resource "google_secret_manager_secret_iam_member" "git_ssh_key_sync" {
  project   = var.project_id
  secret_id = data.google_secret_manager_secret.git_ssh_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/secret-sync]"
}
