#!/usr/bin/env bash
# Build libkernrec.so for the CI test image from any machine, no CUDA needed.
#
# The test image is nvidia/cuda:<ver>-base and has no compiler, so the library
# is prebuilt and committed next to this script. This reproduces that build:
# zig cross-compiles to x86_64 glibc 2.28 against the CUPTI and CUDA headers
# from NVIDIA's own pip wheels and apt packages. Pin CUPTI to the CUDA minor
# the image runs (see CUDA_VERSION in vLLM's docker/Dockerfile).
#
#   ./cross-build.sh            # CUDA 13.0 by default
#   CUDA_MINOR=12.9 ./cross-build.sh
#
# Needs: python3, curl, unzip, ar, tar. Everything else is fetched into
# $WORK (default ./.cross-build, safe to delete).
set -euo pipefail
cd "$(dirname "$0")"

CUDA_MINOR="${CUDA_MINOR:-13.0}"
WORK="${WORK:-$PWD/.cross-build}"
MAJOR="${CUDA_MINOR%%.*}"
mkdir -p "$WORK/wheels" "$WORK/hdr"

# --- toolchain: zig via pip, in a private venv ------------------------------
if [ ! -x "$WORK/venv/bin/python" ]; then
  python3 -m venv "$WORK/venv"
  "$WORK/venv/bin/pip" install -q ziglang pyelftools
fi
ZIG="$WORK/venv/bin/python -m ziglang"

# --- headers + libcupti from pip wheels --------------------------------------
# CUDA 13 wheels are `nvidia-cuda-cupti` (flat nvidia/cu13 layout); CUDA 12
# wheels are `nvidia-cuda-cupti-cu12` (nvidia/cuda_cupti layout).
pypi_pick() { # package version-prefix -> "filename url"
  curl -s "https://pypi.org/pypi/$1/json" | python3 -c '
import json, sys
pkg, pre = sys.argv[1], sys.argv[2]
rel = json.load(sys.stdin)["releases"]
vs = sorted((v for v in rel if v.startswith(pre) and "rc" not in v),
            key=lambda v: [int(x) for x in v.split(".")])
if not vs: sys.exit(f"no {pkg} {pre}* on PyPI")
files = [f for f in rel[vs[-1]] if "manylinux" in f["filename"] and "x86_64" in f["filename"]]
print(files[0]["filename"], files[0]["url"])' "$1" "$2"
}
fetch_wheel() { # package version-prefix
  read -r fn url < <(pypi_pick "$1" "$2")
  [ -s "$WORK/wheels/$fn" ] || curl -sSfL -o "$WORK/wheels/$fn" "$url"
  echo "$WORK/wheels/$fn"
}
if [ "$MAJOR" -ge 13 ]; then
  CUPTI_WHL=$(fetch_wheel nvidia-cuda-cupti "$CUDA_MINOR.")
  RUNTIME_WHL=$(fetch_wheel nvidia-cuda-runtime "$MAJOR.")
else
  CUPTI_WHL=$(fetch_wheel "nvidia-cuda-cupti-cu$MAJOR" "$CUDA_MINOR.")
  RUNTIME_WHL=$(fetch_wheel "nvidia-cuda-runtime-cu$MAJOR" "$MAJOR.")
fi
unzip -q -o "$CUPTI_WHL" 'nvidia/*/include/*' 'nvidia/*/lib/libcupti.so.*' -d "$WORK/hdr"
unzip -q -o "$RUNTIME_WHL" 'nvidia/*/include/*' -d "$WORK/hdr"
INC=$(dirname "$(find "$WORK/hdr" -name cupti.h | head -1)")
CUDA_INC=$(dirname "$(find "$WORK/hdr" -name cuda.h | head -1)")
CUPTI_SO=$(find "$WORK/hdr" -name "libcupti.so.$MAJOR" | head -1)

# --- crt/host_defines.h: only shipped in the apt package cuda-crt -----------
if [ ! -f "$INC/crt/host_defines.h" ]; then
  REPO=https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64
  PKG="cuda-crt-${CUDA_MINOR/./-}"
  # No early exit in awk: with pipefail, curl dying of SIGPIPE would abort us.
  DEB=$(curl -s "$REPO/Packages" | awk -v p="Package: $PKG" '$0==p{f=1} f&&/^Filename:/{print $2; f=0}' | head -1)
  [ -n "$DEB" ] || { echo "cross-build: $PKG not in $REPO" >&2; exit 1; }
  curl -sSfL -o "$WORK/crt.deb" "$REPO/$DEB"
  rm -rf "$WORK/crt" && mkdir -p "$WORK/crt" && (cd "$WORK/crt" && ar x ../crt.deb data.tar.xz && tar -xf data.tar.xz)
  mkdir -p "$INC/crt" && cp "$(dirname "$(find "$WORK/crt" -name host_defines.h | head -1)")"/* "$INC/crt/"
fi

# --- newest kernel activity record struct the header defines ----------------
KT=$(grep -o '} CUpti_ActivityKernel[0-9]*;' "$INC/cupti_activity.h" | sed 's/} //;s/;//' | sort -t l -k3 -n | tail -1)

$ZIG cc -target x86_64-linux-gnu.2.28 -std=gnu11 -O2 -Wall -Wextra -shared -fPIC \
  -DKERNREC_KERNEL_T="$KT" -I"$INC" -I"$CUDA_INC" \
  kernrec.c "$CUPTI_SO" -o libkernrec.so -lpthread -Wl,--strip-all

"$WORK/venv/bin/python" - <<'PY'
from elftools.elf.elffile import ELFFile
with open("libkernrec.so", "rb") as f:
    e = ELFFile(f)
    dyn = e.get_section_by_name(".dynamic")
    ds = e.get_section_by_name(".dynsym")
    needed = [t.needed for t in dyn.iter_tags() if t.entry.d_tag == "DT_NEEDED"]
    exported = [s.name for s in ds.iter_symbols()
                if s["st_info"]["bind"] == "STB_GLOBAL" and s["st_shndx"] != "SHN_UNDEF"]
    assert e["e_machine"] == "EM_X86_64", e["e_machine"]
    assert "InitializeInjection" in exported, exported
    assert any(n.startswith("libcupti.so.") for n in needed), needed
    print("libkernrec.so ok: needs", needed, "exports", exported)
PY
echo "built with $KT against $(basename "$CUPTI_WHL")"
