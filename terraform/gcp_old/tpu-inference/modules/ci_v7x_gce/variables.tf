variable "accelerator_type" {
  type        = string
  description = "Slice shape, named like the Cloud TPU accelerator types: tpu7x-8 is one host, tpu7x-16 is two."

  validation {
    condition     = contains(["tpu7x-8", "tpu7x-16"], var.accelerator_type)
    error_message = "Supported shapes are tpu7x-8 and tpu7x-16."
  }
}

variable "instance_count" {
  type        = number
  description = "Number of slices. Each slice runs one Buildkite agent, on its first host."
}

variable "buildkite_queue_name" {
  type        = string
  description = "The Buildkite agent queue name that the agents will join."
}

variable "boot_disk_size" {
  type        = number
  description = "Boot disk size in GB. /mnt/disks/persist, the Docker data root and the model caches all live on it."
  default     = 1024
}

variable "project_id" {
  type        = string
  description = "The project ID for creating TPU agents"
}

variable "project_short_name" {
  type        = string
  description = "Short name for improved readability"
}

variable "service_account_email" {
  type        = string
  description = "Service account the hosts run as. A Compute Engine instance gets no account unless one is named."
}

variable "reservation_name" {
  type        = string
  description = "The specific Compute Engine reservation the hosts are bound to."
}

variable "subnetwork" {
  type        = string
  description = "Subnetwork for the hosts. They get no external IP, so it needs Cloud NAT and Private Google Access."
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
