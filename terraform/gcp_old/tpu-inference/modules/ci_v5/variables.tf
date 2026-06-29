variable "project_id" {
  default = "vllm-405802"
}

variable "github_app_secret_name" {
  type        = string
  description = "The Buildkite secret name for the GitHub App PEM key."
  default     = "GITHUB_CI_BOT_PEM"
}

