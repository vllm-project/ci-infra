variable "project_id" {
  default = "cloud-tpu-inference-test"
}

variable "buildkite_benchmark_agent_token_name" {
  type = string
  description = "google_secret_manager_secret name for benchmark agent token"
}

variable "huggingface_token_name" {
  type = string
  description = "google_secret_manager_secret name for huggingface token"
}
