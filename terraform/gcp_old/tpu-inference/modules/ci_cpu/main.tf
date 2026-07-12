# 8 nodes for CI cluster
# normal e2 instance device each
# Region: us-east5-b
# Type: e2-standard-2

data "google_client_config" "gcp_client" {
  provider = google-beta
}

resource "google_compute_instance" "buildkite-agent-instance" {
  provider = google-beta
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
    subnetwork = "projects/${var.project_id}/regions/${data.google_client_config.gcp_client.region}/subnetworks/default"
  }

  metadata = {
    enable-osconfig  = "TRUE"
    enable-oslogin   = "true"
    "startup-script" = <<-STARTUP_SCRIPT
      #!/bin/bash

      apt-get update
      apt-get install -y curl build-essential jq git python3 python3-pip
      sudo curl -L https://github.com/mikefarah/yq/releases/download/v4.53.2/yq_linux_amd64 -o /usr/bin/yq && sudo chmod +x /usr/bin/yq

      curl -o- https://get.docker.com/ | bash -

      curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
      /root/.cargo/bin/cargo install minijinja-cli
      cp /root/.cargo/bin/minijinja-cli /usr/bin/minijinja-cli
      chmod 777 /usr/bin/minijinja-cli

      curl -fsSL "https://packages.buildkite.com/buildkite/cli-deb/gpgkey" | sudo gpg --dearmor -o /usr/share/keyrings/buildkite_cli-deb-archive-keyring.gpg
      echo -e "deb [signed-by=/usr/share/keyrings/buildkite_cli-deb-archive-keyring.gpg] https://packages.buildkite.com/buildkite/cli-deb/any/ any main\ndeb-src [signed-by=/usr/share/keyrings/buildkite_cli-deb-archive-keyring.gpg] https://packages.buildkite.com/buildkite/cli-deb/any/ any main" | sudo tee /etc/apt/sources.list.d/buildkite-buildkite-cli-deb.list

      curl -fsSL https://keys.openpgp.org/vks/v1/by-fingerprint/32A37959C2FA5C3C99EFBC32A79206696452D198 | sudo gpg --dearmor -o /usr/share/keyrings/buildkite-agent-archive-keyring.gpg
      echo "deb [signed-by=/usr/share/keyrings/buildkite-agent-archive-keyring.gpg] https://apt.buildkite.com/buildkite-agent stable main" | sudo tee /etc/apt/sources.list.d/buildkite-agent.list
      apt-get update
      apt-get install -y bk buildkite-agent
           
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

      sudo sed -i "s/xxx/${var.buildkite_token_value}/g" /etc/buildkite-agent/buildkite-agent.cfg
      sudo sed -i 's/name="%hostname-%spawn"/name="vllm-cpu-vm-${count.index}"/' /etc/buildkite-agent/buildkite-agent.cfg
      echo 'tags="queue=cpu"' | sudo tee -a /etc/buildkite-agent/buildkite-agent.cfg
      echo 'HF_TOKEN=${var.huggingface_token_value}' | sudo tee -a /etc/environment

      systemctl stop docker
      systemctl start docker

      systemctl enable buildkite-agent
      systemctl start buildkite-agent
    STARTUP_SCRIPT
  }
}

resource "google_compute_address" "static" {
  provider = google-beta
  name     = "vllm-ci-cpu-${count.index}-ip"
  count    = var.instance_count
}
