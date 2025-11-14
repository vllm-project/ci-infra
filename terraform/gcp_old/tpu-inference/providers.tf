provider "google-beta" {
  project = var.project_id
  region  = "us-south1"
  alias   = "us-south1-a"
}

provider "google-beta" {
  project = var.project_id
  region  = "us-east1"
  alias   = "us-east1-d"
}

provider "google-beta" {
  project = var.project_id
  region  = "us-east5"
  zone    = "us-east5-b"
  alias   = "us-east5-b"
}

provider "google-beta" {
  project = var.project_id
  region  = "us-central1"
  zone    = "us-central1-b"
  alias   = "us-central1-b"
}

terraform {
  backend "gcs" {
    bucket = "tpu_commons_ci-infra_tf"
    prefix = "terraform/ci_v6-vllm-state"
  }
}
