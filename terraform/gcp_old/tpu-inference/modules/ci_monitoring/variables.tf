variable "project_id" {
  type        = string
  description = "The GCP project ID where the instance and metrics will live."
}

variable "buildkite_token_value" {
  type        = string
  description = "The Agent Registration Token used to query Buildkite metrics."
}
