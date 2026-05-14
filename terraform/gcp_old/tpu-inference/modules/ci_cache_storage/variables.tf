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
  default     = 30
  description = "Number of days before stale cache folders/files are automatically deleted by GCS GC."
}
