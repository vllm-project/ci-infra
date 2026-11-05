#!/usr/bin/env bash
set -euo pipefail

# Keep the stack's boot-time stable/beta selection and service configuration.
version=3.138.2
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
tar -xzf "$archive" buildkite-agent
./buildkite-agent --version | grep -F "version ${version}"
./buildkite-agent secret get --help >/dev/null
sudo install -o root -g root -m 0755 buildkite-agent /usr/bin/buildkite-agent-stable
/usr/bin/buildkite-agent-stable --version | grep -F "version ${version}"
