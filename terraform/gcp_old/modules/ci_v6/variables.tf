variable "project_id" {
  default = "cloud-tpu-inference-test"
}

variable "buildkite_agent_token_ci_cluster_name" {
  type = string
  description = "google_secret_manager_secret name for ci cluster agent token"
}

variable "huggingface_token_name" {
  type = string
  description = "google_secret_manager_secret name for huggingface token"
}

variable "ci_v6_instance_count" {
  type = number
  description = "number of instances to spawn for ci_v6"
}

variable "ci_v6_disk_size" {
  type = number
  description = "disk size for ci_v6"
}
