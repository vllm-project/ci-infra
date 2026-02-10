terraform {
  backend "gcs" {
    bucket = "tpu_commons_ci-infra_tf"
    prefix = "terraform/ci_v7x-cicd-us-central1-c-vllm-state"
  }
}
