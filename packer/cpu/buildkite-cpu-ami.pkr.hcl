packer {
  required_plugins {
    amazon = {
      version = ">= 1.2.0"
      source  = "github.com/hashicorp/amazon"
    }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "source_ami" {
  type        = string
  description = "Source AMI ID from Buildkite Elastic CI Stack. Fetched from CloudFormation template."
}

variable "security_group_id" {
  type        = string
  description = "Pre-created security group ID for Packer build instances. Fetched from SSM."
}

variable "subnet_id" {
  type        = string
  description = "Subnet ID for Packer build instances. Fetched from SSM."
}

variable "deprecate_days" {
  type        = number
  default     = 7
  description = "Number of days after which the AMI is marked as deprecated"
}

locals {
  timestamp         = regex_replace(timestamp(), "[- TZ:]", "")
  deprecate_hours   = var.deprecate_days * 24
  deprecate_at      = timeadd(timestamp(), "${local.deprecate_hours}h")
}

source "amazon-ebs" "cpu_build_box" {
  ami_name        = "vllm-buildkite-stack-linux-cpu-build-${local.timestamp}"
  ami_description = "vLLM Buildkite CPU Build AMI (optimized for Docker builds with pre-warmed cache)"

  # Deprecate after 7 days - allows rollback window before cleanup
  deprecate_at = local.deprecate_at

  # Network-optimized instance for fast image pulls during AMI build
  instance_type = "r6in.large"

  launch_block_device_mappings {
    delete_on_termination = true
    device_name           = "/dev/xvda"
    volume_size           = 512
    volume_type           = "gp3"
    iops                  = 6000
    throughput            = 500
  }

  region = var.region

  # Use the same base AMI as the Buildkite Elastic CI Stack
  source_ami = var.source_ami

  # Use pre-created security group (managed by Terraform)
  security_group_id = var.security_group_id

  # Use subnet in the same VPC as the security group
  subnet_id = var.subnet_id

  # Assign public IP for internet access (required to reach EC2 API)
  associate_public_ip_address = true

  # Use private IP for SSH (Buildkite agent is in same VPC)
  ssh_interface = "private_ip"

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

  # Ensure Docker data is synced to disk before AMI snapshot
  provisioner "shell" {
    inline = [
      "echo '=== Preparing for AMI snapshot ==='",
      "echo 'Current Docker state:'",
      "docker ps -a",
      "docker volume ls",
      "echo ''",
      "echo 'Stopping Docker gracefully...'",
      "sudo systemctl stop docker",
      "sudo sync",
      "echo ''",
      "echo 'Verifying Docker data on disk:'",
      "sudo ls -la /var/lib/docker/containers/ | head -5",
      "sudo ls -la /var/lib/docker/volumes/ | head -5",
      "echo ''",
      "echo 'Full buildx config for buildkite-agent:'",
      "sudo find /home/buildkite-agent/.docker -type f -o -type d 2>/dev/null | head -30",
      "echo ''",
      "echo 'Current file contents:'",
      "sudo cat /home/buildkite-agent/.docker/buildx/current 2>/dev/null || echo 'No current file'",
      "echo ''",
      "echo 'Creating AMI marker file...'",
      "echo \"AMI_BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)\" | sudo tee /etc/vllm-ami-info",
      "echo \"BUILDER_CONTAINER=buildx_buildkit_baked-vllm-builder0\" | sudo tee -a /etc/vllm-ami-info",
      "echo ''",
      "echo 'Ready for snapshot'"
    ]
  }

  post-processor "manifest" {
    output     = "manifest.json"
    strip_path = true
  }
}
