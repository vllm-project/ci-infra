"""Run more jobs: one input step for every job CI did not pick on its own.

The generator leaves the jobs it did not select out of the uploaded pipeline
and writes them to DEFERRED_FILE instead, next to a copy of this script, both
uploaded as artifacts of the bootstrap job. The pipeline gets one input step
listing them as checkboxes, one list per group, and a job behind it that runs this script: it uploads the jobs
picked in the input, with the deferred jobs they depend on, and a new input
step for whatever is left, so more can be picked later.

Runs on a plain CPU agent, so it uses the standard library only.
"""

import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Set, Tuple

DEFERRED_DIR = "deferred"
DEFERRED_FILE = "deferred_steps.json"
SCRIPT_FILE = "unblock_jobs.py"

INPUT_KEY = "run-more-jobs"
UPLOAD_KEY = "upload-more-jobs"
# The free-text field of the first round; later rounds add "-<round>". The
# group checkbox lists add "-group-<group>" to that.
FIELD_KEY = "more-jobs"
# Newline-separated keys of the deferred jobs uploaded so far, across rounds.
UPLOADED_META_KEY = "more-jobs-uploaded"
GROUP = ":heavy_plus_sign: Run more jobs"
# Buildkite shows a select of 7 or more options as a dropdown, multiple or
# not, so longer groups are split into lists of at most this many.
MAX_CHECKBOXES = 6


def _round_key(prefix: str, round_number: int) -> str:
    return prefix if round_number == 1 else f"{prefix}-{round_number}"


def _group_fields(
    entries: List[Dict[str, Any]], round_number: int
) -> List[Tuple[str, str, List[Dict[str, Any]]]]:
    """(field key, list name, entries) for each checkbox list, in order.

    An entry is listed under its "list" if it has one, else its group. A
    group longer than MAX_CHECKBOXES becomes "<group> (1/3)", "(2/3)", ...
    """
    by_group: Dict[str, List[Dict[str, Any]]] = {}
    for entry in entries:
        by_group.setdefault(entry.get("list", entry["group"]), []).append(entry)
    lists = []
    for group, group_entries in by_group.items():
        chunks = [
            group_entries[i : i + MAX_CHECKBOXES]
            for i in range(0, len(group_entries), MAX_CHECKBOXES)
        ]
        for i, chunk in enumerate(chunks, 1):
            name = f"{group} ({i}/{len(chunks)})" if len(chunks) > 1 else group
            lists.append((name, chunk))
    fields = []
    used: Set[str] = set()
    for group, group_entries in lists:
        slug = re.sub(r"[^a-z0-9]+", "-", group.lower()).strip("-") or "jobs"
        key = f"{_round_key(FIELD_KEY, round_number)}-group-{slug}"
        suffix = 2
        while key in used:
            key = f"{_round_key(FIELD_KEY, round_number)}-group-{slug}-{suffix}"
            suffix += 1
        used.add(key)
        fields.append((key, group, group_entries))
    return fields


def _option_label(label: str) -> str:
    """The step label as plain text: the input dialog shows emoji codes like
    `:nvidia:` and parallel job placeholders like `%N` as they are."""
    label = re.sub(r":[a-z0-9_+-]+:", "", label)
    # "... Shard %N" or "... %N": the step, not one of its parallel jobs.
    label = re.sub(r"(?:\b[Ss]hard\s+)?%N(?:\s*/\s*%t)?", "", label)
    return " ".join(label.split()) or label


def more_jobs_group(
    entries: List[Dict[str, Any]],
    round_number: int,
    artifact_dir: str,
    source_job: str,
    queue: str,
) -> Dict[str, Any]:
    """The input step listing `entries`, and the job that uploads the picks.

    Each entry is {"group": ..., "step": <Buildkite command step>}, and
    optionally "list", the checkbox list to show it in. A select
    with `multiple` shows its options as checkboxes; one per group keeps each
    list short enough to read, under the group's name.
    """
    input_key = _round_key(INPUT_KEY, round_number)
    fields: List[Dict[str, Any]] = [
        {
            "text": "Job keys",
            "key": _round_key(FIELD_KEY, round_number),
            "required": False,
            "hint": (
                "Optional: step keys to run, one per line, on top of anything "
                "ticked below."
            ),
        }
    ]
    for key, group, group_entries in _group_fields(entries, round_number):
        fields.append(
            {
                "select": group,
                "key": key,
                "multiple": True,
                "required": False,
                "options": [
                    {
                        "label": _option_label(e["step"]["label"]),
                        "value": e["step"]["key"],
                    }
                    for e in group_entries
                ],
            }
        )
    download = (
        f"buildkite-agent artifact download '{artifact_dir}/*' . --step {source_job}"
    )
    run = f"python3 {artifact_dir}/{SCRIPT_FILE} {round_number} {artifact_dir}"
    return {
        "group": GROUP,
        "steps": [
            {
                "input": "Run more jobs",
                "key": input_key,
                "depends_on": [],
                "prompt": (
                    "The jobs CI selected for this change already run. "
                    "Tick any others to run too; the jobs they depend on come "
                    "with them."
                ),
                "fields": fields,
            },
            {
                "label": ":rocket: Upload the picked jobs",
                "key": _round_key(UPLOAD_KEY, round_number),
                "depends_on": [input_key],
                "agents": {"queue": queue},
                "commands": [f"{download} && {run}"],
            },
        ],
    }


def pick(
    entries: List[Dict[str, Any]], picked: Set[str]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split `entries` into the picked jobs, with every deferred job they
    depend on, and the rest. A dependency that is not in `entries` is already
    in the build and is left alone.
    """
    by_key = {entry["step"]["key"]: entry for entry in entries}
    unknown = picked - by_key.keys()
    if unknown:
        raise SystemExit(
            "Unknown or already uploaded jobs: " + ", ".join(sorted(unknown))
        )
    chosen = set(picked)
    pending = list(picked)
    while pending:
        for dependency in by_key[pending.pop()]["step"].get("depends_on") or []:
            if dependency in by_key and dependency not in chosen:
                chosen.add(dependency)
                pending.append(dependency)
    selected = [e for e in entries if e["step"]["key"] in chosen]
    remaining = [e for e in entries if e["step"]["key"] not in chosen]
    return selected, remaining


def _grouped(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for entry in entries:
        groups.setdefault(entry["group"], []).append(entry["step"])
    return [{"group": group, "steps": steps} for group, steps in groups.items()]


def _meta_data_get(key: str) -> str:
    # `exists` exits 100 for a key that was never set.
    exists = subprocess.run(["buildkite-agent", "meta-data", "exists", key])
    if exists.returncode == 100:
        return ""
    exists.check_returncode()
    return subprocess.run(
        ["buildkite-agent", "meta-data", "get", key],
        check=True,
        stdout=subprocess.PIPE,
        universal_newlines=True,
    ).stdout


def main(argv: List[str]) -> None:
    round_number = int(argv[1])
    artifact_dir = argv[2]
    with open(os.path.join(artifact_dir, DEFERRED_FILE)) as f:
        deferred = json.load(f)

    uploaded = set(_meta_data_get(UPLOADED_META_KEY).split())
    entries = [e for e in deferred["steps"] if e["step"]["key"] not in uploaded]
    # A multiple select stores its values newline-separated; the text field
    # takes keys one per line, or separated by spaces or commas.
    picked = set(
        _meta_data_get(_round_key(FIELD_KEY, round_number)).replace(",", " ").split()
    )
    for key, _, _ in _group_fields(entries, round_number):
        picked.update(_meta_data_get(key).split())
    selected, remaining = pick(entries, picked)

    groups = _grouped(selected)
    if remaining:
        groups.append(
            more_jobs_group(
                remaining,
                round_number + 1,
                artifact_dir,
                deferred["source_job"],
                deferred["queue"],
            )
        )
    pipeline_path = os.path.join(artifact_dir, f"more-jobs-{round_number}.json")
    with open(pipeline_path, "w") as f:
        json.dump({"steps": groups}, f)
    # Nothing picked still uploads a fresh input step, so the list stays open.
    print("Uploading: " + (", ".join(e["step"]["key"] for e in selected) or "nothing"))
    subprocess.run(["buildkite-agent", "pipeline", "upload", pipeline_path], check=True)
    if not selected:
        return
    # Recorded only once the upload went through, so a retry of a failed
    # upload starts from the same list.
    subprocess.run(
        [
            "buildkite-agent",
            "meta-data",
            "set",
            UPLOADED_META_KEY,
            "\n".join(sorted(uploaded | {e["step"]["key"] for e in selected})),
        ],
        check=True,
    )


if __name__ == "__main__":
    main(sys.argv)
