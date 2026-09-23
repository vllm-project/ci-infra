#!/usr/bin/env bash
# Sourced at the start of a CI step to load the Python function recorder into
# every Python process the step starts. Never fails the step: on any problem it
# says why and leaves the environment alone.
#
# The generator adds this when the build has VLLM_CI_FNREC=1, and exports two
# things first, because neither can be worked out from inside the step:
#
#   FNREC_CHECKOUT  where this step can see the Buildkite checkout. Never
#                   guessed here: the docker plugin does not forward
#                   BUILDKITE_BUILD_CHECKOUT_PATH, and guessing from the device
#                   sends AMD mirrors to a path their pods do not have.
#   FNREC_MODE      "container" or "host". Only the generator knows which.
#
# Exports on success:
#   FNREC_OUT       <checkout>/.fnrec/<job-id>. The kernel recorder has its own
#                   .kernrec/, so neither can reach the other's files.
#   FNREC_ROOT      where vllm's code is, printed by the installer. The recorder
#                   arms only when both are set, so a failed install records
#                   nothing rather than recording against a wrong root.
#   PYTHONPATH      host mode only, gaining FNREC_LIB after the install
fnrec_setup() {
  local branch="${VLLM_CI_BRANCH:-main}"
  local base="https://raw.githubusercontent.com/vllm-project/ci-infra/${branch}/buildkite/ci_selector/recorders/fnrec"
  local dir="/tmp/fnrec.${BUILDKITE_JOB_ID:-local}"
  local mode="${FNREC_MODE:-container}"

  local checkout="${FNREC_CHECKOUT:-}"
  [ -n "$checkout" ] || { echo "fnrec: FNREC_CHECKOUT not set"; return 1; }
  # Fails silently otherwise. Legacy AMD dind mounts no checkout at all.
  [ -d "$checkout" ] || echo "fnrec: no checkout at $checkout; this job will record but nothing will be uploaded" >&2

  export FNREC_BASE="$checkout/.fnrec"
  # Job-scoped: the checkout is reused across jobs and the recorder names
  # files by pid, so a shared directory would merge them into one record.
  export FNREC_OUT="$FNREC_BASE/${BUILDKITE_JOB_ID:-no-job-id}"

  # A previous job may have left one. Buildkite's git clean already removes
  # it, but that is its default rather than ours to depend on.
  rm -rf "$FNREC_BASE"
  mkdir -p "$FNREC_OUT" || { echo "fnrec: cannot create $FNREC_OUT" >&2; return 1; }
  # In a container root writes into an agent-owned checkout, so these land
  # root-owned. Deleting a file needs write on its directory, not the file, so
  # 0777 is what lets the next job's git clean succeed. Getting this wrong
  # wedges the agent, which is worse than losing a recording.
  chmod 0777 "$FNREC_BASE" "$FNREC_OUT" 2>/dev/null || :

  mkdir -p "$dir" || { echo "fnrec: cannot create $dir"; return 1; }
  local installer="install.py"
  if [ "$mode" = host ]; then
    installer="host_install.py"
    export FNREC_LIB="$FNREC_BASE/lib"
  fi
  local f
  for f in fnrec.py "$installer" pack.sh; do
    curl -sSfL --retry 3 --max-time 60 -o "$dir/$f" "$base/$f" \
      || { echo "fnrec: download failed: $base/$f"; return 1; }
  done
  # A truncated download would otherwise surface much later, as a job that
  # recorded nothing.
  python3 -m py_compile "$dir/fnrec.py" "$dir/$installer" 2>/dev/null \
    || { echo "fnrec: fetched payload does not compile"; return 1; }
  chmod +x "$dir/pack.sh"

  # The installer prints where vllm's code is. Its stderr is the only thing
  # separating a failed install from a job that ran no vLLM code, since both
  # leave an empty recording. Keep `export`: it hides the exit status, so a
  # crash here cannot fail the step.
  export FNREC_ROOT=$(python3 "$dir/$installer" 2>"$FNREC_OUT/install.err")

  # After the install, so a failure cannot leave PYTHONPATH pointing at a
  # directory with no recorder in it.
  if [ "$mode" = host ]; then
    export PYTHONPATH="$FNREC_LIB${PYTHONPATH:+:$PYTHONPATH}"
  fi

  echo "fnrec: on  mode=$mode  out=$FNREC_OUT  root=${FNREC_ROOT:-<install failed; see install.err>}"
}

fnrec_setup || echo "fnrec: setup failed; this step runs without the Python recorder"
