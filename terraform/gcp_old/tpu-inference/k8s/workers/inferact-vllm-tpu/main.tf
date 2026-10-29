# The workers in inferact-vllm-tpu: one environment, one state. The project is
# Inferact's, outside the google.com org, and the fleet's operators can create
# instances and buckets in it but not GKE clusters, service accounts or IAM
# bindings, so it is applied by whoever holds those there. See
# modules/worker/README.md.
#
# The state is beside the bare-metal agents' (../../../inferact-vllm-tpu/),
# in the fleet's bucket, so whoever applies needs objects there as well.
terraform {
  required_version = ">= 1.5.0"

  backend "gcs" {
    bucket = "tpu_commons_ci-infra_tf"
    prefix = "terraform/inferact-vllm-tpu-k8s-workers"
  }
}

# For the one Kubernetes object the apply creates: the binding that lets the
# fleet's deployer account into the cluster (modules/worker/deploy.tf). It
# authenticates as whoever runs the apply.
data "google_client_config" "default" {}

provider "kubernetes" {
  host                   = "https://${module.inferact.endpoint}"
  cluster_ca_certificate = base64decode(module.inferact.ca_certificate)
  token                  = data.google_client_config.default.access_token
}

locals {
  fleet = {
    project_id     = "cloud-ullm-inference-ci-cd"
    project_number = "443452445451"
    name_prefix    = "tpu-ci"
    namespace      = "buildkite"
  }

  # The fleet's quietest hours; see maintenance_window in
  # manager/prod.auto.tfvars.
  maintenance_window = {
    start_time = "2026-10-10T15:00:00Z"
    end_time   = "2026-10-10T23:00:00Z"
    recurrence = "FREQ=WEEKLY;BYDAY=SA,SU"
  }

  labels = {
    environment = "production"
    workload    = "tpu-ci"
    owner       = "tpu-inference"
  }
}
