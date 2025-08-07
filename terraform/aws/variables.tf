terraform {
  backend "s3" {}
}

variable "elastic_ci_stack_version" {
  type    = string
  default = "6.21.0"
}

variable "ci_hf_token" {
  type  = string
  description = "Huggingface token used to run CI tests"
}
