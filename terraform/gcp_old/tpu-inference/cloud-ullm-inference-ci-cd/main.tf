data "google_secret_manager_secret_version" "buildkite_agent_token_ci_cluster" {
  secret  = "projects/${var.secret_project_id}/secrets/tpu_commons_buildkite_agent_token"
  version = "latest"
}

data "google_secret_manager_secret_version" "buildkite_agent_token_vllm" {
  secret  = "projects/${var.secret_project_id}/secrets/vllm_buildkite_agent_token"
  version = "latest"
}

data "google_secret_manager_secret_version" "buildkite_analytics_token_ci_cluster" {
  secret  = "projects/${var.secret_project_id}/secrets/tpu_commons_buildkite_analytics_token"
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


module "ci_v6e_1" {
  source = "../modules/ci_v6e"
  providers = {
    google-beta = google-beta.us-east5-a
  }

  accelerator_type                = "v6e-1"
  reserved                        = true
  instance_count                  = 20
  disk_size                       = 1024
  buildkite_queue_name            = "tpu_v6e_queue"
  project_id                      = var.project_id
  project_short_name              = var.project_short_name
  buildkite_token_value           = data.google_secret_manager_secret_version.buildkite_agent_token_ci_cluster.secret_data
  buildkite_analytics_token_value = data.google_secret_manager_secret_version.buildkite_analytics_token_ci_cluster.secret_data
  huggingface_token_value         = data.google_secret_manager_secret_version.huggingface_token.secret_data
}

module "ci_v6e_8" {
  source = "../modules/ci_v6e"
  providers = {
    google-beta = google-beta.us-east5-a
  }

  accelerator_type                = "v6e-8"
  reserved                        = true
  instance_count                  = 7
  disk_size                       = 4096
  buildkite_queue_name            = "tpu_v6e_8_queue"
  project_id                      = var.project_id
  project_short_name              = var.project_short_name
  buildkite_token_value           = data.google_secret_manager_secret_version.buildkite_agent_token_ci_cluster.secret_data
  buildkite_analytics_token_value = data.google_secret_manager_secret_version.buildkite_analytics_token_ci_cluster.secret_data
  huggingface_token_value         = data.google_secret_manager_secret_version.huggingface_token.secret_data
}

module "ci_v6e_1_vllm" {
  source = "../modules/ci_v6e"
  providers = {
    google-beta = google-beta.us-east5-a
  }

  accelerator_type                = "v6e-1"
  reserved                        = true
  purpose                         = "vllm"
  instance_count                  = 10
  disk_size                       = 1024
  buildkite_queue_name            = "tpu_v6e_queue"
  project_id                      = var.project_id
  project_short_name              = var.project_short_name
  buildkite_token_value           = data.google_secret_manager_secret_version.buildkite_agent_token_vllm.secret_data
  buildkite_analytics_token_value = data.google_secret_manager_secret_version.buildkite_analytics_token_vllm.secret_data
  huggingface_token_value         = data.google_secret_manager_secret_version.huggingface_token.secret_data
}

module "ci_v6e_8_vllm" {
  source = "../modules/ci_v6e"
  providers = {
    google-beta = google-beta.us-east5-a
  }

  accelerator_type                = "v6e-8"
  reserved                        = true
  purpose                         = "vllm"
  instance_count                  = 2
  disk_size                       = 4096
  buildkite_queue_name            = "tpu_v6e_8_queue"
  project_id                      = var.project_id
  project_short_name              = var.project_short_name
  buildkite_token_value           = data.google_secret_manager_secret_version.buildkite_agent_token_vllm.secret_data
  buildkite_analytics_token_value = data.google_secret_manager_secret_version.buildkite_analytics_token_vllm.secret_data
  huggingface_token_value         = data.google_secret_manager_secret_version.huggingface_token.secret_data
}


module "ci_v7x_2" {
  source = "../modules/ci_v7x"
  providers = {
    google-beta = google-beta.us-central1-c
  }

  accelerator_type                = "tpu7x-2"
  reserved                        = true
  instance_count                  = 16
  buildkite_queue_name            = "tpu_v7x_2_queue"
  disk_size                       = 2048
  project_id                      = var.project_id
  project_short_name              = var.project_short_name
  buildkite_token_value           = data.google_secret_manager_secret_version.buildkite_agent_token_vllm.secret_data
  buildkite_analytics_token_value = data.google_secret_manager_secret_version.buildkite_analytics_token_vllm.secret_data
  huggingface_token_value         = data.google_secret_manager_secret_version.huggingface_token.secret_data
}

module "ci_v7x_8" {
  source = "../modules/ci_v7x"
  providers = {
    google-beta = google-beta.us-central1-c
  }

  accelerator_type                = "tpu7x-8"
  reserved                        = true
  instance_count                  = 18
  buildkite_queue_name            = "tpu_v7x_8_queue"
  disk_size                       = 4096
  project_id                      = var.project_id
  project_short_name              = var.project_short_name
  buildkite_token_value           = data.google_secret_manager_secret_version.buildkite_agent_token_vllm.secret_data
  buildkite_analytics_token_value = data.google_secret_manager_secret_version.buildkite_analytics_token_vllm.secret_data
  huggingface_token_value         = data.google_secret_manager_secret_version.huggingface_token.secret_data
}

module "ci_v7x_16" {
  source = "../modules/ci_v7x"
  providers = {
    google-beta = google-beta.us-central1-c
  }

  accelerator_type                = "tpu7x-16"
  reserved                        = true
  instance_count                  = 2
  buildkite_queue_name            = "tpu_v7x_16_queue"
  project_id                      = var.project_id
  project_short_name              = var.project_short_name
  buildkite_token_value           = data.google_secret_manager_secret_version.buildkite_agent_token_vllm.secret_data
  buildkite_analytics_token_value = data.google_secret_manager_secret_version.buildkite_analytics_token_vllm.secret_data
  huggingface_token_value         = data.google_secret_manager_secret_version.huggingface_token.secret_data
  # disk_size defaults to 0, disable attached disk
}

module "ci_cpu_64_core_zone_c" {
  source = "../modules/ci_cpu_64_core"
  providers = {
    google-beta = google-beta.us-central1-c
  }
  resource_suffix      = "-zone-c"
  project_id           = var.project_id
  instance_count       = 8
  machine_type         = "n2d-standard-64"
  disk_size            = 250
  disk_type            = "pd-balanced"
  buildkite_queue_name = "cpu_64_core"

  buildkite_token_value   = data.google_secret_manager_secret_version.buildkite_agent_token_ci_cluster.secret_data
  huggingface_token_value = data.google_secret_manager_secret_version.huggingface_token.secret_data
}

# purpose puts these on the self-describing naming scheme,
# vllm-ci-<kind>-<purpose>-<zone>-<index>, shared by the VM, its disk, its
# address, and the Buildkite agent.
module "ci_cpu_vllm_zone_b" {
  source = "../modules/ci_cpu"
  providers = {
    google-beta = google-beta.us-central1-b
  }
  purpose                 = "vllm"
  project_id              = var.project_id
  instance_count          = 8
  buildkite_token_value   = data.google_secret_manager_secret_version.buildkite_agent_token_vllm.secret_data
  huggingface_token_value = data.google_secret_manager_secret_version.huggingface_token.secret_data
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

  buildkite_token_value   = data.google_secret_manager_secret_version.buildkite_agent_token_vllm.secret_data
  huggingface_token_value = data.google_secret_manager_secret_version.huggingface_token.secret_data
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

  buildkite_token_value   = data.google_secret_manager_secret_version.buildkite_agent_token_vllm.secret_data
  huggingface_token_value = data.google_secret_manager_secret_version.huggingface_token.secret_data
}

module "ci_monitoring" {
  source = "../modules/ci_monitoring"
  providers = {
    google-beta = google-beta.us-central1-b
  }

  project_id               = var.project_id
  bq_puller_pipeline_slugs = ["tpu-inference-ci", "vllm-torchtpu-ci"]

  # Drop the tpu-commons entries once its agents are gone.
  buildkite_token_secret_ids = {
    "tpu-commons" = "projects/${var.secret_project_id}/secrets/tpu_commons_buildkite_agent_token"
    "vllm"        = "projects/${var.secret_project_id}/secrets/vllm_buildkite_agent_token"
  }

  # vllm_buildkite_rest_api_token is the tpu-commons token despite its name.
  bq_puller_orgs = {
    "tpu-commons" = "vllm_buildkite_rest_api_token"
    "vllm"        = "vllm_org_buildkite_rest_api_token"
  }
}

module "ci_cache_storage" {
  source = "../modules/ci_cache_storage"

  project_id         = var.project_id
  bucket_name        = "ullm-ci-cache"
  cache_zones        = ["us-central1-b", "us-central1-c"]
  lifecycle_age_days = 4
}
