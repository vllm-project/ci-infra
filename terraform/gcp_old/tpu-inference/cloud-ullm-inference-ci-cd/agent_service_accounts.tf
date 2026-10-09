# Identities for the Buildkite agent VMs, replacing the project's default
# compute service account, whose project-wide roles are far broader than the
# jobs need. Every job on an agent VM can use the VM's account through the
# metadata server, so these accounts get only what the jobs use, scoped to the
# resource where possible.
#
# The cpu and TPU fleets get separate accounts so either can be narrowed later
# without touching the other's VMs: a TPU VM's service account only changes by
# recreating the VM.
#
# Each resource is looked up with a data source first, so a plan fails early
# if one is missing or renamed instead of granting on a name that doesn't exist.

resource "google_service_account" "ci_agent_cpu" {
  project      = var.project_id
  account_id   = "ci-agent-cpu"
  display_name = "Buildkite agents: cpu and cpu_64_core fleets"
}

resource "google_service_account" "ci_agent_tpu" {
  project      = var.project_id
  account_id   = "ci-agent-tpu"
  display_name = "Buildkite agents: v6e and v7x fleets"
}

locals {
  ci_agent_members = {
    cpu = "serviceAccount:${google_service_account.ci_agent_cpu.email}"
    tpu = "serviceAccount:${google_service_account.ci_agent_tpu.email}"
  }

  # Project-wide roles that are read-only or write-only telemetry.
  ci_agent_project_roles = {
    cpu = [
      "roles/artifactregistry.reader",
      "roles/bigquery.jobUser",
      "roles/logging.logWriter",
      "roles/monitoring.metricWriter",
    ]
    tpu = [
      "roles/artifactregistry.reader",
      # Benchmark result uploads (upload_results.py, tpu-inference report_result.sh).
      "roles/bigquery.jobUser",
      "roles/logging.logWriter",
      "roles/monitoring.metricWriter",
      # Multi-host jobs find their peers with `gcloud compute tpus tpu-vm describe`.
      "roles/tpu.viewer",
    ]
  }

  # The startup script reads these at boot (modules/shared/read-secret.sh).
  ci_agent_secrets = [
    "vllm_buildkite_agent_token",
    "vllm_buildkite_analytics_token",
    "vllm_buildkite_hf_token",
  ]

  # Artifact Registry repositories (in this project) the agents push to. The
  # vllm-torchtpu image builds also install torch-tpu from a Python registry in
  # another project, which grants ci-agent-cpu read access outside this repo.
  # Both repos build CI images on cpu_64_core, and tpu-inference also builds
  # on the TPU hosts (run_in_docker.sh). Nightly published images
  # (publish_nightly_images.sh) build on cpu_64_core.
  ci_agent_repo_writers = {
    "tpu-inference-ci" = ["cpu", "tpu"]
    "vllm-torchtpu"    = ["cpu"]
    "vllm-torchtpu-ci" = ["cpu", "tpu"]
  }

  # Bucket-scoped object roles. Only jobs on the TPU queues use these buckets.
  ci_agent_bucket_roles = {
    "tpu-commons-ci"                         = { role = "roles/storage.objectUser", fleets = ["tpu"] }
    "tpu-inference-hf-llm-model-checkpoints" = { role = "roles/storage.objectViewer", fleets = ["tpu"] }
    "ullm-ci-cache"                          = { role = "roles/storage.objectUser", fleets = ["tpu"] }
    "vllm-bm-bk-storage"                     = { role = "roles/storage.objectCreator", fleets = ["tpu"] }
    "vllm-cb-storage2"                       = { role = "roles/storage.objectViewer", fleets = ["tpu"] }
  }
}

data "google_secret_manager_secret" "ci_agent" {
  for_each = toset(local.ci_agent_secrets)

  project   = var.project_id
  secret_id = each.value
}

data "google_artifact_registry_repository" "ci_agent" {
  for_each = local.ci_agent_repo_writers

  project       = var.project_id
  location      = "us-central1"
  repository_id = each.key
}

data "google_storage_bucket" "ci_agent" {
  for_each = local.ci_agent_bucket_roles

  name = each.key
}

# Benchmark results (upload_results.py, tpu-inference report_bigquery.py).
data "google_bigquery_dataset" "benchmark" {
  project    = var.project_id
  dataset_id = "llm_benchmark_analytics"
}

resource "google_project_iam_member" "ci_agent" {
  for_each = merge([
    for fleet, roles in local.ci_agent_project_roles : {
      for role in roles : "${fleet}/${role}" => { fleet = fleet, role = role }
    }
  ]...)

  project = var.project_id
  role    = each.value.role
  member  = local.ci_agent_members[each.value.fleet]
}

resource "google_secret_manager_secret_iam_member" "ci_agent" {
  for_each = {
    for pair in setproduct(local.ci_agent_secrets, keys(local.ci_agent_members)) :
    "${pair[0]}/${pair[1]}" => { secret_id = pair[0], fleet = pair[1] }
  }

  project   = data.google_secret_manager_secret.ci_agent[each.value.secret_id].project
  secret_id = data.google_secret_manager_secret.ci_agent[each.value.secret_id].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = local.ci_agent_members[each.value.fleet]
}

resource "google_artifact_registry_repository_iam_member" "ci_agent_writer" {
  for_each = merge([
    for repo, fleets in local.ci_agent_repo_writers : {
      for fleet in fleets : "${repo}/${fleet}" => { repo = repo, fleet = fleet }
    }
  ]...)

  project    = data.google_artifact_registry_repository.ci_agent[each.value.repo].project
  location   = data.google_artifact_registry_repository.ci_agent[each.value.repo].location
  repository = data.google_artifact_registry_repository.ci_agent[each.value.repo].repository_id
  role       = "roles/artifactregistry.writer"
  member     = local.ci_agent_members[each.value.fleet]
}

resource "google_storage_bucket_iam_member" "ci_agent" {
  for_each = merge([
    for bucket, grant in local.ci_agent_bucket_roles : {
      for fleet in grant.fleets : "${bucket}/${fleet}" => { bucket = bucket, role = grant.role, fleet = fleet }
    }
  ]...)

  bucket = data.google_storage_bucket.ci_agent[each.value.bucket].name
  role   = each.value.role
  member = local.ci_agent_members[each.value.fleet]
}

# The object roles don't include storage.buckets.get, which GCS client
# libraries need to look a bucket up before reading it (vLLM's model loader,
# for one).
resource "google_storage_bucket_iam_member" "ci_agent_bucket_viewer" {
  for_each = merge([
    for bucket, grant in local.ci_agent_bucket_roles : {
      for fleet in grant.fleets : "${bucket}/${fleet}" => { bucket = bucket, fleet = fleet }
    }
  ]...)

  bucket = data.google_storage_bucket.ci_agent[each.value.bucket].name
  role   = "roles/storage.bucketViewer"
  member = local.ci_agent_members[each.value.fleet]
}

# Only jobs on the TPU queues upload benchmark results.
resource "google_bigquery_dataset_iam_member" "ci_agent_tpu" {
  project    = data.google_bigquery_dataset.benchmark.project
  dataset_id = data.google_bigquery_dataset.benchmark.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = local.ci_agent_members["tpu"]
}

resource "google_bigquery_dataset_iam_member" "ci_agent_cpu" {
  project    = data.google_bigquery_dataset.benchmark.project
  dataset_id = data.google_bigquery_dataset.benchmark.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = local.ci_agent_members["cpu"]
}
