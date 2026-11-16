#!/usr/bin/env bash
# CPU-only failure checks; uses installed real CUPTI headers, mocks runtime APIs.
# Then reads the files the checks wrote with kernel_table.read_recording, so the
# recorder's output and the table's completeness rule are tested together.
set -euo pipefail
SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CUDA_ROOT=${CUDA_HOME:-/usr/local/cuda}
CUPTI_INCLUDE=${CUPTI_INCLUDE:-$CUDA_ROOT/extras/CUPTI/include}
CUDA_INCLUDE=${CUDA_INCLUDE:-$CUDA_ROOT/include}
TEST_DIR=$(mktemp -d)
trap 'rm -rf -- "$TEST_DIR"' EXIT
"${CC:-gcc}" -std=gnu11 -O2 -Wall -Wextra -Werror -pthread \
  -I"$CUPTI_INCLUDE" -I"$CUDA_INCLUDE" "$SOURCE_DIR/test_cuda_callbacks.c" \
  -o "$TEST_DIR/check"
for mode in register enable names drops read dropped-query flush write release released-pid \
    marker-idle marker-active marker-release marker-subscribe-fails \
    sync-launch sync-graph sync-unresolved sync-miss sync-release sync-cache-clear \
    sync-write-fails sync-off sync-enable-fails sync-kill legacy-kill; do
  mkdir -p "$TEST_DIR/$mode"
  "$TEST_DIR/check" "$TEST_DIR/$mode" "$mode"
done
"${PYTHON:-python3}" - "$SOURCE_DIR" "$TEST_DIR" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import kernel_table

# mode -> complete? A launch-time file needs no footer; today's format does.
expected = {
    "sync-launch": True, "sync-graph": True, "sync-kill": True,
    "sync-miss": False, "sync-unresolved": False, "sync-release": False,
    "sync-off": True, "legacy-kill": False,
    "sync-write-fails": False, "write": False,
}
for mode, complete in expected.items():
    (path,) = sorted((Path(sys.argv[2]) / mode).glob("kern.*.txt"))
    clean = kernel_table.read_recording(path)[2]
    assert clean is complete, (mode, clean, path.read_text())
    print(f"read_recording {mode}: {'complete' if clean else 'incomplete'}")
PY
