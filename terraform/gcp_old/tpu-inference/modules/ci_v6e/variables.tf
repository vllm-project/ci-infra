variable "accelerator_type" {
  type        = string
  description = "Accelerator type of TPU"
}

variable "reserved" {
  description = "if use reserved tpu resource"
  type        = bool
  default     = true
}

variable "instance_count" {
  type        = number
  description = "Number of TPU instance"
}

variable "disk_size" {
  type        = number
  description = "The mount disk size"
  default     = 2048
}

variable "buildkite_queue_name" {
  type        = string
  description = "The Buildkite agent queue name that the agents will join."
}

variable "project_id" {
  type        = string
  description = "The project ID for creating TPU agents"
}

variable "project_short_name" {
  type        = string
  description = "Short name for improved readability"
}

variable "buildkite_token_value" {
  type        = string
  description = "Agent token used to connect to Buildkite."
}

variable "huggingface_token_value" {
  type        = string
  description = "Hugging Face token for vLLM model serving usage."
}
