data "google_client_config" "gcp_client" {
  provider = google-beta
}

locals {
  # The zone comes from the provider, not the caller, so the name can't
  # disagree with where the VM is.
  zone = data.google_client_config.gcp_client.zone

  # One name for the VM, its disk, its address and the Buildkite agent. One VM
  # per queue, so no index.
  node_name = "vllm-ci-release-${var.purpose}-${local.zone}"
}

resource "google_compute_instance" "agent" {
  provider = google-beta
  name     = local.node_name

  boot_disk {
    auto_delete = true
    device_name = local.node_name

    initialize_params {
      image = "projects/ubuntu-os-cloud/global/images/ubuntu-2404-noble-amd64-v20251021"
      size  = var.disk_size
      type  = var.disk_type
    }

    mode = "READ_WRITE"
  }

  service_account {
    email  = var.service_account_email
    scopes = ["cloud-platform"]
  }

  can_ip_forward      = false
  deletion_protection = false
  enable_display      = false
  machine_type        = var.machine_type

  # A resize needs a stop. The startup script re-runs on boot and is
  # idempotent, so the agent comes back configured.
  allow_stopping_for_update = true

  network_interface {
    access_config {
      # Static, so the TPU Release agent token can be limited to these
      # addresses in the cluster's settings.
      nat_ip = google_compute_address.static.address
    }
    subnetwork = "projects/${var.project_id}/regions/${data.google_client_config.gcp_client.region}/subnetworks/default"
  }

  metadata = {
    enable-osconfig = "TRUE"
    enable-oslogin  = "true"
    "startup-script" = templatefile("${path.module}/startup-script.sh.tftpl", {
      node_name                   = local.node_name
      buildkite_queue_name        = var.buildkite_queue_name
      buildkite_token_secret_name = var.buildkite_token_secret_name
      allowed_pipeline_ids        = join(" ", var.allowed_pipeline_ids)
      read_secret_function        = file("${path.module}/../shared/read-secret.sh")
      keep_agent_connected        = file("${path.module}/../shared/keep-agent-connected.sh")
    })
  }
}

resource "google_compute_address" "static" {
  provider = google-beta
  name     = "${local.node_name}-ip"
}
