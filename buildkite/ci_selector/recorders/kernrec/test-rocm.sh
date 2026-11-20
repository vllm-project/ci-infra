#!/usr/bin/env bash
# CPU-only producer contract checks; SDK 1.3.2 or 1.3.5 headers/library are required.
set -euo pipefail
SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROCM_ROOT=${ROCM_PATH:-/opt/rocm}
SDK_INCLUDE=${ROCPROFILER_SDK_INCLUDE:-$ROCM_ROOT/include}
SDK_LIB=${ROCPROFILER_SDK_LIB:-$ROCM_ROOT/lib}
TEST_DIR=$(mktemp -d)
trap 'rm -rf -- "$TEST_DIR"' EXIT
"${CXX:-g++}" -std=c++17 -O2 -Wall -Wextra -Werror -pthread \
  -D__HIP_PLATFORM_AMD__ -I"$SDK_INCLUDE" "$SOURCE_DIR/test_rocm_sdk.cpp" \
  -L"$SDK_LIB" -Wl,-rpath,"$SDK_LIB" -lrocprofiler-sdk -o "$TEST_DIR/check"
env -u ROCP_TOOL_LIBRARIES -u LD_PRELOAD -u KERNREC_SYNC "$TEST_DIR/check" "$TEST_DIR/recordings"
# The consumer must accept what a SIGKILLed sync=1 producer left behind.
"${PYTHON:-python3}" - "$SOURCE_DIR" "$TEST_DIR/recordings/sync-killed" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import kernel_table
files = list(Path(sys.argv[2]).glob("kern.*.txt"))
assert len(files) == 1, files
names, dropped, complete = kernel_table.read_recording(files[0])
assert names == {"_Z4usedv"} and not dropped and complete, (names, dropped, complete)
assert not kernel_table.read_recording(files[0], footer_rule=True)[2]
print("killed sync=1 ROCm recording reads as complete")
PY
