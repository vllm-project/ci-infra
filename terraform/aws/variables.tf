terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.3"
    }
    buildkite = {
      source  = "buildkite/buildkite"
      version = "1.10.1"
    }
  }
  backend "s3" {}
}

provider "aws" {
  region = "us-west-2"
}

provider "buildkite" {
  organization = "vllm"
}

variable "elastic_ci_stack_version" {
  type    = string
  default = "6.21.0"
}

variable "ci_hf_token" {
  type  = string
  description = "Huggingface token used to run CI tests"
}
