data "google_secret_manager_secret_version" "buildkite_agent_token_vllm" {
  secret  = "projects/${var.secret_project_id}/secrets/vllm_buildkite_agent_token"
  version = "latest"
}

data "google_secret_manager_secret_version" "buildkite_analytics_token_vllm" {
  secret  = "projects/${var.secret_project_id}/secrets/vllm_buildkite_analytics_token"
  version = "latest"
}

data "google_secret_manager_secret_version" "huggingface_token" {
  secret  = "projects/${var.secret_project_id}/secrets/tpu_commons_buildkite_hf_token"
  version = "latest"
}

# This project, outside the google.com org, gets v7x only through Compute
# Engine: the Cloud TPU API that ci_v7x uses offers it no tpu7x types. The
# agents join the same queues as the cicd fleet. The CI grants on our
# registries, buckets, BigQuery and Spanner are made to vllm-ci, so the hosts
# run as it.
locals {
  reservation_name      = "ghostfish-9mpeile911sjq"
  service_account_email = "vllm-ci@${var.project_id}.iam.gserviceaccount.com"
}

module "ci_v7x_8" {
  source = "../modules/ci_v7x_gce"
  providers = {
    google-beta = google-beta.us-central1-c
  }

  accelerator_type                = "tpu7x-8"
  instance_count                  = 1
  buildkite_queue_name            = "tpu_v7x_8_queue"
  boot_disk_size                  = 4096 # the cicd v7x-8 agents' data disk size
  project_id                      = var.project_id
  project_short_name              = var.project_short_name
  service_account_email           = local.service_account_email
  reservation_name                = local.reservation_name
  subnetwork                      = google_compute_subnetwork.ci.id
  buildkite_token_value           = data.google_secret_manager_secret_version.buildkite_agent_token_vllm.secret_data
  buildkite_analytics_token_value = data.google_secret_manager_secret_version.buildkite_analytics_token_vllm.secret_data
  huggingface_token_value         = data.google_secret_manager_secret_version.huggingface_token.secret_data
}

# 2 hosts x 4 chips (2x2x2), one agent on the first host.
module "ci_v7x_16" {
  source = "../modules/ci_v7x_gce"
  providers = {
    google-beta = google-beta.us-central1-c
  }

  accelerator_type                = "tpu7x-16"
  instance_count                  = 1
  buildkite_queue_name            = "tpu_v7x_16_queue"
  project_id                      = var.project_id
  project_short_name              = var.project_short_name
  service_account_email           = local.service_account_email
  reservation_name                = local.reservation_name
  subnetwork                      = google_compute_subnetwork.ci.id
  buildkite_token_value           = data.google_secret_manager_secret_version.buildkite_agent_token_vllm.secret_data
  buildkite_analytics_token_value = data.google_secret_manager_secret_version.buildkite_analytics_token_vllm.secret_data
  huggingface_token_value         = data.google_secret_manager_secret_version.huggingface_token.secret_data
}
