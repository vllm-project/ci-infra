# 1 TPU device each
# Runtime: v2-alpha-tpuv6e

data "google_client_config" "config" {
  provider = google-beta
}

locals {
  # The one place this name is spelled out. The TPU VM, its label, its disk, and
  # the Buildkite agent inside it all derive from here, so an agent in the
  # Buildkite UI always maps onto a `gcloud compute tpus` entry. An empty
  # purpose reproduces the original name, so adding this knob does not rename
  # -- and so does not recreate -- an existing fleet.
  node_names = [for i in range(var.instance_count) :
    var.purpose == "" ?
    "${var.accelerator_type}-ci-${i}-${var.project_short_name}-${data.google_client_config.config.zone}" :
    "${var.accelerator_type}-ci-${var.purpose}-${i}-${var.project_short_name}-${data.google_client_config.config.zone}"
  ]
}

resource "google_compute_disk" "tpu_disk" {
  provider = google-beta
  count    = var.instance_count
  name     = "${local.node_names[count.index]}-disk"
  size     = var.disk_size
  type     = "hyperdisk-balanced"
}

resource "google_tpu_v2_vm" "tpu_v6_ci" {
  provider = google-beta
  count    = var.instance_count
  name     = local.node_names[count.index]

  runtime_version  = "v2-alpha-tpuv6e"
  accelerator_type = var.accelerator_type

  labels = {
    vm_name = local.node_names[count.index]
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
    "startup-script" = <<-STARTUP_SCRIPT
      #!/bin/bash

      # Keep the agent offline until provisioning finishes (unmasked at the
      # end). This script re-runs on every boot and the unit is enabled from
      # the previous run, so systemd starts the agent seconds into boot. A
      # `stop` is not enough: it waits for an in-flight job rather than
      # preventing it, and the agent's postinst restarts the unit on upgrade.
      # Masking blocks both.
      systemctl mask buildkite-agent 2>/dev/null || true
      # Any job picked up since boot is running against a half-provisioned host.
      timeout 30 systemctl stop buildkite-agent 2>/dev/null \
        || systemctl kill -s SIGKILL buildkite-agent 2>/dev/null \
        || true

      apt-get update
      apt-get install -y curl build-essential jq git python3 python3-pip

      curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
      /root/.cargo/bin/cargo install minijinja-cli
      cp /root/.cargo/bin/minijinja-cli /usr/bin/minijinja-cli
      chmod 777 /usr/bin/minijinja-cli

      curl -fsSL https://keys.openpgp.org/vks/v1/by-fingerprint/32A37959C2FA5C3C99EFBC32A79206696452D198 | sudo gpg --dearmor -o /usr/share/keyrings/buildkite-agent-archive-keyring.gpg
      echo "deb [signed-by=/usr/share/keyrings/buildkite-agent-archive-keyring.gpg] https://apt.buildkite.com/buildkite-agent stable main" | sudo tee /etc/apt/sources.list.d/buildkite-agent.list
      apt-get update
      apt-get install -y buildkite-agent

      # ==========================================
      # Setup In-Memory GitHub App Authentication
      # ==========================================
      pip3 install pyjwt requests cryptography --break-system-packages || pip3 install pyjwt requests cryptography

      # 1. Create the Python script
      cat <<'EOF' > /etc/buildkite-agent/get_github_token.py
      import os, time, requests, jwt

      APP_ID = "4156238"
      INSTALLATION_ID = "142868369"

      signing_key = os.environ['_BK_TEMP_GITHUB_APP_PEM'].encode('utf-8')

      payload = { 'iat': int(time.time()), 'exp': int(time.time()) + 600, 'iss': APP_ID }
      encoded_jwt = jwt.encode(payload, signing_key, algorithm='RS256')

      response = requests.post(
          f'https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens',
          headers={'Authorization': f'Bearer {encoded_jwt}', 'Accept': 'application/vnd.github.v3+json'}
      )
      response.raise_for_status()
      print(response.json()['token'])
      EOF

      mkdir -p /etc/buildkite-agent/hooks

      # 2. Create the on-demand Git Credential Helper for GitHub App tokens
      cat <<'EOF' > /etc/buildkite-agent/git-credential-github-app
      #!/bin/bash
      if [ "$1" = "get" ]; then
          export _BK_TEMP_GITHUB_APP_PEM=$(buildkite-agent secret get ${var.github_app_secret_name})
          if [ -n "$_BK_TEMP_GITHUB_APP_PEM" ]; then
              GITHUB_TOKEN=$(python3 /etc/buildkite-agent/get_github_token.py 2>/dev/null || true)
              unset _BK_TEMP_GITHUB_APP_PEM
              if [ -n "$GITHUB_TOKEN" ]; then
                  echo "username=x-access-token"
                  echo "password=$GITHUB_TOKEN"
              fi
          fi
      fi
      EOF

      chmod 500 /etc/buildkite-agent/get_github_token.py
      chmod +x /etc/buildkite-agent/git-credential-github-app
      chown -R buildkite-agent:buildkite-agent /etc/buildkite-agent/

      # Configure Git system-wide (/etc/gitconfig) and globally to use the credential helper and redirect SSH to HTTPS
      git config --system credential.https://github.com.helper "/etc/buildkite-agent/git-credential-github-app"
      git config --system --add url."https://github.com/".insteadOf "git@github.com:"
      git config --system --add url."https://github.com/".insteadOf "ssh://git@github.com/"
      sudo -H -u buildkite-agent git config --global credential.https://github.com.helper "/etc/buildkite-agent/git-credential-github-app"
      sudo -H -u buildkite-agent git config --global --add url."https://github.com/".insteadOf "git@github.com:"
      sudo -H -u buildkite-agent git config --global --add url."https://github.com/".insteadOf "ssh://git@github.com/"
      HOME=/root git config --global credential.https://github.com.helper "/etc/buildkite-agent/git-credential-github-app"
      HOME=/root git config --global --add url."https://github.com/".insteadOf "git@github.com:"
      HOME=/root git config --global --add url."https://github.com/".insteadOf "ssh://git@github.com/"
      # ==========================================

      sudo usermod -a -G docker buildkite-agent
      sudo -u buildkite-agent gcloud auth configure-docker us-central1-docker.pkg.dev --quiet
      sudo -u buildkite-agent gcloud auth configure-docker us-docker.pkg.dev --quiet

      # This script re-runs on every boot, so match the whole line rather than
      # the pristine "xxx" placeholder, which is gone after the first boot.
      sudo sed -i -E 's|^token=.*|token="${var.buildkite_token_value}"|' /etc/buildkite-agent/buildkite-agent.cfg
      
      HOST_NAME_VAL="${local.node_names[count.index]}"
      # Set the system-wide environment variable, avoid using the default HOSTNAME because it's too vague to be useful. For example, t1v-n-01667781-w-0
      echo "HOST_NAME=$HOST_NAME_VAL" | sudo tee -a /etc/environment
      sudo sed -i "s/name=\"%hostname-%spawn\"/name=\"$HOST_NAME_VAL\"/" /etc/buildkite-agent/buildkite-agent.cfg
      echo 'tags="queue=${var.buildkite_queue_name}"' | sudo tee -a /etc/buildkite-agent/buildkite-agent.cfg
      # tee echoes to stdout, which the startup script sends to the serial
      # console, where anyone with compute.instances.getSerialPortOutput can
      # read it. Secrets go to the file only.
      echo 'HF_TOKEN=${var.huggingface_token_value}' | sudo tee -a /etc/environment > /dev/null
      echo 'BUILDKITE_ANALYTICS_TOKEN=${var.buildkite_analytics_token_value}' | sudo tee -a /etc/environment > /dev/null
      echo 'TPU_VERSION=tpu6e' | sudo tee -a /etc/environment

      # Also provide HF_TOKEN and BUILDKITE_ANALYTICS_TOKEN through the agent's
      # own environment hook, matching how the newer machines are provisioned.
      # The agent only knows about variables set in its hooks; /etc/environment
      # is opaque to it.
      sudo mkdir -p /etc/buildkite-agent/hooks
      sudo touch /etc/buildkite-agent/hooks/environment
      sudo chown root:buildkite-agent /etc/buildkite-agent/hooks/environment
      sudo chmod 750 /etc/buildkite-agent/hooks/environment
      printf '#!/bin/bash\nexport HF_TOKEN=%q\nexport BUILDKITE_ANALYTICS_TOKEN=%q\n' \
        '${var.huggingface_token_value}' '${var.buildkite_analytics_token_value}' \
        | sudo tee /etc/buildkite-agent/hooks/environment > /dev/null

      sudo mkdir -p /mnt/disks/persist

      # Format if not already formatted
      if ! blkid /dev/nvme0n2; then
        echo "Formatting /dev/nvme0n2 as ext4..."
        sudo mkfs.ext4 -m 0 -E lazy_itable_init=0,lazy_journal_init=0,discard /dev/nvme0n2
      fi

      # Add to /etc/fstab using UUID
      disk_uuid=$(blkid -s UUID -o value /dev/nvme0n2)
      # An empty UUID= produces an fstab line that can never mount.
      if [ -z "$disk_uuid" ]; then
        echo "FATAL: no UUID on /dev/nvme0n2; leaving the agent masked." >&2
        exit 1
      fi
      if ! grep -q "/mnt/disks/persist" /etc/fstab; then
       echo "UUID=$disk_uuid /mnt/disks/persist ext4 defaults,discard 0 2" | sudo tee -a /etc/fstab
      fi

      # Only mount if not already mounted (first boot or recovery)
      if ! mountpoint -q /mnt/disks/persist; then
        sudo mount /mnt/disks/persist
      fi

      # CI jobs bind-mount /mnt/disks/persist unconditionally, so without it
      # this host would just drain the queue into failures. Bail, agent masked.
      if ! mountpoint -q /mnt/disks/persist; then
        echo "FATAL: /mnt/disks/persist is not mounted; leaving the agent masked." >&2
        exit 1
      fi

      # Backstop for the mask: hold the agent until the disk is mounted,
      # however it comes to be started.
      sudo mkdir -p /etc/systemd/system/buildkite-agent.service.d
      cat <<'DROPIN' | sudo tee /etc/systemd/system/buildkite-agent.service.d/10-persist-disk.conf > /dev/null
      [Unit]
      RequiresMountsFor=/mnt/disks/persist
      DROPIN
      sudo systemctl daemon-reload

      jq ". + {\"data-root\": \"/mnt/disks/persist\"}" /etc/docker/daemon.json > /tmp/daemon.json.tmp && mv /tmp/daemon.json.tmp /etc/docker/daemon.json
      systemctl stop docker
      systemctl daemon-reload
      systemctl start docker

      sudo chmod 777 /mnt/disks/persist

      echo "Installing GCP Ops Agent..."
      curl -sSO https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh
      sudo bash add-google-cloud-ops-agent-repo.sh --also-install

      # ==========================================
      # 1. Backward Compatibility & JAX Cache Setup
      # ==========================================
      echo "Setting up backward compatibility symlink for JAX cache..."
      # Create the persistent directory first
      sudo mkdir -p /mnt/disks/persist/tpu_jax_cache
      sudo chmod 777 /mnt/disks/persist/tpu_jax_cache
      
      # Forcefully intercept old CI jobs writing to /tmp and redirect them to the persistent disk
      sudo rm -rf /tmp/tpu_jax_cache
      sudo ln -s /mnt/disks/persist/tpu_jax_cache /tmp/tpu_jax_cache

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

      ${file("${path.module}/../shared/keep-agent-connected.sh")}

      systemctl unmask buildkite-agent
      systemctl enable buildkite-agent
      systemctl start buildkite-agent
    STARTUP_SCRIPT
  }
}
