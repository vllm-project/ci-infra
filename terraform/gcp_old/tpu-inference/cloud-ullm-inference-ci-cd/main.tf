# Secrets the agent VMs read at boot through modules/shared/read-secret.sh.
# They are created, filled and shared with the VMs' service accounts outside
# Terraform, with gcloud, so no token is in instance metadata or state.
locals {
  buildkite_token_secret_name           = "projects/${var.project_id}/secrets/vllm_buildkite_agent_token"
  huggingface_token_secret_name         = "projects/${var.project_id}/secrets/vllm_buildkite_hf_token"
  buildkite_analytics_token_secret_name = "projects/${var.project_id}/secrets/vllm_buildkite_analytics_token"
}

module "ci_v6e_1_vllm" {
  source = "../modules/ci_v6e"
  providers = {
    google-beta = google-beta.us-east5-a
  }

  accelerator_type                      = "v6e-1"
  reserved                              = true
  purpose                               = "vllm"
  instance_count                        = 30
  disk_size                             = 1024
  buildkite_queue_name                  = "tpu_v6e_queue"
  project_id                            = var.project_id
  project_short_name                    = var.project_short_name
  buildkite_token_secret_name           = local.buildkite_token_secret_name
  buildkite_analytics_token_secret_name = local.buildkite_analytics_token_secret_name
  huggingface_token_secret_name         = local.huggingface_token_secret_name

  service_account_email = google_service_account.ci_agent_tpu.email
}

module "ci_v6e_8_vllm" {
  source = "../modules/ci_v6e"
  providers = {
    google-beta = google-beta.us-east5-a
  }

  accelerator_type                      = "v6e-8"
  reserved                              = true
  purpose                               = "vllm"
  instance_count                        = 9
  disk_size                             = 4096
  buildkite_queue_name                  = "tpu_v6e_8_queue"
  project_id                            = var.project_id
  project_short_name                    = var.project_short_name
  buildkite_token_secret_name           = local.buildkite_token_secret_name
  buildkite_analytics_token_secret_name = local.buildkite_analytics_token_secret_name
  huggingface_token_secret_name         = local.huggingface_token_secret_name

  service_account_email = google_service_account.ci_agent_tpu.email
}


module "ci_v7x_2" {
  source = "../modules/ci_v7x"
  providers = {
    google-beta = google-beta.us-central1-c
  }

  accelerator_type                      = "tpu7x-2"
  reserved                              = true
  instance_count                        = 16
  buildkite_queue_name                  = "tpu_v7x_2_queue"
  disk_size                             = 2048
  project_id                            = var.project_id
  project_short_name                    = var.project_short_name
  buildkite_token_secret_name           = local.buildkite_token_secret_name
  buildkite_analytics_token_secret_name = local.buildkite_analytics_token_secret_name
  huggingface_token_secret_name         = local.huggingface_token_secret_name

  vllm_torchtpu_ssh_checkout = true

  service_account_email = google_service_account.ci_agent_tpu.email
}

# Twelve more tpu7x-8 agents serve the same queue from inferact-vllm-tpu (see
# that env), so this fleet runs six and the other 48 chips of the reservation
# are free for other work.
module "ci_v7x_8" {
  source = "../modules/ci_v7x"
  providers = {
    google-beta = google-beta.us-central1-c
  }

  accelerator_type                      = "tpu7x-8"
  reserved                              = true
  instance_count                        = 6
  buildkite_queue_name                  = "tpu_v7x_8_queue"
  disk_size                             = 4096
  project_id                            = var.project_id
  project_short_name                    = var.project_short_name
  buildkite_token_secret_name           = local.buildkite_token_secret_name
  buildkite_analytics_token_secret_name = local.buildkite_analytics_token_secret_name
  huggingface_token_secret_name         = local.huggingface_token_secret_name

  vllm_torchtpu_ssh_checkout = true

  service_account_email = google_service_account.ci_agent_tpu.email
}

module "ci_v7x_16" {
  source = "../modules/ci_v7x"
  providers = {
    google-beta = google-beta.us-central1-c
  }

  accelerator_type                      = "tpu7x-16"
  reserved                              = true
  instance_count                        = 2
  buildkite_queue_name                  = "tpu_v7x_16_queue"
  project_id                            = var.project_id
  project_short_name                    = var.project_short_name
  buildkite_token_secret_name           = local.buildkite_token_secret_name
  buildkite_analytics_token_secret_name = local.buildkite_analytics_token_secret_name
  huggingface_token_secret_name         = local.huggingface_token_secret_name
  # disk_size defaults to 0, disable attached disk

  vllm_torchtpu_ssh_checkout = true

  service_account_email = google_service_account.ci_agent_tpu.email
}

# 4 hosts x 4 chips (2x2x4). Same multi-host shape as tpu7x-16: the agent runs
# on worker 0 and fans out to the other hosts over ssh. No data disk: a
# READ_WRITE disk cannot be shared by four hosts, and the multi-host jobs
# stream weights from GCS instead. Replaces the hand-built ranlihao-v7x-32 slice.
#
# instance_count is 0: the 16 chips a tpu7x-32 would need are part of the
# kube v7x lane's 72 (k8s/prod.auto.tfvars), which runs the tpu7x-32 benchmark
# case on its 2x2x4 slices. Raising this takes them out of that lane.
module "ci_v7x_32" {
  source = "../modules/ci_v7x"
  providers = {
    google-beta = google-beta.us-central1-c
  }

  accelerator_type                      = "tpu7x-32"
  reserved                              = true
  instance_count                        = 0
  buildkite_queue_name                  = "tpu_v7x_32_queue"
  project_id                            = var.project_id
  project_short_name                    = var.project_short_name
  buildkite_token_secret_name           = local.buildkite_token_secret_name
  buildkite_analytics_token_secret_name = local.buildkite_analytics_token_secret_name
  huggingface_token_secret_name         = local.huggingface_token_secret_name

  vllm_torchtpu_ssh_checkout = true

  service_account_email = google_service_account.ci_agent_tpu.email
}

# purpose puts these on the self-describing naming scheme,
# vllm-ci-<kind>-<purpose>-<zone>-<index>, shared by the VM, its disk, its
# address, and the Buildkite agent.
module "ci_cpu_vllm_zone_b" {
  source = "../modules/ci_cpu"
  providers = {
    google-beta = google-beta.us-central1-b
  }
  purpose                       = "vllm"
  project_id                    = var.project_id
  instance_count                = 8
  buildkite_token_secret_name   = local.buildkite_token_secret_name
  huggingface_token_secret_name = local.huggingface_token_secret_name

  vllm_torchtpu_ssh_checkout = true

  service_account_email = google_service_account.ci_agent_cpu.email
}

module "ci_cpu_64_core_vllm_zone_b" {
  source = "../modules/ci_cpu_64_core"
  providers = {
    google-beta = google-beta.us-central1-b
  }
  purpose              = "vllm"
  project_id           = var.project_id
  instance_count       = 4
  machine_type         = "n2d-standard-64"
  disk_size            = 250
  disk_type            = "pd-balanced"
  buildkite_queue_name = "cpu_64_core"

  buildkite_token_secret_name   = local.buildkite_token_secret_name
  huggingface_token_secret_name = local.huggingface_token_secret_name

  vllm_torchtpu_ssh_checkout = true

  service_account_email = google_service_account.ci_agent_cpu.email
}

module "ci_cpu_64_core_vllm_zone_f" {
  source = "../modules/ci_cpu_64_core"
  providers = {
    google-beta = google-beta.us-central1-f
  }
  purpose              = "vllm"
  project_id           = var.project_id
  instance_count       = 4
  machine_type         = "n2d-standard-64"
  disk_size            = 250
  disk_type            = "pd-balanced"
  buildkite_queue_name = "cpu_64_core"

  buildkite_token_secret_name   = local.buildkite_token_secret_name
  huggingface_token_secret_name = local.huggingface_token_secret_name

  vllm_torchtpu_ssh_checkout = true

  service_account_email = google_service_account.ci_agent_cpu.email
}

module "ci_monitoring" {
  source = "../modules/ci_monitoring"
  providers = {
    google-beta = google-beta.us-central1-b
  }

  project_id = var.project_id
  # Every TPU pipeline, bare metal and kube, so both lanes land in one table
  # with one shape and the bare baseline outlives the bare queues.
  bq_puller_pipeline_slugs = [
    # Bare metal.
    "tpu-inference-ci",
    "tpu-inference-benchmark",
    "tpu-inference-dev",
    "tpu-inference-disagg-gke-benchmark",
    "tpu-inference-kernel-tuning",
    "tpu-tokamax-integration",
    "tpu-vllm-integration",
    "vllm-torchtpu-ci",
    "vllm-torchtpu-dev",
    "vllm-torchtpu-image",
    "vllm-torchtpu-integration",
    "vllm-torchtpu-nightly-image",
    "vllm-torchtpu-pd-disagg-pipeline",
    "vllm-torchtpu-torchtpu-nightly",
    # Kube.
    "tpu-inference-benchmark-kube",
    "tpu-inference-pd-disagg-kube",
    "tpu-inference-pipeline-features-kube",
    "tpu-inference-pipeline-jax-kube",
    "tpu-inference-pipeline-models-kube",
    "tpu-inference-pipeline-parallelism-kube",
    "tpu-inference-pipeline-rl-kube",
    "vllm-torchtpu-pd-disagg-kube",
    "vllm-torchtpu-pipeline-integration-kube",
    "vllm-torchtpu-pipeline-perf-kube",
    "vllm-torchtpu-pipeline-tests-kube",
  ]

  buildkite_token_secret_ids = {
    "vllm" = local.buildkite_token_secret_name
  }

  bq_puller_orgs = {
    "vllm" = "vllm_org_buildkite_rest_api_token"
  }
}

module "ci_cache_storage" {
  source = "../modules/ci_cache_storage"

  project_id         = var.project_id
  bucket_name        = "ullm-ci-cache"
  cache_zones        = ["us-central1-b", "us-central1-c"]
  lifecycle_age_days = 4
}
