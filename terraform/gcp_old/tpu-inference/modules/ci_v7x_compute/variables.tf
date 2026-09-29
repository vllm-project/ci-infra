variable "name" { type = string }
variable "slice_count" {
  type = number
  validation {
    condition     = var.slice_count > 0 && floor(var.slice_count) == var.slice_count
    error_message = "slice_count must be a positive integer."
  }
}
variable "hosts_per_slice" {
  type = number
  validation {
    condition     = contains([1, 2], var.hosts_per_slice)
    error_message = "This module supports one-host v7x-8 and two-host v7x-16 slices."
  }
}
variable "topology" { type = string }
variable "target_queue" { type = string }
variable "validation_queue" { type = string }
variable "project_id" { type = string }
variable "zone" { type = string }
variable "reservation_name" { type = string }
variable "subnetwork" { type = string }
variable "service_account_email" { type = string }
variable "bootstrap_secret" { type = string }
