#!/usr/bin/env bash
# The selector's shadow run on a PR build: what it would have run, recorded
# next to what CI really runs. Gates nothing.
#
# Writes shadow/selection.json (ci-select --emit-keys, plus how it was run) and
# shadow/selector.log as artifacts, and annotates the build. `ci-validate
# shadow` reads the artifact back against the build's job outcomes.
#
# Exit 0 whenever the selector gave an answer, including "run everything" and
# "no base to diff against": those are results. Non-zero only when something
# broke (no uv, no ci-infra, ci-select crashed), and the step is soft_fail.
#
# No -e: every gate below exits on its own, and each one annotates first.
set -uo pipefail

BRANCH="${VLLM_CI_BRANCH:-main}"
REPO_URL="https://github.com/vllm-project/ci-infra"
VLLM_URL="${SHADOW_VLLM_URL:-https://github.com/vllm-project/vllm.git}"
VLLM_CHECKOUT="${SHADOW_VLLM_REPO:-${BUILDKITE_BUILD_CHECKOUT_PATH:?}}"
# A ci-infra checkout to run the selector from, instead of cloning one.
# For rerunning by hand, and for the tests.
CI_INFRA="${SHADOW_CI_INFRA:-}"
CONTEXT=ci-selector-shadow

WORK="$(mktemp -d)"
trap 'rm -rf -- "${WORK}"' EXIT
OUT="${WORK}/shadow"
mkdir -p "${OUT}"
# The selector's worktree cache, kept off the agent's home so nothing outlives
# the job.
export CI_SELECTOR_WORKTREE_CACHE="${WORK}/worktrees"

annotate() { # style markdown
  printf '%s\n' "$2" | buildkite-agent annotate --context "${CONTEXT}" --style "$1" \
    || echo "annotate failed" >&2
}
upload() {
  (cd "${WORK}" && buildkite-agent artifact upload "shadow/*") \
    || echo "artifact upload failed" >&2
}
# selection.json for a run that produced no selection, so the report can tell
# "no answer, and why" from "never ran".
no_answer() { # status reason
  python3 - "$1" "$2" "${BASE:-}" "${HEAD:-}" > "${OUT}/selection.json" <<'PY'
import json, sys
status, reason, base, head = sys.argv[1:5]
print(json.dumps({"status": status, "reason": reason,
                  "base": base or None, "head": head or None}, indent=1))
PY
}
broke() { # reason: an infra failure, soft_fail shows it
  echo "shadow: $1" >&2
  no_answer error "$1"
  upload
  annotate warning ":crystal_ball: **CI selector (shadow)** could not run: $1"
  exit 1
}

cd "${WORK}" || exit 1

echo "--- :package: Fetching the selector"
if [[ -n "${CI_INFRA}" ]]; then
  echo "using ${CI_INFRA}"
else
  git clone --quiet --depth 1 --branch "${BRANCH}" "${REPO_URL}" ci-infra \
    || broke "cannot clone ${REPO_URL}@${BRANCH}"
  CI_INFRA="${WORK}/ci-infra"
fi
SELECTOR="${CI_INFRA}/buildkite/ci_selector"

# Through uv with Python 3.12, as the collect steps run it: the agent's
# python3 is 3.9 and has none of the selector's dependencies.
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf --retry 3 https://astral.sh/uv/install.sh \
    | env UV_INSTALL_DIR="${WORK}/bin" UV_NO_MODIFY_PATH=1 sh >/dev/null 2>&1
  export PATH="${WORK}/bin:${PATH}"
fi
command -v uv >/dev/null 2>&1 || broke "cannot install uv"
sel() { uv run --quiet --no-dev --python 3.12 --project "${SELECTOR}" "$@"; }

echo "--- :git: Finding the PR's base"
# A clone of the build's checkout rather than the checkout itself: the
# selector adds worktrees and the base lookup fetches main, and neither should
# be left behind in a checkout the next job on this agent reuses.
git clone --quiet --shared --no-checkout "${VLLM_CHECKOUT}" vllm 2>/dev/null \
  || git clone --quiet --no-checkout "file://${VLLM_CHECKOUT}" vllm \
  || broke "cannot clone the build checkout ${VLLM_CHECKOUT}"
VLLM="${WORK}/vllm"
HEAD="$(git -C "${VLLM_CHECKOUT}" rev-parse "${BUILDKITE_COMMIT:-HEAD}^{commit}" 2>/dev/null)"
[[ -n "${HEAD}" ]] || broke "cannot resolve the build commit ${BUILDKITE_COMMIT:-HEAD}"
git -C "${VLLM}" checkout --quiet --detach "${HEAD}" || broke "cannot check out ${HEAD}"
MAIN=refs/remotes/upstream/main
fetch_main() { git -C "${VLLM}" fetch --quiet --no-tags "$@" "${VLLM_URL}" "+refs/heads/main:${MAIN}"; }
fetch_main || echo "cannot fetch vLLM main from ${VLLM_URL}"
BASE="$(git -C "${VLLM}" merge-base "${HEAD}" "${MAIN}" 2>/dev/null)"
# A shallow checkout holds too little history for the two to meet. Deepen a
# bounded amount rather than unshallow a repository this size.
for depth in 200 2000; do
  [[ -n "${BASE}" ]] && break
  [[ "$(git -C "${VLLM}" rev-parse --is-shallow-repository)" == true ]] || break
  echo "shallow; deepening by ${depth}"
  git -C "${VLLM}" fetch --quiet --no-tags --deepen="${depth}" "${VLLM_URL}" \
    "+refs/heads/main:${MAIN}" >/dev/null 2>&1
  BASE="$(git -C "${VLLM}" merge-base "${HEAD}" "${MAIN}" 2>/dev/null)"
done
if [[ -z "${BASE}" ]]; then
  reason="no merge-base between ${HEAD:0:12} and vLLM main"
  echo "${reason}; nothing to select against"
  no_answer no-base "${reason}"
  upload
  annotate info ":crystal_ball: **CI selector (shadow)**: no selection, ${reason}."
  exit 0
fi
echo "base ${BASE}  head ${HEAD}"

echo "--- :card_file_box: Fetching the records"
# A missing record is a result, not a failure: the selector then runs on the
# code map alone and says so, which is what it would do in CI too.
records=()
rec_lines=()
if line="$(sel ci-fetch-function-record --out "${WORK}/rec" 2>&1)"; then
  records+=(--table "${WORK}/rec/table.json.gz")
fi
rec_lines+=("$(printf '%s' "${line}" | tail -1 | sed 's/ -> .*//')")
if line="$(sel ci-fetch-kernel-record --out "${WORK}/rec" 2>&1)"; then
  records+=(--kernel-table "${WORK}/rec/kernel_table.json.gz"
    --kernel-symbol-map "${WORK}/rec/kernel_symbol_map.json.gz")
fi
rec_lines+=("$(printf '%s' "${line}" | tail -1 | sed 's/ -> .*//')")
printf '%s\n' "${rec_lines[@]}"
# Without a table argument ci-select would fall back to its own coverage-data/,
# which is not what the fetch just said.
[[ " ${records[*]-} " == *" --table "* ]] || records+=(--table "${WORK}/rec/none.json.gz")
[[ " ${records[*]-} " == *" --kernel-table "* ]] \
  || records+=(--kernel-table "${WORK}/rec/none.json.gz" --kernel-symbol-map "${WORK}/rec/none.json.gz")

echo "--- :crystal_ball: Selecting ${BASE:0:12}..${HEAD:0:12}"
start=$(date +%s)
sel ci-select --repo "${VLLM}" --diff "${BASE}..${HEAD}" --emit-keys "${records[@]}" \
  > "${WORK}/emit.json" 2> "${OUT}/selector.log"
rc=$?
secs=$(( $(date +%s) - start ))
cat "${OUT}/selector.log"
if [[ "${rc}" != 0 ]]; then
  broke "ci-select exited ${rc} after ${secs}s (see selector.log)"
fi

# Stamp what was run onto the answer, so the artifact stands on its own.
python3 - "${WORK}/emit.json" "${BASE}" "${HEAD}" "${secs}" "${rec_lines[@]}" \
  > "${OUT}/selection.json" <<'PY' || broke "cannot read ci-select's output"
import json, sys
path, base, head, secs, *records = sys.argv[1:]
text = open(path).read().strip()
# No stdout and exit 0: ci-select found no changed files.
doc = json.loads(text) if text else {"omit": None, "reason": "no changed files"}
doc.update(status="ok" if text else "no-changes", base=base, head=head,
           seconds=int(secs), records=records)
print(json.dumps(doc, indent=1, sort_keys=True))
PY

echo "--- :arrow_up: Uploading the selection"
upload

summary="$(python3 - "${OUT}/selection.json" "${BUILDKITE_BUILD_URL:-}" <<'PY'
import json, sys
doc, url = json.load(open(sys.argv[1])), sys.argv[2]
head = ":crystal_ball: **CI selector (shadow)**"
rng = f"`{doc['base'][:10]}..{doc['head'][:10]}`"
if doc["status"] == "no-changes":
    print(f"{head}: no changed files in {rng}.")
elif doc.get("omit"):
    print(f"{head}: would run everything: {doc['reason']}.")
else:
    print(f"{head}: would run **{doc['emitted']}** step keys for {rng}.")
print()
print("Records: " + "; ".join(doc.get("records") or ["none"]))
print(f"Took {doc['seconds']}s. Gates nothing; `shadow/selection.json` is on this job.")
if doc.get("keys"):
    print()
    print(f"<details><summary>{len(doc['keys'])} step keys</summary>\n")
    print("\n".join(f"- `{k}`" for k in doc["keys"]))
    print("\n</details>")
PY
)" || summary=":crystal_ball: **CI selector (shadow)**: selection saved, summary failed."
annotate info "${summary}"
printf '%s\n' "${summary}"
