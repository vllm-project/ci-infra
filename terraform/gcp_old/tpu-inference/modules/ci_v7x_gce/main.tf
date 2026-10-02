# TPU v7x CI Module on Compute Engine (tpu7x-standard-4t)
#
# For projects that get v7x through Compute Engine rather than the Cloud TPU
# API that ci_v7x uses. The hosts are ordinary instances, so what the TPU API
# does for a node is done here: each slice is a managed instance group bound
# to a specific reservation, and a two-host slice gets a workload policy so
# its hosts share one ICI domain. Compute Engine still fills in the TPU
# metadata the CI scripts read (accelerator-type, agent-worker-number,
# worker-network-endpoints, tpu-env).

data "google_client_config" "config" {
  provider = google-beta
}

locals {
  is_multi_host = var.hosts_per_slice > 1
  zone          = data.google_client_config.config.zone

  # Same scheme as ci_v7x, so a Buildkite agent maps onto its slice. The shape
  # is spelled like a Cloud TPU accelerator type, which counts TensorCores:
  # 8 per tpu7x-standard-4t host.
  slice_names = toset([for i in range(var.slice_count) :
    "tpu7x-${var.hosts_per_slice * 8}-ci-${i}-${var.project_short_name}-${local.zone}"
  ])

  # base_instance_name below numbers a slice's hosts -w-001, -w-002, ...
  slice_hosts = { for s in local.slice_names : s => [for n in range(var.hosts_per_slice) :
    format("%s-w-%03d.%s.c.%s.internal", s, n + 1, local.zone, var.project_id)
  ] }
}

# Generate a SSH key pair for internal multi-host communication
resource "tls_private_key" "internal_ssh_key" {
  for_each  = local.is_multi_host ? local.slice_names : toset([])
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "google_compute_resource_policy" "slice" {
  provider = google-beta
  for_each = local.is_multi_host ? local.slice_names : toset([])
  name     = each.key

  workload_policy {
    type                 = "HIGH_THROUGHPUT"
    accelerator_topology = var.topology
  }
}

resource "google_compute_instance_template" "slice" {
  provider     = google-beta
  for_each     = local.slice_names
  name_prefix  = "${each.key}-"
  machine_type = "tpu7x-standard-4t"

  labels = {
    vm_name = each.key
  }

  disk {
    source_image = "projects/ubuntu-os-accelerator-images/global/images/ubuntu-accel-2404-amd64-tpu-tpu7x-v20260716"
    disk_type    = "hyperdisk-balanced"
    disk_size_gb = var.boot_disk_size
    boot         = true
    auto_delete  = true
  }

  network_interface {
    subnetwork = var.subnetwork
    nic_type   = "GVNIC"
  }

  metadata = {
    "startup-script" = templatefile("${path.module}/startup-script.sh.tftpl", {
      buildkite_token_secret_name           = var.buildkite_token_secret_name
      huggingface_token_secret_name         = var.huggingface_token_secret_name
      buildkite_analytics_token_secret_name = var.buildkite_analytics_token_secret_name
      read_secret_function                  = file("${path.module}/../shared/read-secret.sh")
      buildkite_queue_name                  = var.buildkite_queue_name
      github_app_secret_name                = var.github_app_secret_name
      is_multi_host                         = local.is_multi_host
      host_name                             = each.key
      head_host                             = local.slice_hosts[each.key][0]
      worker_hosts                          = join(" ", slice(local.slice_hosts[each.key], 1, var.hosts_per_slice))
      private_key_pem                       = local.is_multi_host ? tls_private_key.internal_ssh_key[each.key].private_key_pem : ""
      public_key_openssh                    = local.is_multi_host ? tls_private_key.internal_ssh_key[each.key].public_key_openssh : ""
      keep_agent_connected                  = file("${path.module}/../shared/keep-agent-connected.sh")
      git_setup                             = chomp(var.vllm_torchtpu_ssh_checkout ? file("${path.module}/../shared/git-ssh-checkout-setup.sh") : file("${path.module}/../shared/git-https-setup.sh"))
    })
  }

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

  lifecycle {
    create_before_destroy = true
  }
}

resource "google_compute_instance_group_manager" "slice" {
  provider           = google-beta
  for_each           = local.slice_names
  name               = each.key
  zone               = local.zone
  base_instance_name = "${each.key}-w-###[1]"
  target_size        = var.hosts_per_slice

  version {
    instance_template = google_compute_instance_template.slice[each.key].self_link_unique
  }

  # A multi-host slice is created all at once under its workload policy, so
  # its hosts land in one ICI domain.
  dynamic "target_size_policy" {
    for_each = local.is_multi_host ? [1] : []
    content {
      mode = "BULK"
    }
  }

  dynamic "resource_policies" {
    for_each = local.is_multi_host ? [1] : []
    content {
      workload_policy = google_compute_resource_policy.slice[each.key].self_link
    }
  }

  # Leave a host that fails to start for inspection instead of recreating it.
  instance_lifecycle_policy {
    default_action_on_failure = "DO_NOTHING"
  }

  # A new template, such as a rotated token, reaches hosts created afterwards.
  # It never replaces a host that may be running a job.
  update_policy {
    type                  = "OPPORTUNISTIC"
    minimal_action        = "REPLACE"
    replacement_method    = "SUBSTITUTE"
    max_surge_fixed       = 1
    max_unavailable_fixed = 1
  }

  wait_for_instances        = true
  wait_for_instances_status = "STABLE"
}
