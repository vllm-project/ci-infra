# This project, outside the google.com org, gets v7x only through Compute
# Engine: the Cloud TPU API that ci_v7x uses offers it no tpu7x types. The
# agents join the same queues as the cicd fleet. The CI grants on our
# registries, buckets and BigQuery are made to vllm-ci, so the hosts run as it.
#
# Every bare-metal queue runs here now that the cicd reservation is all in the
# kube v7x lane: tpu7x-8 slices, tpu7x-2s and a tpu7x-16. The multi-host
# scripts find the tpu7x-16's second host through the agent's environment
# hook (modules/ci_v7x_gce) rather than the Cloud TPU API.
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
  # in cloud-ullm-inference-ci-cd/agent_service_accounts.tf.
  buildkite_token_secret_name           = "projects/${var.secret_project_id}/secrets/vllm_buildkite_agent_token"
  buildkite_analytics_token_secret_name = "projects/${var.secret_project_id}/secrets/vllm_buildkite_analytics_token"
  huggingface_token_secret_name         = "projects/${var.secret_project_id}/secrets/vllm_buildkite_hf_token"

  vllm_torchtpu_ssh_checkout = true
}

# Eight tpu7x-2s for the PR builds left on bare metal, which start their
# single-chip steps all at once. The rest of the sixteen these began as went
# back to the reservation. With every tpu-inference PR on bare metal (10-15),
# twelve were 32% busy and a PR build filled all of them; replaying those
# steps (p50 6, max 29 minutes) on eight put the wait at p90 4 and max 10
# minutes, against 0 and 3 on twelve.
module "ci_v7x_2" {
  source = "../modules/ci_v7x_gce"
  providers = {
    google-beta = google-beta.us-central1-c
  }

  slice_count                           = 8
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

# One tpu7x-16 (two tpu7x-standard-4t hosts in one ICI domain) for the
# multi-host steps of builds that stay on bare metal, now that the cicd
# reservation is all in the kube v7x lane. Its 8 chips come from the
# reservation's free capacity; with eight tpu7x-2s returned, the fleet holds 48
# of it.
module "ci_v7x_16" {
  source = "../modules/ci_v7x_gce"
  providers = {
    google-beta = google-beta.us-central1-c
  }

  slice_count                           = 1
  hosts_per_slice                       = 2
  topology                              = "2x2x2"
  buildkite_queue_name                  = "tpu_v7x_16_queue"
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
