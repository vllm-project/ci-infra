#!/usr/bin/env bash
set -euo pipefail

version=3.138.2

# A base image whose stable agent is at least as new as the pin keeps it:
# newer stack bases ship a newer agent and their watchdog kills downgrades.
# REQUIRE_EXACT_AGENT_VERSION=true (the AGENT_ONLY release bake) keeps the
# base only when its agent matches the pin exactly, so release workers always
# carry the pinned agent. Odd or failing --version output falls through to
# the pinned install. BUILDKITE_AGENT_STABLE_PATH only exists as a test seam;
# the default is the real path.
stable_agent=${BUILDKITE_AGENT_STABLE_PATH:-/usr/bin/buildkite-agent-stable}
if [[ -x "$stable_agent" ]] && version_output=$("$stable_agent" --version 2>/dev/null); then
  # Keep a prerelease suffix in the token, then never keep a prerelease:
  # sort -V would rank 3.138.2-beta.1 above the stable 3.138.2 pin.
  installed=$(grep -oE '[0-9]+(\.[0-9]+)+(-[0-9A-Za-z.]+)?' <<<"$version_output" | head -n 1 || true)
  keep=false
  if [[ -n "$installed" && "$installed" != *-* ]]; then
    if [[ "${REQUIRE_EXACT_AGENT_VERSION:-false}" == "true" ]]; then
      if [[ "$installed" == "$version" ]]; then keep=true; fi
    elif [[ "$(printf '%s\n%s\n' "$version" "$installed" | sort -V | head -n 1)" == "$version" ]]; then
      keep=true
    fi
  fi
  if [[ "$keep" == "true" ]]; then
    # Keep the Secrets CLI guarantee the install path below verifies.
    "$stable_agent" secret get --help >/dev/null
    echo "Base image agent $installed satisfies pinned $version; keeping it"
    exit 0
  fi
fi

# Install the pinned agent, preserving the stack's boot-time stable/beta
# selection and service configuration.
case "$(uname -m)" in
  x86_64) arch=amd64; checksum=a5fa9b1190cb622cb446b77ced62e8a92d4b149c7816e1aaeaaa81fa6481b10f ;;
  aarch64) arch=arm64; checksum=95e4b60858bcdd5eb4a9341ec1d9ea0d7965a456cf3615c42e329a34c524a84e ;;
  *) echo 'Unsupported architecture' >&2; exit 1 ;;
esac
archive="buildkite-agent-linux-${arch}-${version}.tar.gz"
workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
cd "$workdir"
curl --fail --silent --show-error --location --retry 3 \
  "https://github.com/buildkite/agent/releases/download/v${version}/${archive}" -o "$archive"
printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check --strict
tar -xzf "$archive" ./buildkite-agent
./buildkite-agent --version | grep -F "version ${version}"
./buildkite-agent secret get --help >/dev/null
sudo install -o root -g root -m 0755 buildkite-agent /usr/bin/buildkite-agent-stable
/usr/bin/buildkite-agent-stable --version | grep -F "version ${version}"
