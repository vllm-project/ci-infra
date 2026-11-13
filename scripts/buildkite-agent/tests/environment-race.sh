#!/usr/bin/env bash
# Run the real environment hook many times at once with the real flock, which
# tests/environment.sh stubs. Only the calls that leave the host are stubbed.
# Needs util-linux flock: run it on an agent host or in a Debian/Ubuntu container.
set -uo pipefail
(( BASH_VERSINFO[0] >= 4 )) && flock --version 2>/dev/null | grep -q util-linux ||
    { echo "SKIP: needs bash 4+ and util-linux flock" >&2; exit 0; }

dir=$(cd "$(dirname "$0")/.." && pwd)
real_sleep=$(command -v sleep)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/bin"
sed -e "s#/usr/local/libexec/#$dir/#" -e "s#/tmp/docker-pull-locks#$tmp/locks#" \
    "$dir/environment" >"$tmp/environment"

stub() { printf '#!/usr/bin/env bash\n%s\n' "$2" >"$tmp/bin/$1"; chmod +x "$tmp/bin/$1"; }
stub aws 'echo password'
stub sleep 'exit 0'
stub buildkite-agent 'exit 0'
stub nvidia-smi 'for n in {1..20}; do echo "  MIG 1g.18gb Device $n: (UUID: MIG-$n)"; done'
stub nvidia-ctk 'for n in {1..20}; do echo "nvidia.com/gpu=MIG-$n"; done'
# A pull lasts PULL_SECONDS and exits PULL_STATUS. mkdir fails if a pull is
# already running, which records an overlap. DELETE_LOCKS deletes the lock
# directory during the pull, as the daily image cleanup can.
stub docker "[[ \$1 == login ]] && { cat >/dev/null; exit 0; }
echo pull >>$tmp/pulls
mkdir $tmp/pulling 2>/dev/null || echo overlap >>$tmp/overlaps
[[ -z \${DELETE_LOCKS:-} ]] || rm -rf $tmp/locks
$real_sleep \${PULL_SECONDS:-0.2}
rmdir $tmp/pulling 2>/dev/null
exit \${PULL_STATUS:-0}"

# run_jobs <how many> [VAR=val...]: leaves one exit status per job in $tmp/status.*
run_jobs() {
    local jobs=$1 n
    shift
    rm -rf "$tmp"/status.* "$tmp"/pulls "$tmp"/overlaps "$tmp"/pulling "$tmp"/locks
    for (( n = 1; n <= jobs; n++ )); do
        (
            env "$@" PATH="$tmp/bin:$PATH" DOCKER_CONFIG="$tmp/docker" BUILDKITE_AGENT_NAME="host-$n" \
                BUILDKITE_JOB_ID="job-$n" BUILDKITE_PLUGIN_DOCKER_IMAGE=test/image:1 \
                bash "$tmp/environment" >/dev/null 2>&1
            echo $? >"$tmp/status.$n"
        ) &
    done
    wait
}
count() { cat "$tmp/$1" 2>/dev/null | wc -l | tr -d ' '; }
statuses() { cat "$tmp"/status.* | sort -u | tr '\n' ' '; }
check() { [[ "$2" == "$3" ]] || { echo "FAIL $1: got '$2', want '$3'"; exit 1; }; }

# The pull works: one job pulls and every other job uses its image.
run_jobs 20
check pull-works-status "$(statuses)" '0 '
check pull-works-pulls "$(count pulls)" 1
printf 'PASS %s\n' pull-works

# The pull fails: never two pulls at once, and the waiters fail rather than
# each repeat five attempts, which would be 100 pulls.
run_jobs 20 PULL_STATUS=1
check pull-fails-status "$(statuses)" '205 '
check pull-fails-overlaps "$(count overlaps)" 0
(( $(count pulls) < 50 )) || { echo "FAIL pull-fails: $(count pulls) pulls"; exit 1; }
printf 'PASS %s\n' pull-fails

# The daily cleanup deletes the lock directory during a good pull: the job passes.
run_jobs 1 DELETE_LOCKS=1
check locks-deleted-status "$(statuses)" '0 '
[[ -n $(find "$tmp/locks" -name '*.done' 2>/dev/null) ]] || { echo "FAIL locks-deleted: no done marker"; exit 1; }
printf 'PASS %s\n' locks-deleted
