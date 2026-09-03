data "google_client_config" "gcp_client" {
  provider = google-beta
}

locals {
  # The zone comes from the provider, not the caller, so a fleet cannot be
  # mislabelled by passing the wrong suffix.
  zone = data.google_client_config.gcp_client.zone

  # One name for the VM, its disk, its address, and the Buildkite agent, so a
  # queue entry in the Buildkite UI maps straight onto a GCE instance.
  # An empty purpose keeps the original names, so the tpu-commons fleet is not
  # renamed and therefore not recreated.
  node_names = [for i in range(var.instance_count) :
    var.purpose == "" ? "vllm-ci-cpu-64-core-${i}" : "vllm-ci-cpu-64-core-${var.purpose}-${local.zone}-${i}"
  ]

  # Addresses are regional, so under the legacy naming they had to carry a zone
  # suffix of their own. purpose puts the zone in the name, so it needs none.
  address_names = [for i in range(var.instance_count) :
    var.purpose == "" ? "vllm-ci-cpu-64-core${var.resource_suffix}-${i}-ip" : "${local.node_names[i]}-ip"
  ]
}

resource "google_compute_instance" "buildkite-agent-instance" {
  provider = google-beta
  count    = var.instance_count
  name     = local.node_names[count.index]

  boot_disk {
    auto_delete = true
    device_name = local.node_names[count.index]

    initialize_params {
      image = "projects/ubuntu-os-cloud/global/images/ubuntu-2404-noble-amd64-v20251021"
      size  = var.disk_size
      type  = var.disk_type
    }

    mode = "READ_WRITE"
  }

  service_account {
    scopes = ["cloud-platform"]
  }

  can_ip_forward      = false
  deletion_protection = false
  enable_display      = false
  machine_type        = var.machine_type

  # Resizing a running instance is refused unless this is set. These are
  # disposable CI agents, and a stop/start re-runs the startup script, which is
  # now idempotent, so the agent comes back configured rather than duplicated.
  allow_stopping_for_update = true

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
      set -e

      apt-get update
      apt-get install -y curl build-essential jq git python3 python3-pip

      curl -o- https://get.docker.com/ | bash -

      curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y

      /root/.cargo/bin/cargo install minijinja-cli || true
      cp /root/.cargo/bin/minijinja-cli /usr/bin/minijinja-cli
      chmod 777 /usr/bin/minijinja-cli

      curl -fsSL "https://packages.buildkite.com/buildkite/cli-deb/gpgkey" | sudo gpg --dearmor --yes -o /usr/share/keyrings/buildkite_cli-deb-archive-keyring.gpg
      echo -e "deb [signed-by=/usr/share/keyrings/buildkite_cli-deb-archive-keyring.gpg] https://packages.buildkite.com/buildkite/cli-deb/any/ any main\ndeb-src [signed-by=/usr/share/keyrings/buildkite_cli-deb-archive-keyring.gpg] https://packages.buildkite.com/buildkite/cli-deb/any/ any main" | sudo tee /etc/apt/sources.list.d/buildkite-buildkite-cli-deb.list

      curl -fsSL https://keys.openpgp.org/vks/v1/by-fingerprint/32A37959C2FA5C3C99EFBC32A79206696452D198 | sudo gpg --dearmor --yes -o /usr/share/keyrings/buildkite-agent-archive-keyring.gpg
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

      # This script re-runs on every boot, so match the whole line rather than
      # the pristine package default, which is gone after the first boot. The
      # name below needs no such treatment: it is the instance name, which is
      # ForceNew, so a rename recreates the VM and this sed always runs against
      # a freshly installed cfg.
      sudo sed -i -E 's|^token=.*|token="${var.buildkite_token_value}"|' /etc/buildkite-agent/buildkite-agent.cfg
      sudo sed -i 's/name="%hostname-%spawn"/name="${local.node_names[count.index]}"/' /etc/buildkite-agent/buildkite-agent.cfg
      sudo sed -i '/^tags=/d' /etc/buildkite-agent/buildkite-agent.cfg
      echo 'tags="queue=${var.buildkite_queue_name}"' | sudo tee -a /etc/buildkite-agent/buildkite-agent.cfg
      sudo sed -i '/^HF_TOKEN=/d' /etc/environment
      # tee echoes to stdout, which the startup script sends to the serial
      # console, where anyone with compute.instances.getSerialPortOutput can
      # read it. Secrets go to the file only.
      echo 'HF_TOKEN=${var.huggingface_token_value}' | sudo tee -a /etc/environment > /dev/null

      ${file("${path.module}/../shared/keep-agent-connected.sh")}

      systemctl stop docker
      systemctl start docker

      systemctl enable buildkite-agent
      systemctl restart buildkite-agent
    STARTUP_SCRIPT
  }
}

resource "google_compute_address" "static" {
  provider = google-beta
  name     = local.address_names[count.index]
  count    = var.instance_count
}
