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

variable "buildkite_queue_name" {
  type        = string
  description = "The Buildkite agent queue name that the agents will join."
}

variable "project_id" {
  default = "cloud-tpu-inference-test"
}
