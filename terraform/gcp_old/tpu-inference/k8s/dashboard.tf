# The kube fleet's health dashboard: health checks from Buildkite to a TPU pod,
# Kueue quota and borrowing, utilization and outcomes over 24 hours or 7 days,
# per-queue workloads and builds, and cluster events. The program and what it
# reads are described in dashboard/app.py.
#
# Cloud Run rather than a Deployment on the manager, because nothing it needs is
# inside the cluster boundary that Connect Gateway does not already expose:
# every cluster is read through the gateway like any operator reads it, and the
# rest is Buildkite, Cloud Monitoring and BigQuery. Off the manager it cannot compete with the
# launchers for nodes, and IAP in front of Cloud Run needs no load balancer,
# certificate or domain.

locals {
  # Every cluster's Connect Gateway endpoint, keyed by membership. workers.tf
  # registers each cluster into this project's fleet under its own name, in its
  # own region.
  dashboard_gateways = {
    for name, location in merge(
      { (google_container_cluster.manager.name) = var.manager_region },
      { for w in google_container_cluster.worker : w.name => w.location },
    ) :
    name => join("/", [
      "https://${location}-connectgateway.googleapis.com/v1",
      "projects/${data.google_project.manager.number}",
      "locations/${location}",
      "gkeMemberships/${name}",
    ])
  }
}

resource "google_service_account" "dashboard" {
  project      = var.project_id
  account_id   = "${var.name_prefix}-dashboard"
  display_name = "TPU CI queue dashboard"
}

# Connect Gateway, read-only, to the manager and every worker. On the project
# for the reason launcher_gateway in iam.tf gives: the gateway checks its own
# gkeMemberships resource, which a binding on the membership does not cover.
# What the dashboard may read inside a cluster is bounded by the RBAC in
# kueue/templates/dashboard_rbac*.yaml.tpl, not by this.
resource "google_project_iam_member" "dashboard_gateway" {
  for_each = toset(["roles/gkehub.gatewayReader", "roles/gkehub.viewer"])

  project = var.project_id
  role    = each.value
  member  = google_service_account.dashboard.member
}

# The node pools of every cluster - shape, autoscaling bounds, and the instance
# groups a node's name ties it to - read from the GKE API. Cluster viewer is
# read-only cluster metadata and nothing inside the cluster, and GKE grants it
# per project or not at all.
resource "google_project_iam_member" "dashboard_cluster_viewer" {
  project = var.project_id
  role    = "roles/container.clusterViewer"
  member  = google_service_account.dashboard.member
}

# Kueue's and the Buildkite controller's metrics in Managed Prometheus, and
# GKE's TPU duty cycle. Monitoring has no grant narrower than the project.
resource "google_project_iam_member" "dashboard_monitoring" {
  project = var.project_id
  role    = "roles/monitoring.viewer"
  member  = google_service_account.dashboard.member
}

# The launcher's per-workload record, for outcomes and phase timings. Read on
# the table, like the launcher's write in iam.tf; running a query is a job, and
# jobs are granted on the project or not at all.
resource "google_bigquery_table_iam_member" "dashboard_timing" {
  project    = var.project_id
  dataset_id = "ci_efficiency_metrics"
  table_id   = "kube_workload_timing"
  role       = "roles/bigquery.dataViewer"
  member     = google_service_account.dashboard.member
}

resource "google_project_iam_member" "dashboard_bigquery_jobs" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = google_service_account.dashboard.member
}

data "google_secret_manager_secret" "dashboard_buildkite_token" {
  project   = var.project_id
  secret_id = var.dashboard_buildkite_token_secret_id
}

resource "google_secret_manager_secret_iam_member" "dashboard_buildkite_token" {
  project   = var.project_id
  secret_id = data.google_secret_manager_secret.dashboard_buildkite_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.dashboard.member
}

resource "google_cloud_run_v2_service" "dashboard" {
  project             = var.project_id
  name                = "${var.name_prefix}-queue-dashboard"
  location            = var.manager_region
  deletion_protection = var.deletion_protection

  # Reachable from anywhere, and only through IAP: iap_enabled puts IAP in
  # front of the service's own URL, and run.managed.requireInvokerIam (enforced
  # in this org) means a request IAP did not sign is refused.
  ingress     = "INGRESS_TRAFFIC_ALL"
  iap_enabled = true

  template {
    service_account = google_service_account.dashboard.email

    # One instance, so every viewer shares one cache and one refresh, and the
    # Buildkite calls do not multiply with traffic. Zero when idle.
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = var.dashboard_image

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        # A page view is answered from the cached snapshot and a stale one is
        # refreshed after the response has gone, so the instance needs CPU
        # between requests; with it throttled the refresh would stall until
        # the next view.
        cpu_idle = false
      }

      env {
        name  = "PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "GATEWAY_URL"
        value = local.dashboard_gateways[google_container_cluster.manager.name]
      }
      env {
        name = "CLUSTERS"
        value = jsonencode([
          for name, gateway in local.dashboard_gateways : { name = name, gateway = gateway }
        ])
      }
      env {
        name  = "NAMESPACE"
        value = var.namespace
      }
      env {
        name  = "KUEUE_METRICS_CLUSTER"
        value = google_container_cluster.manager.name
      }
      env {
        name  = "BUILDKITE_ORG"
        value = var.buildkite_org
      }
      env {
        name  = "BUILDKITE_CLUSTER_ID"
        value = var.buildkite_cluster_id
      }
      env {
        name  = "BUILDKITE_QUEUE"
        value = var.buildkite_queue
      }
      env {
        name  = "TIMING_TABLE"
        value = "${var.project_id}.ci_efficiency_metrics.kube_workload_timing"
      }
      # The launcher's budgets, which the health checks measure waits and runs
      # against: past them the fleet kills the step itself.
      env {
        name  = "QUEUE_BUDGET_SECONDS"
        value = tostring(var.tpu_queue_max_seconds)
      }
      env {
        name  = "TEST_BUDGET_SECONDS"
        value = tostring(var.tpu_test_max_seconds)
      }
      env {
        name = "BUILDKITE_API_TOKEN"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.dashboard_buildkite_token.secret_id
            version = "latest"
          }
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.dashboard_buildkite_token,
    google_bigquery_table_iam_member.dashboard_timing,
  ]
}

# IAP calls the service as its own service agent, which therefore needs invoker
# on it. The agent exists only once something has asked for it, and only the
# beta provider can.
resource "google_project_service_identity" "iap" {
  provider = google-beta

  project = var.project_id
  service = "iap.googleapis.com"
}

resource "google_cloud_run_v2_service_iam_member" "dashboard_iap_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.dashboard.location
  name     = google_cloud_run_v2_service.dashboard.name
  role     = "roles/run.invoker"
  member   = google_project_service_identity.iap.member
}

resource "google_iap_web_cloud_run_service_iam_member" "dashboard_viewers" {
  for_each = toset(var.dashboard_viewers)

  project                = var.project_id
  location               = google_cloud_run_v2_service.dashboard.location
  cloud_run_service_name = google_cloud_run_v2_service.dashboard.name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.value
}

output "dashboard_url" {
  description = "The kube fleet's health dashboard, behind IAP."
  value       = google_cloud_run_v2_service.dashboard.uri
}
