# Agents for the TPU Release Buildkite cluster, which runs only the pipelines
# that publish images. The Docker Hub tokens are cluster secrets there, so no
# agent in the TPU cluster can fetch them, and these VMs are the only agents in
# TPU Release.
#
# They run as their own account rather than ci-agent-cpu. Every CI job on the
# cpu fleets can use ci-agent-cpu, so if it could read the release agent token,
# any of those jobs could start an agent on a release queue and be handed the
# next publish job, secrets included.
#
# One VM per queue, so each pre-bootstrap allowlist names one pipeline. The
# queues, the agent token and the cluster secrets are made in Buildkite and with
# gcloud, outside Terraform, like the other agent tokens in main.tf.

locals {
  release_buildkite_token_secret_name = "projects/${var.project_id}/secrets/tpu_release_buildkite_agent_token"
}

resource "google_service_account" "ci_release" {
  project      = var.project_id
  account_id   = "ci-release"
  display_name = "Buildkite agents: TPU Release cluster"
}

data "google_secret_manager_secret" "release_agent_token" {
  project   = var.project_id
  secret_id = "tpu_release_buildkite_agent_token"
}

resource "google_secret_manager_secret_iam_member" "ci_release_agent_token" {
  project   = data.google_secret_manager_secret.release_agent_token.project
  secret_id = data.google_secret_manager_secret.release_agent_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.ci_release.email}"
}

# vllm-torchtpu's published images (publish_nightly_images.sh). The build also
# installs torch-tpu from ml-oss-artifacts-transient's Python registry, whose
# read grant is made by that project's owners, outside this repo.
data "google_artifact_registry_repository" "release_vllm_torchtpu" {
  project       = var.project_id
  location      = "us-central1"
  repository_id = "vllm-torchtpu"
}

resource "google_artifact_registry_repository_iam_member" "ci_release_vllm_torchtpu" {
  project    = data.google_artifact_registry_repository.release_vllm_torchtpu.project
  location   = data.google_artifact_registry_repository.release_vllm_torchtpu.location
  repository = data.google_artifact_registry_repository.release_vllm_torchtpu.repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.ci_release.email}"
}

# The startup script's output in Cloud Logging, which is the only way to see a
# failed boot without SSH.
resource "google_project_iam_member" "ci_release_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.ci_release.email}"
}

module "ci_release_tpu_inference" {
  source = "../modules/ci_release"
  providers = {
    google-beta = google-beta.us-central1-b
  }

  project_id                  = var.project_id
  purpose                     = "tpu-inference"
  buildkite_queue_name        = "release-tpu-inference"
  buildkite_token_secret_name = local.release_buildkite_token_secret_name
  # tpu-inference-nightly-image
  allowed_pipeline_ids = ["01a11838-ee3f-4bbd-9a52-e311a6cf7b71"]

  # The cpu fleet's machine, where this build takes 15-21 min today.
  machine_type = "e2-standard-2"
  disk_size    = 100

  service_account_email = google_service_account.ci_release.email
}

module "ci_release_vllm_torchtpu" {
  source = "../modules/ci_release"
  providers = {
    google-beta = google-beta.us-central1-b
  }

  project_id                  = var.project_id
  purpose                     = "vllm-torchtpu"
  buildkite_queue_name        = "release-vllm-torchtpu"
  buildkite_token_secret_name = local.release_buildkite_token_secret_name
  # vllm-torchtpu-nightly-image
  allowed_pipeline_ids = ["01a03701-56d0-48db-9f8f-ad6c2bb80daf"]

  # A quarter of cpu_64_core, where the three images take 7-8 min. It builds
  # once a day, so a slower build costs only that build's wall clock, while a
  # 64-core VM would sit idle the rest of the day.
  machine_type = "n2d-standard-16"
  disk_size    = 250

  service_account_email = google_service_account.ci_release.email
}
