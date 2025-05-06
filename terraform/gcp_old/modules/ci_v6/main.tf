# 16 nodes for CI cluster
# 1 TPU v6e device each
# Region: us-east5-b
# Type: v6e-1
# Runtime: v2-alpha-tpuv6e

resource "google_compute_disk" "disk_east5_b" {
  provider = google-beta.us-east5-b
  count = 16

  name  = "tpu-disk-east5-b-${count.index}"
  size  = 256
  type  = "hyperdisk-balanced"
  zone  = "us-east5-b"
}

resource "google_tpu_v2_vm" "tpu_v6_ci" {
  provider = google-beta.us-east5-b
  count = 16
  name = "vllm-tpu-v6-ci-${count.index}"
  zone = "us-east5-b" 

  runtime_version = "v2-alpha-tpuv6e"

  accelerator_type = "v6e-1"

  network_config {
    network = "default"
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

      sudo sed -i "s/xxx/${var.buildkite_agent_token_ci_cluster}/g" /etc/buildkite-agent/buildkite-agent.cfg
      sudo sed -i 's/name="%hostname-%spawn"/name="vllm-tpu-${count.index}"/' /etc/buildkite-agent/buildkite-agent.cfg
      echo 'tags="queue=tpu_v6e_queue"' | sudo tee -a /etc/buildkite-agent/buildkite-agent.cfg
      echo 'HF_TOKEN=${var.huggingface_token}' | sudo tee -a /etc/environment

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