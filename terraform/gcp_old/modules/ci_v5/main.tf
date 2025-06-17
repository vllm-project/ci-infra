data "google_secret_manager_secret_version" "buildkite_agent_token_ci_cluster" {
  secret = "projects/${var.project_id}/secrets/buildkite_agent_token_ci_cluster"
  version = "latest"
}

data "google_secret_manager_secret_version" "huggingface_token" {
  secret = "projects/${var.project_id}/secrets/huggingface_token"
  version = "latest"
}

locals {
  buildkite_token_value   = data.google_secret_manager_secret_version.buildkite_agent_token_ci_cluster.secret_data
  huggingface_token_value = data.google_secret_manager_secret_version.huggingface_token.secret_data
}

resource "google_compute_disk" "disk_v5" {
  provider = google-beta.us-south1-a
  count = 7

  name  = "tpu-disk-south1-a-${count.index + 1}"
  size  = 512
  type  = "pd-ssd"
  zone  = "us-south1-a"
}

resource "google_tpu_v2_vm" "tpu_v5" {
  provider = google-beta.us-south1-a
  count = 7
  name = "vllm-tpu-v5-${count.index + 1}"
  zone = "us-south1-a"

  runtime_version = "v2-alpha-tpuv5-lite"

  accelerator_type = "v5litepod-1"

  data_disks {
    source_disk = google_compute_disk.disk_v5[count.index].id
    mode = "READ_WRITE"
  }

  network_config {
    network = "default"
    enable_external_ips = true
  }

  metadata = {
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
      sudo sed -i 's/name="%hostname-%spawn"/name="vllm-tpu-${count.index}"/' /etc/buildkite-agent/buildkite-agent.cfg
      echo 'tags="queue=tpu_v5_queue"' | sudo tee -a /etc/buildkite-agent/buildkite-agent.cfg
      echo 'HF_TOKEN=${local.huggingface_token_value}' | sudo tee -a /etc/environment

      sudo mkfs.ext4 -m 0 -E lazy_itable_init=0,lazy_journal_init=0,discard /dev/sdb
      sudo mkdir -p /mnt/disks/persist
      sudo mount -o discard,defaults /dev/sdb /mnt/disks/persist

      jq ". + {\"data-root\": \"/mnt/disks/persist\"}" /etc/docker/daemon.json > /tmp/daemon.json.tmp && mv /tmp/daemon.json.tmp /etc/docker/daemon.json
      systemctl stop docker
      systemctl daemon-reload
      systemctl start docker

      systemctl enable buildkite-agent
      systemctl start buildkite-agent
    EOF
  }
}
