#!/usr/bin/env bash
# collect.sh under mocked externals: buildkite-agent and aws stand-ins on PATH.
# Checks the publishing rules that matter:
#
#   1. happy path: a foldable build -> table under <commit>/, latest.json written
#   2. no recordings at all         -> exit 0, nothing published, not a failure
#   3. the fold itself fails        -> exit 1, nothing published
#   4. recordings nothing can be read out of -> the delivery check refuses
#   5. no AWS identity              -> artifacts only, exit 0
#   6. a branch that is not main    -> artifacts only, exit 0, S3 untouched
#   7. fewer jobs delivered than the generator armed -> exit 1, S3 untouched
#
# Real python3 and the real fold, so what is under test is what runs in CI.
# The fold runs through uv exactly as collect.sh runs it in CI, so these cover
# how it is invoked as well as its gates.
# FNREC_CI_INFRA points the script at this checkout instead of cloning, and
# FNREC_VLLM_REPO at a throwaway repo holding the recorded file at the commit.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../../.." && pwd)
fail=0

# collect.sh runs the fold through uv, as in CI. Its PATH below gets no venv,
# so a fold that went back to the bare python3 fails here the way it would on
# an agent, where that python3 has no ci_selector and is 3.9.
command -v uv >/dev/null 2>&1 || { echo "these tests need uv on PATH" >&2; exit 1; }

s3_digest() { (cd "$1" && find . -type f | sort | xargs cksum 2>/dev/null); }

# A repo the fold can resolve the recorded commit and file in.
make_vllm_repo() { # dir [marker] -> prints the commit
  local d=$1 marker=${2:-}
  mkdir -p "$d/vllm"
  # The marker keeps two repos from producing the same commit: identical
  # content committed in the same second hashes identically.
  printf 'def plain():\n    return 1\n%s' "${marker:+# $marker\n}" > "$d/vllm/mod.py"
  git -C "$d" init --quiet
  git -C "$d" config user.email t@e && git -C "$d" config user.name t
  git -C "$d" add -A && git -C "$d" commit --quiet -m rec
  git -C "$d" rev-parse HEAD
}

# One job's recording, in the shape the recorder writes.
write_job() { # dir step_key
  local d=$1 key=$2
  mkdir -p "$d"
  printf '#start\tpid=1\troot=vllm/\tpy=3.12.13\tBUILDKITE_JOB_ID=%s\tBUILDKITE_STEP_KEY=%s\n#root\tvllm/\tt=1\nvllm/mod.py\tplain\t1\n#end\troot=1\tother=0\terrors=0\tlast_error=\tt=2\n' \
    "$(basename "$d")" "$key" > "$d/fn.a.txt"
  printf '{"event":"collected","selected":3,"deselected":0}\n{"event":"session","passed":3,"failed":0,"errors":0,"skipped":0,"selected":3,"testsfailed":0,"exitstatus":0}\n' \
    > "$d/pytest.1.jsonl"
  : > "$d/pytest.installed"
}

run_case() { # name expect_exit expect_latest expect_commit mode [s3_untouched]
  local name=$1 want_rc=$2 want_latest=$3 want_commit=$4 mode=$5 want_untouched=${6:-no}
  local T; T=$(mktemp -d)
  mkdir -p "$T/bin" "$T/s3" "$T/artifacts" "$T/build/.fnrec"
  local commit; commit=$(make_vllm_repo "$T/vllm")

  case "$mode" in
    no-recordings) ;;
    # No root in the header and no #root line: the reader returns nothing for
    # it, so the job contributes no row and the table comes out empty.
    unreadable) mkdir -p "$T/build/.fnrec/job-a"
             printf '#start\tpid=1\troot=\tpy=3.12.13\n' > "$T/build/.fnrec/job-a/fn.a.txt" ;;
    *) write_job "$T/build/.fnrec/job-a" step-x ;;
  esac

  # A repo that does not contain the recorded commit: the fold refuses rather
  # than reading every file as absent.
  local vllm="$T/vllm"
  [[ "$mode" == "fold-fails" ]] && vllm="$T/other" && make_vllm_repo "$T/other" elsewhere >/dev/null
  local branch=main
  [[ "$mode" == "fork-branch" ]] && branch="tahsintunan:some-branch"
  local expected=1
  [[ "$mode" == "below-floor" ]] && expected=10

  cat > "$T/bin/buildkite-agent" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$T/agent.log"
case "\$1 \$2" in
  # Both download calls land here. Merge rather than copy the directory, or
  # the second call nests it and invents a job that never existed.
  "artifact download") mkdir -p .fnrec && cp -R "$T/build/.fnrec/." .fnrec/ 2>/dev/null ;;
  "artifact upload") for f in \$3; do cp "\$f" "$T/artifacts/" 2>/dev/null; done ;;
esac
EOF
  cat > "$T/bin/aws" <<EOF
#!/usr/bin/env bash
if [[ "$mode" == "no-identity" && "\$1" == "sts" ]]; then exit 255; fi
case "\$1 \$2" in
  "sts get-caller-identity") echo '{"Account":"1"}' ;;
  "s3 cp")
    dst="\${4#s3://}"; mkdir -p "$T/s3/\$(dirname "\$dst")" && cp "\$3" "$T/s3/\$dst" ;;
esac
EOF
  chmod +x "$T/bin"/*
  local s3_before; s3_before=$(s3_digest "$T/s3")

  ( cd "$T" && PATH="$T/bin:$PATH" \
      BUILDKITE_COMMIT="$commit" BUILDKITE_BUILD_NUMBER=42 \
      BUILDKITE_PIPELINE_SLUG=ci CI_SELECTOR_BUCKET=bkt BUILDKITE_BRANCH="$branch" \
      FNREC_CI_INFRA="$REPO" FNREC_VLLM_REPO="$vllm" \
      FNREC_EXPECTED_JOBS="$expected" \
      bash "$HERE/collect.sh" >"$T/log" 2>&1 ); rc=$?

  local latest=no commit_up=no untouched=no
  [[ -f "$T/s3/bkt/ci/fnrec/latest.json" ]] && latest=yes
  [[ -f "$T/s3/bkt/ci/fnrec/$commit/table.json.gz" ]] && commit_up=yes
  [[ "$(s3_digest "$T/s3")" == "$s3_before" ]] && untouched=yes
  local verdict=OK
  [[ "$rc" == "$want_rc" && "$latest" == "$want_latest" && "$commit_up" == "$want_commit" ]] \
    || { verdict=FAIL; fail=1; }
  [[ "$want_untouched" == "no" || "$untouched" == "yes" ]] || { verdict=FAIL; fail=1; }
  # The artifact goes up before any gate, so a refused build is still readable.
  if [[ "$mode" == "happy" || "$mode" == "fork-branch" ]]; then
    [[ -f "$T/artifacts/table.json.gz" ]] || { verdict=FAIL; fail=1; echo "      no artifact"; }
  fi
  # Which gate refused, not just that something did. Unreadable recordings mean
  # nothing was recorded, so the delivery check catches them before a table is
  # built at all, and collect.sh's own row check never sees them.
  if [[ "$mode" == "unreadable" || "$mode" == "below-floor" ]]; then
    grep -q "REFUSING" "$T/log" \
      || { verdict=FAIL; fail=1; echo "      refused, but not by the delivery check"; }
  fi
  printf '%-4s %-14s exit=%s (want %s)  latest=%s (want %s)  commit_upload=%s (want %s)  s3_untouched=%s\n' \
    "$verdict" "$name" "$rc" "$want_rc" "$latest" "$want_latest" "$commit_up" "$want_commit" "$untouched"
  [[ "$verdict" == FAIL ]] && sed 's/^/      /' "$T/log" | tail -14
  rm -rf "$T"
}

run_case happy          0 yes yes happy
run_case no-recordings  0 no  no  no-recordings
run_case fold-fails     1 no  no  fold-fails
run_case unreadable     1 no  no  unreadable
run_case no-identity    0 no  no  no-identity
run_case fork-branch    0 no  no  fork-branch  yes
run_case below-floor    1 no  no  below-floor  yes
echo; [[ $fail == 0 ]] && echo "collect.sh publishing rules: PASS" \
  || { echo "collect.sh publishing rules: FAIL"; exit 1; }
