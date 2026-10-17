#!/usr/bin/env bash
# CPU-only producer contract checks; SDK 1.3.2 headers/library are required.
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
env -u ROCP_TOOL_LIBRARIES -u LD_PRELOAD "$TEST_DIR/check" "$TEST_DIR/recordings"
