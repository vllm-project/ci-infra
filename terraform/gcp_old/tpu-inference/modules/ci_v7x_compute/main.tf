terraform {
  required_providers {
    google = { source = "hashicorp/google", version = ">= 7.46.0, < 8.0.0" }
  }
}
locals {
  slices = { for i in range(var.slice_count) : format("%s-%02d", var.name, i + 1) => i }
  files  = { "bootstrap.sh" = "0755", "tpu-run" = "0755", "smoke.py" = "0644", "git-credential-github-app" = "0755", "get-github-token.py" = "0755", "keep-agent-connected.sh" = "0755" }
}
resource "google_compute_resource_policy" "slice" {
  for_each = var.hosts_per_slice > 1 ? local.slices : {}
  name     = each.key
  workload_policy {
    type                 = "HIGH_THROUGHPUT"
    accelerator_topology = var.topology
  }
}
resource "google_compute_instance_template" "ci" {
  for_each     = local.slices
  name_prefix  = "${each.key}-"
  machine_type = "tpu7x-standard-4t"
  labels       = { purpose = "buildkite", owner = "kevin", slice = each.key }
  disk {
    source_image = "projects/ubuntu-os-accelerator-images/global/images/ubuntu-accel-2404-amd64-tpu-tpu7x-v20260716"
    disk_type    = "hyperdisk-balanced"
    disk_size_gb = 1024
    boot         = true
    auto_delete  = true
  }
  network_interface {
    subnetwork = var.subnetwork
    nic_type   = "GVNIC"
  }
  metadata = {
    enable-oslogin     = "TRUE"
    serial-port-enable = "TRUE"
    tpu-ci-config = jsonencode({
      slice            = each.key
      target_queue     = var.target_queue
      validation_queue = var.validation_queue
      bootstrap_secret = var.bootstrap_secret
      hosts            = [for n in range(var.hosts_per_slice) : format("%s-w-%03d.%s.c.%s.internal", each.key, n + 1, var.zone, var.project_id)]
    })
  }
  metadata_startup_script = join("\n", concat([
    "#!/bin/bash", "set -euo pipefail", "install -d -m 0755 /opt/tpu-ci",
    "curl --fail --retry 5 -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/attributes/tpu-ci-config > /opt/tpu-ci/config.json"
  ], [for name, mode in local.files : "echo '${filebase64(name == "keep-agent-connected.sh" ? "${path.module}/../shared/${name}" : "${path.module}/${name}")}' | base64 -d > /opt/tpu-ci/${name}\nchmod ${mode} /opt/tpu-ci/${name}"], ["exec /opt/tpu-ci/bootstrap.sh"]))
  service_account {
    email  = var.service_account_email
    scopes = ["cloud-platform"]
  }
  scheduling {
    provisioning_model          = "RESERVATION_BOUND"
    on_host_maintenance         = "TERMINATE"
    automatic_restart           = true
    instance_termination_action = "DELETE"
  }
  reservation_affinity {
    type = "SPECIFIC_RESERVATION"
    specific_reservation {
      key    = "compute.googleapis.com/reservation-name"
      values = [var.reservation_name]
    }
  }
  shielded_instance_config {
    enable_secure_boot          = false
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }
  lifecycle { create_before_destroy = true }
}
resource "google_compute_instance_group_manager" "slice" {
  for_each           = local.slices
  name               = each.key
  base_instance_name = "${each.key}-w-###[1]"
  target_size        = var.hosts_per_slice
  version { instance_template = google_compute_instance_template.ci[each.key].self_link }
  dynamic "target_size_policy" {
    for_each = var.hosts_per_slice > 1 ? [1] : []
    content { mode = "BULK" }
  }
  instance_lifecycle_policy { default_action_on_failure = "DO_NOTHING" }
  dynamic "resource_policies" {
    for_each = var.hosts_per_slice > 1 ? [1] : []
    content { workload_policy = google_compute_resource_policy.slice[each.key].self_link }
  }
  wait_for_instances        = true
  wait_for_instances_status = "STABLE"
  lifecycle {
    precondition {
      condition     = (var.hosts_per_slice == 1 && var.topology == null) || (var.hosts_per_slice == 2 && var.topology == "2x2x2")
      error_message = "Two-host slices require topology 2x2x2; independent single hosts have no workload policy."
    }
  }
}
