variable "slice_count" {
  type        = number
  description = "Number of slices. Each slice runs one Buildkite agent, on its first host."
}

variable "hosts_per_slice" {
  type        = number
  description = "tpu7x-standard-4t hosts (4 chips each) per slice: 1 for a tpu7x-8 slice, 2 for tpu7x-16."
}

variable "topology" {
  type        = string
  description = "Chip topology of a multi-host slice, e.g. 2x2x2 for two hosts. Null for a single host, which takes no workload policy."
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
