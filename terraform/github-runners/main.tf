data "aws_caller_identity" "current" {}

# Runners live in the account's default VPC (where the existing vllm-runners
# instances already run). Swap to a dedicated VPC/subnets here if desired.
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# GitHub App credentials backing the runners. Create these SSM parameters
# manually before the first apply (see README.md) so secrets stay out of code
# and state diffs.
data "aws_ssm_parameter" "github_app_id" {
  name = var.github_app_id_ssm_name
}

data "aws_ssm_parameter" "github_app_key_base64" {
  name = var.github_app_key_base64_ssm_name
}

data "aws_ssm_parameter" "github_app_webhook_secret" {
  name = var.github_app_webhook_secret_ssm_name
}

locals {
  github_app = {
    id             = data.aws_ssm_parameter.github_app_id.value
    key_base64     = data.aws_ssm_parameter.github_app_key_base64.value
    webhook_secret = data.aws_ssm_parameter.github_app_webhook_secret.value
  }
}

# Download the prebuilt Lambda artifacts that match the module version, instead
# of building them locally. Order here defines the index used below.
module "lambdas" {
  source  = "github-aws-runners/github-runner/aws//modules/download-lambda"
  version = "7.7.1"

  lambdas = [
    { name = "webhook", tag = "v7.7.1" },
    { name = "runners", tag = "v7.7.1" },
    { name = "runner-binaries-syncer", tag = "v7.7.1" },
  ]
}

# Event-driven, ephemeral autoscaling runners:
# GitHub `workflow_job` webhook -> API Gateway -> Lambda -> one EC2 instance per
# queued job (JIT runner) -> runs a single job -> instance is terminated.
module "runners" {
  source  = "github-aws-runners/github-runner/aws"
  version = "7.7.1"

  aws_region = var.aws_region
  vpc_id     = data.aws_vpc.default.id
  subnet_ids = data.aws_subnets.default.ids
  prefix     = var.prefix

  github_app = local.github_app

  webhook_lambda_zip                = module.lambdas.files[0]
  runners_lambda_zip                = module.lambdas.files[1]
  runner_binaries_syncer_lambda_zip = module.lambdas.files[2]

  enable_organization_runners = var.enable_organization_runners
  runner_group_name           = var.runner_group_name
  runner_extra_labels         = var.runner_extra_labels

  runner_os                     = "linux"
  runner_architecture           = "x64"
  instance_types                = var.instance_types
  instance_target_capacity_type = var.instance_target_capacity_type

  # Ephemeral = one fresh instance per job (required for safe public-repo CI).
  enable_ephemeral_runners = true
  enable_job_queued_check  = true
  runners_maximum_count    = var.runners_maximum_count

  # Reap stuck/idle instances; keep brief minimum lifetime to avoid churn.
  minimum_running_time_in_minutes = 5
  scale_down_schedule_expression  = "cron(* * * * ? *)"

  # Allows SSM Session Manager access into runners for debugging.
  enable_ssm_on_runners = true

  # Retry a scaled-up job once if the instance dies (e.g. spot reclaim).
  job_retry = {
    enable           = true
    max_attempts     = 1
    delay_in_seconds = 180
  }

  # ----------------------------------------------------------------------------
  # Runner image (AMI)
  # ----------------------------------------------------------------------------
  # The module default AMI is Amazon Linux 2023. That reproduces the
  # `actions/setup-python` failure we hit on the bare vllm-runners box
  # ("version '3.12' ... not found for this operating system"), because
  # actions/python-versions only ships prebuilt CPython for Ubuntu.
  #
  # Before relying on this for the pre-commit gate, point `ami` at a custom AMI
  # that ships Python 3.12 + git + shellcheck (+ wget/xz), ideally with the
  # versions pre-seeded in /opt/hostedtoolcache so setup-python is instant.
  # Build it with Packer (see packer/) or mirror actions/runner-images.
  #
  # ami = {
  #   filter = { name = ["vllm-gha-runner-*"], state = ["available"] }
  #   owners = [data.aws_caller_identity.current.account_id]
  # }
  #
  # As a stop-gap on the default AMI, install packages at boot:
  # userdata_pre_install = <<-EOF
  #   dnf install -y git wget xz jq
  # EOF
}

# Automatically points the GitHub App's webhook at the API Gateway endpoint
# created above (so you don't paste the URL into the App by hand).
module "webhook_github_app" {
  source     = "github-aws-runners/github-runner/aws//modules/webhook-github-app"
  version    = "7.7.1"
  depends_on = [module.runners]

  github_app       = local.github_app
  webhook_endpoint = module.runners.webhook.endpoint
}

output "webhook_endpoint" {
  description = "API Gateway endpoint the GitHub App webhook posts to."
  value       = module.runners.webhook.endpoint
}

output "runner_group_name" {
  description = "Runner group the runners register into."
  value       = var.runner_group_name
}
