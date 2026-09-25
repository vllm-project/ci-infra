terraform {
  required_version = ">= 1.7"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 7.46.0" }
  }
  backend "gcs" {
    bucket = "inferact-terraform-states"
    prefix = "inferact-vllm-tpu/buildkite-pools"
  }
}
provider "google" {
  project = "inferact-vllm-tpu"
  region  = "us-central1"
  zone    = "us-central1-c"
}

# Shared network, NAT, service account and bootstrap secret remain owned by the
# existing buildkite-kevin-test state. Retiring its TPU slice must retain them.
data "google_compute_subnetwork" "ci" {
  name   = "tpu-v7x-32-kevin-test"
  region = "us-central1"
}

locals {
  pools = {
    v7x_16 = {
      slice_count     = 2
      hosts_per_slice = 2
      topology        = "2x2x2"
      queue           = "tpu_v7x_16_queue"
    }
    v7x_8 = {
      slice_count     = 4
      hosts_per_slice = 1
      topology        = null
      queue           = "tpu_v7x_8_queue"
    }
  }
}

module "ci" {
  for_each              = local.pools
  source                = "../modules/ci_v7x_compute"
  name                  = replace("inferact-${each.key}", "_", "-")
  slice_count           = each.value.slice_count
  hosts_per_slice       = each.value.hosts_per_slice
  topology              = each.value.topology
  target_queue          = each.value.queue
  validation_queue      = "${each.value.queue}_test"
  project_id            = "inferact-vllm-tpu"
  zone                  = "us-central1-c"
  reservation_name      = "ghostfish-9mpeile911sjq"
  subnetwork            = data.google_compute_subnetwork.ci.self_link
  service_account_email = "tpu-v7x-32-kevin-test@inferact-vllm-tpu.iam.gserviceaccount.com"
  bootstrap_secret      = "tpu-v7x-32-kevin-test"
}

output "pools" {
  value = { for key, pool in local.pools : key => {
    validation_queue = "${pool.queue}_test"
    target_queue     = pool.queue
    agents           = pool.slice_count
    hosts            = pool.slice_count * pool.hosts_per_slice
    physical_chips   = pool.slice_count * pool.hosts_per_slice * 4
    devices_per_job  = pool.hosts_per_slice * 8
  } }
}
