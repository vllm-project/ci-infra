#!/usr/bin/env bash
set -euo pipefail

readonly MIN_START_TIMEOUT_USEC=120000000
readonly HOOK=/etc/buildkite-agent/hooks/environment
readonly SOURCE_LINE='source /usr/local/libexec/buildkite-gpu-cdi-env.sh'

failures=0

check() {
  local description=$1
  shift
  if "$@"; then
    printf 'PASS: %s\n' "${description}"
  else
    printf 'FAIL: %s\n' "${description}" >&2
    failures=$((failures + 1))
  fi
}

timeout_at_least_minimum() {
  local timeout_usec=$1
  local minimum_usec=$2
  [[ "${timeout_usec}" =~ ^[0-9]+$ ]] &&
    ((timeout_usec >= minimum_usec))
}

effective_timeout="$(
  systemctl show buildkite-agent.service -p TimeoutStartUSec --value
)"
effective_timeout_usec="$({
  LC_ALL=C systemd-analyze timespan "${effective_timeout}"
} | awk '$1 == "us:" {print $2}')"

check "Buildkite agent service is active" systemctl is-active --quiet buildkite-agent.service
check "environment hook sources the CDI selector" grep -Fqx "${SOURCE_LINE}" "${HOOK}"
check "CDI selector helper is executable" test -x /usr/local/libexec/buildkite-gpu-cdi-env.sh
check "agent startup regenerates the NVIDIA CDI spec" \
  bash -c "systemctl show buildkite-agent.service -p ExecStartPre --value | grep -Fq nvidia-ctk"
check "effective startup timeout is at least 120 seconds (${effective_timeout})" \
  timeout_at_least_minimum "${effective_timeout_usec}" "${MIN_START_TIMEOUT_USEC}"
check "NVIDIA CDI inventory contains the all selector" \
  bash -c "nvidia-ctk cdi list 2>/dev/null | grep -Fqx nvidia.com/gpu=all"

((failures == 0)) || exit 1
