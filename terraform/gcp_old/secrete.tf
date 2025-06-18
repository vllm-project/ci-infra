resource "google_secret_manager_secret" "buildkite_agent_token_benchmark_cluster" {
  project   = var.project_id
  secret_id = "buildkite-agent-token-benchmark-cluster"
  
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
  secret_id = "buildkite-agent-token-ci-cluster"
  
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
  secret_id = "huggingface-token"
  
  replication {
    auto {}
  }
  
  labels = {
    environment = "ci"
    component   = "huggingface"
  }
}