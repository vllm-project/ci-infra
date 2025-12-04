data "google_secret_manager_secret_version" "buildkite_agent_token_ci_cluster" {
  secret = "projects/${var.secret_project_id}/secrets/tpu_commons_buildkite_agent_token"
  version = "latest"
}

data "google_secret_manager_secret_version" "huggingface_token" {
  secret  = "projects/${var.secret_project_id}/secrets/tpu_commons_buildkite_hf_token"
  version = "latest"
}

module "ci_v6e_1" {
  source    = "../modules/ci_v6e"
  providers = {
    google-beta = google-beta.us-central1-b
  }

  accelerator_type                 = "v6e-1"
  reserved                         = true
  instance_count                   = 24
  buildkite_queue_name             = "tpu_v6e_queue"
  project_id                       = var.project_id
  project_short_name               = var.project_short_name
  buildkite_token_value            = data.google_secret_manager_secret_version.buildkite_agent_token_ci_cluster.secret_data
  huggingface_token_value          = data.google_secret_manager_secret_version.huggingface_token.secret_data
}

module "ci_v6e_8" {
  source    = "../modules/ci_v6e"
  providers = {
    google-beta = google-beta.us-central1-b
  }

  accelerator_type                 = "v6e-8"
  reserved                         = true
  instance_count                   = 13
  buildkite_queue_name             = "tpu_v6e_8_queue"
  project_id                       = var.project_id
  project_short_name               = var.project_short_name
  buildkite_token_value            = data.google_secret_manager_secret_version.buildkite_agent_token_ci_cluster.secret_data
  huggingface_token_value          = data.google_secret_manager_secret_version.huggingface_token.secret_data
}
