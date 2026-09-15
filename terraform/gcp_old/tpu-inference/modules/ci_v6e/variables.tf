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

variable "purpose" {
  type        = string
  description = <<-DESC
    What this fleet is for. When set, every name this module creates -- the TPU
    VM, its label, its disk, and the Buildkite agent -- becomes
    <accelerator_type>-ci-<purpose>-<index>-<project_short_name>-<zone>. The
    zone is read from the provider, so it is never passed in. Empty keeps the
    original unsuffixed names.
  DESC
  default     = ""
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

variable "buildkite_analytics_token_value" {
  type        = string
  description = "Analytics token used to push test data to Buildkite."
}

variable "github_app_secret_name" {
  type        = string
  description = "The Buildkite secret name for the GitHub App PEM key."
  default     = "GITHUB_CI_BOT_PEM"
}
