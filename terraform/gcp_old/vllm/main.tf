module "benchmark" {
  source = "../modules/benchmark"
  providers = {
    google-beta.us-east1-d = google-beta.us-east1-d
  }

  project_id = var.project_id
  buildkite_benchmark_agent_token_name = var.buildkite_benchmark_agent_token_name
  huggingface_token_name = var.huggingface_token_name
}
module "ci_v6" {
  source = "../modules/ci_v6"
  providers = {
    google-beta.us-east5-b = google-beta.us-east5-b
  }  
  project_id = var.project_id
  buildkite_ci_agent_token_name = var.buildkite_ci_agent_token_name
  huggingface_token_name = var.huggingface_token_name
  ci_v6_instance_count = var.ci_v6_instance_count
  ci_v6_disk_size = var.ci_v6_disk_size
}

module "ci_v5" {
  source = "../modules/ci_v5"
  providers = {
    google-beta.us-south1-a = google-beta.us-south1-a
  }

  project_id = var.project_id
  buildkite_ci_agent_token_name = var.buildkite_ci_agent_token_name
  huggingface_token_name = var.huggingface_token_name
}