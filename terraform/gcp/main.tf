provider "google" {
  project = "vllm-gcp"
  region  = "us-central1"
}

provider "google-beta" {
  project = "vllm-gcp"
  region  = "us-central1"
}

variable "buildkite_agent_token" {
  type      = string
  sensitive = true
}

variable "project_id" {
  default = "vllm-gcp"
}

resource "random_id" "default" {
  byte_length = 8
}

resource "google_storage_bucket" "default" {
  name     = "${random_id.default.hex}-terraform-remote-backend"
  location = "US"

  force_destroy               = false
  public_access_prevention    = "enforced"
  uniform_bucket_level_access = true

  versioning {
    enabled = true
  }
}

resource "local_file" "default" {
  file_permission = "0644"
  filename        = "${path.module}/backend.tf"

  # You can store the template in a file and use the templatefile function for
  # more modularity, if you prefer, instead of storing the template inline as
  # we do here.
  content = <<-EOT
  terraform {
    backend "gcs" {
      bucket = "${google_storage_bucket.default.name}"
    }
  }
  EOT
}

# GKE Autopilot cluster
resource "google_container_cluster" "autopilot_ci_cluster" {
  name     = "gke-autopilot-vllm-ci-cluster"
  location = "us-central1"

  enable_autopilot = true

  # Set `deletion_protection` to `true` will ensure that one cannot
  # accidentally delete this instance by use of Terraform.
  deletion_protection = false
}

# GKE cluster
resource "google_container_cluster" "ci_cluster" {
  name           = "vllm-ci-test-cluster"
  location       = "us-central1"
  node_locations = ["us-central1-a", "us-central1-b", "us-central1-c"]
  network        = "default"

  # We can't create a cluster with no node pool defined, but we want to only use
  # separately managed node pools. So we create the smallest possible default
  # node pool and immediately delete it.
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false
}

resource "google_container_cluster" "ci_cluster_us_west_1" {
  name           = "vllm-ci-test-cluster-us-west-1"
  location       = "us-west1"
  node_locations = ["us-west1-a", "us-west1-b"]
  network        = "default"

  # We can't create a cluster with no node pool defined, but we want to only use
  # separately managed node pools. So we create the smallest possible default
  # node pool and immediately delete it.
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false
}

locals {
  node_pools = {
    gpu_L4x1_pool_ondemand = {
      name_suffix           = "-l4-1"
      total_min_node_count = 0
      total_max_node_count  = 64
      guest_accelerator_count = 1
      machine_type          = "g2-standard-12"
      preemptible = false
      startup_script = ""
    },
    gpu_L4x2_pool = {
      name_suffix           = "-l4-2"
      total_min_node_count = 0
      total_max_node_count  = 10
      guest_accelerator_count = 2
      machine_type          = "g2-standard-24"
      preemptible = false
      startup_script = ""
    },
    gpu_L4x4_pool = {
      name_suffix           = "-l4-4"
      total_min_node_count = 0
      total_max_node_count  = 3
      guest_accelerator_count = 4
      machine_type          = "g2-standard-48"
      preemptible = false
      startup_script = ""
    }
  }
}


resource "google_container_node_pool" "node_pool" {
  for_each   = local.node_pools

  name       = "${google_container_cluster.ci_cluster.name}${each.value.name_suffix}"
  location   = "us-central1"
  node_locations = ["us-central1-a", "us-central1-b", "us-central1-c"]
  cluster    = google_container_cluster.ci_cluster.name
  node_count = each.value.total_min_node_count

  autoscaling {
    total_min_node_count = tostring(each.value.total_min_node_count)
    total_max_node_count = tostring(each.value.total_max_node_count)
    location_policy      = "ANY"
  }

  management {
    auto_repair  = "true"
    auto_upgrade = "true"
  }

  node_config {
    oauth_scopes = [
      "https://www.googleapis.com/auth/logging.write",
      "https://www.googleapis.com/auth/monitoring",
      "https://www.googleapis.com/auth/devstorage.read_only",
      "https://www.googleapis.com/auth/trace.append",
      "https://www.googleapis.com/auth/service.management.readonly",
      "https://www.googleapis.com/auth/servicecontrol",
    ]

    labels = {
      env = var.project_id
    }

    guest_accelerator {
      type  = "nvidia-l4"
      count = each.value.guest_accelerator_count
      gpu_driver_installation_config {
        gpu_driver_version = "LATEST"
      }
    }

    machine_type = each.value.machine_type
    image_type   = "cos_containerd"
    preemptible  = each.value.preemptible
    tags         = ["gke-node", "${var.project_id}-gke"]

    disk_size_gb = "512"
    disk_type    = "pd-balanced"

    metadata = {
      disable-legacy-endpoints = "true"
      startup_script=each.value.startup_script
    }
  }
}

resource "google_container_node_pool" "node_pool_us_west_1" {
  for_each   = local.node_pools

  name       = "${google_container_cluster.ci_cluster_us_west_1.name}${each.value.name_suffix}"
  location   = "us-west1"
  node_locations = ["us-west1-a", "us-west1-b"]
  cluster    = google_container_cluster.ci_cluster_us_west_1.name
  node_count = each.value.total_min_node_count

  autoscaling {
    total_min_node_count = tostring(each.value.total_min_node_count)
    total_max_node_count = tostring(each.value.total_max_node_count)
    location_policy      = "ANY"
  }

  management {
    auto_repair  = "true"
    auto_upgrade = "true"
  }

  node_config {
    oauth_scopes = [
      "https://www.googleapis.com/auth/logging.write",
      "https://www.googleapis.com/auth/monitoring",
      "https://www.googleapis.com/auth/devstorage.read_only",
      "https://www.googleapis.com/auth/trace.append",
      "https://www.googleapis.com/auth/service.management.readonly",
      "https://www.googleapis.com/auth/servicecontrol",
    ]

    labels = {
      env = var.project_id
    }

    guest_accelerator {
      type  = "nvidia-l4"
      count = each.value.guest_accelerator_count
      gpu_driver_installation_config {
        gpu_driver_version = "LATEST"
      }
    }

    machine_type = each.value.machine_type
    image_type   = "cos_containerd"
    preemptible  = each.value.preemptible
    tags         = ["gke-node", "${var.project_id}-gke"]

    disk_size_gb = "512"
    disk_type    = "pd-balanced"

    metadata = {
      disable-legacy-endpoints = "true"
      startup_script=each.value.startup_script
    }
  }
}

resource "google_service_account" "artifact_registry_sa" {
  account_id   = "artifact-registry-sa"
  display_name = "Service Account for Artifact Registry"
  project      = var.project_id
}

resource "google_project_iam_member" "artifact_registry_iam" {
  project = var.project_id
  role    = "roles/artifactregistry.repoAdmin"
  member  = "serviceAccount:${google_service_account.artifact_registry_sa.email}"
}

# also make the service account a Storage Admin for bucket named
resource "google_storage_bucket_iam_member" "artifact_registry_bucket_iam" {
  bucket = "vllm-build-artifacts-gcp"
  role   = "roles/storage.admin"
  member = "serviceAccount:${google_service_account.artifact_registry_sa.email}"
}

resource "google_compute_instance_template" "builder_template" {
  name        = "builder-template"
  machine_type = "n2-highmem-64"  # 64 vCPUs, 512 GB memory

  scheduling {
    preemptible       = true
    automatic_restart = false
  }

  metadata = {
    "startup-script" = <<-EOF
      #!/bin/bash

      apt-get update
      apt-get install -y curl build-essential

      curl -o- https://get.docker.com/ | bash -

      curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
      /root/.cargo/bin/cargo install minijinja-cli
      cp /root/.cargo/bin/minijinja-cli /usr/bin/minijinja-cli
      chmod 777 /usr/bin/minijinja-cli

      curl -fsSL https://keys.openpgp.org/vks/v1/by-fingerprint/32A37959C2FA5C3C99EFBC32A79206696452D198 | sudo gpg --dearmor -o /usr/share/keyrings/buildkite-agent-archive-keyring.gpg
      echo "deb [signed-by=/usr/share/keyrings/buildkite-agent-archive-keyring.gpg] https://apt.buildkite.com/buildkite-agent stable main" | sudo tee /etc/apt/sources.list.d/buildkite-agent.list
      apt-get update
      apt-get install -y buildkite-agent

      sudo usermod -a -G docker buildkite-agent
      sudo -u buildkite-agent gcloud auth configure-docker us-central1-docker.pkg.dev --quiet

      sudo sed -i "s/xxx/${var.buildkite_agent_token}/g" /etc/buildkite-agent/buildkite-agent.cfg
      echo 'tags="queue=cpu-gcp"' | sudo tee -a /etc/buildkite-agent/buildkite-agent.cfg
      systemctl enable buildkite-agent
      systemctl start buildkite-agent
    EOF
  }

  disk {
    source_image = "debian-cloud/debian-11"  # Replace with your desired image
    boot         = true
    disk_size_gb = 512
  }

  network_interface {
    network = "default"
    access_config {
      // This block's existence will create ephemeral IP allowing egress internet access
    }
  }

  service_account {
    email  = google_service_account.artifact_registry_sa.email
    scopes = ["cloud-platform"]
  }
}

resource "google_compute_instance_template" "small_cpu_template" {
  name        = "small-cpu-template"
  machine_type = "n2-highmem-4"  # 4 vCPUs, 32GB memory

  scheduling {
    preemptible       = true
    automatic_restart = false
  }

  metadata = {
    "startup-script" = <<-EOF
      #!/bin/bash

      apt-get update
      apt-get install -y curl build-essential

      curl -o- https://get.docker.com/ | bash -

      curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
      /root/.cargo/bin/cargo install minijinja-cli
      cp /root/.cargo/bin/minijinja-cli /usr/bin/minijinja-cli
      chmod 777 /usr/bin/minijinja-cli

      curl -fsSL https://keys.openpgp.org/vks/v1/by-fingerprint/32A37959C2FA5C3C99EFBC32A79206696452D198 | sudo gpg --dearmor -o /usr/share/keyrings/buildkite-agent-archive-keyring.gpg
      echo "deb [signed-by=/usr/share/keyrings/buildkite-agent-archive-keyring.gpg] https://apt.buildkite.com/buildkite-agent stable main" | sudo tee /etc/apt/sources.list.d/buildkite-agent.list
      apt-get update
      apt-get install -y buildkite-agent

      sudo usermod -a -G docker buildkite-agent
      sudo -u buildkite-agent gcloud auth configure-docker us-central1-docker.pkg.dev --quiet

      sudo sed -i "s/xxx/${var.buildkite_agent_token}/g" /etc/buildkite-agent/buildkite-agent.cfg
      echo 'tags="queue=small-cpu-gcp"' | sudo tee -a /etc/buildkite-agent/buildkite-agent.cfg
      systemctl enable buildkite-agent
      systemctl start buildkite-agent
    EOF
  }

  disk {
    source_image = "debian-cloud/debian-11"
    boot         = true
    disk_size_gb = 128
  }

  network_interface {
    network = "default"
    access_config {
      // This block's existence will create ephemeral IP allowing egress internet access
    }
  }

  service_account {
    email  = google_service_account.artifact_registry_sa.email
    scopes = ["cloud-platform"]
  }
}

resource "google_compute_region_instance_group_manager" "builder_group" {
  name               = "builder-group"
  base_instance_name = "builder-instance"
  region = "us-central1"
  distribution_policy_zones  = ["us-central1-a", "us-central1-b"]

  version {
    instance_template  = google_compute_instance_template.builder_template.self_link_unique
  }

  target_size        = 1
}

resource "google_compute_region_instance_group_manager" "small_cpu_group" {
  name               = "small-cpu-group"
  base_instance_name = "small-cpu-instance"
  region = "us-central1"
  distribution_policy_zones  = ["us-central1-a", "us-central1-b"]
  version {
    instance_template  = google_compute_instance_template.small_cpu_template.self_link_unique
  }
  target_size        = 1
}

resource "google_artifact_registry_repository" "vllm-repo" {
  provider      = google-beta

  location      = "us-central1"
  repository_id = "vllm-ci-test-repo-gcp"
  description   = "Hosting images for vLLM CI test"
  format        = "DOCKER"

  cleanup_policies {
    id     = "delete-old-images"
    action = "DELETE"
    condition {
      older_than   = "604800s" // 7 days
    }
  }
}
