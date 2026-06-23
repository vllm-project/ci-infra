variable "elastic_ci_stack_version" {
  type    = string
  default = "6.21.0"
}

variable "ci_hf_token" {
  type        = string
  description = "Huggingface token used to run CI tests"
}

variable "mac_metal_key_name" {
  type        = string
  description = "EC2 key pair name for SSH access to the Mac Metal instance."
}
