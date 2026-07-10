# 1 TPU device each
# TPU v7x CI Module (Unified Single/Multi-host)
# Runtime: v2-alpha-tpu7-ubuntu2404

data "google_client_config" "config" {
  provider = google-beta
}

locals {
  # Detect multi-host based on accelerator_type suffix
  # Expected format: tpu7x-N where N is the number of cores.
  # For v7x, usually 8 cores/ 4 chips per host. (there're 2 core per chip for v7x)
  tpu_size_parts = split("-", var.accelerator_type)
  tpu_core_size  = length(local.tpu_size_parts) > 1 ? tonumber(local.tpu_size_parts[1]) : 0
  is_multi_host  = local.tpu_core_size > 8

  has_attached_disk = var.disk_size > 0
}

# Generate a SSH key pair for internal multi-host communication
resource "tls_private_key" "internal_ssh_key" {
  count     = local.is_multi_host ? var.instance_count : 0
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "google_compute_disk" "tpu_disk" {
  provider = google-beta
  count    = local.has_attached_disk ? var.instance_count : 0
  name     = "${var.accelerator_type}-ci-${count.index}-${var.project_short_name}-${data.google_client_config.config.zone}-disk"
  size     = var.disk_size
  type     = "hyperdisk-balanced"
}

resource "google_tpu_v2_vm" "tpu_v7x_ci" {
  provider = google-beta
  count    = var.instance_count

  name             = "${var.accelerator_type}-ci-${count.index}-${var.project_short_name}-${data.google_client_config.config.zone}"
  runtime_version  = "v2-alpha-tpu7-ubuntu2404"
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

  dynamic "data_disks" {
    for_each = local.has_attached_disk ? [1] : []
    content {
      source_disk = google_compute_disk.tpu_disk[count.index].id
      mode        = "READ_WRITE"
    }
  }

  metadata = {
    "startup-script" = templatefile("${path.module}/startup-script.sh.tftpl", {
      buildkite_token_value           = var.buildkite_token_value
      huggingface_token_value         = var.huggingface_token_value
      buildkite_analytics_token_value = var.buildkite_analytics_token_value
      buildkite_queue_name            = var.buildkite_queue_name
      github_app_secret_name          = var.github_app_secret_name
      is_multi_host                   = local.is_multi_host
      accelerator_type                = var.accelerator_type
      project_short_name              = var.project_short_name
      instance_index                  = count.index
      zone                            = data.google_client_config.config.zone
      private_key_pem                 = local.is_multi_host ? tls_private_key.internal_ssh_key[count.index].private_key_pem : ""
      public_key_openssh              = local.is_multi_host ? tls_private_key.internal_ssh_key[count.index].public_key_openssh : ""
      has_attached_disk               = local.has_attached_disk
    })
  }
}
