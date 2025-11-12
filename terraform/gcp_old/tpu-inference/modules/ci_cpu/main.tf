# 8 nodes for CI cluster
# normal e2 instance device each
# Region: us-east5-b
# Type: e2-standard-2

data "google_secret_manager_secret_version" "buildkite_agent_token_ci_cluster" {
  secret = "projects/${var.project_id}/secrets/tpu_commons_buildkite_agent_token"
  version = "latest"
}

locals {
  buildkite_token_value = data.google_secret_manager_secret_version.buildkite_agent_token_ci_cluster.secret_data
}

resource "google_compute_instance" "buildkite-agent-instance" {
  provider = google-beta.us-east5-b
  count    = var.instance_count
  name     = "vllm-ci-cpu-${count.index}"

  boot_disk {
    auto_delete = true
    device_name = "vllm-ci-cpu-${count.index}"

    initialize_params {
      image = "projects/ubuntu-os-cloud/global/images/ubuntu-2404-noble-amd64-v20251021"
      size  = 100
      type  = "pd-balanced"
    }

    mode = "READ_WRITE"
  }

  service_account {
    scopes = ["cloud-platform"]
  }

  can_ip_forward      = false
  deletion_protection = false
  enable_display      = false
  machine_type        = "e2-standard-2"

  network_interface {
    access_config {
      nat_ip = google_compute_address.static[count.index].address
    }
    subnetwork = "projects/${var.project_id}/regions/us-east5/subnetworks/default"
  }

  metadata = {
    enable-osconfig  = "TRUE"
    enable-oslogin   = "true"
    "startup-script" = <<-EOF
      #!/bin/bash

      apt-get update
      apt-get install -y curl build-essential jq

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

      sudo sed -i "s/xxx/${local.buildkite_token_value}/g" /etc/buildkite-agent/buildkite-agent.cfg
      sudo sed -i 's/name="%hostname-%spawn"/name="vllm-cpu-vm-${count.index}"/' /etc/buildkite-agent/buildkite-agent.cfg
      echo 'tags="queue=cpu"' | sudo tee -a /etc/buildkite-agent/buildkite-agent.cfg

      systemctl stop docker
      systemctl start docker

      systemctl enable buildkite-agent
      systemctl start buildkite-agent
    EOF
  }
}

resource "google_compute_address" "static" {
  provider = google-beta.us-east5-b
  name     = "vllm-ci-cpu-${count.index}-ip"
  count    = var.instance_count
}
