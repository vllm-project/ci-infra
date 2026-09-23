#!/usr/bin/env bash
# Build libkernrec.so against whatever CUPTI this machine has.
#
# Looks for CUPTI headers and library in the CUDA toolkit first
# ($CUDA_HOME, default /usr/local/cuda), then in the pip wheels torch ships
# (nvidia-cuda-cupti-cuXX), which carry both headers and libcupti.so.
set -euo pipefail
cd "$(dirname "$0")"

CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"
CUPTI_INC="" CUPTI_LIB="" CUDA_INC=""

if [ -f "$CUDA_HOME/extras/CUPTI/include/cupti.h" ]; then
  CUPTI_INC="$CUDA_HOME/extras/CUPTI/include"
  CUPTI_LIB="$CUDA_HOME/extras/CUPTI/lib64"
fi
if [ -f "$CUDA_HOME/include/cuda.h" ]; then
  CUDA_INC="$CUDA_HOME/include"
fi

pipdir() { python3 -c "import os,importlib; m=importlib.import_module('$1'); print(os.path.dirname(m.__file__))" 2>/dev/null || true; }
if [ -z "$CUPTI_INC" ]; then
  P=$(pipdir nvidia.cuda_cupti)
  if [ -n "$P" ] && [ -f "$P/include/cupti.h" ]; then
    CUPTI_INC="$P/include"; CUPTI_LIB="$P/lib"
  fi
fi
if [ -z "$CUDA_INC" ]; then
  P=$(pipdir nvidia.cuda_runtime)
  if [ -n "$P" ] && [ -f "$P/include/cuda.h" ]; then CUDA_INC="$P/include"; fi
fi

[ -n "$CUPTI_INC" ] || { echo "build.sh: cupti.h not found (set CUDA_HOME)" >&2; exit 1; }
[ -n "$CUDA_INC" ] || { echo "build.sh: cuda.h not found (set CUDA_HOME)" >&2; exit 1; }

LIBFILE=$(ls "$CUPTI_LIB"/libcupti.so* 2>/dev/null | sort | head -1)
[ -n "$LIBFILE" ] || { echo "build.sh: libcupti.so not found in $CUPTI_LIB" >&2; exit 1; }

# Kernel activity records are versioned structs. Take the newest the header
# defines; the fields we read sit in the shared prefix.
KT=CUpti_ActivityKernel9
for cand in CUpti_ActivityKernel11 CUpti_ActivityKernel10; do
  if printf '#include <cupti.h>\nint main(void){return (int)sizeof(%s);}\n' "$cand" \
     | gcc -x c -fsyntax-only -I"$CUPTI_INC" -I"$CUDA_INC" - 2>/dev/null; then
    KT=$cand; break
  fi
done

gcc -std=gnu11 -O2 -Wall -Wextra -shared -fPIC \
  -DKERNREC_KERNEL_T="$KT" -I"$CUPTI_INC" -I"$CUDA_INC" \
  kernrec.c -o libkernrec.so "$LIBFILE" -Wl,-rpath,"$CUPTI_LIB" -lpthread

echo "built $(pwd)/libkernrec.so"
echo "  record struct: $KT"
echo "  cupti:         $LIBFILE"
echo "  headers:       $CUPTI_INC"
