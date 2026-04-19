# =====================================================================
# PART 1: INFRASTRUCTURE MONITORING (Ops Metrics)
# This section maintains the existing e2-micro instance that runs the 
# buildkite-agent-metrics binary to export queue/agent metrics.
# =====================================================================
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

# =====================================================================
# PART 2: ANALYTICAL DATA STORAGE (BigQuery)
# =====================================================================

resource "google_bigquery_dataset" "ci_analytics" {
  dataset_id  = "ci_efficiency_metrics"
  project     = var.project_id
  location    = "us-central1" # Regional location for compliance
  description = "Analytical store for Buildkite execution performance"
}

resource "google_bigquery_table" "step_logs" {
  dataset_id          = google_bigquery_dataset.ci_analytics.dataset_id
  project             = var.project_id
  table_id            = "step_execution_logs"
  deletion_protection = true 

  schema = <<EOF
[
  {"name": "build_id", "type": "STRING", "mode": "REQUIRED"},
  {"name": "commit_hash", "type": "STRING", "mode": "NULLABLE"},
  {"name": "step_name", "type": "STRING", "mode": "REQUIRED"},
  {"name": "pipeline_slug", "type": "STRING", "mode": "NULLABLE"},
  {"name": "branch", "type": "STRING", "mode": "NULLABLE"},
  {"name": "state", "type": "STRING", "mode": "NULLABLE"},
  {"name": "wait_duration_sec", "type": "FLOAT", "mode": "NULLABLE"},
  {"name": "run_duration_sec", "type": "FLOAT", "mode": "NULLABLE"},
  {"name": "created_at", "type": "TIMESTAMP", "mode": "NULLABLE"}
]
EOF
}

# =====================================================================
# PART 3: FUNCTION SOURCE CODE STORAGE (GCS)
# =====================================================================

resource "google_storage_bucket" "source_bucket" {
  name                        = "${var.project_id}-bk-webhook-src"
  project                     = var.project_id
  location                    = "us-central1"
  force_destroy               = true
  uniform_bucket_level_access = true # Mandatory for some enterprise Org Policies
}

data "archive_file" "function_zip" {
  type        = "zip"
  source_dir  = "${path.module}/src"
  output_path = "${path.module}/files/webhook_receiver.zip"
}

resource "google_storage_bucket_object" "source_object" {
  name   = "src-${data.archive_file.function_zip.output_md5}.zip"
  bucket = google_storage_bucket.source_bucket.name
  source = data.archive_file.function_zip.output_path
}

# =====================================================================
# PART 4: SECURITY & PERMISSIONS (IAM)
# Fetching the API Access Token from Secret Manager
# =====================================================================

data "google_secret_manager_secret_version" "webhook_secret_val" {
  project = var.project_id
  secret  = "vllm_buildkite_rest_api_token"
  version = "latest"
}

resource "google_service_account" "function_sa" {
  account_id   = "ci-webhook-receiver-sa"
  project      = var.project_id
  display_name = "SA for Buildkite Webhook Function"
}

resource "google_project_iam_member" "bq_editor" {
  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.function_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "secret_access" {
  project   = var.project_id
  secret_id = data.google_secret_manager_secret_version.webhook_secret_val.secret
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.function_sa.email}"
}

# =====================================================================
# PART 5: THE PULLER (Cloud Functions v2)
# Internal function triggered by Scheduler (No public internet access)
# =====================================================================

resource "google_cloudfunctions2_function" "webhook_receiver" {
  name        = "buildkite-webhook-handler"
  project     = var.project_id
  location    = "us-central1"
  description = "Polls Buildkite API and stores events into BigQuery"

  build_config {
    runtime     = "python310"
    entry_point = "handle_webhook"
    source {
      storage_source {
        bucket = google_storage_bucket.source_bucket.name
        object = google_storage_bucket_object.source_object.name
      }
    }
  }

  service_config {
    max_instance_count    = 3
    available_memory      = "256M"
    timeout_seconds       = 60
    service_account_email = google_service_account.function_sa.email

    environment_variables = {
      BQ_TABLE_ID    = "${var.project_id}.${google_bigquery_dataset.ci_analytics.dataset_id}.${google_bigquery_table.step_logs.table_id}"
      WEBHOOK_SECRET = data.google_secret_manager_secret_version.webhook_secret_val.secret_data
      PIPELINE_SLUG  = var.pipeline_slug
      ORG_SLUG       = var.org_slug
    }
  }
}

# =====================================================================
# PART 6: SCHEDULER
# =====================================================================

resource "google_service_account" "scheduler_sa" {
  account_id   = "ci-scheduler-sa"
  project      = var.project_id
  display_name = "SA to trigger CI Polling Function"
}

resource "google_cloud_run_service_iam_member" "scheduler_invoker" {
  project  = google_cloudfunctions2_function.webhook_receiver.project
  location = google_cloudfunctions2_function.webhook_receiver.location
  service  = google_cloudfunctions2_function.webhook_receiver.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_sa.email}"
}

resource "google_cloud_scheduler_job" "pull_trigger" {
  name             = "buildkite-metrics-puller"
  project          = var.project_id
  region           = "us-central1"
  schedule         = "*/10 * * * *" # Runs every 10 minutes
  time_zone        = "UTC"
  attempt_deadline = "320s"

  http_target {
    http_method = "POST"
    uri         = google_cloudfunctions2_function.webhook_receiver.url
    oidc_token {
      service_account_email = google_service_account.scheduler_sa.email
    }
  }
}
