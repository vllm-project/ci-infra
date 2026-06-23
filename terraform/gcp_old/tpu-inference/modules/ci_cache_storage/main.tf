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

  soft_delete_policy {
    retention_duration_seconds = var.retention_seconds
  }
}

resource "google_storage_anywhere_cache" "jax_cache" {
  provider = google-beta
  for_each = toset(var.cache_zones)

  bucket = google_storage_bucket.jax_cache_bucket.name
  zone   = each.value
  ttl    = "86400s"
}
