variable "project_id" {
  type        = string
  description = "The GCP project ID where the instance and metrics will live."
}

variable "buildkite_token_secret_ids" {
  type        = map(string)
  description = "Buildkite org slug => Secret Manager resource name of that org's Agent Registration Token (projects/.../secrets/...). One metrics exporter runs per entry."
}

variable "bq_puller_pipeline_slug" {
  type        = string
  description = "Buildkite pipeline slug the BigQuery puller reads. The same slug is used in every org in bq_puller_orgs."
}

variable "bq_puller_orgs" {
  type        = map(string)
  description = "Buildkite org slug => name of a Secret Manager secret in project_id holding a REST API token for that org. A token is scoped to one org, so each entry needs its own. The puller polls every entry and stamps each BigQuery row with its org."
}
