#!/usr/bin/env bash
# Pack this job's recording into one tarball, in place. $1 is the step's exit
# status so far, written first to fnrec.json so the collect step can file the
# job and judge its row without the Buildkite API. Uploads nothing:
# the step's artifact_paths does that, and a glob also covers the raw files,
# so failing here only costs the packing. Never fails the step, and always
# says what it did -- a silent exit 0 looks exactly like success.
set -u

_fnrec_say() { echo "fnrec: $*" || :; }

if [ -z "${FNREC_OUT:-}" ]; then
    _fnrec_say "FNREC_OUT is unset; setup never ran. Nothing packed."
    exit 0
fi

if [ ! -d "${FNREC_OUT}" ]; then
    _fnrec_say "no recording directory at ${FNREC_OUT}. Nothing packed."
    exit 0
fi

# What the collect step needs to file this job under its step, from the job's
# own environment. A job that never gets here (killed, or a failed command
# under set -e) has no fnrec.json, and the collect step reads it as not passed.
_fnrec_json() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
printf '{"step_key":"%s","label":"%s","job_id":"%s","build":"%s","commit":"%s","parallel_job":"%s","parallel_job_count":"%s","retry_count":"%s","exit_status":%s}\n' \
    "$(_fnrec_json "${BUILDKITE_STEP_KEY:-}")" "$(_fnrec_json "${BUILDKITE_LABEL:-}")" \
    "${BUILDKITE_JOB_ID:-}" "${BUILDKITE_BUILD_NUMBER:-}" "${BUILDKITE_COMMIT:-}" \
    "${BUILDKITE_PARALLEL_JOB:-}" "${BUILDKITE_PARALLEL_JOB_COUNT:-}" "${BUILDKITE_RETRY_COUNT:-}" \
    "$(printf '%s' "${1:-null}" | tr -cd '0-9' | grep . || echo null)" \
    > "${FNREC_OUT}/fnrec.json" 2>/dev/null || _fnrec_say "could not write fnrec.json"
chmod 0666 "${FNREC_OUT}/fnrec.json" 2>/dev/null || :

BASE="$(dirname "${FNREC_OUT}")"
JOB="$(basename "${FNREC_OUT}")"
COUNT="$(find "${FNREC_OUT}" -type f 2>/dev/null | wc -l | tr -d ' ')"

# Build under a name no artifact glob matches, then rename. A step killed
# mid-tar would otherwise publish a truncated tarball that reads as a complete
# but nearly empty recording: a wrong answer rather than a missing one.
if ! tar -czf "${BASE}/${JOB}.tar.gz.part" -C "${BASE}" "${JOB}"; then
    _fnrec_say "tar failed for ${FNREC_OUT} (${COUNT} files); leaving the raw files for artifact_paths."
    rm -f "${BASE}/${JOB}.tar.gz.part" || :
    exit 0
fi

if ! mv "${BASE}/${JOB}.tar.gz.part" "${BASE}/${JOB}.tar.gz"; then
    _fnrec_say "could not move the tarball into place; leaving the raw files for artifact_paths."
    rm -f "${BASE}/${JOB}.tar.gz.part" || :
    exit 0
fi

# The agent user must be able to delete this during the next job's git clean.
chmod 0666 "${BASE}/${JOB}.tar.gz" || :

if rm -rf "${FNREC_OUT}"; then
    _fnrec_say "packed ${COUNT} files into ${BASE}/${JOB}.tar.gz"
else
    _fnrec_say "packed ${COUNT} files into ${BASE}/${JOB}.tar.gz but could not remove ${FNREC_OUT}; both will upload."
fi
exit 0
