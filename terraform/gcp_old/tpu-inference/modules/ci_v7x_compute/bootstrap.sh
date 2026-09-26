#!/bin/bash
set -euo pipefail
umask 027
rm -f /run/tpu-ci-ready
systemctl stop buildkite-agent 2>/dev/null || true
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git jq rsync openssh-server python3-venv docker.io pciutils
id buildkite-agent >/dev/null 2>&1 || useradd --create-home --home-dir /var/lib/buildkite-agent --shell /bin/bash buildkite-agent
usermod -aG docker buildkite-agent
# Dedicated disposable CI hosts: Docker already grants host-root access.
printf 'buildkite-agent ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/buildkite-agent
chmod 0440 /etc/sudoers.d/buildkite-agent
systemctl enable --now docker
install -d -m 0700 -o buildkite-agent -g buildkite-agent /var/lib/buildkite-agent/.ssh
install -d -m 0755 /etc/buildkite-agent/hooks
install -d -o buildkite-agent -g buildkite-agent /var/lib/buildkite-agent/builds /var/lib/buildkite-agent/plugins /var/lib/buildkite-agent/tpu-work
# Secret payload is added by the operator, never embedded in metadata or Terraform state.
python3 - <<'PY'
import base64, json, pathlib, time, urllib.request
meta='http://metadata.google.internal/computeMetadata/v1/'
def metadata(path):
    return urllib.request.urlopen(urllib.request.Request(meta+path,headers={'Metadata-Flavor':'Google'}),timeout=20).read().decode()
project=metadata('project/project-id')
ci=json.loads(pathlib.Path('/opt/tpu-ci/config.json').read_text())
for attempt in range(180):
    try:
        token=json.loads(metadata('instance/service-accounts/default/token'))['access_token']
        url=f'https://secretmanager.googleapis.com/v1/projects/{project}/secrets/{ci["bootstrap_secret"]}/versions/latest:access'
        response=json.load(urllib.request.urlopen(urllib.request.Request(url,headers={'Authorization':'Bearer '+token}),timeout=20))
        data=json.loads(base64.b64decode(response['payload']['data']))
        break
    except Exception as exc:
        print('Waiting for CI bootstrap secret:',type(exc).__name__,flush=True)
        time.sleep(10)
else:
    raise SystemExit('CI bootstrap secret unavailable')
ssh=pathlib.Path('/var/lib/buildkite-agent/.ssh')
for name,key in [('id_rsa','ssh_private'),('authorized_keys','ssh_public')]:
    path=ssh/name
    path.write_text(data[key])
    path.chmod(0o600)
(ssh/'config').write_text('Host 10.13.240.* inferact-v7x-*\n  User buildkite-agent\n  Port 2222\n  IdentityFile ~/.ssh/id_rsa\n  StrictHostKeyChecking accept-new\n  ConnectTimeout 15\n  ServerAliveInterval 15\n  ServerAliveCountMax 3\n')
name=metadata('instance/name')
if name.endswith('-001'):
    config=pathlib.Path('/etc/buildkite-agent/buildkite-agent.cfg')
    queue = ci['target_queue'] if pathlib.Path('/etc/buildkite-agent/dispatch-enabled').exists() else ci['validation_queue']
    config.write_text('token="'+data['agent_token']+'"\nname="'+ci['slice']+'"\ntags="queue='+queue+',target_queue='+ci['target_queue']+',slice='+ci['slice']+',tpu=v7x"\nbuild-path="/var/lib/buildkite-agent/builds"\nhooks-path="/etc/buildkite-agent/hooks"\nplugins-path="/var/lib/buildkite-agent/plugins"\nspawn=1\n')
    config.chmod(0o640)
PY
chown -R buildkite-agent:buildkite-agent /var/lib/buildkite-agent/.ssh
# Separate SSH listener authenticates the slice-local CI account; operator SSH keeps OS Login.
cat > /etc/ssh/sshd_config_tpu_ci <<'EOF'
Port 2222
ListenAddress 0.0.0.0
HostKey /etc/ssh/ssh_host_ed25519_key
PidFile /run/sshd-tpu-ci.pid
AuthorizedKeysFile .ssh/authorized_keys
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
UsePAM no
AllowUsers buildkite-agent
StrictModes yes
Subsystem sftp internal-sftp
EOF
# Unlock the account for pubkey auth with an unusable password hash.
usermod -p '*' buildkite-agent
cat > /etc/systemd/system/tpu-ci-ssh.service <<'EOF'
[Unit]
Description=Slice-local Buildkite SSH
After=network.target
[Service]
LimitMEMLOCK=infinity
ExecStartPre=/usr/bin/mkdir -p /run/sshd
ExecStart=/usr/sbin/sshd -D -f /etc/ssh/sshd_config_tpu_ci
Restart=on-failure
[Install]
WantedBy=multi-user.target
EOF
ln -sf /opt/tpu-ci/tpu-run /usr/local/bin/tpu-run
python3 -m venv /opt/tpu-ci/venv
/opt/tpu-ci/venv/bin/pip install --disable-pip-version-check 'jax[tpu]==0.11.1' pyjwt cryptography requests
# VFIO device access for unprivileged TPU jobs, including after reboot.
cat > /etc/udev/rules.d/99-tpu-ci.rules <<'EOF'
SUBSYSTEM=="vfio", GROUP="buildkite-agent", MODE="0660"
SUBSYSTEM=="misc", KERNEL=="vfio", GROUP="buildkite-agent", MODE="0660"
EOF
udevadm control --reload-rules
udevadm trigger
find /dev/vfio -type c -exec chgrp buildkite-agent {} + -exec chmod g+rw {} +
version=3.138.0
curl -fsSL --retry 5 "https://github.com/buildkite/agent/releases/download/v${version}/buildkite-agent-linux-amd64-${version}.tar.gz" -o "/tmp/buildkite-agent-linux-amd64-${version}.tar.gz"
curl -fsSL --retry 5 "https://github.com/buildkite/agent/releases/download/v${version}/buildkite-agent-${version}.SHA256SUMS" -o /tmp/buildkite-agent.SHA256SUMS
(cd /tmp && grep " buildkite-agent-linux-amd64-${version}.tar.gz$" buildkite-agent.SHA256SUMS | sha256sum -c -)
tar -xzf "/tmp/buildkite-agent-linux-amd64-${version}.tar.gz" -C /usr/local/bin ./buildkite-agent
cat > /etc/buildkite-agent/hooks/environment <<'EOF'
#!/bin/bash
export TPU_VERSION=tpu7x
export SSH_USER=buildkite-agent
export HEAD_INTERNAL_IP="$(python3 -c 'import json,socket; print(socket.gethostbyname(json.load(open("/opt/tpu-ci/config.json"))["hosts"][0]))')"
export WORKER_IPS="$(python3 -c 'import json,socket; print(",".join(socket.gethostbyname(h) for h in json.load(open("/opt/tpu-ci/config.json"))["hosts"][1:]))')"
export TPU_HOST_COUNT="$(python3 -c 'import json; print(len(json.load(open("/opt/tpu-ci/config.json"))["hosts"]))')"
EOF
chmod 0755 /etc/buildkite-agent/hooks/environment
cat > /etc/systemd/system/buildkite-agent.service <<'EOF'
[Unit]
Description=Buildkite agent for the dedicated TPU slice
After=network-online.target tpu-ci-ssh.service docker.service
Wants=network-online.target
ConditionPathExists=/run/tpu-ci-ready
[Service]
LimitMEMLOCK=infinity
User=buildkite-agent
Group=buildkite-agent
Environment=HOME=/var/lib/buildkite-agent
ExecStart=/usr/local/bin/buildkite-agent start --config /etc/buildkite-agent/buildkite-agent.cfg
Restart=on-failure
RestartSec=10
KillMode=mixed
TimeoutStopSec=300
[Install]
WantedBy=multi-user.target
EOF
# CI uses a GitHub App key scoped by the Buildkite job's secret access policy.
install -m 0755 /opt/tpu-ci/git-credential-github-app /etc/buildkite-agent/git-credential-github-app
install -m 0755 /opt/tpu-ci/get-github-token.py /etc/buildkite-agent/get-github-token.py
git config --system credential.https://github.com.helper /etc/buildkite-agent/git-credential-github-app
git config --system --replace-all url.https://github.com/.insteadOf git@github.com:
git config --system --add url.https://github.com/.insteadOf ssh://git@github.com/
sudo -H -u buildkite-agent gcloud auth configure-docker us-central1-docker.pkg.dev --quiet
install -d -m 0777 /mnt/disks/persist /mnt/disks/persist/tpu_jax_cache
# Docker jobs need the same unlimited memory locking as host-side TPU jobs.
python3 - <<'PYDOCKER'
import json,pathlib
p=pathlib.Path('/etc/docker/daemon.json')
c=json.loads(p.read_text()) if p.exists() else {}
c.setdefault('default-ulimits',{})['memlock']={'Name':'memlock','Hard':-1,'Soft':-1}
p.write_text(json.dumps(c,indent=2)+'\n')
PYDOCKER
systemctl restart docker
chmod -R a+rX /opt/tpu-ci
systemctl daemon-reload
systemctl enable --now tpu-ci-ssh
touch /run/tpu-ci-ready
if [ -f /etc/buildkite-agent/buildkite-agent.cfg ]; then
  bash /opt/tpu-ci/keep-agent-connected.sh
  chown root:buildkite-agent /etc/buildkite-agent/buildkite-agent.cfg
  systemctl enable --now buildkite-agent
fi
echo 'TPU CI bootstrap complete'
