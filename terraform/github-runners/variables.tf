variable "aws_region" {
  description = "AWS region to run the GitHub Actions runners in."
  type        = string
  default     = "us-west-2"
}

variable "prefix" {
  description = "Prefix for all resources created by the runner module."
  type        = string
  default     = "vllm-gha"
}

variable "module_version" {
  description = "Pinned version of philips-labs/github-runner/aws. Keep in sync with the module/submodule version literals and the lambda tags."
  type        = string
  default     = "6.1.0"
}

variable "enable_organization_runners" {
  description = "Register runners at the org level (true) so they can serve a runner group, vs repo level (false)."
  type        = bool
  default     = true
}

variable "runner_group_name" {
  description = "Org runner group the runners join. Workflows target it with `runs-on: {group: <name>}`. The group must already exist."
  type        = string
  default     = "vllm-runners"
}

variable "runner_extra_labels" {
  description = "Extra labels applied to the runners (in addition to self-hosted/linux/x64)."
  type        = list(string)
  default     = ["vllm-runners"]
}

variable "instance_types" {
  description = "Candidate EC2 instance types (x86_64). First match with capacity wins."
  type        = list(string)
  default     = ["m7i.large", "m6i.large", "c7i.large"]
}

variable "instance_target_capacity_type" {
  description = "\"on-demand\" (reliable, recommended for a gating check) or \"spot\" (cheaper)."
  type        = string
  default     = "on-demand"
}

variable "runners_maximum_count" {
  description = "Max concurrent runner instances (autoscaling upper bound)."
  type        = number
  default     = 20
}

# SSM SecureString/String parameters holding the GitHub App credentials.
# Create these manually before the first apply (see README.md).
variable "github_app_id_ssm_name" {
  type    = string
  default = "/github-runners/app-id"
}

variable "github_app_key_base64_ssm_name" {
  type    = string
  default = "/github-runners/key-base64"
}

variable "github_app_webhook_secret_ssm_name" {
  type    = string
  default = "/github-runners/webhook-secret"
}
