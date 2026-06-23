# Reuses the existing CI-infra state bucket with a dedicated key so this stack
# has its own state, isolated from terraform/aws.
bucket         = "vllm-aws-ci-infra-tfstate-73a29571ae23e9cb"
dynamodb_table = "terraform-state-lock"
key            = "github-runners/terraform.tfstate"
region         = "us-west-2"
