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
# Stands in for libcuda.so.1: cuInit writes what libkernrec.so writes for a
# process that launches nothing, but only when the armed recorder is injected
# (and the case does not say it never loads, or hangs).
cat > "$WORK/fixtures/cuda.c" <<'C'
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int cuInit(unsigned flags) {
  char path[4096];
  FILE *log;
  (void)flags;
  snprintf(path, sizeof path, "%s/probes", getenv("CASE_DIR"));
  if ((log = fopen(path, "a"))) { fputs("probe\n", log); fclose(log); }
  if (strcmp(getenv("MODE"), "cuda-probe-hang") == 0) sleep(60);
  if (strcmp(getenv("MODE"), "cuda-probe-fail") == 0) return 0;
  snprintf(path, sizeof path, "%s/cache/libkernrec.so", getenv("CASE_DIR"));
  if (!getenv("CUDA_INJECTION64_PATH") || strcmp(getenv("CUDA_INJECTION64_PATH"), path)) return 0;
  snprintf(path, sizeof path, "%s/kern.%ld.txt", getenv("KERNREC_DIR"), (long)getpid());
  if (!(log = fopen(path, "w"))) return 1;
  fprintf(log, "# kernrec v1 backend=cuda recorder=cupti activity=1 sync=1 pid=%ld ppid=1 exe=python\n"
          "# end records=0 unique=0 dropped=0 errors=0 unresolved=0\n", (long)getpid());
  fclose(log);
  return 0;
}
C
mkdir -p "$WORK/fixtures/cuda"
modes="cuda cuda-probe-ok cuda-probe-fail cuda-probe-hang cuda-recorded rocm rocm10 rocm-spawn rocm-fork rocm-forkserver missing-sdk missing-compiler failed-build wrong-version conflict stale disabled"
if command -v "${CC:-cc}" >/dev/null 2>&1; then
  "${CC:-cc}" -shared -fPIC "$WORK/fixtures/tool.c" -o "$WORK/fixtures/tool.so"
  "${CC:-cc}" -shared -fPIC "$WORK/fixtures/sdk.c" -o "$WORK/fixtures/sdk.so"
  "${CC:-cc}" -shared -fPIC -DPATCH=3 "$WORK/fixtures/sdk.c" -o "$WORK/fixtures/wrong-sdk.so"
  "${CC:-cc}" -shared -fPIC -DPATCH=5 "$WORK/fixtures/sdk.c" -o "$WORK/fixtures/rocm10-sdk.so"
  "${CC:-cc}" -shared -fPIC "$WORK/fixtures/cuda.c" -o "$WORK/fixtures/cuda/libcuda.so.1"
else
  # No C compiler (the GitHub runner image has none): placeholder libraries,
  # and only the cases that never load them. The CUDA case is among them.
  for lib in tool sdk wrong-sdk rocm10-sdk; do printf 'placeholder\n' > "$WORK/fixtures/$lib.so"; done
  modes="cuda cuda-recorded rocm-fork rocm-forkserver missing-sdk missing-compiler failed-build conflict stale disabled"
  echo "no C compiler (${CC:-cc}): skipping rocm rocm10 rocm-spawn wrong-version cuda-probe-*"
fi

for shell in bash sh; do
for mode in $modes; do
  case_dir="$WORK/$shell-$mode"
  mkdir -p "$case_dir/checkout" "$case_dir/cache" "$case_dir/source" \
    "$case_dir/rocm/include/rocprofiler-sdk" "$case_dir/rocm/lib" "$case_dir/bin"
  touch "$case_dir/rocm/include/rocprofiler-sdk/rocprofiler.h"
  cp "$WORK/fixtures/sdk.so" "$case_dir/rocm/lib/librocprofiler-sdk.so"
  cp "$WORK/fixtures/tool.so" "$case_dir/cache/libkernrec.so"
  cp "$HERE/kernrec_torch.py" "$case_dir/cache/kernrec_torch.py"
  cp "$HERE/kernrec_rocm_kineto.py" "$case_dir/cache/kernrec_rocm_kineto.py"
  mkdir -p "$case_dir/site"
  [[ "$mode" != wrong-version ]] || cp "$WORK/fixtures/wrong-sdk.so" "$case_dir/rocm/lib/librocprofiler-sdk.so"
  [[ "$mode" != rocm10 ]] || cp "$WORK/fixtures/rocm10-sdk.so" "$case_dir/rocm/lib/librocprofiler-sdk.so"
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
  backend=rocm; [[ "$mode" != cuda* ]] || backend=cuda
  # An empty job's exit probe runs only after a successful workload.
  status=7; [[ "$mode" != cuda-* ]] || status=0
  compiler=true; [[ "$mode" != missing-compiler ]] || compiler="$case_dir/missing-cxx"
  existing_tool=""; [[ "$mode" != conflict ]] || existing_tool=existing-profiler
  disable=""; [[ "$mode" != disabled ]] || disable=1
  mkdir -p "$case_dir/other-checkout"
  rc=0
  env -u KERNREC_ACTIVE_DIR -u VLLM_WORKER_MULTIPROC_METHOD -u VLLM_WORKER_SHUTDOWN_TIMEOUT_SECONDS \
    CASE_DIR="$case_dir" MODE="$mode" STATUS="$status" TOOL_FIXTURE="$WORK/fixtures/tool.so" KERNREC_PROBE_TIMEOUT=2 \
    LD_LIBRARY_PATH="$WORK/fixtures/cuda" \
    PATH="$case_dir/bin:$PATH" CXX="$compiler" ROCM_PATH="$case_dir/rocm" \
    KERNREC_CACHE_DIR="$case_dir/cache" KERNREC_SOURCE_DIR="$case_dir/source" KERNREC_SITE_DIR="$case_dir/site" \
    KERNREC_BACKEND="$backend" BUILDKITE_BUILD_CHECKOUT_PATH="$case_dir/other-checkout" \
    KERNREC_CHECKOUT_PATH="$case_dir/checkout" KERNREC_DISABLE="$disable" \
    BUILDKITE_JOB_ID=job BUILDKITE_STEP_KEY=step BUILDKITE_LABEL=$'quoted "label"\nline' \
    ROCP_TOOL_LIBRARIES="$existing_tool" LD_PRELOAD="$WORK/fixtures/tool.so" \
    HSA_OVERRIDE_GFX_VERSION=9.4.2 \
    "$shell" -c '
      set -eu
      case "$MODE" in
        rocm-spawn) export VLLM_WORKER_MULTIPROC_METHOD=spawn VLLM_WORKER_SHUTDOWN_TIMEOUT_SECONDS=17 ;;
        rocm-fork) export VLLM_WORKER_MULTIPROC_METHOD=fork ;;
        rocm-forkserver) export VLLM_WORKER_MULTIPROC_METHOD=forkserver ;;
      esac
      . "$1/ci_setup.sh"
      if [ "$MODE" = rocm ]; then . "$1/ci_setup.sh"; fi
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
    "worker_method": os.environ.get("VLLM_WORKER_MULTIPROC_METHOD"),
    "worker_shutdown": os.environ.get("VLLM_WORKER_SHUTDOWN_TIMEOUT_SECONDS"),
}))
with (root / "workload").open("a") as stream:
    stream.write("once\n")
if os.environ["MODE"] == "cuda-recorded":
    (Path(os.environ["KERNREC_DIR"]) / "kern.5.txt").write_text("k\n")
raise SystemExit(int(os.environ["STATUS"]))
PY
      # The generator finishes explicitly; the EXIT trap finishes again.
      case "$MODE" in cuda-*) kernrec_finish 0 ;; esac
    ' _ "$HERE" >"$case_dir/log" 2>&1 || rc=$?
  [[ "$rc" == "$status" ]] || { cat "$case_dir/log"; echo "$mode changed workload exit status: $rc"; exit 1; }
  "$KERNREC_PYTHON" - "$case_dir" "$mode" "$WORK/fixtures/tool.so" "$status" "$HERE" <<'PY'
import json
import sys
from pathlib import Path
root, mode, previous_preload = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
sys.path.insert(0, sys.argv[5])
import kernel_table
initial = json.loads((root / "initial.json").read_text())
final = json.loads((root / "checkout/.kernrec/job/kernrec.json").read_text())
assert (root / "workload").read_text() == "once\n"
assert not (root / "network").exists()
assert initial["sidecar"]["exit_status"] is None
assert final["exit_status"] == int(sys.argv[4])
assert final["recordings"] == sorted(p.name for p in (root / "checkout/.kernrec/job").glob("kern.*.txt"))
assert "recordings" not in initial["sidecar"]
assert final["label"] == 'quoted "label"\nline'
assert final["provenance"]["environment"]["HSA_OVERRIDE_GFX_VERSION"] == "9.4.2"
assert final["collection_available"] == (mode.startswith("cuda") or mode in {"rocm", "rocm10", "rocm-spawn"})
job = root / "checkout/.kernrec/job"
health = kernel_table._trace_health(job, sorted(job.glob("kern.*.txt")))
if mode in {"cuda-probe-ok", "cuda-probe-fail", "cuda-probe-hang"}:
    # One probe for both finishes, and its recording stays out of the upload.
    assert (root / "probes").read_text() == "probe\n"
    assert final["recordings"] == []
    probe = final["empty_probe"]
    assert probe["ok"] is (mode == "cuda-probe-ok"), probe
    assert mode != "cuda-probe-hang" or "timed out" in probe["detail"], probe
    assert health[1:3] == ((True, True) if probe["ok"] else (False, False)), health
else:
    assert "empty_probe" not in final
    assert not (root / "probes").exists()
    if mode == "cuda-recorded":
        assert final["recordings"] == ["kern.5.txt"]
if mode in {"rocm", "rocm10", "rocm-spawn"}:
    assert initial["tool"] == str(root / "cache/libkernrec_rocm.so")
    assert initial["preload"] == str(root / "rocm/lib/librocprofiler-sdk.so") + ":" + previous_preload
    assert (root / "builds").read_text() == "build\n", "reuse the job's successful build"
    assert final["provenance"]["versions"]["rocprofiler-sdk"] == ("1.3.5" if mode == "rocm10" else "1.3.2")
    assert final["provenance"]["expected_rocprofiler_sdk"] == final["provenance"]["versions"]["rocprofiler-sdk"]
    assert initial["worker_method"] == "spawn"
    assert initial["worker_shutdown"] == ("17" if mode == "rocm-spawn" else "30")
    assert (root / "site/kernrec_rocm_kineto.pth").read_text() == "import kernrec_rocm_kineto\n"
    assert not (root / "site/kernrec_torch.pth").exists()
    assert final["provenance"]["environment"]["VLLM_WORKER_MULTIPROC_METHOD"] == "spawn"
    assert final["provenance"]["environment"]["VLLM_WORKER_SHUTDOWN_TIMEOUT_SECONDS"] == initial["worker_shutdown"]
elif mode.startswith("cuda"):
    assert initial["cuda"] == str(root / "cache/libkernrec.so")
    assert initial["preload"] == previous_preload
    assert initial["worker_method"] is None
    assert initial["worker_shutdown"] == "30"
    assert (root / "site/kernrec_torch.pth").read_text() == "import kernrec_torch\n"
    assert (root / "site/kernrec_torch.py").read_text() == (root / "cache/kernrec_torch.py").read_text()
else:
    assert not (root / "site/kernrec_torch.pth").exists()
    assert not (root / "site/kernrec_rocm_kineto.pth").exists()
    assert final["collection_error"]
    assert initial["preload"] == previous_preload
    assert initial["tool"] == ("existing-profiler" if mode == "conflict" else "")
    assert initial["worker_method"] == ({"rocm-fork": "fork", "rocm-forkserver": "forkserver"}.get(mode))
    assert initial["worker_shutdown"] is None
    if mode == "stale":
        assert (root / "checkout/.kernrec/job/kern.99.txt").read_text() == "previous_kernel\n"
PY
  echo "ci_setup.sh $mode ($shell): PASS"
done
done
