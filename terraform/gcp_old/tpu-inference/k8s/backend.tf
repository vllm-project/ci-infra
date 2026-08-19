terraform {
  backend "gcs" {
    bucket = "cloud-ullm-inference-ci-cd-tf-state"
    prefix = "buildkite-tpu-ci/foundation"
  }
}
