# The workers in cloud-ullm-inference-ci-cd: one environment, one state, applied
# by whoever administers the project. Each worker is a module block in a file
# of its own name.
#
# manager/ grants each worker what it needs from the fleet's project once its
# name is in manager/prod.auto.tfvars, and generate_manifests.py reads its
# shapes and quotas from its module block, so they are stated once. That is
# why every argument there is a literal, or one of the locals below, which the
# generator also reads. See README "Add a worker".
terraform {
  required_version = ">= 1.7.0"

  backend "gcs" {
    bucket = "tpu_commons_ci-infra_tf"
    prefix = "terraform/cloud-ullm-inference-ci-cd-k8s-workers"
  }
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
