variable "project_id" {
  type        = string
  description = "The project the VM runs in."
}

variable "purpose" {
  type        = string
  description = <<-EOT
    What this agent publishes, e.g. tpu-inference. Names the VM, its disk, its
    address and the Buildkite agent: vllm-ci-release-<purpose>-<zone>.
  EOT
}

variable "buildkite_queue_name" {
  type        = string
  description = "The TPU Release cluster queue the agent serves."
}

variable "buildkite_token_secret_name" {
  type        = string
  description = <<-EOT
    projects/<project>/secrets/<name> of the TPU Release cluster's agent token.
    The VM reads it at boot with its own service account.
  EOT
}

variable "allowed_pipeline_ids" {
  type        = list(string)
  description = <<-EOT
    UUIDs of the only pipelines whose jobs this agent runs. The pre-bootstrap
    hook rejects every other job before checkout.
  EOT

  validation {
    condition     = length(var.allowed_pipeline_ids) > 0
    error_message = "List at least one pipeline; an empty list would reject every job."
  }
}

variable "machine_type" {
  type = string
}

variable "disk_size" {
  type    = number
  default = 100
}

variable "disk_type" {
  type    = string
  default = "pd-balanced"
}

variable "service_account_email" {
  type        = string
  description = <<-EOT
    The account the VM runs as. Every job on the VM can use it, so it must not
    be one that CI agents share.
  EOT
}
