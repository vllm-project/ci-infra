terraform {
  required_providers {
    github = {
      source  = "integrations/github"
      version = "6.6.0"
    }
  }

  required_version = ">= 1.8"
}

# Configure the GitHub Provider
provider "github" {
  owner = "vllm-project"
  token = var.admin_token
}
