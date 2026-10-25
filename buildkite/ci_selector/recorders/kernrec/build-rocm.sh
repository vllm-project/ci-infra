#!/usr/bin/env bash
# Build against the SDK installed in the workload image; do not commit the binary.
set -euo pipefail
SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROCM_ROOT=${ROCM_PATH:-/opt/rocm}
SDK_INCLUDE=${ROCPROFILER_SDK_INCLUDE:-$ROCM_ROOT/include}
SDK_LIB=${ROCPROFILER_SDK_LIB:-$ROCM_ROOT/lib}
OUTPUT=${1:-$SOURCE_DIR/libkernrec_rocm.so}
if [[ ! -f "$SDK_INCLUDE/rocprofiler-sdk/rocprofiler.h" ||
      ! -f "$SDK_LIB/librocprofiler-sdk.so" ]]; then
  echo "kernrec-rocm: ROCProfiler SDK headers/library missing under $ROCM_ROOT" >&2
  exit 1
fi
"${CXX:-g++}" -std=c++17 -O2 -Wall -Wextra -Werror -shared -fPIC -pthread \
  -D__HIP_PLATFORM_AMD__ -I"$SDK_INCLUDE" "$SOURCE_DIR/kernrec_rocm.cpp" \
  -L"$SDK_LIB" -Wl,-rpath,"$SDK_LIB" -lrocprofiler-sdk -o "$OUTPUT"
echo "built $OUTPUT"
