variable "project_id" {
  type        = string
  description = "The GCP project ID where the instance and metrics will live."
}

variable "buildkite_token_secret_id" {
  type        = string
  description = "The Secret Manager ID for the Agent Registration Token (e.g., projects/.../secrets/...)"
}

variable "pipeline_slug" {
  type        = string
  description = "The specific Buildkite pipeline slug to monitor (e.g., tpu-inference-ci)"
}

variable "org_slug" {
  type        = string
  description = "The specific Buildkite org slug to monitor (e.g., tpu-commons)"
}
