resource "google_secret_manager_secret" "buildkite_agent_token_benchmark_cluster" {
  project   = var.project_id
  secret_id = "bm-agent-hf-token"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "buildkite_agent_token_ci_cluster" {
  project   = var.project_id
  secret_id = "tpu_commons_buildkite_agent_token"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "buildkite_analytics_token_ci_cluster" {
  project   = var.project_id
  secret_id = "tpu_commons_buildkite_analytics_token"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "huggingface_token" {
  project   = var.project_id
  secret_id = "tpu_commons_buildkite_hf_token"

  replication {
    auto {}
  }
}