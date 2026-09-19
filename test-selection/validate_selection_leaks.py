#!/usr/bin/env python3
"""Validate the canonical vLLM CI test-selection leak corpus."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "selection-leaks.json"
SCHEMA_PATH = ROOT / "selection-leaks.schema.json"

JOB_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]+$")
PR_URL_RE = re.compile(r"^https://github\.com/vllm-project/vllm/pull/([1-9][0-9]*)$")
BUILDKITE_URL_RE = re.compile(
    r"^https://buildkite\.com/vllm/ci/builds/([1-9][0-9]*)(?:#([0-9a-f-]+))?$"
)
PR_STATES = {"blocked", "not_selected", "skipped", "canceled"}
CONFIRMATION_KINDS = {"fix", "revert", "reproduction", "code_analysis"}


class ValidationError(Exception):
    """A user-correctable corpus validation failure."""


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{path}: {exc}") from exc


def require_keys(value: Any, expected: set[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{context}: expected an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValidationError(
            f"{context}: key mismatch (missing={missing}, extra={extra})"
        )
    return value


def positive_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"{context}: expected a positive integer")
    return value


def nonempty(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{context}: expected a non-empty string")
    return value


def validate_buildkite_job(value: Any, context: str) -> tuple[int, str]:
    obj = require_keys(value, {"build_number", "job_id", "url"}, context)
    build = positive_int(obj["build_number"], f"{context}.build_number")
    job_id = nonempty(obj["job_id"], f"{context}.job_id")
    if not JOB_ID_RE.fullmatch(job_id):
        raise ValidationError(f"{context}.job_id: invalid Buildkite job UUID")
    match = BUILDKITE_URL_RE.fullmatch(nonempty(obj["url"], f"{context}.url"))
    if match is None or int(match.group(1)) != build or match.group(2) != job_id:
        raise ValidationError(f"{context}.url: must point to its exact build and job")
    return build, job_id


def validate_pr_ci(value: Any, context: str) -> None:
    obj = require_keys(
        value, {"ran", "state", "build_number", "job_id", "url"}, context
    )
    if obj["ran"] is not False:
        raise ValidationError(f"{context}.ran: positive leaks require false")
    if obj["state"] not in PR_STATES:
        raise ValidationError(f"{context}.state: unsupported non-running state")

    build = positive_int(obj["build_number"], f"{context}.build_number")
    match = BUILDKITE_URL_RE.fullmatch(nonempty(obj["url"], f"{context}.url"))
    if match is None or int(match.group(1)) != build:
        raise ValidationError(f"{context}.url: must point to the PR build")

    job_id = obj["job_id"]
    if obj["state"] == "not_selected":
        if job_id is not None or match.group(2) is not None:
            raise ValidationError(
                f"{context}: not_selected requires a null job_id and build-level URL"
            )
        return
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise ValidationError(f"{context}.job_id: invalid Buildkite job UUID")
    if match.group(2) != job_id:
        raise ValidationError(f"{context}.url: must point to its exact job")


def validate_record(
    record: Any, index: int
) -> tuple[str, tuple[int, str], tuple[int, str, int]]:
    context = f"records[{index}]"
    obj = require_keys(
        record,
        {
            "id",
            "job_name",
            "job_key",
            "culprit_pr",
            "pr_ci",
            "main_failure",
            "causal_confirmation",
        },
        context,
    )
    record_id = nonempty(obj["id"], f"{context}.id")
    if not ID_RE.fullmatch(record_id):
        raise ValidationError(
            f"{context}.id: use lowercase letters, digits, and hyphens"
        )
    job_name = nonempty(obj["job_name"], f"{context}.job_name")
    job_key = nonempty(obj["job_key"], f"{context}.job_key")

    culprit = require_keys(
        obj["culprit_pr"], {"number", "url", "merge_commit"}, f"{context}.culprit_pr"
    )
    pr_number = positive_int(culprit["number"], f"{context}.culprit_pr.number")
    pr_url = nonempty(culprit["url"], f"{context}.culprit_pr.url")
    pr_match = PR_URL_RE.fullmatch(pr_url)
    if pr_match is None or int(pr_match.group(1)) != pr_number:
        raise ValidationError(f"{context}.culprit_pr.url: must point to the culprit PR")
    if not SHA_RE.fullmatch(
        nonempty(culprit["merge_commit"], f"{context}.culprit_pr.merge_commit")
    ):
        raise ValidationError(f"{context}.culprit_pr.merge_commit: expected a full SHA")

    validate_pr_ci(obj["pr_ci"], f"{context}.pr_ci")

    main_failure = require_keys(
        obj["main_failure"],
        {"build_number", "job_id", "url", "signature"},
        f"{context}.main_failure",
    )
    main_build, main_job_id = validate_buildkite_job(
        {key: main_failure[key] for key in ("build_number", "job_id", "url")},
        f"{context}.main_failure",
    )
    nonempty(main_failure["signature"], f"{context}.main_failure.signature")

    confirmation = require_keys(
        obj["causal_confirmation"],
        {"kind", "reference_url", "notes"},
        f"{context}.causal_confirmation",
    )
    if confirmation["kind"] not in CONFIRMATION_KINDS:
        raise ValidationError(f"{context}.causal_confirmation.kind: unsupported kind")
    reference = nonempty(
        confirmation["reference_url"], f"{context}.causal_confirmation.reference_url"
    )
    if not reference.startswith("https://"):
        raise ValidationError(
            f"{context}.causal_confirmation.reference_url: require HTTPS"
        )
    nonempty(confirmation["notes"], f"{context}.causal_confirmation.notes")

    return (
        record_id,
        (main_build, job_name.casefold()),
        (pr_number, job_key, main_job_id),
    )


def main() -> int:
    try:
        schema = load_json(SCHEMA_PATH)
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            raise ValidationError(f"{SCHEMA_PATH}: expected JSON Schema draft 2020-12")

        data = require_keys(
            load_json(DATA_PATH),
            {"$schema", "schema_version", "records"},
            str(DATA_PATH),
        )
        if data["$schema"] != "./selection-leaks.schema.json":
            raise ValidationError(f"{DATA_PATH}: unexpected $schema path")
        if data["schema_version"] != 1:
            raise ValidationError(f"{DATA_PATH}: unsupported schema_version")
        if not isinstance(data["records"], list) or not data["records"]:
            raise ValidationError(f"{DATA_PATH}: records must be a non-empty array")

        ids: set[str] = set()
        incident_keys: set[tuple[int, str, int]] = set()
        sort_keys: list[tuple[int, str]] = []
        for index, record in enumerate(data["records"]):
            record_id, sort_key, incident_key = validate_record(record, index)
            if record_id in ids:
                raise ValidationError(f"records[{index}].id: duplicate {record_id}")
            if incident_key in incident_keys:
                raise ValidationError(
                    f"records[{index}]: duplicate culprit PR/job/main-failure tuple"
                )
            ids.add(record_id)
            incident_keys.add(incident_key)
            sort_keys.append(sort_key)

        if sort_keys != sorted(sort_keys):
            raise ValidationError("records: sort by main build number, then job_name")
    except ValidationError as exc:
        print(f"selection-leaks validation failed: {exc}", file=sys.stderr)
        return 1

    print(f"selection-leaks validation passed: {len(ids)} records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
