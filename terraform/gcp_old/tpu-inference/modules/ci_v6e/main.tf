# 1 TPU device each
# Runtime: v2-alpha-tpuv6e

data "google_client_config" "config" {
  provider = google-beta
}

resource "google_compute_disk" "tpu_disk" {
  provider = google-beta
  count    = var.instance_count
  name     = "${var.accelerator_type}-ci-${count.index}-${var.project_short_name}-${data.google_client_config.config.zone}-disk"
  size     = var.disk_size
  type     = "hyperdisk-balanced"
}

resource "google_tpu_v2_vm" "tpu_v6_ci" {
  provider = google-beta
  count    = var.instance_count
  name     = "${var.accelerator_type}-ci-${count.index}-${var.project_short_name}-${data.google_client_config.config.zone}"

  runtime_version  = "v2-alpha-tpuv6e"
  accelerator_type = var.accelerator_type

  dynamic "scheduling_config" {    
    for_each = var.reserved ? [1] : []
    content {
      reserved = var.reserved
    }
  }

  network_config {
    network             = "projects/${var.project_id}/global/networks/default"
    enable_external_ips = true
  }

  data_disks {
    source_disk = google_compute_disk.tpu_disk[count.index].id
    mode        = "READ_WRITE"
  }

  metadata = {
    "startup-script" = <<-EOF
      #!/bin/bash

      apt-get update
      apt-get install -y curl build-essential jq

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

      sudo sed -i "s/xxx/${var.buildkite_token_value}/g" /etc/buildkite-agent/buildkite-agent.cfg
      sudo sed -i 's/name="%hostname-%spawn"/name="${var.accelerator_type}-ci-${count.index}-${var.project_short_name}-${data.google_client_config.config.zone}"/' /etc/buildkite-agent/buildkite-agent.cfg
      echo 'tags="queue=${var.buildkite_queue_name}"' | sudo tee -a /etc/buildkite-agent/buildkite-agent.cfg
      echo 'HF_TOKEN=${var.huggingface_token_value}' | sudo tee -a /etc/environment
      echo 'BUILDKITE_ANALYTICS_TOKEN=${var.buildkite_analytics_token_value}' | sudo tee -a /etc/environment

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
