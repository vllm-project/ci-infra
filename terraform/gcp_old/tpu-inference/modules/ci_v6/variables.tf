variable "project_id" {
  default = "cloud-tpu-inference-test"
}

variable "buildkite_token_value" {
  type        = string
  sensitive   = true
  description = "Agent token used to connect to Buildkite."
}

variable "huggingface_token_value" {
  type        = string
  sensitive   = true
  description = "Hugging Face token for vLLM model serving usage."
}
