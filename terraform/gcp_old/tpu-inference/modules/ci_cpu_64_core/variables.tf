variable "project_id" {
  type        = string
  description = "The GCP project ID"
}

variable "instance_count" {
  type        = number
  description = "Number of CI CPU VMs to create"
}

variable "machine_type" {
  type        = string
  default     = "n2-standard-64"
  description = "The machine type to use for the build nodes"
}

variable "disk_size" {
  type        = number
  default     = 250
  description = "Size of the boot disk in GB"
}

variable "disk_type" {
  type        = string
  default     = "pd-balanced"
  description = "The GCE disk type"
}

variable "buildkite_queue_name" {
  type        = string
  default     = "cpu_64_core"
  description = "The buildkite queue tag for these agents"
}

variable "buildkite_token_value" {
  type        = string
  description = "Agent token used to connect to Buildkite."
  sensitive   = true
}

variable "huggingface_token_value" {
  type        = string
  description = "Hugging Face token for vLLM model serving usage."
  sensitive   = true
}

variable "resource_suffix" {
  description = "Suffix to append to resource names to avoid collisions across zones"
  type        = string
  default     = ""
}
