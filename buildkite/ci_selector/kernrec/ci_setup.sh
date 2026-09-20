#!/usr/bin/env bash
# Sourced at the start of a CI step to load the kernel-launch recorder into
# every CUDA process the step starts. Nothing here may fail the step: on any
# problem it prints why and leaves the environment untouched.
#
# The pipeline generator adds this when the build has VLLM_CI_KERNREC=1. It
# fetches the prebuilt library from the same ci-infra branch that generated
# the pipeline, so a branch under test records with its own recorder.
#
# Exports, on success:
#   CUDA_INJECTION64_PATH  the driver dlopens this at cuInit in every process
#   KERNREC_DIR            <checkout>/.fnrec/<job-id>, next to the Python
#                          recorder's files, matched by artifact_paths
#   LD_LIBRARY_PATH        gains torch's bundled CUPTI so the library resolves
#                          even in a process that initializes CUDA before
#                          torch has preloaded libcupti

kernrec_setup() {
  local branch="${VLLM_CI_BRANCH:-main}"
  local base="https://raw.githubusercontent.com/vllm-project/ci-infra/${branch}/buildkite/ci_selector/kernrec"
  local dir=/tmp/kernrec
  mkdir -p "$dir" || { echo "kernrec: cannot create $dir"; return 1; }

  if [ ! -s "$dir/libkernrec.so" ]; then
    curl -sSfL --retry 3 --max-time 60 -o "$dir/libkernrec.so" "$base/libkernrec.so" \
      || { echo "kernrec: download failed: $base/libkernrec.so"; return 1; }
  fi

  # The recording must land in the agent's checkout, where artifact_paths
  # looks for it. Tests run from /vllm-workspace, the image's own copy of the
  # repo, so the working directory is the wrong place. In the k8s stack the
  # agent runs inside the pod and BUILDKITE_BUILD_CHECKOUT_PATH is a real
  # directory; the docker plugin mounts the checkout at /workdir.
  local root="" cand
  for cand in "${BUILDKITE_BUILD_CHECKOUT_PATH:-}" /workdir; do
    if [ -n "$cand" ] && [ -d "$cand" ]; then root="$cand"; break; fi
  done
  [ -n "$root" ] || root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
  export KERNREC_DIR="$root/.fnrec/${BUILDKITE_JOB_ID:-local}"

  # Under the docker plugin this shell is root and the checkout is a bind
  # mount owned by the agent user. Anything root creates with default modes
  # cannot be removed by the agent's `git clean` and breaks every later job
  # on that machine. Create the directories world-writable up front, and
  # open up whatever the recorder wrote when the step ends, even on failure.
  mkdir -p "$KERNREC_DIR" && chmod 0777 "$root/.fnrec" "$KERNREC_DIR"
  # shellcheck disable=SC2064
  trap "chmod -R a+rwX '$root/.fnrec' 2>/dev/null || true" EXIT

  # libkernrec needs libcupti.so.<major>. torch ships it in a pip wheel that is
  # not on the loader path until torch itself has loaded it.
  local cupti
  cupti=$(python3 - <<'PY' 2>/dev/null
import glob, os, sysconfig
root = os.path.join(sysconfig.get_paths()["purelib"], "nvidia")
hits = sorted(glob.glob(os.path.join(root, "**", "libcupti.so.1[0-9]"), recursive=True))
print(os.path.dirname(hits[0]) if hits else "")
PY
)
  if [ -n "$cupti" ]; then
    export LD_LIBRARY_PATH="$cupti${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  fi

  export CUDA_INJECTION64_PATH="$dir/libkernrec.so"
  echo "kernrec: on  dir=$KERNREC_DIR  cupti=${cupti:-<not found; relying on torch preload>}"
}

kernrec_setup || echo "kernrec: setup failed; this step runs without the recorder"
