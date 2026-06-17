terraform {
  backend "s3" {}
  required_version = ">= 1.8"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0" # module v7 requires the AWS provider >= 6.33
    }
  }
}

provider "aws" {
  region = var.aws_region
}
