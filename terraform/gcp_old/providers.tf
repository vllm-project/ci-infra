provider "google-beta" {
  project = var.project_id
  region  = "us-south1-a"
  alias   = "us-south1-a"
}

provider "google-beta" {
  project = var.project_id
  region  = "us-east1-d"
  alias   = "us-east1-d"
}

provider "google-beta" {
  project = var.project_id
  region  = "us-east5-b"
  zone    = "us-east5-b"
  alias   = "us-east5-b"
}

terraform {
  backend "gcs" {
    bucket = "vllm-tf-state"
    prefix = "terraform/state"
  }
}
