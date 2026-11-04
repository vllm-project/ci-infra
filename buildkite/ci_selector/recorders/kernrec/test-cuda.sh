#!/usr/bin/env bash
# CPU-only failure checks; uses installed real CUPTI headers, mocks runtime APIs.
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
for mode in register enable names drops read dropped-query flush write; do
  "$TEST_DIR/check" "$TEST_DIR/$mode" "$mode"
done
