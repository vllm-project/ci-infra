#!/usr/bin/env bash
# Exercise the real environment hook with every external command stubbed (aws,
# docker, nvidia-smi, nvidia-ctk, flock, sleep, buildkite-agent), so it needs no
# AWS credentials, GPU or agent. Needs bash 4+ and GNU grep like the hook itself:
# run it on an agent host or in a Linux container, not under macOS bash 3.2.
set -euo pipefail
(( BASH_VERSINFO[0] >= 4 )) || { echo "SKIP: needs bash 4+" >&2; exit 0; }

dir=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp" "/tmp/docker-pull-locks/test_image_$$"*' EXIT
mkdir "$tmp/bin"
# Source the repo's CDI script instead of the installed one.
# and keep the host-wide ECR login marker and lock inside $tmp.
sed -e "s#/usr/local/libexec/#$dir/#" -e "s#/tmp/buildkite-ecr-login#$tmp/ecr-login#" \
    "$dir/environment" >"$tmp/environment"
marker=$tmp/ecr-login.success

# A stub fails its first $FAIL_<NAME> calls, then succeeds. Calls are logged to
# $tmp/<name>.log and sleeps to $tmp/sleep.log.
stub() { printf '#!/usr/bin/env bash\n%s\n' "$2" >"$tmp/bin/$1"; chmod +x "$tmp/bin/$1"; }
counted='n=$(cat "$T/$N.log" 2>/dev/null | wc -l); echo "$*" >>"$T/$N.log"; (( n >= FAIL )) || exit 1'
# "ecr" (the pull-through-cache login) counts apart from "ecr-public", so each
# login can be failed on its own; $3 is the service after --cli-connect-timeout 10.
stub aws "[[ \$3 == ecr ]] && N=awscache FAIL=\${FAIL_AWS_CACHE:-0} || N=aws FAIL=\${FAIL_AWS:-0}; T=$tmp; $counted"   # empty stdout: only pipefail sees it fail
stub nvidia-smi "N=smi FAIL=\${FAIL_SMI:-0} T=$tmp; $counted; echo '  MIG 1g.18gb Device 0: (UUID: MIG-abc)'"
# A login records its registry in the Docker config, as docker login does.
stub docker "[[ \$1 == login ]] && { cat >/dev/null; mkdir -p \$DOCKER_CONFIG; echo \"\\\"\${!#}\\\": {}\" >>\$DOCKER_CONFIG/config.json; exit 0; }; N=pull FAIL=\${FAIL_PULL:-0} T=$tmp; $counted"
stub sleep "echo \$1 >>$tmp/sleep.log"
# WAITER=1 loses the non-blocking lock; WAITER_DONE=<file> is created when the blocking wait ends.
# LOGIN_LOCK_BUSY=1 times out the bounded wait (-w) for the ECR login lock.
stub flock "[[ \$1 == -n ]] && { [[ -z \${WAITER:-} ]]; exit; }; [[ \$1 == -w ]] && { [[ -z \${LOGIN_LOCK_BUSY:-} ]]; exit; }; [[ -z \${WAITER_DONE:-} ]] || touch \$WAITER_DONE; exit 0"
# Lists our device first, then enough lines to fill the pipe, so `grep -q` exits
# early and the writer dies of SIGPIPE like the real one can.
stub nvidia-ctk "echo nvidia.com/gpu=MIG-abc; exec seq 1000000"
stub buildkite-agent "echo \"\$3=\$4\" >>$tmp/marks.log; [[ -z \${MARK_FAILS:-} ]]"

# run <name> <expected-status> <expected-mark|-> <expected-sleeps|-> [VAR=val...]
run() {
    local name=$1 want=$2 mark=$3 sleeps=$4 status=0 got_mark got_sleeps
    shift 4
    rm -f "$tmp"/*.log
    # KEEP_MARKER=1 keeps a previous job's ECR login, as on a busy host.
    [[ -n ${KEEP_MARKER:-} ]] || rm -rf "$marker" "$tmp/docker"
    rm -f "/tmp/docker-pull-locks/test_image_$$"*
    env "$@" PATH="$tmp/bin:$PATH" DOCKER_CONFIG="$tmp/docker" BUILDKITE_AGENT_NAME=host-1 BUILDKITE_JOB_ID=job-1 \
        BUILDKITE_PLUGIN_DOCKER_IMAGE="test/image:$$" \
        bash "$tmp/environment" >/dev/null 2>"$tmp/stderr" || status=$?
    [[ "$status" -eq "$want" ]] || { echo "FAIL $name: expected status $want, got $status"; cat "$tmp/stderr"; exit 1; }
    got_mark=$(cat "$tmp/marks.log" 2>/dev/null || true)
    got_sleeps=$(tr '\n' ' ' 2>/dev/null <"$tmp/sleep.log" || true)
    [[ "$mark" == - || "$got_mark" == "$mark" ]] || { echo "FAIL $name: mark was '$got_mark', want '$mark'"; exit 1; }
    [[ "$sleeps" == - || "$got_sleeps" == "$sleeps" ]] || { echo "FAIL $name: sleeps '$got_sleeps', want '$sleeps'"; exit 1; }
    printf 'PASS %s\n' "$name"
}
calls() { cat "$tmp/$1.log" 2>/dev/null | wc -l | tr -d ' '; }

run all-pass 0 '' ''
run login-blip 0 'infra-retry:job-1:ecr-public-login=ecr-public-login,3,recovered' '2 4 ' FAIL_AWS=2
grep -q 'failed (attempt 1/5); retrying in 2s' "$tmp/stderr" || { echo "FAIL log line"; exit 1; }
run login-down 202 'infra-retry:job-1:ecr-public-login=ecr-public-login,5,exhausted' '2 4 8 16 ' FAIL_AWS=99
[[ $(calls aws) -eq 5 ]] || { echo "FAIL login-down: aws called $(calls aws) times"; exit 1; }
grep -q -- '--cli-connect-timeout 10' "$tmp/aws.log" || { echo "FAIL aws timeout flag"; exit 1; }
run cache-login-blip 0 'infra-retry:job-1:ecr-cache-login=ecr-cache-login,2,recovered' '2 ' FAIL_AWS_CACHE=1
run cache-login-down 203 'infra-retry:job-1:ecr-cache-login=ecr-cache-login,5,exhausted' '2 4 8 16 ' FAIL_AWS_CACHE=99
[[ $(calls aws) -eq 1 && $(calls awscache) -eq 5 ]] || { echo "FAIL cache-login-down: public login called $(calls aws), cache login $(calls awscache) times"; exit 1; }
run nvidia-smi-blip 0 'infra-retry:job-1:gpu-discovery=gpu-discovery,2,recovered' '2 ' FAIL_SMI=1
run nvidia-smi-down 204 'infra-retry:job-1:gpu-discovery=gpu-discovery,5,exhausted' '2 4 8 16 ' FAIL_SMI=99
run pull-blip 0 'infra-retry:job-1:image-pull=image-pull,2,recovered' '2 ' FAIL_PULL=1
run pull-down 205 'infra-retry:job-1:image-pull=image-pull,5,exhausted' '2 4 8 16 ' FAIL_PULL=99
run mark-write-fails-blip 0 - '2 ' FAIL_PULL=1 MARK_FAILS=1
# Two calls that each retry in one job keep separate counts.
run two-classes 0 $'infra-retry:job-1:gpu-discovery=gpu-discovery,2,recovered\ninfra-retry:job-1:image-pull=image-pull,3,recovered' '2 2 4 ' FAIL_SMI=1 FAIL_PULL=2
run mark-write-fails-down 205 - '2 4 8 16 ' FAIL_PULL=99 MARK_FAILS=1

# Waiter: the winner's pull failed (no done marker), so the waiter fails at once
# instead of repeating every retry behind the lock, one agent after another.
run waiter-fails-fast 205 '' ''  WAITER=1
[[ $(calls pull) -eq 0 ]] || { echo "FAIL waiter-fails-fast: pull called $(calls pull) times"; exit 1; }
run waiter-sees-done 0 '' '' WAITER=1 WAITER_DONE="/tmp/docker-pull-locks/test_image_$$.lock.done"
[[ $(calls pull) -eq 0 ]] || { echo "FAIL waiter-sees-done: pulled"; exit 1; }

# One ECR login per host every 10 h: a later job reuses it, an old one is redone.
run first-job-logs-in 0 '' ''
[[ -f $marker && $(calls aws) -eq 1 && $(calls awscache) -eq 1 ]] || { echo "FAIL first-job-logs-in"; exit 1; }
KEEP_MARKER=1 run next-job-reuses-login 0 '' ''
[[ $(calls aws) -eq 0 && $(calls awscache) -eq 0 ]] || { echo "FAIL next-job-reuses-login: logged in again"; exit 1; }
touch -t 202001010000 "$marker"
KEEP_MARKER=1 run stale-login-is-redone 0 '' ''
[[ $(calls aws) -eq 1 && $(calls awscache) -eq 1 ]] || { echo "FAIL stale-login-is-redone"; exit 1; }
# Credentials gone from the Docker config (a logout, a rewritten config): log in
# again even though the marker is fresh.
KEEP_MARKER=1 run first-of-pair 0 '' ''
rm "$tmp/docker/config.json"
KEEP_MARKER=1 run lost-credentials-relogin 0 '' ''
[[ $(calls aws) -eq 1 && $(calls awscache) -eq 1 ]] || { echo "FAIL lost-credentials-relogin"; exit 1; }
sed -i '/dkr.ecr/d' "$tmp/docker/config.json"
KEEP_MARKER=1 run one-registry-missing-relogin 0 '' ''
[[ $(calls aws) -eq 1 && $(calls awscache) -eq 1 ]] || { echo "FAIL one-registry-missing-relogin"; exit 1; }
sed -i '/"public.ecr.aws"/d' "$tmp/docker/config.json"
KEEP_MARKER=1 run public-registry-missing-relogin 0 '' ''
[[ $(calls aws) -eq 1 && $(calls awscache) -eq 1 ]] || { echo "FAIL public-registry-missing-relogin"; exit 1; }
# A failed login leaves no marker, so the next job tries again.
run failed-login-not-reused 203 - - FAIL_AWS_CACHE=99
[[ ! -e $marker ]] || { echo "FAIL failed-login-not-reused: marker written"; exit 1; }
# A failed pull may mean the stored login is gone, so the next job logs in again.
run failed-pull-drops-login 205 - - FAIL_PULL=99
[[ ! -e $marker ]] || { echo "FAIL failed-pull-drops-login: marker kept"; exit 1; }
# Another job holding the login lock too long (an ECR outage it is retrying
# through) fails this job as a login failure, rather than queuing it to repeat
# the same retries.
run login-lock-timeout 202 - - LOGIN_LOCK_BUSY=1
[[ $(calls aws) -eq 0 ]] || { echo "FAIL login-lock-timeout: logged in anyway"; exit 1; }
