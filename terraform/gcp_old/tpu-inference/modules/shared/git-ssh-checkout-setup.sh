# Fetch git@github.com remotes over SSH with the read-only deploy key that the
# pre-checkout hook loads per job, and push them over HTTPS with the app token,
# since a deploy key can't push. --replace-all keeps reruns of this script on
# reboot from piling up duplicate entries.
git config --system credential.https://github.com.helper "/etc/buildkite-agent/git-credential-github-app"
git config --system --unset-all url."https://github.com/".insteadOf
git config --system --replace-all url."https://github.com/".pushInsteadOf "git@github.com:"
git config --system --add url."https://github.com/".pushInsteadOf "ssh://git@github.com/"
sudo -H -u buildkite-agent git config --global credential.https://github.com.helper "/etc/buildkite-agent/git-credential-github-app"
sudo -H -u buildkite-agent git config --global --unset-all url."https://github.com/".insteadOf
sudo -H -u buildkite-agent git config --global --replace-all url."https://github.com/".pushInsteadOf "git@github.com:"
sudo -H -u buildkite-agent git config --global --add url."https://github.com/".pushInsteadOf "ssh://git@github.com/"
HOME=/root git config --global credential.https://github.com.helper "/etc/buildkite-agent/git-credential-github-app"
HOME=/root git config --global --unset-all url."https://github.com/".insteadOf
HOME=/root git config --global --replace-all url."https://github.com/".pushInsteadOf "git@github.com:"
HOME=/root git config --global --add url."https://github.com/".pushInsteadOf "ssh://git@github.com/"

# Pin GitHub's SSH host keys, read over HTTPS from its meta API, in the
# root-owned global known_hosts. Agent 4.x no longer runs ssh-keyscan
# before a checkout, so without this the first clone stops at a host key
# prompt.
github_host_keys=$(curl -fsSL https://api.github.com/meta | jq -r '.ssh_keys[] | "github.com " + .')
if [ -n "$github_host_keys" ]; then
  touch /etc/ssh/ssh_known_hosts
  sed -i '/^github\.com /d' /etc/ssh/ssh_known_hosts
  echo "$github_host_keys" >> /etc/ssh/ssh_known_hosts
fi

cat <<'EOF' > /etc/buildkite-agent/hooks/pre-checkout
#!/bin/bash
# vllm-torchtpu is checked out over SSH with its read-only deploy key. The key
# is the Buildkite secret VLLM_TORCHTPU_DEPLOY_KEY, whose access policy lists
# the pipelines that need it, so other jobs on this VM can't read it.
case "$BUILDKITE_REPO" in
  git@github.com:vllm-project/vllm-torchtpu.git|ssh://git@github.com/vllm-project/vllm-torchtpu.git)
    key_file=$(mktemp)
    if buildkite-agent secret get VLLM_TORCHTPU_DEPLOY_KEY > "$key_file" && [ -s "$key_file" ]; then
      export GIT_SSH_COMMAND="ssh -i $key_file -o IdentitiesOnly=yes -o BatchMode=yes"
      export VLLM_TORCHTPU_DEPLOY_KEY_FILE="$key_file"
    else
      rm -f "$key_file"
      # Fail the checkout instead of waiting at a prompt.
      export GIT_SSH_COMMAND="ssh -o BatchMode=yes"
      echo "pre-checkout: this pipeline can't read VLLM_TORCHTPU_DEPLOY_KEY, so the checkout will fail." >&2
    fi
    ;;
esac
EOF
cat <<'EOF' > /etc/buildkite-agent/hooks/pre-exit
#!/bin/bash
if [ -n "$VLLM_TORCHTPU_DEPLOY_KEY_FILE" ]; then
  rm -f "$VLLM_TORCHTPU_DEPLOY_KEY_FILE"
fi
EOF
# Root-owned, unlike the rest of /etc/buildkite-agent, so a job can't rewrite
# the hooks that later jobs run.
chown root:root /etc/buildkite-agent/hooks/pre-checkout /etc/buildkite-agent/hooks/pre-exit
chmod 755 /etc/buildkite-agent/hooks/pre-checkout /etc/buildkite-agent/hooks/pre-exit
