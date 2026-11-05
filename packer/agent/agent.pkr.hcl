packer {
  required_plugins {
    amazon = {
      version = "= 1.3.9"
      source  = "github.com/hashicorp/amazon"
    }
  }
}
variable "source_ami" { type = string }
variable "architecture" { type = string }
variable "subnet_id" { type = string }
variable "security_group_id" { type = string }

source "amazon-ebs" "agent" {
  region                      = "us-east-1"
  source_ami                  = var.source_ami
  instance_type               = var.architecture == "arm64" ? "t4g.small" : "t3.small"
  ami_name                    = "vllm-agent-3.138.2-${var.architecture}-${formatdate("YYYYMMDDhhmmss", timestamp())}"
  ami_description             = "Buildkite release worker with pinned agent 3.138.2"
  subnet_id                   = var.subnet_id
  security_group_id           = var.security_group_id
  associate_public_ip_address = true
  ssh_interface               = "private_ip"
  ssh_username                = "ec2-user"
  ssh_timeout                 = "15m"
}
build {
  sources = ["source.amazon-ebs.agent"]
  provisioner "shell" {
    script = "install-agent.sh"
  }
  post-processor "manifest" {
    output     = "manifest-${var.architecture}.json"
    strip_path = true
  }
}
