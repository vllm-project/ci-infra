variable "region" {
  type    = string
  default = "us-east-1"
}

variable "deprecate_days" {
  type        = number
  default     = 7
  description = "Number of days after which the AMI is marked as deprecated"
}

locals {
  timestamp    = regex_replace(timestamp(), "[- TZ:]", "")
  deprecate_at = timeadd(timestamp(), "${var.deprecate_days * 24}h")
}

source "amazon-ebs" "cpu_build_box" {
  ami_name        = "vllm-buildkite-stack-linux-cpu-build-${local.timestamp}"
  ami_description = "vLLM Buildkite CPU Build AMI (optimized for Docker builds with pre-warmed cache)"
  ami_groups      = ["all"]

  # Deprecate after 7 days - allows rollback window before cleanup
  deprecate_at = local.deprecate_at

  instance_type = "r6in.16xlarge"

  launch_block_device_mappings {
    delete_on_termination = true
    device_name           = "/dev/xvda"
    volume_size           = 512
    volume_type           = "gp3"
    iops                  = 6000
    throughput            = 500
  }

  region = var.region

  source_ami_filter {
    filters = {
      architecture = "x86_64"
      name         = "buildkite-stack-linux-x86_64-2024-05-27T04-51-04Z-us-east-1"
    }
    most_recent = true
    owners      = ["172840064832"]
  }

  ssh_username = "ec2-user"
  ssh_timeout  = "30m"
}

build {
  sources = ["source.amazon-ebs.cpu_build_box"]

  provisioner "file" {
    destination = "/tmp"
    source      = "scripts"
  }

  provisioner "shell" {
    script = "scripts/install-build-tools.sh"
  }

  provisioner "shell" {
    script = "scripts/pull-base-images.sh"
  }

  post-processor "manifest" {
    output     = "manifest.json"
    strip_path = true
  }
}
