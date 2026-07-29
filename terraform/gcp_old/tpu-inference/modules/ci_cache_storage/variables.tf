variable "project_id" {
  type        = string
  description = "The GCP Project ID where the JAX cache bucket will be hosted."
}

variable "bucket_name" {
  type        = string
  description = "Custom name for the JAX compilation cache GCS bucket."
}

variable "location" {
  type        = string
  default     = "us-central1"
  description = "GCP region for the storage bucket. Should match TPU region for minimal latency."
}

variable "lifecycle_age_days" {
  type        = number
  default     = 4
  description = "Number of days before stale cache folders/files are automatically deleted by GCS GC."
}

variable "retention_seconds" {
  type        = number
  default     = 0
  description = "Number of seconds for the objects to be kept within GCS after the deletion."
}

variable "cache_zones" {
  type        = list(string)
  default     = []
  description = "The zones where Rapid Cache (Anywhere Cache) should be enabled for the bucket."
}

