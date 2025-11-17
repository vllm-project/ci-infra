variable "project_id" {
  default = "cloud-tpu-inference-test"
}

variable "instance_count" {
  default = 8
}

variable "buildkite_ci_agent_token_name" {
  type = string
  description = "google_secret_manager_secret name for ci agent token"
}
