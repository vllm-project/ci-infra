# 16 nodes for CI cluster
# 1 TPU v6e device each
# Region: us-east5-b
# Type: v6e-1
# Runtime: v2-alpha-tpuv6e

resource "google_compute_disk" "disk_east5_b" {
  provider = google-beta.us-east5-b
  count    = 24

  name = "tpu-disk-east5-b-${count.index}"
  size = 2048
  type = "hyperdisk-balanced"
  zone = "us-east5-b"
}

resource "google_tpu_v2_vm" "tpu_v6_ci" {
  provider = google-beta.us-east5-b
  count    = 24
  name     = "vllm-tpu-v6-ci-${count.index}"
  zone     = "us-east5-b"

  runtime_version = "v2-alpha-tpuv6e"

  accelerator_type = "v6e-1"

  network_config {
    network             = "projects/${var.project_id}/global/networks/default"
    enable_external_ips = true
  }

  data_disks {
    source_disk = google_compute_disk.disk_east5_b[count.index].id
    mode        = "READ_WRITE"
  }

  metadata = {
    "startup-script" = <<-EOF
      #!/bin/bash

      apt-get update
      apt-get install -y curl build-essential jq git python3 python3-pip

      curl -o- https://get.docker.com/ | bash -

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

      # 2. Create the Buildkite pre-checkout hook
      cat <<'EOF' > /etc/buildkite-agent/hooks/pre-checkout
      #!/bin/bash
      set -e

      echo "--- 🔐 Generating temporary GitHub token"

      export _BK_TEMP_GITHUB_APP_PEM=$(buildkite-agent secret get ${var.github_app_secret_name})

      if [ -z "$_BK_TEMP_GITHUB_APP_PEM" ]; then
          echo "🚨 Error: Failed to fetch secret from Buildkite Secrets. Does the secret exist?"
          exit 1
      fi

      GITHUB_TOKEN=$(python3 /etc/buildkite-agent/get_github_token.py)
      unset _BK_TEMP_GITHUB_APP_PEM

      # Static URL rewrite for SSH to HTTPS, and Basic Auth header for HTTPS authentication
      git config --global url."https://github.com/".insteadOf "git@github.com:"
      AUTH_TOKEN=$(echo -n "x-access-token:$GITHUB_TOKEN" | base64 | tr -d '\n')
      git config --global http.https://github.com/.extraheader "Authorization: Basic $AUTH_TOKEN"
      EOF

      chmod 500 /etc/buildkite-agent/get_github_token.py
      chmod +x /etc/buildkite-agent/hooks/pre-checkout
      chown -R buildkite-agent:buildkite-agent /etc/buildkite-agent/
      # ==========================================

      sudo usermod -a -G docker buildkite-agent
      sudo -u buildkite-agent gcloud auth configure-docker us-central1-docker.pkg.dev --quiet

      sudo sed -i "s/xxx/${var.buildkite_token_value}/g" /etc/buildkite-agent/buildkite-agent.cfg
      sudo sed -i 's/name="%hostname-%spawn"/name="vllm-tpu-${count.index}"/' /etc/buildkite-agent/buildkite-agent.cfg
      echo 'tags="queue=tpu_v6e_queue"' | sudo tee -a /etc/buildkite-agent/buildkite-agent.cfg
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
