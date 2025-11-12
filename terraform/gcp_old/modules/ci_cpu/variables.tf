variable "project_id" {
  default = "cloud-tpu-inference-test"
}

variable "instance_count" {
  default = 8
}

variable "buildkite_agent_token_ci_cluster_name" {
  type = string
  description = "google_secret_manager_secret name for ci cluster agent token"
}
