#!/usr/bin/env bash
# Verify preflight, injection, and workload status without a GPU or downloads.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
: "${KERNREC_PYTHON:?set to the project virtual-environment Python}"
export KERNREC_PYTHON
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/fixtures"
cat > "$WORK/fixtures/sdk.c" <<'C'
#include <stdint.h>
#ifndef PATCH
#define PATCH 2
#endif
int rocprofiler_get_version(uint32_t* major, uint32_t* minor, uint32_t* patch) {
  *major = 1; *minor = 3; *patch = PATCH; return 0;
}
C
printf 'int tool(void) { return 0; }\n' > "$WORK/fixtures/tool.c"
cc -shared -fPIC "$WORK/fixtures/tool.c" -o "$WORK/fixtures/tool.so"
cc -shared -fPIC "$WORK/fixtures/sdk.c" -o "$WORK/fixtures/sdk.so"
cc -shared -fPIC -DPATCH=3 "$WORK/fixtures/sdk.c" -o "$WORK/fixtures/wrong-sdk.so"

for mode in cuda rocm missing-sdk missing-compiler failed-build wrong-version conflict stale; do
  case_dir="$WORK/$mode"
  mkdir -p "$case_dir/checkout" "$case_dir/cache" "$case_dir/source" \
    "$case_dir/rocm/include/rocprofiler-sdk" "$case_dir/rocm/lib" "$case_dir/bin"
  touch "$case_dir/rocm/include/rocprofiler-sdk/rocprofiler.h"
  cp "$WORK/fixtures/sdk.so" "$case_dir/rocm/lib/librocprofiler-sdk.so"
  cp "$WORK/fixtures/tool.so" "$case_dir/cache/libkernrec.so"
  [[ "$mode" != wrong-version ]] || cp "$WORK/fixtures/wrong-sdk.so" "$case_dir/rocm/lib/librocprofiler-sdk.so"
  [[ "$mode" != missing-sdk ]] || rm "$case_dir/rocm/lib/librocprofiler-sdk.so"
  if [[ "$mode" == stale ]]; then
    mkdir -p "$case_dir/checkout/.kernrec/job"
    printf 'previous_kernel\n' > "$case_dir/checkout/.kernrec/job/kern.99.txt"
  fi
  cat > "$case_dir/source/build-rocm.sh" <<'BUILD'
#!/usr/bin/env bash
printf 'build\n' >> "$CASE_DIR/builds"
[[ "$MODE" != failed-build ]] || exit 1
cp "$TOOL_FIXTURE" "$1"
BUILD
  cat > "$case_dir/bin/curl" <<'CURL'
#!/usr/bin/env bash
printf 'unexpected network request\n' >> "$CASE_DIR/network"
exit 99
CURL
  chmod +x "$case_dir/bin/curl"
  backend=rocm; [[ "$mode" != cuda ]] || backend=cuda
  compiler=true; [[ "$mode" != missing-compiler ]] || compiler="$case_dir/missing-cxx"
  existing_tool=""; [[ "$mode" != conflict ]] || existing_tool=existing-profiler
  mkdir -p "$case_dir/other-checkout"
  rc=0
  env -u KERNREC_ACTIVE_DIR \
    CASE_DIR="$case_dir" MODE="$mode" TOOL_FIXTURE="$WORK/fixtures/tool.so" \
    PATH="$case_dir/bin:$PATH" CXX="$compiler" ROCM_PATH="$case_dir/rocm" \
    KERNREC_CACHE_DIR="$case_dir/cache" KERNREC_SOURCE_DIR="$case_dir/source" \
    KERNREC_BACKEND="$backend" BUILDKITE_BUILD_CHECKOUT_PATH="$case_dir/other-checkout" \
    KERNREC_CHECKOUT_PATH="$case_dir/checkout" \
    BUILDKITE_JOB_ID=job BUILDKITE_STEP_KEY=step BUILDKITE_LABEL=$'quoted "label"\nline' \
    ROCP_TOOL_LIBRARIES="$existing_tool" LD_PRELOAD="$WORK/fixtures/tool.so" \
    HSA_OVERRIDE_GFX_VERSION=9.4.2 \
    bash -c '
      set -eu
      . "$1/ci_setup.sh"
      if [[ "$MODE" == rocm ]]; then . "$1/ci_setup.sh"; fi
      "$KERNREC_PYTHON" - <<'"'"'PY'"'"'
import json
import os
from pathlib import Path
root = Path(os.environ["CASE_DIR"])
(root / "initial.json").write_text(json.dumps({
    "sidecar": json.loads((Path(os.environ["KERNREC_DIR"]) / "kernrec.json").read_text()),
    "cuda": os.environ.get("CUDA_INJECTION64_PATH"),
    "tool": os.environ.get("ROCP_TOOL_LIBRARIES"),
    "preload": os.environ.get("LD_PRELOAD"),
}))
with (root / "workload").open("a") as stream:
    stream.write("once\n")
raise SystemExit(7)
PY
    ' _ "$HERE" >"$case_dir/log" 2>&1 || rc=$?
  [[ "$rc" == 7 ]] || { cat "$case_dir/log"; echo "$mode changed workload exit status: $rc"; exit 1; }
  "$KERNREC_PYTHON" - "$case_dir" "$mode" "$WORK/fixtures/tool.so" <<'PY'
import json
import sys
from pathlib import Path
root, mode, previous_preload = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
initial = json.loads((root / "initial.json").read_text())
final = json.loads((root / "checkout/.kernrec/job/kernrec.json").read_text())
assert (root / "workload").read_text() == "once\n"
assert not (root / "network").exists()
assert initial["sidecar"]["exit_status"] is None
assert final["exit_status"] == 7
assert final["label"] == 'quoted "label"\nline'
assert final["provenance"]["environment"]["HSA_OVERRIDE_GFX_VERSION"] == "9.4.2"
assert final["collection_available"] == (mode in {"cuda", "rocm"})
if mode == "rocm":
    assert initial["tool"] == str(root / "cache/libkernrec_rocm.so")
    assert initial["preload"] == str(root / "rocm/lib/librocprofiler-sdk.so") + ":" + previous_preload
    assert (root / "builds").read_text() == "build\n", "reuse the job's successful build"
    assert final["provenance"]["versions"]["rocprofiler-sdk"] == "1.3.2"
elif mode == "cuda":
    assert initial["cuda"] == str(root / "cache/libkernrec.so")
    assert initial["preload"] == previous_preload
else:
    assert final["collection_error"]
    assert initial["preload"] == previous_preload
    assert initial["tool"] == ("existing-profiler" if mode == "conflict" else "")
    if mode == "stale":
        assert (root / "checkout/.kernrec/job/kern.99.txt").read_text() == "previous_kernel\n"
PY
  echo "ci_setup.sh $mode: PASS"
done
