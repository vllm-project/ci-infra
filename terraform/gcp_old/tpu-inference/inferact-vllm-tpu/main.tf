# This project, outside the google.com org, gets v7x only through Compute
# Engine: the Cloud TPU API that ci_v7x uses offers it no tpu7x types. The
# agents join the same queues as the cicd fleet. The CI grants on our
# registries, buckets, BigQuery and Spanner are made to vllm-ci, so the hosts
# run as it.
#
# Only single hosts run here: tpu7x-8 slices and tpu7x-2s, the bare-metal
# fleet's single-host queues once the cicd reservation went to the kube v7x
# lane. Multi-host queues stay on the Cloud TPU API in
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

# Sixteen of the fleet's 48 chips as tpu7x-2s: the PR builds left on bare metal
# start many single-chip steps at once (tpu-inference's unit tests hold one
# chip for up to 90 minutes), and below about twelve agents they queue. A
# replay of 10-07..10-08 put 16 tpu7x-2 + 8 tpu7x-8 at p90 waits of 8 and 34
# minutes.
module "ci_v7x_2" {
  source = "../modules/ci_v7x_gce"
  providers = {
    google-beta = google-beta.us-central1-c
  }

  slice_count                           = 16
  hosts_per_slice                       = 1
  chips_per_host                        = 1
  topology                              = null
  buildkite_queue_name                  = "tpu_v7x_2_queue"
  boot_disk_size                        = 2048 # the cicd v7x-2 agents' data disk size
  project_id                            = var.project_id
  project_short_name                    = var.project_short_name
  service_account_email                 = local.service_account_email
  reservation_name                      = local.reservation_name
  subnetwork                            = google_compute_subnetwork.ci.id
  buildkite_token_secret_name           = "projects/${var.secret_project_id}/secrets/vllm_buildkite_agent_token"
  buildkite_analytics_token_secret_name = "projects/${var.secret_project_id}/secrets/vllm_buildkite_analytics_token"
  huggingface_token_secret_name         = "projects/${var.secret_project_id}/secrets/vllm_buildkite_hf_token"

  vllm_torchtpu_ssh_checkout = true
}
