variable "notification_channel_ids" {
  type        = list(string)
  description = "List of Google Cloud Monitoring notification channel IDs"
  default     = []
}

# Disk & Resource Utilization Alerts (v6, v7, and CPU)

resource "google_monitoring_alert_policy" "high_disk_utilization" {
  display_name = "CI Agent High Disk Utilization (>90%)"
  combiner     = "OR"
  notification_channels = var.notification_channel_ids
  
  conditions {
    display_name = "Disk Usage > 90% (TPU v6/v7 and CPU Workers)"
    condition_threshold {
      filter          = "metric.type=\"agent.googleapis.com/disk/percent_used\" AND (resource.type=\"gce_instance\" OR resource.type=\"tpu_worker\")"
      comparison      = "COMPARISON_GT"
      threshold_value = 90
      duration        = "300s" # 5 minutes
      
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_MEAN"
        cross_series_reducer = "REDUCE_NONE"
      }
    }
  }
}

# Job-Type Success Rate Tracking & Degradation

resource "google_monitoring_alert_policy" "pipeline_degradation" {
  display_name = "Buildkite Pipeline Success Rate Degradation"
  combiner     = "OR"
  notification_channels = var.notification_channel_ids
  
  conditions {
    display_name = "Success Rate < 80% over 2 hours"
    condition_threshold {
      # The numerator only passed jobs
      filter          = "metric.type=\"custom.googleapis.com/buildkite/job_status\" AND metric.labels.status=\"passed\""
      comparison      = "COMPARISON_LT"
      threshold_value = 0.8 # 80%
      duration        = "0s"
      aggregations {
        alignment_period     = "7200s" # 2 hours
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
      # The denominator all jobs
      denominator_filter = "metric.type=\"custom.googleapis.com/buildkite/job_status\""
      denominator_aggregations {
        alignment_period     = "7200s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }
}

# Agent / VM Health & Flapping (Multi-Host Focus)

resource "google_monitoring_alert_policy" "agent_flapping" {
  display_name = "Buildkite Agent Flapping (High Failure Rate)"
  combiner     = "OR"
  notification_channels = var.notification_channel_ids
  
  conditions {
    display_name = "> 5 failures in 30 mins per agent"
    condition_threshold {
      filter          = "metric.type=\"custom.googleapis.com/buildkite/job_status\" AND metric.labels.status=\"failed\""
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      duration        = "0s"
      aggregations {
        alignment_period     = "1800s" # 30 mins
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.labels.agent_name"] # Group by individual agent
      }
    }
  }
}

# Pipeline Timeout Surge Alerting

resource "google_monitoring_alert_policy" "timeout_spike" {
  display_name = "Spike in Buildkite Job Timeouts"
  combiner     = "OR"
  notification_channels = var.notification_channel_ids
  
  conditions {
    display_name = "> 3 timeouts in 1 hour"
    condition_threshold {
      filter          = "metric.type=\"custom.googleapis.com/buildkite/job_status\" AND metric.labels.status=\"timed_out\""
      comparison      = "COMPARISON_GT"
      threshold_value = 3
      duration        = "0s"
      aggregations {
        alignment_period     = "3600s" # 1 hour
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }
}

# Cloud Monitoring Dashboard (Disk & Resource Utilization)

resource "google_monitoring_dashboard" "ci_observability_dashboard" {
  dashboard_json = jsonencode({
    displayName = "CI/CD Observability & Resource Utilization"
    gridLayout = {
      columns = 2   
      widgets = [
        {
          title = "Disk Utilization (TPU v6, v7, CPU)"
          xyChart = {
            dataSets = [
              {
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"agent.googleapis.com/disk/percent_used\" AND (resource.type=\"tpu_worker\" OR resource.type=\"gce_instance\")"
                    aggregation = {
                      perSeriesAligner   = "ALIGN_MEAN"
                      crossSeriesReducer = "REDUCE_NONE"
                    }
                  }
                }
              }
            ]
          }
        },
        {
          title = "CPU Utilization (TPU v6, v7, CPU)"
          xyChart = {
            dataSets = [
              {
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"compute.googleapis.com/instance/cpu/utilization\" AND (resource.type=\"tpu_worker\" OR resource.type=\"gce_instance\")"
                    aggregation = {
                      perSeriesAligner   = "ALIGN_MEAN"
                      crossSeriesReducer = "REDUCE_NONE"
                    }
                  }
                }
              }
            ]
          }
        }
      ]
    }
  })
}

