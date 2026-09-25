#!/usr/bin/env bash
# run.sh under mocked externals: buildkite-agent and uv stand-ins on PATH, a
# local repo standing in for vLLM on GitHub, and a stubbed ci-select.
#
#   1. happy path       -> selection.json artifact, info annotation, exit 0,
#                          ci-select asked for merge-base..head with the records
#   2. selector omits   -> "would run everything: <reason>", exit 0
#   3. no records       -> still selects, on explicit empty paths, exit 0
#   4. no merge-base    -> status no-base artifact, annotation, exit 0
#   5. shallow checkout -> deepens until the base is found
#   6. ci-select crash  -> exit 1, warning annotation, artifact says error
#   7. no changed files -> status no-changes, exit 0
#
# Real git and python3; the selector itself is stubbed, since what is under
# test is how run.sh finds its range, runs it and reports it.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
fail=0

commit() { # repo message
  echo "$2" >> "$1/f.txt"
  git -C "$1" add -A && git -C "$1" commit --quiet -m "$2"
}

# upstream: main with m1..m4; branch pr off m2 with p1. Prints nothing.
make_upstream() { # dir
  local d=$1
  git init --quiet -b main "$d"
  git -C "$d" config user.email t@e && git -C "$d" config user.name t
  commit "$d" m1; commit "$d" m2
  git -C "$d" checkout --quiet -b pr
  commit "$d" p1
  git -C "$d" checkout --quiet main
  commit "$d" m3; commit "$d" m4
}

run_case() { # name want_rc want_status want_annotation_regex mode
  local name=$1 want_rc=$2 want_status=$3 want_note=$4 mode=$5
  local T; T=$(mktemp -d)
  mkdir -p "$T/bin" "$T/artifacts" "$T/ci-infra/buildkite/ci_selector"
  make_upstream "$T/upstream" >/dev/null
  local upstream_url="file://$T/upstream"
  case "$mode" in
    no-base)
      # A checkout sharing no history with main.
      git init --quiet -b pr "$T/checkout"
      git -C "$T/checkout" config user.email t@e && git -C "$T/checkout" config user.name t
      commit "$T/checkout" orphan >/dev/null ;;
    shallow) git clone --quiet --depth 1 --branch pr "$upstream_url" "$T/checkout" ;;
    *) git clone --quiet --branch pr "$upstream_url" "$T/checkout" ;;
  esac
  local head; head=$(git -C "$T/checkout" rev-parse HEAD)
  local base; base=$(git -C "$T/upstream" rev-parse main~2)

  cat > "$T/bin/buildkite-agent" <<EOF
#!/usr/bin/env bash
case "\$1 \$2" in
  "artifact upload") for f in \$3; do cp "\$f" "$T/artifacts/"; done ;;
  "annotate ") ;;
esac
if [[ "\$1" == annotate ]]; then
  printf '%s\n' "\$*" > "$T/annotate.args"; cat > "$T/annotation.md"
fi
EOF
  # uv run ... --project DIR <command> args: dispatch on the command.
  cat > "$T/bin/uv" <<EOF
#!/usr/bin/env bash
while [[ \$# -gt 0 && "\$1" != --project ]]; do shift; done
shift 2
cmd=\$1; shift
case "\$cmd" in
  ci-fetch-function-record|ci-fetch-kernel-record)
    [[ "$mode" == no-records ]] && { echo "error: nothing published yet" >&2; exit 1; }
    out=\$2; mkdir -p "\$out"
    if [[ \$cmd == ci-fetch-function-record ]]; then
      : > "\$out/table.json.gz"; echo "function record for e33de821c0 (build 91049): 390 step rows -> \$out"
    else
      : > "\$out/kernel_table.json.gz"; : > "\$out/kernel_symbol_map.json.gz"
      echo "kernel record for e33de821c0 (build 91049): 227 step rows, 566 objects -> \$out"
    fi ;;
  ci-select)
    printf '%s\n' "\$*" > "$T/select.args"
    echo "preflight: something" >&2
    case "$mode" in
      crash) echo "Traceback: boom" >&2; exit 1 ;;
      no-changes) echo "no changed files" >&2 ;;
      omit) echo '{"emitted":0,"env":{},"keep":0,"keys":[],"omit":true,"reason":"run-all: setup.py changed","unnameable":[]}' ;;
      *) echo '{"emitted":2,"env":{"VLLM_CI_ONLY_STEP_KEYS":"[\"a\",\"b\"]"},"kept":2,"keys":["a","b"],"omit":false,"reason":"","unnameable":[]}' ;;
    esac ;;
  *) echo "unexpected uv command \$cmd" >&2; exit 9 ;;
esac
EOF
  chmod +x "$T/bin"/*

  ( cd "$T" && PATH="$T/bin:$PATH" BUILDKITE_BUILD_CHECKOUT_PATH="$T/checkout" \
      BUILDKITE_COMMIT="$head" SHADOW_CI_INFRA="$T/ci-infra" SHADOW_VLLM_URL="$upstream_url" \
      bash "$HERE/run.sh" >"$T/log" 2>&1 ); local rc=$?

  local verdict=OK status=missing
  [[ -f "$T/artifacts/selection.json" ]] \
    && status=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["status"])' "$T/artifacts/selection.json")
  [[ "$rc" == "$want_rc" && "$status" == "$want_status" ]] || { verdict=FAIL; fail=1; }
  grep -Eq "$want_note" "$T/annotation.md" 2>/dev/null \
    || { verdict=FAIL; fail=1; echo "      annotation does not match /$want_note/"; }
  grep -q -- "--context ci-selector-shadow" "$T/annotate.args" 2>/dev/null \
    || { verdict=FAIL; fail=1; echo "      annotation not under the ci-selector-shadow context"; }
  if [[ "$mode" == happy || "$mode" == shallow ]]; then
    grep -q -- "--diff $base..$head" "$T/select.args" \
      || { verdict=FAIL; fail=1; echo "      ci-select not asked for $base..$head: $(cat "$T/select.args" 2>/dev/null)"; }
  fi
  if [[ "$mode" == happy ]]; then
    grep -q -- "--table .*/rec/table.json.gz --kernel-table .*/rec/kernel_table.json.gz" "$T/select.args" \
      || { verdict=FAIL; fail=1; echo "      records not passed"; }
    grep -q "e33de821c0" "$T/annotation.md" || { verdict=FAIL; fail=1; echo "      record commit not annotated"; }
    [[ -f "$T/artifacts/selector.log" ]] || { verdict=FAIL; fail=1; echo "      no selector.log artifact"; }
  fi
  if [[ "$mode" == shallow ]]; then
    grep -q "shallow; deepening" "$T/log" || { verdict=FAIL; fail=1; echo "      never deepened"; }
  fi
  if [[ "$mode" == no-records ]]; then
    grep -q -- "--table .*/rec/none.json.gz" "$T/select.args" \
      || { verdict=FAIL; fail=1; echo "      fell back to the default table"; }
  fi
  # The build's checkout is left as it was: no fetched main, no worktrees.
  if git -C "$T/checkout" rev-parse --verify -q refs/remotes/upstream/main >/dev/null \
    || [[ "$(git -C "$T/checkout" worktree list | wc -l | tr -d ' ')" != 1 ]]; then
    verdict=FAIL; fail=1; echo "      the build checkout was modified"
  fi
  printf '%-4s %-12s exit=%s (want %s)  status=%s (want %s)\n' \
    "$verdict" "$name" "$rc" "$want_rc" "$status" "$want_status"
  [[ "$verdict" == FAIL ]] && sed 's/^/      /' "$T/log" | tail -20
  rm -rf "$T"
}

run_case happy       0 ok         'would run \*\*2\*\* step keys'   happy
run_case omit        0 ok         'would run everything: run-all: setup.py changed' omit
run_case no-records  0 ok         'Records: error: nothing published yet' no-records
run_case no-base     0 no-base    'no selection, no merge-base'      no-base
run_case shallow     0 ok         'would run \*\*2\*\* step keys'   shallow
run_case crash       1 error      'could not run: ci-select exited 1' crash
run_case no-changes  0 no-changes 'no changed files'                  no-changes
echo; [[ $fail == 0 ]] && echo "shadow run.sh: PASS" || { echo "shadow run.sh: FAIL"; exit 1; }
