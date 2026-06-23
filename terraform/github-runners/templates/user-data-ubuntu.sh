#!/bin/bash -e
# Ubuntu 24.04 user-data for github-aws-runners, adapted from the module's
# default (Amazon Linux 2023) template:
#   - apt-get instead of dnf
#   - default user is "ubuntu" (also set runner_run_as = "ubuntu")
#   - install AWS CLI v2 explicitly; Ubuntu's stock AMI has none, but
#     install-runner.sh (aws s3 cp) and start-runner.sh (aws ec2/ssm) need it
# The runner's own bin/installdependencies.sh installs libicu on Ubuntu.

exec > >(tee /var/log/user-data.log | logger -t user-data -s 2>/dev/console) 2>&1
set +x

%{ if enable_debug_logging }
set -x
%{ endif }

${pre_install}

export DEBIAN_FRONTEND=noninteractive

apt_get() {
  for attempt in 1 2 3 4 5; do
    apt-get "$@" && return 0
    echo "apt-get $* failed (attempt $attempt/5) - retrying"
    sleep 5
  done
  return 1
}

apt_get update -y
apt_get install -y --no-install-recommends jq git curl ca-certificates unzip

# AWS CLI v2 (required by install-runner.sh and start-runner.sh).
if ! command -v aws >/dev/null 2>&1; then
  tmpdir="$(mktemp -d)"
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "$tmpdir/awscliv2.zip"
  unzip -q "$tmpdir/awscliv2.zip" -d "$tmpdir"
  "$tmpdir/aws/install"
  rm -rf "$tmpdir"
fi

user_name=ubuntu

${install_runner}

${post_install}

# Register runner job hooks
%{ if hook_job_started != "" }
cat > /opt/actions-runner/hook_job_started.sh <<'EOF'
${hook_job_started}
EOF
echo ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/actions-runner/hook_job_started.sh | tee -a /opt/actions-runner/.env
%{ endif }

%{ if hook_job_completed != "" }
cat > /opt/actions-runner/hook_job_completed.sh <<'EOF'
${hook_job_completed}
EOF
echo ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/opt/actions-runner/hook_job_completed.sh | tee -a /opt/actions-runner/.env
%{ endif }

${start_runner}
