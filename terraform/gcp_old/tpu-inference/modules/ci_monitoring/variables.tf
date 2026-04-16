variable "project_id" {
  type        = string
  description = "The GCP project ID where the instance and metrics will live."
}

variable "buildkite_token_value" {
  type        = string
  description = "The Agent Registration Token used to query Buildkite metrics."
}

variable "pipeline_slug" {
  type        = string
  description = "The specific Buildkite pipeline slug to monitor (e.g., tpu-inference-ci)"
}

variable "org_slug" {
  type        = string
  description = "The specific Buildkite org slug to monitor (e.g., tpu-commons)"
}
