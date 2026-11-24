#!/usr/bin/env python3
"""Shared JSONL test-outcome records, active retry chains, and 3-tier skip plans.

Used by three callers without duplicating record parsing or retry-chain rules:
  1. `runtime_shard_plugin.py` inside a test job (writes flushed per-test JSONL
     and skips already-passed files/node IDs on a tagged test retry).
  2. `runtime_shard_coverage.py` in the post-test coverage job (reads every
     attempt's records and checks coverage across the active retry chain).
  3. The job's pre-pytest shell (`prepare` and `maybe-skip-command` CLIs).

Standard library only so the module can be packed straight into job env vars.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

RETRY_IN_ENV = "CI_TEST_RETRY_IN"
RETRY_TAG_PREFIX = "test-retry:"
RETRY_DIR = "/tmp/ci-test-retry-in"
TAG_FILE = ".tag"
COVERAGE_DIR = ".runtime-shard-coverage"
ARTIFACTS_JSONL = f"{COVERAGE_DIR}/*/*.jsonl"
ARTIFACTS_JSON = f"{COVERAGE_DIR}/*/*.json"
META_TIMEOUT_SECONDS = 15
DOWNLOAD_TIMEOUT_SECONDS = 60
SKIP_COMMAND_EXIT = 0
RUN_COMMAND_EXIT = 10


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_str_list(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, str):
            return False
    return True


def _validate_optional_str_list(value: Any) -> bool:
    return value is None or _is_str_list(value)


def _parse_legacy_json(data: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    if not (
        isinstance(data.get("step"), str)
        and _is_int(data.get("position"))
        and _is_int(data.get("shard"))
        and _is_int(data.get("retry"))
        and isinstance(data.get("finished"), bool)
        and _is_number(data.get("finished_at"))
        and _is_str_list(data.get("ran"))
        and _validate_optional_str_list(data.get("expected"))
        and _validate_optional_str_list(data.get("planned"))
    ):
        return None
    build_id = data.get("build_id")
    if build_id is not None and not isinstance(build_id, str):
        return None
    mode = data.get("mode", "full")
    if mode not in ("full", "test-retry"):
        return None
    passed = data.get("passed")
    if passed is not None and not _is_str_list(passed):
        return None
    failed = data.get("failed")
    if failed is not None and not _is_str_list(failed):
        return None
    passed_sorted = sorted(set(passed)) if passed is not None else []
    return {
        "step": data["step"],
        "position": data["position"],
        "shard": data["shard"],
        "retry": data["retry"],
        "build_id": build_id,
        "mode": mode,
        "finished": data["finished"],
        "finished_at": float(data["finished_at"]),
        "exitstatus": data.get("exitstatus") if _is_int(data.get("exitstatus")) else None,
        "expected": data.get("expected"),
        "planned": data.get("planned"),
        "ran": sorted(set(data["ran"])),
        # Legacy #884 JSON records did not prove 3-phase pass, so default empty.
        "passed": passed_sorted,
        "passed_counts": {nodeid: 1 for nodeid in passed_sorted},
        "failed": sorted(set(failed)) if failed is not None else [],
        "skipped_command": bool(data.get("skipped_command", False)),
    }


def _parse_record_with_reason(path: str) -> tuple:
    """Parse one record file, returning `(record, unreadable_detail)`."""
    try:
        with open(path) as f:
            raw = f.read()
    except OSError as error:
        return None, str(error)
    if not raw.strip():
        return None, None
    if path.endswith(".json"):
        try:
            data = json.loads(raw)
        except ValueError as error:
            return None, str(error)
        return _parse_legacy_json(data), None

    lines = raw.splitlines()
    try:
        header = json.loads(lines[0])
    except ValueError as error:
        return None, str(error)
    if not isinstance(header, dict) or header.get("event") != "start":
        return None, None
    if not (
        isinstance(header.get("step"), str)
        and _is_int(header.get("position"))
        and _is_int(header.get("shard"))
        and _is_int(header.get("retry"))
        and header.get("mode") in ("full", "test-retry")
        and _validate_optional_str_list(header.get("expected"))
        and _validate_optional_str_list(header.get("planned"))
    ):
        return None, None
    build_id = header.get("build_id")
    if build_id is not None and not isinstance(build_id, str):
        return None, None

    ran: Set[str] = set()
    passed: Set[str] = set()
    passed_counts: Dict[str, int] = {}
    not_passed: Set[str] = set()
    failed: Set[str] = set()
    finished = False
    finished_at = 0.0
    exitstatus: Optional[int] = None

    for index, line in enumerate(lines[1:], start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            # A truncated final line from a mid-write SIGKILL is dropped; an
            # interior corrupt line invalidates the file.
            if index == len(lines) - 1:
                break
            return None, None
        if not isinstance(event, dict):
            return None, None
        kind = event.get("event")
        if kind == "test":
            nodeid = event.get("nodeid")
            is_passed = event.get("passed")
            if not isinstance(nodeid, str) or not isinstance(is_passed, bool):
                return None, None
            ran.add(nodeid)
            if is_passed:
                if nodeid not in not_passed:
                    passed.add(nodeid)
                    passed_counts[nodeid] = passed_counts.get(nodeid, 0) + 1
            else:
                not_passed.add(nodeid)
                passed.discard(nodeid)
                passed_counts.pop(nodeid, None)
                if event.get("outcome") in ("failed", "error"):
                    failed.add(nodeid)
        elif kind == "finish":
            if not isinstance(event.get("finished"), bool) or not _is_number(
                event.get("finished_at")
            ):
                return None, None
            raw_exit = event.get("exitstatus")
            if raw_exit is not None and not _is_int(raw_exit):
                return None, None
            finished = event["finished"]
            finished_at = float(event["finished_at"])
            exitstatus = raw_exit

    return (
        {
            "step": header["step"],
            "position": header["position"],
            "shard": header["shard"],
            "retry": header["retry"],
            "build_id": build_id,
            "mode": header["mode"],
            "finished": finished,
            "finished_at": finished_at,
            "exitstatus": exitstatus,
            "expected": header.get("expected"),
            "planned": header.get("planned"),
            "ran": sorted(ran),
            "passed": sorted(passed),
            "passed_counts": passed_counts,
            "failed": sorted(failed),
            "skipped_command": bool(header.get("skipped_command", False)),
        },
        None,
    )


def parse_record_file(path: str) -> Optional[Dict[str, Any]]:
    """Parse one coverage/retry record file (flushed JSONL or legacy JSON).

    A job killed mid-write may leave a truncated final line in a JSONL file;
    that incomplete line is ignored while all earlier flushed lines are kept.

    Args:
        path: Path to a `.jsonl` or `.json` record file.

    Returns:
        Normalized record dict, or None when the file is unreadable or malformed.
    """
    record, _ = _parse_record_with_reason(path)
    return record


def read_records(folder: str) -> List[Dict[str, Any]]:
    """Read every valid `.jsonl` and `.json` record under `folder`.

    Args:
        folder: Root directory into which Buildkite downloaded artifacts.

    Returns:
        Parsed records; unreadable or malformed files are skipped with a log line.
    """
    records: List[Dict[str, Any]] = []
    for artifact_glob in (ARTIFACTS_JSONL, ARTIFACTS_JSON):
        pattern = os.path.join(folder, artifact_glob)
        for path in sorted(glob.glob(pattern)):
            record, unreadable_detail = _parse_record_with_reason(path)
            if record is None:
                if unreadable_detail is not None:
                    print(
                        f"runtime-shard coverage: skipped unreadable {path}: {unreadable_detail}"
                    )
                else:
                    print(f"runtime-shard coverage: skipped malformed {path}")
                continue
            records.append(record)
    return records


def active_chain(
    records: Sequence[Dict[str, Any]],
    step: str,
    position: int,
    shard: int,
    build_id: Optional[str] = None,
    before_retry: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Return the active retry chain for one `(step, position, shard)`.

    Walks backward through consecutive retry numbers until reaching a `full`
    attempt or a missing retry gap. A full rerun (`/ci retry` or manual Retry)
    starts a fresh pass history, and a missing intermediate attempt stops the
    walk fail-safely so earlier attempts are never reused across an unknown run.

    Args:
        records: Parsed records from all downloaded attempts.
        step: Step key.
        position: Command position in the step's `commands` list.
        shard: Parallel shard index (0-based).
        build_id: Current build ID; when given, records with another build ID
            are ignored.
        before_retry: When given (inside attempt `R`), walks backward from
            `R - 1`. When None (coverage checker), walks backward from the
            highest recorded retry number.

    Returns:
        Chronological list (oldest to newest) of records in the active chain,
        or empty list when no chain starts at the target retry.
    """
    by_retry: Dict[int, Dict[str, Any]] = {}
    for record in records:
        if (
            record["step"] != step
            or record["position"] != position
            or record["shard"] != shard
        ):
            continue
        if build_id is not None and record.get("build_id") != build_id:
            continue
        retry = record["retry"]
        if before_retry is not None and retry >= before_retry:
            continue
        best = by_retry.get(retry)
        rank = (record["finished"], record["finished_at"])
        if best is None or rank > (best["finished"], best["finished_at"]):
            by_retry[retry] = record

    if not by_retry:
        return []

    if before_retry is not None:
        if before_retry <= 0:
            return []
        start_retry = before_retry - 1
    else:
        start_retry = max(by_retry)

    reversed_chain: List[Dict[str, Any]] = []
    retry_num = start_retry
    while retry_num >= 0:
        record = by_retry.get(retry_num)
        if record is None:
            break
        reversed_chain.append(record)
        if record.get("mode", "full") == "full":
            break
        retry_num -= 1
    reversed_chain.reverse()
    return reversed_chain


def chain_passed(chain: Sequence[Dict[str, Any]]) -> Set[str]:
    """Return node IDs proven passed across an intact active retry chain.

    For a node ID appearing multiple times in the chain's base `planned` list
    (e.g. `pytest --keep-duplicates`), every planned occurrence must have
    passed within a single attempt without any non-pass outcome; partial passes
    before a crash never count and never add up across attempts.

    Args:
        chain: Chronological records starting at a `full` attempt.

    Returns:
        Set of node IDs safe to treat as passed.
    """
    if not chain:
        return set()
    base_planned = chain[0].get("planned")
    if not isinstance(base_planned, list):
        return set()
    planned_counts: Dict[str, int] = {}
    for nodeid in base_planned:
        planned_counts[nodeid] = planned_counts.get(nodeid, 0) + 1

    passed: Set[str] = set()
    for record in chain:
        record_counts = record.get("passed_counts")
        for nodeid in record.get("passed") or ():
            required = planned_counts.get(nodeid, 1)
            if required > 1:
                if (
                    not isinstance(record_counts, dict)
                    or record_counts.get(nodeid, 0) < required
                ):
                    continue
            passed.add(nodeid)
    return passed


def build_retry_plan(
    records: Sequence[Dict[str, Any]],
    step: str,
    position: int,
    shard: int,
    build_id: Optional[str],
    current_retry: int,
    needs_expected: bool = False,
) -> Dict[str, Any]:
    """Compute the 3-tier skip plan for a tagged test retry.

    Requires an intact active chain reaching back to a `full` attempt with a
    valid `planned` list. Any gap, missing full base, or failed base
    collection fails safe by returning an empty plan (full rerun).

    Args:
        records: Parsed prior-attempt records.
        step: Step key.
        position: Command position.
        shard: Parallel shard index.
        build_id: Current Buildkite build ID, or None locally.
        current_retry: Current `BUILDKITE_RETRY_COUNT`.
        needs_expected: True when this shard owns the command's coverage
            inventory (`shards >= 2` and `shard == fallback`), so Tier 1 must
            not skip the command while `has_expected` is still False.

    Returns:
        Dict with `active` (whether an intact prior chain was found), `passed`
        (set of node IDs), `skip_command` (Tier 1 bool), `skip_files` (Tier 2
        set of rootpath-relative file paths), `has_expected` (whether an
        earlier attempt already captured a valid non-empty inventory), and
        `base_planned` (list of node IDs from the chain's full attempt, or
        None).
    """
    empty = {
        "active": False,
        "passed": set(),
        "skip_command": False,
        "skip_files": set(),
        "has_expected": False,
        "base_planned": None,
    }
    chain = active_chain(
        records,
        step,
        position,
        shard,
        build_id=build_id,
        before_retry=current_retry,
    )
    if not chain or chain[0].get("mode", "full") != "full":
        return empty
    base_planned = chain[0].get("planned")
    if not isinstance(base_planned, list):
        return empty

    base_counts: Dict[str, int] = {}
    for nodeid in base_planned:
        base_counts[nodeid] = base_counts.get(nodeid, 0) + 1

    # Fail safe to a full rerun when any attempt in the chain:
    # 1. exited non-zero without a recorded failed/errored test (e.g. a
    #    session-finalization hook failure), or
    # 2. collected new tests, extra duplicate occurrences, or failed collection
    #    relative to the base full attempt's `planned` list.
    for index, record in enumerate(chain):
        exitstatus = record.get("exitstatus")
        if (
            record.get("finished")
            and _is_int(exitstatus)
            and exitstatus != 0
            and not record.get("failed")
        ):
            return empty
        if index == 0 or record.get("skipped_command"):
            continue
        rec_planned = record.get("planned")
        if not isinstance(rec_planned, list):
            return empty
        rec_counts: Dict[str, int] = {}
        for nodeid in rec_planned:
            rec_counts[nodeid] = rec_counts.get(nodeid, 0) + 1
            if rec_counts[nodeid] > base_counts.get(nodeid, 0):
                return empty
        for nodeid in record.get("ran") or ():
            if nodeid not in base_counts:
                return empty

    passed = chain_passed(chain)
    has_expected = False
    for record in chain:
        expected = record.get("expected")
        if isinstance(expected, list) and len(expected) > 0:
            has_expected = True

    skip_command = False
    skip_files: Set[str] = set()

    by_file: Dict[str, List[str]] = {}
    for nodeid in base_planned:
        file_path = nodeid.split("::")[0]
        by_file.setdefault(file_path, []).append(nodeid)
    for file_path, nodeids in by_file.items():
        if nodeids and set(nodeids).issubset(passed):
            skip_files.add(file_path)
    # Never skip a command whose collection was empty (e.g. exit 5 on a
    # broken selector), or whose inventory-owning shard still lacks a valid
    # expected list for #884 coverage.
    if (
        base_planned
        and chain[-1]["finished"]
        and (not needs_expected or has_expected)
        and set(base_planned).issubset(passed)
    ):
        skip_command = True

    return {
        "active": True,
        "passed": passed,
        "skip_command": skip_command,
        "skip_files": skip_files,
        "has_expected": has_expected,
        "base_planned": base_planned,
    }


def _record_path(coverage_dir: str, job_id: str, position: int) -> str:
    return os.path.join(coverage_dir, job_id, f"{position}.jsonl")


def _ensure_record_dir(coverage_dir: str, job_id: str) -> str:
    """Create 0777 directories so the Buildkite agent can delete root-written files."""
    job_dir = os.path.join(coverage_dir, job_id)
    os.makedirs(job_dir, exist_ok=True)
    os.chmod(coverage_dir, 0o777)
    os.chmod(job_dir, 0o777)
    return job_dir


def write_jsonl_start(
    coverage_dir: str,
    job_id: str,
    step: str,
    position: int,
    shard: int,
    retry: int,
    build_id: Optional[str],
    mode: str,
    expected: Optional[List[str]],
    planned: Optional[List[str]],
    skipped_command: bool = False,
) -> Optional[str]:
    """Write line 1 (`start` header) of a command's JSONL record."""
    try:
        _ensure_record_dir(coverage_dir, job_id)
        path = _record_path(coverage_dir, job_id, position)
        header = {
            "event": "start",
            "v": 2,
            "build_id": build_id,
            "job_id": job_id,
            "step": step,
            "position": position,
            "shard": shard,
            "retry": retry,
            "mode": mode,
            "expected": expected,
            "planned": planned,
            "skipped_command": skipped_command,
        }
        with open(path, "w") as f:
            f.write(json.dumps(header) + "\n")
            f.flush()
        os.chmod(path, 0o666)
        return path
    except OSError as error:
        print(f"runtime-shard: no coverage record: {error}")
        return None


def append_jsonl_event(path: Optional[str], payload: Dict[str, Any]) -> None:
    """Append and flush one JSONL event line (best effort, never fails tests)."""
    if not path:
        return
    try:
        with open(path, "a") as f:
            f.write(json.dumps(payload) + "\n")
            f.flush()
    except OSError as error:
        print(f"runtime-shard: couldn't append coverage event: {error}")


def prepare_retry_dir(
    dest_dir: str = RETRY_DIR,
    env: Optional[Dict[str, str]] = None,
    run_cmd: Callable[..., Any] = subprocess.run,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> bool:
    """Prepare prior-attempt JSONL artifacts on a tagged test retry.

    Does nothing on Attempt 0 or untagged retries (`/ci retry` or manual
    per-job Retry). Honors a directory already populated by a host
    `pre-command` hook via `CI_TEST_RETRY_IN`. Fails safe to False (full run)
    on any agent or download error.

    Args:
        dest_dir: Local directory to download prior artifacts into.
        env: Environment mapping (defaults to `os.environ`).
        run_cmd: Subprocess runner (injectable for tests).
        which: Executable lookup (injectable for tests).

    Returns:
        True when this attempt is a verified test retry with a ready directory.
    """
    env_map = os.environ if env is None else env
    existing = env_map.get(RETRY_IN_ENV)
    try:
        retry_count = int(env_map.get("BUILDKITE_RETRY_COUNT") or 0)
    except ValueError:
        retry_count = 0

    if retry_count > 0 and existing and os.path.isfile(os.path.join(existing, TAG_FILE)):
        return True

    # Always clear stale state from dest_dir before any early return so an
    # untagged retry or Attempt 0 can never inherit an old `.tag`.
    if os.path.exists(dest_dir):
        shutil.rmtree(dest_dir, ignore_errors=True)

    if retry_count <= 0:
        return False

    job_id = env_map.get("BUILDKITE_JOB_ID")
    if not job_id or not which("buildkite-agent"):
        return False

    try:
        tag_run = run_cmd(
            ["buildkite-agent", "meta-data", "get", f"{RETRY_TAG_PREFIX}{job_id}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=META_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False

    tag = (tag_run.stdout or "").strip() if tag_run.returncode == 0 else ""
    if not tag:
        return False

    os.makedirs(dest_dir, exist_ok=True)

    dl_env = dict(env_map)
    dl_env["BUILDKITE_AGENT_INCLUDE_RETRIED_JOBS"] = "true"
    try:
        dl_run = run_cmd(
            ["buildkite-agent", "artifact", "download", ARTIFACTS_JSONL, dest_dir],
            check=False,
            capture_output=True,
            text=True,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
            env=dl_env,
        )
    except (OSError, subprocess.TimeoutExpired):
        shutil.rmtree(dest_dir, ignore_errors=True)
        return False

    output = (dl_run.stdout or "") + (dl_run.stderr or "")
    if dl_run.returncode != 0 and "no artifacts found for downloading" not in output:
        shutil.rmtree(dest_dir, ignore_errors=True)
        return False

    with open(os.path.join(dest_dir, TAG_FILE), "w") as f:
        f.write(tag + "\n")
    return True


def maybe_skip_command(
    step: str,
    position: int,
    shard: int,
    retry_in_dir: str,
    coverage_dir: Optional[str],
    env: Optional[Dict[str, str]] = None,
    needs_expected: bool = False,
) -> bool:
    """Tier 1 check: skip launching pytest when every planned test already passed.

    When skipping, writes an explicit `skipped_command: true` JSONL record for
    this retry so subsequent retries and the coverage checker see an unbroken
    chain.

    Args:
        step: Step key.
        position: Command position.
        shard: Parallel shard index.
        retry_in_dir: Directory containing downloaded prior JSONL records.
        coverage_dir: Output directory for this attempt's JSONL record.
        env: Environment mapping (defaults to `os.environ`).
        needs_expected: True when this shard owns the command's coverage
            inventory so Tier 1 waits until a valid inventory exists.

    Returns:
        True when the command was skipped and pytest should not be launched.
    """
    env_map = os.environ if env is None else env
    try:
        current_retry = int(env_map.get("BUILDKITE_RETRY_COUNT") or 0)
    except ValueError:
        return False
    if current_retry <= 0 or not retry_in_dir or not os.path.isdir(retry_in_dir):
        return False

    records = read_records(retry_in_dir)
    plan = build_retry_plan(
        records,
        step,
        position,
        shard,
        build_id=env_map.get("BUILDKITE_BUILD_ID"),
        current_retry=current_retry,
        needs_expected=needs_expected,
    )
    if not plan["skip_command"]:
        return False

    if coverage_dir:
        job_id = env_map.get("BUILDKITE_JOB_ID", "no-job-id")
        path = write_jsonl_start(
            coverage_dir=coverage_dir,
            job_id=job_id,
            step=step,
            position=position,
            shard=shard,
            retry=current_retry,
            build_id=env_map.get("BUILDKITE_BUILD_ID"),
            mode="test-retry",
            expected=None,
            planned=plan["base_planned"],
            skipped_command=True,
        )
        append_jsonl_event(
            path,
            {
                "event": "finish",
                "finished": True,
                "finished_at": time.time(),
                "exitstatus": 0,
            },
        )
    count = len(plan["base_planned"] or [])
    print(
        f"pytest-retry: command {position + 1} already passed ({count} tests); skipping"
    )
    return True


def _main(argv: Sequence[str]) -> int:
    if len(argv) < 2:
        return 2
    action = argv[1]
    if action == "prepare":
        dest = argv[2] if len(argv) > 2 else RETRY_DIR
        prepare_retry_dir(dest_dir=dest)
        return 0
    if action == "maybe-skip-command" and len(argv) >= 5:
        step = argv[2]
        position = int(argv[3])
        shard = int(argv[4])
        needs_expected = (
            len(argv) >= 7 and int(argv[6]) >= 2 and shard == int(argv[5])
        )
        retry_in = os.environ.get(RETRY_IN_ENV, "")
        coverage_dir = os.environ.get("RUNTIME_SHARD_COVERAGE_DIR")
        skipped = maybe_skip_command(
            step,
            position,
            shard,
            retry_in,
            coverage_dir,
            needs_expected=needs_expected,
        )
        return SKIP_COMMAND_EXIT if skipped else RUN_COMMAND_EXIT
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
