# 16 nodes for CI cluster
# 1 TPU v6e device each
# Region: us-east5-b
# Type: v6e-1
# Runtime: v2-alpha-tpuv6e

data "google_secret_manager_secret_version" "buildkite_agent_token_ci_cluster" {
  secret = "projects/${var.project_id}/secrets/buildkite_agent_token_ci_cluster"
  version = "latest"
}

data "google_secret_manager_secret_version" "huggingface_token" {
  secret  = "projects/${var.project_id}/secrets/huggingface_token"
  version = "latest"
}

locals {
  buildkite_token_value   = data.google_secret_manager_secret_version.buildkite_agent_token_ci_cluster.secret_data
  huggingface_token_value = data.google_secret_manager_secret_version.huggingface_token.secret_data
}

resource "tls_private_key" "buildkite_agent_ssh_key" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "google_compute_disk" "disk_east5_b" {
  provider = google-beta.us-east5-b
  count = 24

  name  = "tpu-disk-east5-b-${count.index}"
  size  = 2048
  type  = "hyperdisk-balanced"
  zone  = "us-east5-b"
}

resource "google_tpu_v2_vm" "tpu_v6_ci" {
  provider = google-beta.us-east5-b
  count = 24
  name = "vllm-tpu-v6-ci-${count.index}"
  zone = "us-east5-b" 

  runtime_version = "v2-alpha-tpuv6e"

  accelerator_type = "v6e-1"

  network_config {
    network = "projects/${var.project_id}/global/networks/default"
    enable_external_ips = true
  }

  data_disks {
    source_disk = google_compute_disk.disk_east5_b[count.index].id
    mode = "READ_WRITE"
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

      sudo mkdir -p /var/lib/buildkite-agent/.ssh
      sudo chown buildkite-agent:buildkite-agent /var/lib/buildkite-agent/.ssh
      sudo chmod 700 /var/lib/buildkite-agent/.ssh

      echo '${tls_private_key.buildkite_agent_ssh_key.private_key_pem}' | sudo -u buildkite-agent tee /var/lib/buildkite-agent/.ssh/id_rsa
      sudo -u buildkite-agent chmod 600 /var/lib/buildkite-agent/.ssh/id_rsa
      echo '${tls_private_key.buildkite_agent_ssh_key.public_key_openssh}' | sudo -u buildkite-agent tee /var/lib/buildkite-agent/.ssh/id_rsa.pub
      sudo -u buildkite-agent chmod 644 /var/lib/buildkite-agent/.ssh/id_rsa.pub

      sudo sed -i "s/xxx/${local.buildkite_token_value}/g" /etc/buildkite-agent/buildkite-agent.cfg
      sudo sed -i 's/name="%hostname-%spawn"/name="vllm-tpu-${count.index}"/' /etc/buildkite-agent/buildkite-agent.cfg
      echo 'tags="queue=tpu_v6e_queue"' | sudo tee -a /etc/buildkite-agent/buildkite-agent.cfg
      echo 'HF_TOKEN=${local.huggingface_token_value}' | sudo tee -a /etc/environment

      sudo mkdir -p /mnt/disks/persist

      # Format if not already formatted
      if ! blkid /dev/nvme0n2; then
        echo "Formatting /dev/nvme0n2 as ext4..."
        sudo mkfs.ext4 -m 0 -E lazy_itable_init=0,lazy_journal_init=0,discard /dev/nvme0n2
      fi

      # Add to /etc/fstab using UUID
      disk_uuid=$(blkid -s UUID -o value /dev/nvme0n2)
      if ! grep -q "/mnt/disks/persist" /etc/fstab; then
       echo "UUID=$disk_uuid /mnt/disks/persist ext4 defaults,discard 0 2" | sudo tee -a /etc/fstab
      fi

      # Only mount if not already mounted (first boot or recovery)
      if ! mountpoint -q /mnt/disks/persist; then
        sudo mount /mnt/disks/persist
      fi

      jq ". + {\"data-root\": \"/mnt/disks/persist\"}" /etc/docker/daemon.json > /tmp/daemon.json.tmp && mv /tmp/daemon.json.tmp /etc/docker/daemon.json
      systemctl stop docker
      systemctl daemon-reload
      systemctl start docker

      sudo chmod 777 /mnt/disks/persist

      systemctl enable buildkite-agent
      systemctl start buildkite-agent
    EOF
  }
}


output "buildkite_agent_public_key" {
  value = tls_private_key.buildkite_agent_ssh_key.public_key_openssh
  sensitive = true
}