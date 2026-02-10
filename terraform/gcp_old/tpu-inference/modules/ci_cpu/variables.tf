variable "project_id" {
  default = "cloud-tpu-inference-test"
}

variable "instance_count" {
  type        = number
  description = "Number of VM instance"
}
