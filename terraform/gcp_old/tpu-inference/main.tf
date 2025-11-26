# module "benchmark" {
#   source = "./modules/benchmark"
#   providers = {
#     google-beta.us-east1-d = google-beta.us-east1-d
#   }

#   buildkite_agent_token_benchmark_cluster = var.buildkite_agent_token_benchmark_cluster
#   huggingface_token     = var.huggingface_token
# }

data "google_secret_manager_secret_version" "buildkite_agent_token_ci_cluster" {
  secret = "projects/${var.project_id}/secrets/tpu_commons_buildkite_agent_token"
  version = "latest"
}

data "google_secret_manager_secret_version" "huggingface_token" {
  secret  = "projects/${var.project_id}/secrets/tpu_commons_buildkite_hf_token"
  version = "latest"
}

module "ci_v6" {
  source = "./modules/ci_v6"
  providers = {
    google-beta.us-east5-b = google-beta.us-east5-b
  }
  project_id              = var.project_id
  buildkite_token_value   = data.google_secret_manager_secret_version.buildkite_agent_token_ci_cluster.secret_data
  huggingface_token_value = data.google_secret_manager_secret_version.huggingface_token.secret_data
}

module "ci_v6e_8" {
  source    = "./modules/ci_v6e"
  providers = {
    google-beta = google-beta.us-central1-b
  }

  accelerator_type                 = "v6e-8"
  reserved                         = true
  instance_count                   = 6
  buildkite_queue_name             = "tpu_v6e_8_queue"
  project_id                       = var.project_id
  buildkite_token_value            = data.google_secret_manager_secret_version.buildkite_agent_token_ci_cluster.secret_data
  huggingface_token_value          = data.google_secret_manager_secret_version.huggingface_token.secret_data
}

module "ci_cpu" {
  source    = "./modules/ci_cpu"
  providers = {
    google-beta = google-beta.us-east5-b
  }
  project_id                       = var.project_id
  instance_count                   = 8
}

# module "ci_v5" {
#   source = "./modules/ci_v5"
#   providers = {
#     google-beta.us-south1-a = google-beta.us-south1-a
#   }

#   buildkite_agent_token_ci_cluster = var.buildkite_agent_token_ci_cluster
#   huggingface_token     = var.huggingface_token
# }
