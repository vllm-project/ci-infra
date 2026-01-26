variable "project_id" {
  default = "cloud-tpu-inference-test"
}

variable "buildkite_token_value" {
  type        = string
  description = "Agent token used to connect to Buildkite."
}

variable "huggingface_token_value" {
  type        = string
  description = "Hugging Face token for vLLM model serving usage."
}

variable "buildkite_analytics_token_value" {
  type        = string
  description = "Analytics token used to push test data to Buildkite."
}