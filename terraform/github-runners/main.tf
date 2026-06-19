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
  # Stock Ubuntu 24.04 (Canonical) instead of the module default (Amazon Linux
  # 2023). Ubuntu 24.04 ships Python 3.12 as the system python3 AND
  # actions/setup-python has prebuilt CPython for Ubuntu, so the pre-commit
  # workflow works unchanged (the AL2023 default fails `setup-python`).
  # Pairs with the custom Ubuntu userdata template and runner_run_as below.
  ami = {
    filter = {
      name  = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
      state = ["available"]
    }
    owners = ["099720109477"] # Canonical
  }
  userdata_template = "${path.module}/templates/user-data-ubuntu.sh"
  runner_run_as     = "ubuntu" # Ubuntu's default user (module default is ec2-user)

  # Ubuntu's stock AMI has no CloudWatch agent; skip it to keep the image clean.
  enable_cloudwatch_agent = false
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
