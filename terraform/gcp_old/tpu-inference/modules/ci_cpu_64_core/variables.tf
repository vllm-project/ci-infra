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
  description = <<-DESC
    Legacy: suffixes only the regional static address. Kept so the original
    us-central1-c fleet holds its address names. Prefer purpose.
  DESC
  type        = string
  default     = ""
}

variable "purpose" {
  description = <<-DESC
    What this fleet is for. When set, every name this module creates -- the VM,
    its boot disk, the static address, and the Buildkite agent -- becomes
    vllm-ci-cpu-64-core-<purpose>-<zone>-<index>, and resource_suffix is
    ignored. The zone is read from the provider, so it is never passed in.
    Empty keeps the original unsuffixed names.
  DESC
  type        = string
  default     = ""
}
variable "github_app_secret_name" {
  type        = string
  description = "The Buildkite secret name for the GitHub App PEM key."
  default     = "GITHUB_CI_BOT_PEM"
}
