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

variable "ecr_token" {
  type        = string
  sensitive   = true
  description = "ECR authentication token for pulling cache images"
}

variable "vllm_commit" {
  type        = string
  description = "vLLM commit SHA for cache warming"
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

  # Upload scripts to the instance
  provisioner "file" {
    destination = "/tmp"
    source      = "scripts"
  }

  # Upload bake config (separate from vLLM repo to avoid polluting git/Docker context)
  provisioner "file" {
    destination = "/tmp/ci-bake-config.json"
    source      = "ci-config/bake-config.json"
  }

  # Upload cache-warm overlay
  provisioner "file" {
    destination = "/tmp/cache-warm-overlay.hcl"
    source      = "cache-warm-overlay.hcl"
  }

  # Upload vLLM repo (cloned by pipeline) for cache warming
  provisioner "file" {
    destination = "/tmp/vllm"
    source      = "vllm-cache-source"
  }

  # Configure network settings for high-throughput operations
  provisioner "shell" {
    script = "scripts/configure-network.sh"
  }

  # Install BuildKit as standalone systemd service (runs as ec2-user with sudo)
  provisioner "shell" {
    script = "scripts/install-build-tools.sh"
  }

  # Warm the cache by building vLLM image (runs as buildkite-agent)
  provisioner "shell" {
    environment_vars = [
      "ECR_TOKEN=${var.ecr_token}",
      "VLLM_COMMIT=${var.vllm_commit}"
    ]
    inline = [
      "sudo -u buildkite-agent -i ECR_TOKEN=\"$ECR_TOKEN\" VLLM_COMMIT=\"$VLLM_COMMIT\" bash /tmp/scripts/warm-cache.sh"
    ]
  }

  # Prepare for AMI snapshot
  provisioner "shell" {
    inline = [
      "echo '=== Preparing for AMI snapshot ==='",
      "echo 'Docker info:'",
      "docker info 2>&1 | grep -E 'Docker Root Dir|Storage Driver' || docker info",
      "echo ''",
      "echo 'BuildKit cache size:'",
      "sudo du -sh /var/lib/buildkit 2>/dev/null || echo 'No buildkit cache'",
      "sudo ls -la /var/lib/buildkit/ 2>/dev/null | head -10 || true",
      "echo ''",
      "echo 'Stopping buildkitd and Docker gracefully...'",
      "sudo systemctl stop buildkitd || true",
      "sudo systemctl stop docker",
      "sudo sync",
      "echo ''",
      "echo 'Verifying data on disk:'",
      "sudo du -sh /var/lib/buildkit || echo 'No buildkit cache'",
      "echo ''",
      "echo 'Creating AMI marker file...'",
      "echo \"AMI_BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)\" | sudo tee /etc/vllm-ami-info",
      "echo \"BUILDKIT_CACHE=/var/lib/buildkit\" | sudo tee -a /etc/vllm-ami-info",
      "echo ''",
      "echo 'Ready for snapshot'"
    ]
  }

  post-processor "manifest" {
    output     = "manifest.json"
    strip_path = true
  }
}
