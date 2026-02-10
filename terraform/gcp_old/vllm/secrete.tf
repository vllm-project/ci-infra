resource "google_secret_manager_secret" "buildkite_agent_token_benchmark_cluster" {
  project   = var.project_id
  secret_id = "buildkite_agent_token_benchmark_cluster"
  
  replication {
    auto {}
  }
  
  labels = {
    environment = "ci"
    component   = "buildkite"
    cluster     = "benchmark"
  }
}

resource "google_secret_manager_secret" "buildkite_agent_token_ci_cluster" {
  project   = var.project_id
  secret_id = "buildkite_agent_token_ci_cluster"
  
  replication {
    auto {}
  }
  
  labels = {
    environment = "ci"
    component   = "buildkite"
    cluster     = "ci"
  }
}

resource "google_secret_manager_secret" "huggingface_token" {
  project   = var.project_id
  secret_id = "huggingface_token"
  
  replication {
    auto {}
  }
  
  labels = {
    environment = "ci"
    component   = "huggingface"
  }
}