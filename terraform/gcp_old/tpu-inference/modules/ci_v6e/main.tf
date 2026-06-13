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

  labels = {
    vm_name = "${var.accelerator_type}-ci-${count.index}-${var.project_short_name}-${data.google_client_config.config.zone}"
  }

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
     
      # Force stop the buildkite-agent and start at the end to avoid race condition
      sudo systemctl stop buildkite-agent

      sudo usermod -a -G docker buildkite-agent
      sudo -u buildkite-agent gcloud auth configure-docker us-central1-docker.pkg.dev --quiet

      sudo sed -i "s/xxx/${var.buildkite_token_value}/g" /etc/buildkite-agent/buildkite-agent.cfg
      
      HOST_NAME_VAL="${var.accelerator_type}-ci-${count.index}-${var.project_short_name}-${data.google_client_config.config.zone}"
      # Set the system-wide environment variable, avoid using the default HOSTNAME because it's too vague to be useful. For example, t1v-n-01667781-w-0
      echo "HOST_NAME=$HOST_NAME_VAL" | sudo tee -a /etc/environment
      sudo sed -i "s/name=\"%hostname-%spawn\"/name=\"$HOST_NAME_VAL\"/" /etc/buildkite-agent/buildkite-agent.cfg
      echo 'tags="queue=${var.buildkite_queue_name}"' | sudo tee -a /etc/buildkite-agent/buildkite-agent.cfg
      echo 'HF_TOKEN=${var.huggingface_token_value}' | sudo tee -a /etc/environment
      echo 'BUILDKITE_ANALYTICS_TOKEN=${var.buildkite_analytics_token_value}' | sudo tee -a /etc/environment
      echo 'TPU_VERSION=tpu6e' | sudo tee -a /etc/environment

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

      echo "Installing GCP Ops Agent..."
      curl -sSO https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh
      sudo bash add-google-cloud-ops-agent-repo.sh --also-install

      # ==========================================
      # 1. JAX Cache Setup via GCS FUSE
      # ==========================================
      echo "Installing gcsfuse..."
      export GCSFUSE_REPO=gcsfuse-`lsb_release -c -s`
      curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo tee /usr/share/keyrings/google-cloud.asc > /dev/null
      echo "deb [signed-by=/usr/share/keyrings/google-cloud.asc] https://packages.cloud.google.com/apt $GCSFUSE_REPO main" | sudo tee /etc/apt/sources.list.d/gcsfuse.list

      sudo apt-get update
      sudo apt-get install -y gcsfuse

      # Configure FUSE to allow other users (CI Agent / Docker Containers)
      sudo sed -i 's/#user_allow_other/user_allow_other/g' /etc/fuse.conf

      # Create local buffer cache directory on the local disk
      sudo mkdir -p /var/cache/gcsfuse
      sudo chmod 777 /var/cache/gcsfuse

      # Create target mount point and apply Safety Lock (immutable attribute)
      # This prevents any writes from falling back to the persistent disk if FUSE drops.
      sudo mkdir -p /mnt/disks/persist/tpu_jax_cache
      sudo chmod 777 /mnt/disks/persist/tpu_jax_cache
      sudo chattr +i /mnt/disks/persist/tpu_jax_cache

      # Setting up backward compatibility symlink for legacy CI jobs
      sudo rm -rf /tmp/tpu_jax_cache
      sudo ln -s /mnt/disks/persist/tpu_jax_cache /tmp/tpu_jax_cache

      echo "Mounting GCS bucket: ullm-ci-cache..."
      if sudo gcsfuse \
          --file-cache-max-size-mb=10240 \
          --cache-dir=/var/cache/gcsfuse \
          --dir-mode=777 \
          --file-mode=777 \
          --metadata-cache-ttl-secs=-1 \
          --kernel-list-cache-ttl-secs=-1 \
          --metadata-cache-negative-ttl-secs=0 \
          --stat-cache-max-size-mb=-1 \
          --type-cache-max-size-mb=-1 \
          -o allow_other \
          ullm-ci-cache \
          /mnt/disks/persist/tpu_jax_cache; then
        
        echo "GCS FUSE mount successful."
        echo 'CI_CACHE_FUSE_MOUNTED=1' | sudo tee -a /etc/environment
      else
        echo "ERROR: Failed to mount GCS FUSE bucket."
        exit 1
      fi

      # ==========================================
      # 2. Automated Disk Garbage Collection (Cron)
      # ==========================================
      echo "Setting up daily cron job for JAX cache cleanup..."
      echo -e '0 2 * * * root find /mnt/disks/persist/tpu_jax_cache -type f -mtime +30 -delete > /dev/null 2>&1\n30 2 * * * root find /mnt/disks/persist/tpu_jax_cache -type d -empty -delete > /dev/null 2>&1' | sudo tee /etc/cron.d/tpu_cache_cleanup
      sudo chmod 0644 /etc/cron.d/tpu_cache_cleanup

      # ==========================================
      # 3. System Log Cleaner
      # ==========================================
      # Inject ultra-strict limit policy for syslog and kern.log (50M, 1 backup)
      echo "Configuring strict logrotate for syslog and kern.log..."
      sudo sed -i '1i/var/log/syslog\n/var/log/kern.log\n{\n  size 50M\n  rotate 1\n  missingok\n  notifempty\n  compress\n  delaycompress\n  postrotate\n    /usr/lib/rsyslog/rsyslog-rotate\n  endscript\n}\n' /etc/logrotate.d/rsyslog
      
      # Force rotate once
      sudo logrotate -f /etc/logrotate.conf

      systemctl enable buildkite-agent
      systemctl start buildkite-agent
    EOF
  }
}
