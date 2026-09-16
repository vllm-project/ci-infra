#!/usr/bin/env bash
set -euo pipefail

readonly MIN_START_TIMEOUT_SECONDS=120

die() {
  echo "ERROR: $*" >&2
  exit 1
}

[[ "$(id -u)" -eq 0 ]] || die "run as root"
[[ "${CONFIRM_BUILDKITE_AGENTS_PAUSED:-}" == 1 ]] ||
  die "pause every host agent, then set CONFIRM_BUILDKITE_AGENTS_PAUSED=1"
[[ "${CONFIRM_BUILDKITE_API_JOBS_ZERO:-}" == 1 ]] ||
  die "verify zero assigned jobs in the Buildkite API, then set CONFIRM_BUILDKITE_API_JOBS_ZERO=1"

if [[ -n "$(docker ps -q)" ]]; then
  die "running containers remain; drain the host first"
fi

if nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null |
  grep -q '[0-9]'; then
  die "GPU compute processes remain; drain the host first"
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
hook=/etc/buildkite-agent/hooks/environment
drop_in_dir=/etc/systemd/system/buildkite-agent.service.d
drop_in="${drop_in_dir}/gpu-cdi.conf"
source_line='source /usr/local/libexec/buildkite-gpu-cdi-env.sh'

test -f "${script_dir}/buildkite-gpu-cdi-env.sh"
test -f "${script_dir}/buildkite-gpu-cdi.conf"
test -f "${hook}"

configured_timeout_seconds="$({
  sed -n 's/^TimeoutStartSec=\([0-9][0-9]*\)$/\1/p' \
    "${script_dir}/buildkite-gpu-cdi.conf"
} | tail -1)"
[[ "${configured_timeout_seconds}" =~ ^[0-9]+$ ]] ||
  die "buildkite-gpu-cdi.conf must set TimeoutStartSec in whole seconds"
((configured_timeout_seconds >= MIN_START_TIMEOUT_SECONDS)) ||
  die "TimeoutStartSec must be at least ${MIN_START_TIMEOUT_SECONDS} seconds"

install -d -o root -g root -m 0755 /usr/local/libexec "${drop_in_dir}"
install -o root -g root -m 0755 \
  "${script_dir}/buildkite-gpu-cdi-env.sh" \
  /usr/local/libexec/buildkite-gpu-cdi-env.sh
install -o root -g root -m 0644 \
  "${script_dir}/buildkite-gpu-cdi.conf" \
  "${drop_in}"

if ! grep -Fqx "${source_line}" "${hook}"; then
  if [[ ! -e "${hook}.pre-cdi" ]]; then
    cp -a "${hook}" "${hook}.pre-cdi"
  fi
  printf '\n%s\n' "${source_line}" >>"${hook}"
fi

nvidia-ctk --quiet cdi generate --output=/var/run/cdi/nvidia.yaml
systemctl daemon-reload

grep -Fqx "${source_line}" "${hook}"
systemctl show buildkite-agent.service -p ExecStartPre --value |
  grep -Fq 'nvidia-ctk'

effective_timeout="$(
  systemctl show buildkite-agent.service -p TimeoutStartUSec --value
)"
effective_timeout_usec="$({
  LC_ALL=C systemd-analyze timespan "${effective_timeout}"
} | awk '$1 == "us:" {print $2}')"
[[ "${effective_timeout_usec}" =~ ^[0-9]+$ ]] ||
  die "could not parse effective TimeoutStartSec: ${effective_timeout}"
((effective_timeout_usec >= MIN_START_TIMEOUT_SECONDS * 1000000)) ||
  die "effective TimeoutStartSec is below ${MIN_START_TIMEOUT_SECONDS} seconds"

echo "CDI hook and ${effective_timeout} agent startup timeout installed; agent was not restarted"
