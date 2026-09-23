#!/usr/bin/env bash
# collect.sh under mocked externals: buildkite-agent, curl, aws, python3
# stand-ins on PATH. Checks the publishing rules that matter:
#
#   1. happy path: table + usable map -> both under <commit>/, latest.json written
#   2. table built, no map        -> artifacts only, nothing to S3, exit 1
#   3. kernel_table.py download fails -> nothing published, exit 1
#   4. table with no rows          -> artifacts only, nothing to S3, exit 1
#      (a build with no recordings at all is not a failure: exit 0, nothing to fold)
#   5. no AWS identity             -> artifacts only, exit 0
#   7. a build not on main (a fork branch under test) -> artifacts only, exit 0,
#      S3 untouched: only main's recordings describe what PRs are compared to
#   8. folding another build (KERNREC_SOURCE_BUILD_ID/_NUMBER/_JOBS): both
#      downloads name it, recordings come per job, the table carries its number
#   6. rerun of a commit already published, this time with a corrupt map
#      -> exit 1 and every byte already under S3 (pair + latest.json) untouched
#      (Codex P2 on #620: the commit prefix is written as a unit)
#
# Real python3 is used for kernel_table.py; only the network/cloud commands
# are faked. Run from anywhere; needs bash and python3.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
fail=0

s3_digest() { (cd "$1" && find . -type f | sort | xargs cksum 2>/dev/null); }

run_case() { # name expect_exit expect_latest(yes|no) expect_commit_upload(yes|no) mode [s3_untouched(yes|no)]
  local name=$1 want_rc=$2 want_latest=$3 want_commit=$4 mode=$5 want_untouched=${6:-no}
  local T; T=$(mktemp -d)
  mkdir -p "$T/bin" "$T/s3" "$T/artifacts" "$T/build/.fnrec/job-a"
  # a recording + sidecar for one non-parallel step
  printf '# kernrec v1 pid=1 ppid=0 exe=python\nkernA\n# end records=1 unique=1 dropped=0\n' > "$T/build/.fnrec/job-a/kern.1.txt"
  echo '{"step_key":"step-x","exit_status":0,"parallel_job":"","parallel_job_count":""}' > "$T/build/.fnrec/job-a/kernrec.json"
  # no-rows: a recording that cannot be filed (blank step key) -> a table with no rows
  [[ "$mode" == "no-rows" ]] && echo '{"step_key":"","exit_status":0}' > "$T/build/.fnrec/job-a/kernrec.json"
  # a usable symbol map unless the case says otherwise
  if [[ "$mode" != "no-map" ]]; then
    python3 - "$T/build/kernel_symbol_map.json.gz" <<'PY'
import gzip, json, sys
json.dump({"version": 1, "commit": "abc", "objects": [{"source": "csrc/a.cu", "target": "_C", "object": "o", "device": True, "symbols": ["kernA"], "deps": ["csrc/a.cu"]}], "stats": {}}, gzip.open(sys.argv[1], "wt"))
PY
  fi
  # rerun: an earlier build already published a valid pair for this commit and
  # latest.json points at it; this build's map is garbage
  if [[ "$mode" == "corrupt-map-rerun" ]]; then
    mkdir -p "$T/s3/bkt/ci/abc"
    cp "$T/build/kernel_symbol_map.json.gz" "$T/s3/bkt/ci/abc/"
    printf 'table from build 41' | gzip > "$T/s3/bkt/ci/abc/kernel_table.json.gz"
    echo '{"commit":"abc","build":41}' > "$T/s3/bkt/ci/latest.json"
    printf 'this is not gzip' > "$T/build/kernel_symbol_map.json.gz"
  fi
  local s3_before; s3_before=$(s3_digest "$T/s3")

  # --- stubs ---------------------------------------------------------------
  cat > "$T/bin/buildkite-agent" <<EOF
#!/usr/bin/env bash
# artifact download PATTERN DEST | artifact upload GLOB
echo "\$*" >> "$T/agent.log"
case "\$1 \$2" in
  "artifact download")
    if [[ "\$3" == *fnrec* ]]; then cp -R "$T/build/.fnrec" "\$4/" 2>/dev/null; fi
    if [[ "\$3" == *kernel_symbol_map* ]]; then cp "$T/build/kernel_symbol_map.json.gz" "\$4/" 2>/dev/null || exit 1; fi
    ;;
  "artifact upload") for f in \$3; do cp "\$f" "$T/artifacts/"; done ;;
esac
EOF
  cat > "$T/bin/curl" <<EOF
#!/usr/bin/env bash
# only the kernel_table.py fetch goes through curl; -o is the output
out=""; while [[ \$# -gt 0 ]]; do [[ "\$1" == "-o" ]] && out="\$2"; shift; done
if [[ "$mode" == "no-builder" ]]; then echo "curl: (22) 404" >&2; exit 22; fi
cp "$HERE/kernel_table.py" "\$out"
EOF
  cat > "$T/bin/aws" <<EOF
#!/usr/bin/env bash
if [[ "$mode" == "no-identity" && "\$1" == "sts" ]]; then exit 255; fi
case "\$1 \$2" in
  "sts get-caller-identity") echo '{"Account":"1"}' ;;
  "s3 cp")
    src="\$3"; dst="\$4"; dst="\${dst#s3://}"
    if [[ "\$*" == *--recursive* ]]; then mkdir -p "$T/s3/\$dst" && cp -R "\$src"/. "$T/s3/\$dst/"; else mkdir -p "$T/s3/\$(dirname "\$dst")" && cp "\$src" "$T/s3/\$dst"; fi ;;
esac
EOF
  chmod +x "$T/bin"/*

  local branch=main
  [[ "$mode" == "fork-branch" ]] && branch="khluu:some-branch"
  local extra=()
  [[ "$mode" == "other-build" ]] && extra=(KERNREC_SOURCE_BUILD_ID=src-uuid KERNREC_SOURCE_BUILD_NUMBER=41 KERNREC_SOURCE_JOBS=job-a)
  ( cd "$T" && PATH="$T/bin:$PATH" BUILDKITE_COMMIT=abc BUILDKITE_BUILD_NUMBER=42 BUILDKITE_PIPELINE_SLUG=ci CI_SELECTOR_BUCKET=bkt BUILDKITE_BRANCH="$branch" \
      env ${extra[@]+"${extra[@]}"} bash "$HERE/collect.sh" >"$T/log" 2>&1 ); rc=$?
  local latest=no commit=no untouched=no
  [[ -f "$T/s3/bkt/ci/latest.json" ]] && latest=yes
  [[ -f "$T/s3/bkt/ci/abc/kernel_table.json.gz" ]] && commit=yes
  [[ "$(s3_digest "$T/s3")" == "$s3_before" ]] && untouched=yes
  local verdict=OK
  [[ "$rc" == "$want_rc" && "$latest" == "$want_latest" && "$commit" == "$want_commit" ]] || { verdict=FAIL; fail=1; }
  [[ "$want_untouched" == "no" || "$untouched" == "yes" ]] || { verdict=FAIL; fail=1; }
  if [[ "$mode" == "other-build" ]]; then
    # both downloads went to the source build, per job, and the table is stamped with it
    [[ "$(grep -c -- '--build src-uuid' "$T/agent.log")" == 2 ]] || { verdict=FAIL; fail=1; echo "      agent calls: $(cat "$T/agent.log")"; }
    grep -q '^artifact download .fnrec/job-a/\* ' "$T/agent.log" || { verdict=FAIL; fail=1; echo "      no per-job download"; }
    python3 -c 'import gzip,json,sys; t=json.load(gzip.open(sys.argv[1],"rt")); sys.exit(0 if t["source"]["build"] == 41 else 1)' "$T/artifacts/kernel_table.json.gz" \
      || { verdict=FAIL; fail=1; echo "      table not stamped with build 41"; }
  fi
  printf '%-4s %-18s exit=%s (want %s)  latest=%s (want %s)  commit_upload=%s (want %s)  s3_untouched=%s\n' "$verdict" "$name" "$rc" "$want_rc" "$latest" "$want_latest" "$commit" "$want_commit" "$untouched"
  [[ "$verdict" == FAIL ]] && sed 's/^/      /' "$T/log" | tail -12
  rm -rf "$T"
}

run_case happy             0 yes yes normal
run_case no-map            1 no  no  no-map
run_case no-builder        1 no  no  no-builder
run_case no-rows           1 no  no  no-rows
run_case no-identity       0 no  no  no-identity
run_case corrupt-map-rerun 1 yes yes corrupt-map-rerun yes
run_case fork-branch       0 no  no  fork-branch yes
run_case other-build       0 yes yes other-build
echo; [[ $fail == 0 ]] && echo "collect.sh publishing rules: PASS" || { echo "collect.sh publishing rules: FAIL"; exit 1; }
