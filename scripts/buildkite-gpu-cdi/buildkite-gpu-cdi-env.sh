#!/usr/bin/env bash

# Use Docker's native CDI device injection instead of the legacy --gpus hook.
# The legacy hook can lose GPU cgroup access after `systemctl daemon-reload`.
cdi_selector="${BUILDKITE_PLUGIN_DOCKER_GPUS:-all}"
cdi_selector="${cdi_selector#device=}"

if [[ -n "${UUID:-}" ]]; then
  cdi_selector="${UUID}"
fi

cdi_device="nvidia.com/gpu=${cdi_selector}"
if ! nvidia-ctk cdi list 2>/dev/null | grep -Fqx "${cdi_device}"; then
  echo "ERROR: CDI device ${cdi_device} is absent from the NVIDIA CDI spec" >&2
  exit 1
fi

unset BUILDKITE_PLUGIN_DOCKER_GPUS
export BUILDKITE_PLUGIN_DOCKER_DEVICES_0="${cdi_device}"
