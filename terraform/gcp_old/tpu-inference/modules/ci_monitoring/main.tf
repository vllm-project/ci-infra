# Provision the e2-micro compute instance for monitoring
resource "google_compute_instance" "monitoring_instance" {
  provider     = google-beta
  project      = var.project_id
  name         = "vllm-ci-monitoring-cpu-0"
  machine_type = "e2-micro"

  # Use the default Compute Engine Service Account to maintain consistency with existing setup
  service_account {
    scopes = ["cloud-platform"]
  }

  boot_disk {
    auto_delete = true
    initialize_params {
      image = "projects/ubuntu-os-cloud/global/images/ubuntu-2404-noble-amd64-v20251021"
      size  = 20
      type  = "pd-standard"
    }
  }

  network_interface {
    # Allocate an ephemeral public IP so the instance can download binaries and call the Buildkite REST API
    access_config {}
    subnetwork = "projects/${var.project_id}/regions/us-central1/subnetworks/default"
  }

  metadata = {
    enable-oslogin = "true"
    "startup-script" = <<-EOF
      #!/bin/bash
      
      # Step 1: Download the official buildkite-agent-metrics binary
      wget -q https://github.com/buildkite/buildkite-agent-metrics/releases/download/v5.5.0/buildkite-agent-metrics-linux-amd64 -O /usr/local/bin/buildkite-agent-metrics
      chmod +x /usr/local/bin/buildkite-agent-metrics

      # Step 2: Configure Systemd background service
      cat << 'SERVICE_EOF' > /etc/systemd/system/bk-metrics.service
      [Unit]
      Description=Buildkite Agent Metrics Exporter
      After=network.target

      [Service]
      Type=simple
      # Use GCP backend. The binary will automatically use the default Service Account's credentials to write to Cloud Monitoring.
      ExecStart=/usr/local/bin/buildkite-agent-metrics \
        -backend stackdriver \
        -stackdriver-projectid ${var.project_id} \
        -token ${var.buildkite_token_value} \
        -interval 15s
      Restart=always
      RestartSec=10

      [Install]
      WantedBy=multi-user.target
      SERVICE_EOF

      # Step 3: Enable and start the systemd service
      systemctl daemon-reload
      systemctl enable bk-metrics
      systemctl start bk-metrics
    EOF
  }
}
