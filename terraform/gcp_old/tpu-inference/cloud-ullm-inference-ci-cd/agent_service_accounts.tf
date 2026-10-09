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
# inferact-vllm-tpu's agents run as that project's vllm-ci account (see that
# env), listed here as the "inferact" fleet. Its grants on resources in this
# project and in cloud-tpu-inference-test are made here; its roles in
# inferact-vllm-tpu itself are not managed in this repo.
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
    cpu      = "serviceAccount:${google_service_account.ci_agent_cpu.email}"
    tpu      = "serviceAccount:${google_service_account.ci_agent_tpu.email}"
    inferact = "serviceAccount:vllm-ci@inferact-vllm-tpu.iam.gserviceaccount.com"
  }

  # Project-wide roles that are read-only or write-only telemetry.
  ci_agent_project_roles = {
    cpu = [
      "roles/artifactregistry.reader",
      # Failed-step records (vllm-torchtpu record_ci_failure.py).
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
    inferact = [
      # Benchmark result uploads and failed-step records.
      "roles/bigquery.jobUser",
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

  # Repositories the inferact fleet pulls CI images from. The cpu and tpu fleets
  # read every repository here through their project-wide role.
  ci_agent_repo_readers = {
    "tpu-inference-ci" = ["inferact"]
    "vllm-torchtpu-ci" = ["inferact"]
  }

  # Bucket-scoped object roles. Only jobs on the TPU queues use these buckets.
  ci_agent_bucket_roles = {
    "tpu-commons-ci"                         = { role = "roles/storage.objectUser", fleets = ["tpu"] }
    "tpu-inference-hf-llm-model-checkpoints" = { role = "roles/storage.objectViewer", fleets = ["tpu"] }
    "ullm-ci-cache"                          = { role = "roles/storage.objectUser", fleets = ["tpu"] }
    "vllm-bm-bk-storage"                     = { role = "roles/storage.objectCreator", fleets = ["tpu"] }
    "vllm-cb-storage2"                       = { role = "roles/storage.objectViewer", fleets = ["tpu"] }
  }

  # The inferact fleet's roles on the same buckets, which differ from the tpu
  # fleet's: ullm-ci-cache takes new objects but no overwrites or deletes, and
  # tpu-commons-ci takes writes only under xprof/ (ci_agent_inferact_xprof).
  ci_agent_inferact_bucket_roles = {
    "tpu-commons-ci"                         = ["roles/storage.bucketViewer", "roles/storage.objectViewer"]
    "tpu-inference-hf-llm-model-checkpoints" = ["roles/storage.bucketViewer", "roles/storage.objectViewer"]
    "ullm-ci-cache"                          = ["roles/storage.objectCreator", "roles/storage.objectViewer"]
    "vllm-bm-bk-storage"                     = ["roles/storage.objectCreator"]
    "vllm-cb-storage2"                       = ["roles/storage.objectViewer"]
  }

  # Tables in llm_benchmark_analytics the agents write to. Granted per table so
  # a job can't create or drop other tables in the dataset.
  ci_agent_bq_tables = {
    # Benchmark results (upload_results.py, tpu-inference report_bigquery.py).
    "benchmark_runs" = ["tpu", "inferact"]
    # Failed-step records (vllm-torchtpu record_ci_failure.py).
    "ci_failures" = ["cpu", "tpu", "inferact"]
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

data "google_bigquery_dataset" "benchmark" {
  project    = var.project_id
  dataset_id = "llm_benchmark_analytics"
}

data "google_bigquery_table" "ci_agent" {
  for_each = local.ci_agent_bq_tables

  project    = data.google_bigquery_dataset.benchmark.project
  dataset_id = data.google_bigquery_dataset.benchmark.dataset_id
  table_id   = each.key
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

resource "google_artifact_registry_repository_iam_member" "ci_agent_reader" {
  for_each = merge([
    for repo, fleets in local.ci_agent_repo_readers : {
      for fleet in fleets : "${repo}/${fleet}" => { repo = repo, fleet = fleet }
    }
  ]...)

  project    = data.google_artifact_registry_repository.ci_agent[each.value.repo].project
  location   = data.google_artifact_registry_repository.ci_agent[each.value.repo].location
  repository = data.google_artifact_registry_repository.ci_agent[each.value.repo].repository_id
  role       = "roles/artifactregistry.reader"
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

resource "google_storage_bucket_iam_member" "ci_agent_inferact" {
  for_each = merge([
    for bucket, roles in local.ci_agent_inferact_bucket_roles : {
      for role in roles : "${bucket}/${role}" => { bucket = bucket, role = role }
    }
  ]...)

  bucket = data.google_storage_bucket.ci_agent[each.value.bucket].name
  role   = each.value.role
  member = local.ci_agent_members["inferact"]
}

# Profiles from the DeepSeek xprof benchmark scripts (tpu-inference
# scripts/multihost/benchmarks, vllm-torchtpu benchmarking configs).
resource "google_storage_bucket_iam_member" "ci_agent_inferact_xprof" {
  bucket = data.google_storage_bucket.ci_agent["tpu-commons-ci"].name
  role   = "roles/storage.objectUser"
  member = local.ci_agent_members["inferact"]

  condition {
    title      = "xprof-writes"
    expression = "resource.name.startsWith(\"projects/_/buckets/tpu-commons-ci/objects/xprof/\")"
  }
}

resource "google_bigquery_table_iam_member" "ci_agent" {
  for_each = merge([
    for table, fleets in local.ci_agent_bq_tables : {
      for fleet in fleets : "${table}/${fleet}" => { table = table, fleet = fleet }
    }
  ]...)

  project    = data.google_bigquery_table.ci_agent[each.value.table].project
  dataset_id = data.google_bigquery_table.ci_agent[each.value.table].dataset_id
  table_id   = data.google_bigquery_table.ci_agent[each.value.table].table_id
  role       = "roles/bigquery.dataEditor"
  member     = local.ci_agent_members[each.value.fleet]
}
