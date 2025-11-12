# module "benchmark" {
#   source = "../modules/benchmark"
#   providers = {
#     google-beta.us-east1-d = google-beta.us-east1-d
#   }
#   project_id            = var.project_id
# }

module "ci_v6" {
  source = "../modules/ci_v6"
  providers = {
    google-beta.us-east5-b = google-beta.us-east5-b
  }
  project_id            = var.project_id
  buildkite_agent_token_ci_cluster_name = var.buildkite_agent_token_ci_cluster_name
  huggingface_token_name = var.huggingface_token_name
  ci_v6_instance_count = var.ci_v6_instance_count
  ci_v6_disk_size = var.ci_v6_disk_size
}

module "ci_cpu" {
  source    = "../modules/ci_cpu"
  providers = {
    google-beta.us-east5-b = google-beta.us-east5-b
  }
  project_id = var.project_id
  buildkite_agent_token_ci_cluster_name = var.buildkite_agent_token_ci_cluster_name


}

# module "ci_v5" {
#   source = "../modules/ci_v5"
#   providers = {
#     google-beta.us-south1-a = google-beta.us-south1-a
#   }
#   project_id = var.project_id
# }
