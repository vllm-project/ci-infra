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

variable "buildkite_token_secret_name" {
  type        = string
  description = "Secret Manager secret holding the Buildkite agent token, as projects/<project>/secrets/<name>. The VM reads its latest version at boot, so its service account needs secretAccessor on it."
}

variable "huggingface_token_secret_name" {
  type        = string
  description = "Secret Manager secret holding the Hugging Face token, as projects/<project>/secrets/<name>. The VM reads its latest version at boot, so its service account needs secretAccessor on it."
}

variable "buildkite_analytics_token_secret_name" {
  type        = string
  description = "Secret Manager secret holding the Buildkite Test Engine token, as projects/<project>/secrets/<name>. The VM reads its latest version at boot, so its service account needs secretAccessor on it."
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

variable "service_account_email" {
  type        = string
  default     = null
  description = "Service account the agent VMs run as. Null keeps the project's default compute service account."
}
