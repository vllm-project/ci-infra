data "google_secret_manager_secret_version" "buildkite_agent_token_ci_cluster" {
  secret = "projects/${var.secret_project_id}/secrets/tpu_commons_buildkite_agent_token"
  version = "latest"
}

data "google_secret_manager_secret_version" "buildkite_analytics_token_ci_cluster" {
  secret  = "projects/${var.secret_project_id}/secrets/tpu_commons_buildkite_analytics_token"
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
  instance_count                   = 0
  disk_size                        = 512
  buildkite_queue_name             = "tpu_v6e_queue"
  project_id                       = var.project_id
  project_short_name               = var.project_short_name
  buildkite_token_value            = data.google_secret_manager_secret_version.buildkite_agent_token_ci_cluster.secret_data
  buildkite_analytics_token_value  = data.google_secret_manager_secret_version.buildkite_analytics_token_ci_cluster.secret_data
  huggingface_token_value          = data.google_secret_manager_secret_version.huggingface_token.secret_data
}

module "ci_v6e_8" {
  source    = "../modules/ci_v6e"
  providers = {
    google-beta = google-beta.us-central1-b
  }

  accelerator_type                 = "v6e-8"
  reserved                         = true
  instance_count                   = 0
  buildkite_queue_name             = "tpu_v6e_8_queue"
  project_id                       = var.project_id
  project_short_name               = var.project_short_name
  buildkite_token_value            = data.google_secret_manager_secret_version.buildkite_agent_token_ci_cluster.secret_data
  buildkite_analytics_token_value  = data.google_secret_manager_secret_version.buildkite_analytics_token_ci_cluster.secret_data
  huggingface_token_value          = data.google_secret_manager_secret_version.huggingface_token.secret_data
}


module "ci_v7x_2" {
  source    = "../modules/ci_v7x"
  providers = {
    google-beta = google-beta.us-central1-c
  }

  accelerator_type                 = "tpu7x-2"
  reserved                         = true
  instance_count                   = 16
  buildkite_queue_name             = "tpu_v7x_2_queue"
  disk_size                        = 1024
  project_id                       = var.project_id
  project_short_name               = var.project_short_name
  buildkite_token_value            = data.google_secret_manager_secret_version.buildkite_agent_token_ci_cluster.secret_data
  buildkite_analytics_token_value  = data.google_secret_manager_secret_version.buildkite_analytics_token_ci_cluster.secret_data
  huggingface_token_value          = data.google_secret_manager_secret_version.huggingface_token.secret_data
}

module "ci_v7x_8" {
  source    = "../modules/ci_v7x"
  providers = {
    google-beta = google-beta.us-central1-c
  }

  accelerator_type                 = "tpu7x-8"
  reserved                         = true
  instance_count                   = 16
  buildkite_queue_name             = "tpu_v7x_8_queue"
  disk_size                        = 2048
  project_id                       = var.project_id
  project_short_name               = var.project_short_name
  buildkite_token_value            = data.google_secret_manager_secret_version.buildkite_agent_token_ci_cluster.secret_data
  buildkite_analytics_token_value  = data.google_secret_manager_secret_version.buildkite_analytics_token_ci_cluster.secret_data
  huggingface_token_value          = data.google_secret_manager_secret_version.huggingface_token.secret_data
}
