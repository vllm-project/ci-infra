#!/usr/bin/env bash
# Sourced at the start of a CI step to load the Python function recorder into
# every Python process the step starts. Nothing here may fail the step: on any
# problem it prints why and leaves the environment untouched.
#
# The pipeline generator adds this when the build has VLLM_CI_FNREC=1. It
# fetches fnrec.py from the same ci-infra branch that generated the pipeline,
# installs it into the interpreter's site-packages next to a one-line .pth
# (`import fnrec`), so every interpreter that starts, pytest, engine cores,
# Ray workers, records itself, and exports:
#
#   FNREC_DIR   <checkout>/.fnrec/<job-id>, the same directory the kernel
#               recorder writes to, matched by artifact_paths .fnrec/**/*
#
# and installs fnrec_pytest.py as a pytest plugin, which writes each pytest
# run's collected count and summary line there too (pytest.<pid>.txt).
#
# The container is ephemeral, so the .pth does not outlive the step.

fnrec_setup() {
  local branch="${VLLM_CI_BRANCH:-main}"
  local base="https://raw.githubusercontent.com/vllm-project/ci-infra/${branch}/buildkite/ci_selector/fnrec"

  # Where the agent's artifact upload looks: the checkout, not the image's
  # copy of the repo the tests run from (see kernrec/ci_setup.sh).
  local root="" cand
  for cand in "${BUILDKITE_BUILD_CHECKOUT_PATH:-}" /workdir; do
    if [ -n "$cand" ] && [ -d "$cand" ]; then root="$cand"; break; fi
  done
  [ -n "$root" ] || root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
  local dir="$root/.fnrec/${BUILDKITE_JOB_ID:-local}"
  # World-writable: root writes here under the docker plugin and the agent
  # user must be able to `git clean` it afterwards.
  mkdir -p "$dir" && chmod 0777 "$root/.fnrec" "$dir" || { echo "fnrec: cannot create $dir"; return 1; }

  local site
  site=$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' 2>/dev/null)
  [ -n "$site" ] && [ -d "$site" ] || { echo "fnrec: no site-packages for python3"; return 1; }
  [ -w "$site" ] || { echo "fnrec: $site is not writable"; return 1; }

  curl -sSfL --retry 3 --max-time 60 -o "$site/fnrec.py" "$base/fnrec.py" \
    || { echo "fnrec: download failed: $base/fnrec.py"; return 1; }
  python3 -m py_compile "$site/fnrec.py" 2>/dev/null || { echo "fnrec: fnrec.py does not compile"; rm -f "$site/fnrec.py"; return 1; }
  printf 'import fnrec\n' > "$site/fnrec.pth"

  # Test outcomes without the job log: a pytest11 entry point, so every pytest
  # this interpreter starts writes its collected count and summary line next
  # to the recordings (fnrec_pytest.py says why). The marker tells the collect
  # step the plugin was in place, so a job with no pytest.*.txt ran no pytest
  # rather than lost its counts. Optional: the recorder works without it.
  if curl -sSfL --retry 3 --max-time 60 -o "$site/fnrec_pytest.py" "$base/fnrec_pytest.py" \
     && python3 -m py_compile "$site/fnrec_pytest.py" 2>/dev/null; then
    local di="$site/fnrec_pytest-0.dist-info"
    mkdir -p "$di" \
      && printf 'Metadata-Version: 2.1\nName: fnrec-pytest\nVersion: 0\n' > "$di/METADATA" \
      && printf '[pytest11]\nfnrec = fnrec_pytest\n' > "$di/entry_points.txt" \
      && : > "$dir/pytest.installed" && chmod 0666 "$dir/pytest.installed"
  else
    rm -f "$site/fnrec_pytest.py"
    echo "fnrec: no pytest plugin; the table will need job logs for test counts"
  fi

  export FNREC_DIR="$dir"
  echo "fnrec: on  dir=$FNREC_DIR  site=$site  python=$(python3 -c 'import platform; print(platform.python_version())')"
}

fnrec_setup || echo "fnrec: setup failed; this step runs without the Python recorder"
