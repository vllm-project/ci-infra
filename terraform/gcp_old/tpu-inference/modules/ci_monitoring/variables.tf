variable "project_id" {
  type        = string
  description = "The GCP project ID where the instance and metrics will live."
}

variable "buildkite_token_secret_ids" {
  type        = map(string)
  description = "Buildkite org slug => Secret Manager resource name of that org's Agent Registration Token (projects/.../secrets/...). One metrics exporter runs per entry."
}

variable "bq_puller_pipeline_slugs" {
  type        = list(string)
  description = "Buildkite pipeline slugs the BigQuery puller reads. Every slug is polled in every org in bq_puller_orgs; a slug missing from an org is skipped."
}

variable "bq_puller_orgs" {
  type        = map(string)
  description = "Buildkite org slug => name of a Secret Manager secret in project_id holding a REST API token for that org. A token is scoped to one org, so each entry needs its own. Rows are stamped with org_slug."
}
