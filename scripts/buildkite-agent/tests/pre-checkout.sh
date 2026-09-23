#!/usr/bin/env bash
# Exercise the real hook without accessing the agent secret store.
set -euo pipefail
hook=$(cd "$(dirname "$0")/.." && pwd)/pre-checkout
sha=002c42b2b4358cc689ec19f2e591a38de6694718

function /usr/bin/buildkite-agent() {
    case "$1 $2" in
        'secret get') printf 'test-only-placeholder' ;;
        'redactor add') cat >/dev/null ;;
        *) return 1 ;;
    esac
}
# The deployed hook uses GNU base64; also run these tests on macOS.
function base64() { command base64 | tr -d '\n'; }

check() (
    name=$1 branch=$2 commit=$3 pr=$4 refspec=$5 expected=$6
    export BUILDKITE_PIPELINE_ID=018cdabc-d930-49f6-9085-634c4cb582ed
    export BUILDKITE_REPO=https://github.com/vllm-project/vllm.git
    export BUILDKITE_BRANCH="$branch" BUILDKITE_COMMIT="$commit"
    unset BUILDKITE_PULL_REQUEST BUILDKITE_REFSPEC GIT_CONFIG_COUNT
    [[ "$pr" == unset ]] || export BUILDKITE_PULL_REQUEST="$pr"
    [[ "$refspec" == unset ]] || export BUILDKITE_REFSPEC="$refspec"
    source "$hook"
    [[ "${BUILDKITE_REFSPEC:-}" == "$expected" ]]
    [[ "$BUILDKITE_BRANCH" == "$branch" ]]
    [[ "$BUILDKITE_COMMIT" == "$commit" ]]
    [[ "$GIT_CONFIG_COUNT" == 1 ]]
    printf 'PASS %s\n' "$name"
)

check manual-fork owner:feature "$sha" false unset "$sha"
check missing-pr owner:feature "$sha" unset unset "$sha"
check empty-pr owner:feature "$sha" '' unset "$sha"
check webhook-pr owner:feature "$sha" 123 unset ''
check custom-refspec owner:feature "$sha" false refs/custom refs/custom
check upstream-branch feature "$sha" false unset ''
check pr-ref refs/pull/123/head "$sha" false unset ''
check head-commit owner:feature HEAD false unset ''
check short-sha owner:feature 002c42b false unset ''
check missing-commit owner:feature '' false unset ''
check invalid-sha owner:feature zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz false unset ''

# When the agent secret store is unreachable, the hook must exit 255 so the
# generated steps' automatic exit_status 255 retry fires instead of failing
# the build.
export BUILDKITE_SECRET_FETCH_DELAY=0
(
    function /usr/bin/buildkite-agent() { return 1; }
    export BUILDKITE_PIPELINE_ID=018cdabc-d930-49f6-9085-634c4cb582ed
    export BUILDKITE_REPO=https://github.com/vllm-project/vllm.git
    export BUILDKITE_BRANCH=feature BUILDKITE_COMMIT="$sha"
    unset BUILDKITE_PULL_REQUEST BUILDKITE_REFSPEC GIT_CONFIG_COUNT
    source "$hook" 2>/dev/null
) || status=$?
[[ "${status:-0}" -eq 255 ]] || { echo "FAIL secret-store-down: expected 255, got ${status:-0}"; exit 1; }
printf 'PASS %s\n' secret-store-down

# A transient secret-store failure (e.g. a host DNS blip) is retried in-hook.
(
    failures=$(mktemp)
    trap 'rm -f "$failures"' EXIT
    echo 2 >"$failures"
    function /usr/bin/buildkite-agent() {
        case "$1 $2" in
            'secret get')
                local remaining
                remaining=$(<"$failures")
                if ((remaining > 0)); then
                    echo $((remaining - 1)) >"$failures"
                    return 1
                fi
                printf 'test-only-placeholder'
                ;;
            'redactor add') cat >/dev/null ;;
            *) return 1 ;;
        esac
    }
    export BUILDKITE_PIPELINE_ID=018cdabc-d930-49f6-9085-634c4cb582ed
    export BUILDKITE_REPO=https://github.com/vllm-project/vllm.git
    export BUILDKITE_BRANCH=feature BUILDKITE_COMMIT="$sha"
    unset BUILDKITE_PULL_REQUEST BUILDKITE_REFSPEC GIT_CONFIG_COUNT
    source "$hook" 2>/dev/null
    [[ "$GIT_CONFIG_COUNT" == 1 && "$(<"$failures")" == 0 ]]
) || { echo "FAIL secret-store-transient"; exit 1; }
printf 'PASS %s\n' secret-store-transient
