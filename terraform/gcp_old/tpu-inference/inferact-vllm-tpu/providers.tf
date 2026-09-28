provider "google-beta" {
  project = var.project_id
  region  = "us-central1"
  zone    = "us-central1-c"
  alias   = "us-central1-c"
}

terraform {
  backend "gcs" {
    bucket = "tpu_commons_ci-infra_tf"
    prefix = "terraform/inferact-vllm-tpu-vllm-state"
  }
}
