#!/bin/bash
# cleanup-dockerhub-rocm-cache.sh
#
# Removes stale commit-tagged BuildKit cache entries from Docker Hub.
#
# Background: every CI build (PR and main) writes a commit-specific tag
# (rocm-<sha>) to rocm/vllm-ci-cache via ci-rocm.hcl's get_cache_to_rocm().
# Without periodic cleanup these accumulate indefinitely at several GB each.
#
# This script should be run on a schedule (e.g., weekly via a Buildkite
# scheduled build or cron).  It keeps:
#   - rocm-latest        (the warm main-branch baseline, never deleted)
#   - any tag pushed within the last KEEP_DAYS days
#
# Required environment variables:
#   DOCKERHUB_USERNAME   - Docker Hub account with push rights to rocm/vllm-ci-cache
#   DOCKERHUB_TOKEN      - Docker Hub personal access token (read/write/delete scope)
#
# Optional:
#   KEEP_DAYS            - Age threshold in days (default: 30)
#   CACHE_REPO           - Docker Hub repo to clean (default: rocm/vllm-ci-cache)
#   DRY_RUN              - Set to "1" to list tags that would be deleted without
#                          actually deleting them (default: 0)

set -euo pipefail

KEEP_DAYS="${KEEP_DAYS:-30}"
CACHE_REPO="${CACHE_REPO:-rocm/vllm-ci-cache}"
DRY_RUN="${DRY_RUN:-0}"

DOCKERHUB_USERNAME="${DOCKERHUB_USERNAME:?DOCKERHUB_USERNAME must be set}"
DOCKERHUB_TOKEN="${DOCKERHUB_TOKEN:?DOCKERHUB_TOKEN must be set}"

NAMESPACE="${CACHE_REPO%%/*}"
REPONAME="${CACHE_REPO##*/}"

echo "=== Docker Hub ROCm cache cleanup ==="
echo "Repo:      ${CACHE_REPO}"
echo "Keep days: ${KEEP_DAYS}"
echo "Dry run:   ${DRY_RUN}"
echo ""

# ── Auth ──────────────────────────────────────────────────────────────────────

echo "--- :key: Authenticating with Docker Hub"
JWT=$(curl -sSf "https://hub.docker.com/v2/users/login" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"${DOCKERHUB_USERNAME}\",\"password\":\"${DOCKERHUB_TOKEN}\"}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
echo "Authenticated as ${DOCKERHUB_USERNAME}"

# ── List all tags with pagination ─────────────────────────────────────────────

echo "--- :mag: Listing tags in ${CACHE_REPO}"

declare -a STALE_TAGS=()
CUTOFF_EPOCH=$(date -d "-${KEEP_DAYS} days" +%s 2>/dev/null \
    || python3 -c "import time; print(int(time.time()) - ${KEEP_DAYS}*86400)")

PAGE_URL="https://hub.docker.com/v2/repositories/${NAMESPACE}/${REPONAME}/tags/?page_size=100"
TOTAL_CHECKED=0
TOTAL_KEPT=0

while [[ -n "${PAGE_URL}" && "${PAGE_URL}" != "null" ]]; do
    RESPONSE=$(curl -sSf "${PAGE_URL}" \
        -H "Authorization: Bearer ${JWT}")

    # Extract next page URL
    PAGE_URL=$(echo "${RESPONSE}" | python3 -c \
        "import sys,json; d=json.load(sys.stdin); print(d.get('next') or '')")

    # Process each tag on this page
    while IFS= read -r TAG_JSON; do
        NAME=$(echo "${TAG_JSON}" | python3 -c \
            "import sys,json; d=json.load(sys.stdin); print(d['name'])")
        LAST_UPDATED=$(echo "${TAG_JSON}" | python3 -c \
            "import sys,json; d=json.load(sys.stdin); print(d.get('tag_last_pushed') or d.get('last_updated') or '')")

        TOTAL_CHECKED=$((TOTAL_CHECKED + 1))

        # Always keep rocm-latest and any non-commit tags
        if [[ "${NAME}" == "rocm-latest" ]]; then
            echo "  KEEP  ${NAME}  (protected baseline tag)"
            TOTAL_KEPT=$((TOTAL_KEPT + 1))
            continue
        fi

        # Only touch commit-specific tags (rocm-<40-hex-char sha>)
        if ! [[ "${NAME}" =~ ^rocm-[0-9a-f]{40}$ ]]; then
            echo "  KEEP  ${NAME}  (not a commit tag)"
            TOTAL_KEPT=$((TOTAL_KEPT + 1))
            continue
        fi

        # Parse the push timestamp and compare against cutoff
        if [[ -z "${LAST_UPDATED}" ]]; then
            echo "  KEEP  ${NAME}  (no timestamp — skipping to be safe)"
            TOTAL_KEPT=$((TOTAL_KEPT + 1))
            continue
        fi

        TAG_EPOCH=$(python3 -c \
            "import datetime; s='${LAST_UPDATED}'; \
             s=s.rstrip('Z').split('.')[0]; \
             print(int(datetime.datetime.fromisoformat(s).replace(tzinfo=datetime.timezone.utc).timestamp()))" \
            2>/dev/null || echo "0")

        if [[ "${TAG_EPOCH}" -lt "${CUTOFF_EPOCH}" ]]; then
            AGE_DAYS=$(( ($(date +%s) - TAG_EPOCH) / 86400 ))
            echo "  STALE ${NAME}  (${AGE_DAYS}d old, last pushed ${LAST_UPDATED})"
            STALE_TAGS+=("${NAME}")
        else
            echo "  KEEP  ${NAME}  (recent, last pushed ${LAST_UPDATED})"
            TOTAL_KEPT=$((TOTAL_KEPT + 1))
        fi
    done < <(echo "${RESPONSE}" | python3 -c \
        "import sys,json; [print(json.dumps(t)) for t in json.load(sys.stdin).get('results',[])]")
done

echo ""
echo "Checked: ${TOTAL_CHECKED} tags — keeping ${TOTAL_KEPT}, deleting ${#STALE_TAGS[@]}"

if [[ "${#STALE_TAGS[@]}" -eq 0 ]]; then
    echo "Nothing to delete."
    exit 0
fi

# ── Delete stale tags ─────────────────────────────────────────────────────────

echo ""
if [[ "${DRY_RUN}" == "1" ]]; then
    echo "--- :no_entry: DRY RUN — the following tags would be deleted:"
    printf '  %s\n' "${STALE_TAGS[@]}"
    echo "Set DRY_RUN=0 to actually delete them."
    exit 0
fi

echo "--- :wastebasket: Deleting ${#STALE_TAGS[@]} stale tags"
DELETED=0
FAILED=0

for TAG in "${STALE_TAGS[@]}"; do
    HTTP_STATUS=$(curl -sSo /dev/null -w "%{http_code}" \
        -X DELETE \
        "https://hub.docker.com/v2/repositories/${NAMESPACE}/${REPONAME}/tags/${TAG}/" \
        -H "Authorization: Bearer ${JWT}")

    if [[ "${HTTP_STATUS}" == "204" ]]; then
        echo "  Deleted: ${TAG}"
        DELETED=$((DELETED + 1))
    else
        echo "  FAILED (HTTP ${HTTP_STATUS}): ${TAG}"
        FAILED=$((FAILED + 1))
    fi
done

echo ""
echo "=== Cleanup complete: ${DELETED} deleted, ${FAILED} failed ==="
if [[ "${FAILED}" -gt 0 ]]; then
    exit 1
fi
