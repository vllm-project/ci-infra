# This project, outside the google.com org, gets v7x only through Compute
# Engine: the Cloud TPU API that ci_v7x uses offers it no tpu7x types. The
# agents join the same queues as the cicd fleet. The CI grants on our
# registries, buckets, BigQuery and Spanner are made to vllm-ci, so the hosts
# run as it.
#
# Only single-host tpu7x-8 slices run here, standing in for tpu7x-8 nodes
# released from the cicd fleet. Multi-host queues stay on the Cloud TPU API in
# cloud-ullm-inference-ci-cd, where the scripts' multi-host path already works.
# The reservation is deleted on 2027-01-22, so these agents need another home
# by then.
locals {
  reservation_name      = "ghostfish-9mpeile911sjq"
  service_account_email = "vllm-ci@${var.project_id}.iam.gserviceaccount.com"
}

module "ci_v7x_8" {
  source = "../modules/ci_v7x_gce"
  providers = {
    google-beta = google-beta.us-central1-c
  }

  slice_count           = 8
  hosts_per_slice       = 1
  topology              = null
  buildkite_queue_name  = "tpu_v7x_8_queue"
  boot_disk_size        = 4096 # the cicd v7x-8 agents' data disk size
  project_id            = var.project_id
  project_short_name    = var.project_short_name
  service_account_email = local.service_account_email
  reservation_name      = local.reservation_name
  subnetwork            = google_compute_subnetwork.ci.id
  # The hosts read these at boot. Their vllm-ci account's access is granted
  # in cloud-ullm-inference-ci-cd/secrets.tf, next to the secrets.
  buildkite_token_secret_name           = "projects/${var.secret_project_id}/secrets/vllm_buildkite_agent_token"
  buildkite_analytics_token_secret_name = "projects/${var.secret_project_id}/secrets/vllm_buildkite_analytics_token"
  huggingface_token_secret_name         = "projects/${var.secret_project_id}/secrets/vllm_buildkite_hf_token"

  vllm_torchtpu_ssh_checkout = true
}
