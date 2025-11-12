variable "project_id" {
  default = "vllm-405802"
}

variable "buildkite_agent_token_ci_cluster_name" {
  type = string
  description = "google_secret_manager_secret name for ci cluster agent token"
}

variable "huggingface_token_name" {
  type = string
  description = "google_secret_manager_secret name for huggingface token"
}
