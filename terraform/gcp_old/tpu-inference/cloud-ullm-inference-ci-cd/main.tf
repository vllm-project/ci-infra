data "google_secret_manager_secret_version" "buildkite_agent_token_ci_cluster" {
  secret = "projects/${var.secret_project_id}/secrets/tpu_commons_buildkite_agent_token"
  version = "latest"
}

data "google_secret_manager_secret_version" "buildkite_analytics_token_ci_cluster" {
  secret  = "projects/${var.secret_project_id}/secrets/tpu_commons_buildkite_analytics_token"
  version = "latest"
}

data "google_secret_manager_secret_version" "huggingface_token" {
  secret  = "projects/${var.secret_project_id}/secrets/tpu_commons_buildkite_hf_token"
  version = "latest"
}

module "ci_v6e_1" {
  source    = "../modules/ci_v6e"
  providers = {
    google-beta = google-beta.us-central1-b
  }

  accelerator_type                 = "v6e-1"
  reserved                         = true
  instance_count                   = 16
  disk_size                        = 512
  buildkite_queue_name             = "tpu_v6e_queue"
  project_id                       = var.project_id
  project_short_name               = var.project_short_name
  buildkite_token_value            = data.google_secret_manager_secret_version.buildkite_agent_token_ci_cluster.secret_data
  buildkite_analytics_token_value  = data.google_secret_manager_secret_version.buildkite_analytics_token_ci_cluster.secret_data
  huggingface_token_value          = data.google_secret_manager_secret_version.huggingface_token.secret_data
}

module "ci_v6e_8" {
  source    = "../modules/ci_v6e"
  providers = {
    google-beta = google-beta.us-central1-b
  }

  accelerator_type                 = "v6e-8"
  reserved                         = true
  instance_count                   = 0
  buildkite_queue_name             = "tpu_v6e_8_queue"
  project_id                       = var.project_id
  project_short_name               = var.project_short_name
  buildkite_token_value            = data.google_secret_manager_secret_version.buildkite_agent_token_ci_cluster.secret_data
  buildkite_analytics_token_value  = data.google_secret_manager_secret_version.buildkite_analytics_token_ci_cluster.secret_data
  huggingface_token_value          = data.google_secret_manager_secret_version.huggingface_token.secret_data
}


module "ci_v7x_2" {
  source    = "../modules/ci_v7x"
  providers = {
    google-beta = google-beta.us-central1-c
  }

  accelerator_type                 = "tpu7x-2"
  reserved                         = true
  instance_count                   = 16
  buildkite_queue_name             = "tpu_v7x_2_queue"
  disk_size                        = 2048
  project_id                       = var.project_id
  project_short_name               = var.project_short_name
  buildkite_token_value            = data.google_secret_manager_secret_version.buildkite_agent_token_ci_cluster.secret_data
  buildkite_analytics_token_value  = data.google_secret_manager_secret_version.buildkite_analytics_token_ci_cluster.secret_data
  huggingface_token_value          = data.google_secret_manager_secret_version.huggingface_token.secret_data
}

module "ci_v7x_8" {
  source    = "../modules/ci_v7x"
  providers = {
    google-beta = google-beta.us-central1-c
  }

  accelerator_type                 = "tpu7x-8"
  reserved                         = true
  instance_count                   = 11
  buildkite_queue_name             = "tpu_v7x_8_queue"
  disk_size                        = 4096
  project_id                       = var.project_id
  project_short_name               = var.project_short_name
  buildkite_token_value            = data.google_secret_manager_secret_version.buildkite_agent_token_ci_cluster.secret_data
  buildkite_analytics_token_value  = data.google_secret_manager_secret_version.buildkite_analytics_token_ci_cluster.secret_data
  huggingface_token_value          = data.google_secret_manager_secret_version.huggingface_token.secret_data
}

module "ci_cpu_64_core" {
  source    = "../modules/ci_cpu_64_core"
  providers = {
    google-beta = google-beta.us-central1-b
  }

  project_id              = var.project_id
  instance_count          = 4
  machine_type            = "n2-standard-64"
  disk_size               = 250
  disk_type               = "pd-balanced"
  buildkite_queue_name    = "cpu_64_core"

  buildkite_token_value   = data.google_secret_manager_secret_version.buildkite_agent_token_ci_cluster.secret_data
  huggingface_token_value = data.google_secret_manager_secret_version.huggingface_token.secret_data
}

module "ci_monitoring" {
  source    = "../modules/ci_monitoring"
  providers = {
    google-beta = google-beta.us-central1-b
  }

  project_id                     = var.project_id
  buildkite_token_value = data.google_secret_manager_secret_version.buildkite_agent_token_ci_cluster.secret_data
}

resource "google_compute_resource_policy" "v7x221" {
  name   = "v7x221"
  region = "us-central1"
  project = var.project_id  
  workload_policy {
    type                 = "HIGH_THROUGHPUT"
    accelerator_topology = "2x2x1"
  }
}

resource "google_container_cluster" "tpu-cluster" {
  name     = "tpu-v7x-cluster"
  location = "us-central1" 
  remove_default_node_pool = true
  initial_node_count       = 2
}

resource "google_container_node_pool" "tpu_v7x_pool1" {
  name       = "tpu-v7x-8-pool1"
  location   = "us-central1-c"
  cluster    = google_container_cluster.tpu-cluster.name
  node_count = 1
  project    = var.project_id
  placement_policy {
    type = "COMPACT"
    policy_name = google_compute_resource_policy.v7x221.name
  }
  node_config {
    machine_type = "ct7x-8"
    labels = {
      "topology" = "2x2x1"
    }
    reservation_affinity {
      consume_reservation_type = "ANY_RESERVATION"
    }

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform"
    ]
  }
}

resource "google_container_node_pool" "tpu_v7x_pool2" {
  name       = "tpu-v7x-8-pool2"
  location   = "us-central1-c"
  cluster    = google_container_cluster.tpu-cluster.name
  node_count = 1
  project    = var.project_id
  placement_policy {
    type = "COMPACT"
    policy_name = google_compute_resource_policy.v7x221.name
  }
  node_config {
    machine_type = "ct7x-8"
    labels = {
      "topology" = "2x2x1"
    }
    reservation_affinity {
      consume_reservation_type = "ANY_RESERVATION"
    }

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform"
    ]
  }
}
