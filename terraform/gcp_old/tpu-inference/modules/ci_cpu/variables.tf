variable "project_id" {
  default = "cloud-tpu-inference-test"
}

variable "instance_count" {
  type        = number
  description = "Number of VM instance"
}

variable "buildkite_token_value" {
  type        = string
  description = "Agent token used to connect to Buildkite."
}

variable "huggingface_token_value" {
  type        = string
  description = "Hugging Face token for vLLM model serving usage."
}

