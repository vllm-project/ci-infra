#!/usr/bin/env bash
# Sourced before CI workloads. CUDA uses the shipped CUPTI tool; ROCm builds
# our tool against the image's preprovisioned ROCProfiler SDK. Setup failures
# leave the workload runnable and an unavailable recording sidecar.

kernrec_write_sidecar() {
  [[ -n "${KERNREC_DIR:-}" && -d "$KERNREC_DIR" ]] || return 0
  "${KERNREC_PYTHON:-python3}" - "${1:-null}" <<'PY' 2>/dev/null || true
import importlib.metadata
import json
import os
import platform
import sys
from pathlib import Path

env = os.environ
versions = {}
if env.get("KERNREC_SDK_VERSION"):
    versions["rocprofiler-sdk"] = env["KERNREC_SDK_VERSION"]
for package in ("torch", "amd-aiter", "triton", "pytorch-triton-rocm"):
    try:
        versions[package] = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        pass
try:
    versions["rocm"] = (
        Path(env.get("ROCM_PATH", "/opt/rocm")) / ".info" / "version"
    ).read_text().strip()
except OSError:
    pass
names = {"ROCR_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES",
         "HSA_OVERRIDE_GFX_VERSION"}
backend = env.get("KERNREC_BACKEND", "cuda")
data = {
    "step_key": env.get("BUILDKITE_STEP_KEY", ""),
    "label": env.get("BUILDKITE_LABEL", ""),
    "job_id": env.get("BUILDKITE_JOB_ID", "local"),
    "build": env.get("BUILDKITE_BUILD_NUMBER", ""),
    "commit": env.get("BUILDKITE_COMMIT", ""),
    "parallel_job": env.get("BUILDKITE_PARALLEL_JOB", ""),
    "parallel_job_count": env.get("BUILDKITE_PARALLEL_JOB_COUNT", ""),
    "exit_status": None if sys.argv[1] == "null" else int(sys.argv[1]),
    "host": platform.node(),
    "backend": backend,
    "recorder": "rocprofiler-sdk" if backend == "rocm" else "cupti",
    "collection_available": env.get("KERNREC_COLLECTION_AVAILABLE") == "1",
    "collection_error": env.get("KERNREC_COLLECTION_ERROR") or None,
    "provenance": {
        "ci_infra_branch": env.get("KERNREC_BRANCH", env.get("VLLM_CI_BRANCH", "main")),
        "versions": versions,
        "expected_rocprofiler_sdk": "1.3.2" if backend == "rocm" else None,
        "environment": {key: value for key, value in sorted(env.items())
                        if key in names or key.startswith("VLLM_ROCM_USE_AITER")},
    },
}
path = Path(env["KERNREC_DIR"]) / "kernrec.json"
temporary = path.with_suffix(".json.tmp")
temporary.write_text(json.dumps(data, indent=2) + "\n")
temporary.chmod(0o666)
temporary.replace(path)
PY
}

kernrec_finish() {
  local rc="${1:-0}"
  kernrec_write_sidecar "$rc"
  if [[ -n "${KERNREC_ROOT:-}" ]]; then
    chmod -R a+rwX "$KERNREC_ROOT/.kernrec" 2>/dev/null || true
  fi
}

kernrec_setup() {
  local branch="${KERNREC_BRANCH:-${VLLM_CI_BRANCH:-main}}"
  local base="https://raw.githubusercontent.com/vllm-project/ci-infra/${branch}/buildkite/ci_selector/recorders/kernrec"
  local dir="${KERNREC_CACHE_DIR:-/tmp/kernrec/${BUILDKITE_JOB_ID:-local}}"
  local root="" candidate
  for candidate in "${KERNREC_CHECKOUT_PATH:-}" "${BUILDKITE_BUILD_CHECKOUT_PATH:-}" /workdir; do
    if [[ -n "$candidate" && -d "$candidate" ]]; then root="$candidate"; break; fi
  done
  [[ -n "$root" ]] || root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
  export KERNREC_ROOT="$root" KERNREC_DIR="$root/.kernrec/${BUILDKITE_JOB_ID:-local}"
  export KERNREC_BACKEND="${KERNREC_BACKEND:-cuda}"
  export KERNREC_COLLECTION_AVAILABLE=0 KERNREC_COLLECTION_ERROR="setup incomplete"
  unset KERNREC_SDK_VERSION
  mkdir -p "$KERNREC_DIR" && chmod 0777 "$root/.kernrec" "$KERNREC_DIR" || return 1
  kernrec_write_sidecar null
  # The generator also calls finish: another prelude may replace the EXIT trap.
  trap 'kernrec_finish $?' EXIT
  if [[ "${KERNREC_ACTIVE_DIR:-}" != "$KERNREC_DIR" ]] && compgen -G "$KERNREC_DIR/kern.*.txt" >/dev/null; then
    KERNREC_COLLECTION_ERROR="existing recordings require a fresh job directory"
    return 1
  fi
  mkdir -p "$dir" || return 1

  case "$KERNREC_BACKEND" in
    cuda)
      if [[ ! -s "$dir/libkernrec.so" ]]; then
        KERNREC_COLLECTION_ERROR="CUDA recorder download failed"
        curl -sSfL --retry 3 --max-time 60 -o "$dir/libkernrec.so.tmp" "$base/libkernrec.so" \
          && mv "$dir/libkernrec.so.tmp" "$dir/libkernrec.so" || return 1
      fi
      local cupti
      cupti=$("${KERNREC_PYTHON:-python3}" - <<'PY' 2>/dev/null
import glob
import os
import sysconfig
root = os.path.join(sysconfig.get_paths()["purelib"], "nvidia")
hits = sorted(glob.glob(os.path.join(root, "**", "libcupti.so.1[0-9]"), recursive=True))
print(os.path.dirname(hits[0]) if hits else "")
PY
      )
      if [[ -n "$cupti" ]]; then
        export LD_LIBRARY_PATH="$cupti${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
      fi
      export CUDA_INJECTION64_PATH="$dir/libkernrec.so"
      ;;
    rocm)
      local rocm="${ROCM_PATH:-/opt/rocm}" sdk_include sdk_lib source file
      sdk_include="${ROCPROFILER_SDK_INCLUDE:-$rocm/include}"
      sdk_lib="${ROCPROFILER_SDK_LIB:-$rocm/lib}"
      KERNREC_COLLECTION_ERROR="preprovisioned ROCProfiler SDK or compiler unavailable"
      [[ -f "$sdk_include/rocprofiler-sdk/rocprofiler.h" && -f "$sdk_lib/librocprofiler-sdk.so" ]] || return 1
      command -v "${CXX:-g++}" >/dev/null 2>&1 || return 1
      KERNREC_COLLECTION_ERROR="another ROCProfiler tool is already configured"
      [[ -z "${ROCP_TOOL_LIBRARIES:-}" || "$ROCP_TOOL_LIBRARIES" == "$dir/libkernrec_rocm.so" ]] || return 1
      if [[ ! -s "$dir/libkernrec_rocm.so" ]]; then
        source="${KERNREC_SOURCE_DIR:-$dir/source}"
        mkdir -p "$source" || return 1
        if [[ -z "${KERNREC_SOURCE_DIR:-}" ]]; then
          KERNREC_COLLECTION_ERROR="ROCm recorder source download failed"
          for file in build-rocm.sh kernrec_rocm.cpp; do
            curl -sSfL --retry 3 --max-time 60 -o "$source/$file" "$base/$file" || return 1
          done
        fi
        KERNREC_COLLECTION_ERROR="ROCm recorder build failed; see setup.log"
        timeout 60 bash "$source/build-rocm.sh" "$dir/libkernrec_rocm.so.tmp" >"$KERNREC_DIR/setup.log" 2>&1 \
          && mv "$dir/libkernrec_rocm.so.tmp" "$dir/libkernrec_rocm.so" || return 1
      fi
      KERNREC_COLLECTION_ERROR="ROCProfiler SDK 1.3.2 runtime preflight failed; see preflight.log"
      timeout 10 "${KERNREC_PYTHON:-python3}" - "$sdk_lib/librocprofiler-sdk.so" "$dir/libkernrec_rocm.so" >"$KERNREC_DIR/preflight.log" 2>&1 <<'PY' || return 1
import ctypes
import sys
sdk = ctypes.CDLL(sys.argv[1])
version = [ctypes.c_uint32() for _ in range(3)]
sdk.rocprofiler_get_version.argtypes = [ctypes.POINTER(ctypes.c_uint32)] * 3
sdk.rocprofiler_get_version.restype = ctypes.c_int
status = sdk.rocprofiler_get_version(*(ctypes.byref(part) for part in version))
actual = tuple(part.value for part in version)
if status != 0 or actual != (1, 3, 2):
    raise SystemExit(f"expected ROCProfiler SDK 1.3.2, got {actual}, status={status}")
ctypes.CDLL(sys.argv[2])
print("ROCProfiler SDK 1.3.2 and recorder dependencies loaded")
PY
      export KERNREC_SDK_VERSION=1.3.2
      export ROCP_TOOL_LIBRARIES="$dir/libkernrec_rocm.so"
      case ":${LD_PRELOAD:-}:" in
        *":$sdk_lib/librocprofiler-sdk.so:"*) ;;
        *) export LD_PRELOAD="$sdk_lib/librocprofiler-sdk.so${LD_PRELOAD:+:$LD_PRELOAD}" ;;
      esac
      ;;
    *) KERNREC_COLLECTION_ERROR="unsupported recorder backend: $KERNREC_BACKEND"; return 1 ;;
  esac
  export KERNREC_COLLECTION_AVAILABLE=1 KERNREC_COLLECTION_ERROR=""
  KERNREC_ACTIVE_DIR="$KERNREC_DIR"
  kernrec_write_sidecar null
  echo "kernrec: on  backend=$KERNREC_BACKEND  dir=$KERNREC_DIR"
}

if ! kernrec_setup; then
  kernrec_write_sidecar null
  echo "kernrec: ${KERNREC_COLLECTION_ERROR:-setup failed}; workload runs without collection"
fi
