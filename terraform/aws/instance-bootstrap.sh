#!/bin/bash
# Source of truth for the per-instance boot script referenced by
# BootstrapScriptUrl in cloudformation_stack.tf. Upload this file to
# s3://vllm-ci/instance-bootstrap.sh after edits.

set -euo pipefail

echo "install algif_aead /bin/false" > /etc/modprobe.d/disable-algif-aead.conf
rmmod algif_aead 2>/dev/null || true
