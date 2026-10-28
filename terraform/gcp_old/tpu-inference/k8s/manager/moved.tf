# Grants that changed key, so the plan moves them rather than revoking and
# re-granting. kueue_manager_gateway lost its per-project key; env_secret_sync
# gained one.
#
# Safe to delete once applied: a moved block only matters to a state that still
# holds the old address.

moved {
  from = google_project_iam_member.kueue_manager_gateway["cloud-ullm-inference-ci-cd"]
  to   = google_project_iam_member.kueue_manager_gateway
}

moved {
  from = google_secret_manager_secret_iam_member.env_secret_sync["BUILDKITE_ANALYTICS_TOKEN"]
  to   = google_secret_manager_secret_iam_member.env_secret_sync["cloud-ullm-inference-ci-cd/BUILDKITE_ANALYTICS_TOKEN"]
}

moved {
  from = google_secret_manager_secret_iam_member.env_secret_sync["HF_TOKEN"]
  to   = google_secret_manager_secret_iam_member.env_secret_sync["cloud-ullm-inference-ci-cd/HF_TOKEN"]
}
