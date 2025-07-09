# module "benchmark" {
#   source = "./modules/benchmark"
#   providers = {
#     google-beta.us-east1-d = google-beta.us-east1-d
#   }

#   buildkite_agent_token_benchmark_cluster = var.buildkite_agent_token_benchmark_cluster
#   huggingface_token     = var.huggingface_token
# }

module "ci_v6" {
  source = "./modules/ci_v6"
  providers = {
    google-beta.us-east5-b = google-beta.us-east5-b
  }

  buildkite_agent_token_ci_cluster = var.buildkite_agent_token_ci_cluster
  huggingface_token     = var.huggingface_token
  project_id            = var.project_id
}

# module "ci_v5" {
#   source = "./modules/ci_v5"
#   providers = {
#     google-beta.us-south1-a = google-beta.us-south1-a
#   }

#   buildkite_agent_token_ci_cluster = var.buildkite_agent_token_ci_cluster
#   huggingface_token     = var.huggingface_token
# }


output "buildkite_agent_public_key" {
  value = module.ci_v6.buildkite_agent_public_key
  sensitive = true
}