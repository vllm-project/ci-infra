resource "google_storage_bucket" "jax_cache_bucket" {
  name                        = var.bucket_name
  project                     = var.project_id
  location                    = var.location
  force_destroy               = false
  uniform_bucket_level_access = true
  storage_class               = "STANDARD"

  # Auto garbage collection(guarantee the rsync performance)
  lifecycle_rule {
    condition {
      age = var.lifecycle_age_days
    }
    action {
      type = "Delete"
    }
  }

  # Import the existing bucket without destroy it
  lifecycle {
    ignore_changes = [
      project
    ]
  }
}
