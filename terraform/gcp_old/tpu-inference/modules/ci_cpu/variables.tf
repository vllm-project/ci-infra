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

variable "purpose" {
  type        = string
  description = <<-DESC
    What this fleet is for. When set, every name this module creates -- the VM,
    its boot disk, the static address, and the Buildkite agent -- becomes
    vllm-ci-cpu-<purpose>-<zone>-<index>. The zone is read from the provider, so
    it is never passed in. Empty keeps the original unsuffixed names.
  DESC
  default     = ""
}

variable "github_app_secret_name" {
  type        = string
  description = "The Buildkite secret name for the GitHub App PEM key."
  default     = "GITHUB_CI_BOT_PEM"
}

variable "agent_tags" {
  type        = string
  description = "Buildkite agent tags. The queue named here must exist in the agent's cluster."
  default     = "queue=cpu"
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
