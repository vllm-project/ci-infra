#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v packer >/dev/null; then
  mkdir -p "$HOME/.local/bin"
  curl -fsSL https://releases.hashicorp.com/packer/1.11.2/packer_1.11.2_linux_amd64.zip -o /tmp/agent-packer.zip
  unzip -o /tmp/agent-packer.zip -d "$HOME/.local/bin"
  rm /tmp/agent-packer.zip
fi
subnet=$(aws ssm get-parameter --name /buildkite/packer/subnet-id --region us-east-1 --query Parameter.Value --output text)
security_group=$(aws ssm get-parameter --name /buildkite/packer/security-group-id --region us-east-1 --query Parameter.Value --output text)
packer init agent.pkr.hcl
for architecture in amd64 arm64; do
  if [[ "$architecture" == amd64 ]]; then
    source_ami=${AGENT_SOURCE_AMI_AMD64:?Set the current x86 worker AMI}
  else
    source_ami=${AGENT_SOURCE_AMI_ARM64:?Set the current ARM worker AMI}
  fi
  [[ "$source_ami" =~ ^ami-[a-f0-9]+$ ]] || exit 1
  packer build -var "source_ami=$source_ami" -var "architecture=$architecture" \
    -var "subnet_id=$subnet" -var "security_group_id=$security_group" agent.pkr.hcl
  buildkite-agent artifact upload "manifest-${architecture}.json"
done
