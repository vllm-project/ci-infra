variable "project_id" {
  type        = string
  description = "The GCP project ID where the instance and metrics will live."
}

variable "buildkite_token_secret_ids" {
  type        = map(string)
  description = "Buildkite org slug => Secret Manager resource name of that org's Agent Registration Token (projects/.../secrets/...). One metrics exporter runs per entry."
}

variable "pipeline_slug" {
  type        = string
  description = "The specific Buildkite pipeline slug to monitor (e.g., tpu-inference-ci)"
}

variable "org_slug" {
  type        = string
  description = "The specific Buildkite org slug to monitor (e.g., tpu-commons)"
}
