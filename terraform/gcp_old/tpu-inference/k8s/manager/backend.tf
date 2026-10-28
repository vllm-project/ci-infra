terraform {
  backend "gcs" {
    bucket = "tpu_commons_ci-infra_tf"
    prefix = "terraform/cloud-ullm-inference-ci-cd-k8s-state"
  }
}
