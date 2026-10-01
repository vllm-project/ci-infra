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

variable "buildkite_token_secret_name" {
  type        = string
  description = "Secret Manager secret holding the Buildkite agent token, as projects/<project>/secrets/<name>. The VM reads its latest version at boot, so its service account needs secretAccessor on it."
}

variable "huggingface_token_secret_name" {
  type        = string
  description = "Secret Manager secret holding the Hugging Face token, as projects/<project>/secrets/<name>. The VM reads its latest version at boot, so its service account needs secretAccessor on it."
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

variable "vllm_torchtpu_ssh_checkout" {
  type        = bool
  description = <<-DESC
    Check vllm-torchtpu out over SSH with its read-only deploy key instead of
    over HTTPS with the CI bot's GitHub App token. A pre-checkout hook reads the
    key per job from the Buildkite secret VLLM_TORCHTPU_DEPLOY_KEY, whose access
    policy limits it to the pipelines that need it. Pushes still go over HTTPS.
  DESC
  default     = false
}
