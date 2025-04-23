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
  }

  backend "local" {}
}

provider "aws" {
  region = "us-west-2"
}

variable "state_prefix" {
  type        = string
  default     = "vllm-aws-ci-infra"
}
