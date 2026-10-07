terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.41"
    }
    # Only for google_project_service_identity, which has no GA resource; see
    # dashboard.tf.
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 7.41"
    }
  }
}
