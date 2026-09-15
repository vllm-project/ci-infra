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
