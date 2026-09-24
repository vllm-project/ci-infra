#!/usr/bin/env bash
# Regression test for the root-owned-leftover bug (Buildkite build 89825).
#
# Under the docker plugin a CI step runs as root inside the container while
# the checkout it writes into is a bind mount owned by the agent user. If the
# recorder leaves a root:root 0755 directory there, the agent's `git clean`
# on the next job fails and every later job on that machine breaks.
#
# This reproduces those conditions in a throwaway Ubuntu container: an
# agent-owned git checkout, a root shell sourcing ci_setup.sh and writing
# recordings, then the agent user running `git clean -ffdxq`. Three cases:
#
#   new normal   ci_setup.sh from this directory, step exits normally
#   new kill     same, but the step's shell is SIGKILLed so the EXIT trap
#                never runs (timeouts, OOM kills)
#   old normal   the pre-fix script, expected to FAIL: proves the test bites
#
# Needs docker and network. Pass a git ref as $1 to use its ci_setup.sh as
# the "old" script (default: the last commit before the fix).
set -euo pipefail
cd "$(dirname "$0")"
OLD_REF="${1:-fed98fb~1}"
# The setup script fetches libkernrec.so from this ci-infra branch on GitHub;
# it must be pushed. Default: the branch you are on.
KERNREC_TEST_BRANCH="${KERNREC_TEST_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
export KERNREC_TEST_BRANCH
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

cp ci_setup.sh "$WORK/ci_setup_new.sh"

git show "$OLD_REF:buildkite/ci_selector/recorders/kernrec/ci_setup.sh" \
  > "$WORK/ci_setup_old.sh" 2>/dev/null \
  || git show "$OLD_REF:buildkite/ci_selector/kernrec/ci_setup.sh" > "$WORK/ci_setup_old.sh"

cat > "$WORK/inner.sh" <<'EOF'
. /ci_setup.sh
# the recorder's footprint: its directory (default umask, as the old one did) and files in it
mkdir -p "$KERNREC_DIR"
echo kernel_a > "$KERNREC_DIR/kern.100.txt"; echo kernel_b > "$KERNREC_DIR/kern.101.txt"
[ "${SLEEP_THEN_DIE:-}" = 1 ] && sleep 60
exit 0
EOF

cat > "$WORK/case.sh" <<'EOF'
# inside the container, as root: $1 = old|new, $2 = normal|kill
set -u
apt-get -qq update >/dev/null 2>&1 && apt-get -qq install -y curl ca-certificates git >/dev/null 2>&1
useradd -u 1000 -m agent
mkdir /co && chown agent:agent /co
su agent -s /bin/sh -c 'cd /co && git init -q . && git -c user.email=a@b -c user.name=a commit -q --allow-empty -m init'
cp "/scripts/ci_setup_$1.sh" /ci_setup.sh
cd /co
export BUILDKITE_BUILD_CHECKOUT_PATH=/co BUILDKITE_JOB_ID=job1 VLLM_CI_BRANCH="${KERNREC_TEST_BRANCH:-main}"
if [ "$2" = kill ]; then
  SLEEP_THEN_DIE=1 sh /scripts/inner.sh & pid=$!; sleep 8; kill -9 $pid; wait $pid 2>/dev/null || true
else
  sh /scripts/inner.sh
fi
find /co/.kernrec -exec stat -c "   %A %u:%g %n" {} \;
su agent -s /bin/sh -c 'cd /co && git clean -ffdxq' 2>&1 | sed 's/^/   /' | head -4
[ -e /co/.kernrec ] && echo "RESULT: FAILED (leftover the agent cannot remove)" || echo "RESULT: OK"
EOF

fail=0
run() { # label script mode expect
  echo "=== $1 (expect $4)"
  out=$(docker run --rm -v "$WORK":/scripts:ro -e KERNREC_TEST_BRANCH="${KERNREC_TEST_BRANCH:-main}" ubuntu:22.04 sh /scripts/case.sh "$2" "$3" 2>&1 | grep -v '^kernrec: on')
  echo "$out"
  echo "$out" | grep -q "RESULT: $4" || { echo "!!! unexpected result"; fail=1; }
}
run "new script, normal exit" new normal OK
run "new script, step shell SIGKILLed" new kill OK
run "old script, normal exit" old normal FAILED
echo; [ $fail = 0 ] && echo "checkout permission test: PASS" || { echo "checkout permission test: FAIL"; exit 1; }
