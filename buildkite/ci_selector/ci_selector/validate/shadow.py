# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Score shadow runs: what a PR build ran against what the selector would run.

Each PR build with the shadow step on carries `shadow/selection.json`, the
selector's answer for that PR (see `shadow/run.sh`). This reads it back with
the build's own job outcomes and reports, per build, the jobs today ran, the
ones the selector would have kept, and every failure in a step it would have
skipped, classified:

  flake         the job failed, then passed on a retry in the same build
  pre-existing  the same step key failed on a main build of the pipeline
                within the window around the PR's base commit, so main was
                already broken there and skipping it hid nothing new
  MISS          neither: a failure the selector would have hidden

MISS is the number the exit bar is about, and each one is still a human call.

The selector names tests, never plumbing, and the generator adds each kept
step's dependencies itself. Those dependencies are not in the artifact, so a
failed step that is only a dependency of a kept one reads as would-skip here.
That errs toward MISS, never away from it. Image builds, the AMD always-run
steps, pre-commit and the shadow step itself are treated as kept.

Usage:
  export BK_TOKEN=...   # read_builds, read_artifacts
  ci-validate shadow --builds 91234 91240 [--json-out out.json]
  ci-validate shadow --prs 55755 53280     # the latest finished build of each
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..handwritten import AMD_ALWAYS_RUN_STEP_KEYS
from ..scripts.fetch import API, HttpTransport, Throttle, Transport

ARTIFACT_PATH = "shadow/selection.json"
SHADOW_STEP_KEY = "ci-selector-shadow"
MAIN_BRANCH = "main"
# Job states that mean the job ran and did not pass.
FAILED_STATES = frozenset({"failed", "timed_out"})
PASSED_STATE = "passed"
FINISHED_BUILD_STATES = frozenset({"passed", "failed", "canceled"})
DEFAULT_WINDOW_HOURS = 48
GH_REPO = "vllm-project/vllm"


def is_plumbing(key: str | None) -> bool:
    """Steps the generator runs whatever the selection says."""
    if not key:
        return True
    return (
        key.startswith("image-build")
        or key in AMD_ALWAYS_RUN_STEP_KEYS
        or key in ("pre-commit", "bootstrap", SHADOW_STEP_KEY)
    )


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def attempts(build: dict) -> dict[tuple[str, int | None], list[dict]]:
    """(step key, shard) -> that job's attempts, oldest first.

    A retry is a new job under the same key and shard, with the old one marked
    `retried`. Jobs that never started ran nothing and are left out.
    """
    out: dict[tuple[str, int | None], list[dict]] = {}
    for job in build.get("jobs") or []:
        if job.get("type") != "script" or not job.get("started_at"):
            continue
        slot = (job.get("step_key") or "", job.get("parallel_group_index"))
        out.setdefault(slot, []).append(job)
    for tries in out.values():
        tries.sort(key=lambda j: j.get("started_at") or "")
    return out


def failed_step_keys(build: dict) -> set[str]:
    """Step keys whose final attempt failed in this build."""
    return {
        key
        for (key, _), tries in attempts(build).items()
        if key and tries[-1].get("state") in FAILED_STATES
    }


class MainHistory:
    """Main builds of one pipeline, fetched once per UTC day and kept.

    A day's builds are cached on disk once the day is two days old, since by
    then every build in it has finished and the answer cannot change.
    """

    def __init__(self, transport: Transport, pipeline_url: str, cache: Path | None):
        self._t = transport
        self._url = pipeline_url
        self._cache = cache
        self._days: dict[str, list[dict]] = {}

    def _day(self, day: datetime) -> list[dict]:
        name = day.strftime("%Y-%m-%d")
        if name in self._days:
            return self._days[name]
        path = self._cache / f"main-{name}.json" if self._cache else None
        if path and path.is_file():
            rows = json.loads(path.read_text())
        else:
            query = urllib.parse.urlencode(
                {
                    "branch": MAIN_BRANCH,
                    "created_from": _iso(day),
                    "created_to": _iso(day + timedelta(days=1)),
                    "exclude_pipeline": "true",
                }
            )
            rows = [
                {
                    "number": b.get("number"),
                    "commit": b.get("commit"),
                    "created_at": b.get("created_at"),
                    "state": b.get("state"),
                    "failed": sorted(failed_step_keys(b)),
                }
                for b in self._t.paged(f"{self._url}/builds?{query}")
            ]
            settled = datetime.now(UTC) - day > timedelta(days=2)
            if path and settled:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(rows))
        self._days[name] = rows
        return rows

    def failed_near(self, when: datetime, hours: int) -> dict[str, list[int]]:
        """step key -> main build numbers within ±hours of `when` it failed in."""
        lo, hi = when - timedelta(hours=hours), when + timedelta(hours=hours)
        day = lo.replace(hour=0, minute=0, second=0, microsecond=0)
        out: dict[str, list[int]] = {}
        while day <= hi:
            for b in self._day(day):
                created = _ts(b["created_at"])
                if created is None or not lo <= created <= hi:
                    continue
                for key in b["failed"]:
                    out.setdefault(key, []).append(b["number"])
            day += timedelta(days=1)
        return out


def commit_time(sha: str, repo: Path | None) -> datetime | None:
    """When `sha` was committed: from a local checkout if it has it, else
    GitHub through `gh`. None when neither can say."""
    if repo:
        out = subprocess.run(
            ["git", "-C", str(repo), "show", "-s", "--format=%cI", sha],
            capture_output=True,
            text=True,
        )
        if out.returncode == 0 and out.stdout.strip():
            return _ts(out.stdout.strip())
    try:
        out = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{GH_REPO}/commits/{sha}",
                "--jq",
                ".commit.committer.date",
            ],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    return _ts(out.stdout.strip()) if out.returncode == 0 else None


def shadow_selection(transport: Transport, build_url: str) -> dict | None:
    """The build's shadow/selection.json, or None if it has none."""
    for art in transport.paged(f"{build_url}/artifacts"):
        if (art.get("path") or "").removeprefix("./") == ARTIFACT_PATH:
            blob = transport.get_bytes(art["download_url"], accept="*/*")
            return json.loads(blob) if blob is not None else None
    return None


def score_build(
    build: dict,
    selection: dict | None,
    history: MainHistory,
    base_time: datetime | None,
    window_hours: int = DEFAULT_WINDOW_HOURS,
) -> dict:
    number = build.get("number")
    row: dict = {
        "build": number,
        "url": build.get("web_url"),
        "branch": build.get("branch"),
        "pr": (build.get("pull_request") or {}).get("id"),
        "commit": build.get("commit"),
        "state": build.get("state"),
    }
    if build.get("state") not in FINISHED_BUILD_STATES:
        return {**row, "skip": f"build is {build.get('state')}"}
    if selection is None:
        return {**row, "skip": f"no {ARTIFACT_PATH} (shadow step off, or it broke)"}
    row["base"] = selection.get("base")
    row["status"] = selection.get("status")
    if selection.get("status") != "ok":
        return {
            **row,
            "skip": f"shadow {selection.get('status')}: {selection.get('reason')}",
        }

    slots = attempts(build)
    ran = {slot: tries for slot, tries in slots.items() if slot[0] != SHADOW_STEP_KEY}
    run_all = bool(selection.get("omit"))
    keys = set(selection.get("keys") or [])

    def kept(key: str) -> bool:
        return run_all or key in keys or is_plumbing(key)

    ran_keys = {k for k, _ in ran}
    row.update(
        run_all=run_all,
        reason=selection.get("reason") or "",
        emitted=len(keys),
        ran_jobs=len(ran),
        ran_steps=len(ran_keys),
        kept_jobs=sum(1 for k, _ in ran if kept(k)),
        kept_steps=sum(1 for k in ran_keys if kept(k)),
        # Kept by the selector but not run today: today's rules skipped it, or
        # it sits behind a block step nobody unblocked.
        not_run_kept=sorted(keys - ran_keys),
        records=selection.get("records") or [],
    )
    when = base_time or _ts(build.get("created_at"))
    row["base_time"] = _iso(when) if when else None
    row["base_time_source"] = "commit" if base_time else "build created_at"
    main_failed = history.failed_near(when, window_hours) if when else {}

    failures = []
    for (key, shard), tries in sorted(
        ran.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)
    ):
        if kept(key) or not any(j.get("state") in FAILED_STATES for j in tries):
            continue
        final = tries[-1]
        if final.get("state") == PASSED_STATE:
            verdict, why = "flake", f"passed on attempt {len(tries)}"
        elif key in main_failed:
            nums = sorted(main_failed[key])
            verdict = "pre-existing"
            why = f"failed on main build(s) {', '.join(map(str, nums[:5]))}"
        else:
            verdict, why = "MISS", ""
        failures.append(
            {
                "step_key": key,
                "shard": shard,
                "label": final.get("name"),
                "state": final.get("state"),
                "soft_failed": bool(final.get("soft_failed")),
                "verdict": verdict,
                "why": why,
                "url": final.get("web_url"),
            }
        )
    row["skipped_failures"] = failures
    row["miss"] = sum(1 for f in failures if f["verdict"] == "MISS")
    return row


def _latest_build_for_pr(
    transport: Transport, pipeline_url: str, pr: int
) -> int | None:
    """The newest finished build of the PR's head commit."""
    out = subprocess.run(
        ["gh", "pr", "view", str(pr), "--repo", GH_REPO, "--json", "headRefOid"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return None
    head = json.loads(out.stdout)["headRefOid"]
    query = urllib.parse.urlencode({"commit": head, "exclude_jobs": "true"})
    builds = [
        b
        for b in transport.paged(f"{pipeline_url}/builds?{query}")
        if b.get("state") in FINISHED_BUILD_STATES
    ]
    return max((b["number"] for b in builds), default=None)


def render(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        head = f"build {r['build']}" + (f"  PR #{r['pr']}" if r.get("pr") else "")
        if "skip" in r:
            lines.append(f"{head}: SKIP ({r['skip']})")
            continue
        ratio = f"{r['kept_jobs'] / r['ran_jobs']:.2f}" if r["ran_jobs"] else "-"
        tally = {v: 0 for v in ("MISS", "pre-existing", "flake")}
        for f in r["skipped_failures"]:
            tally[f["verdict"]] += 1
        lines.append(
            f"{head}  ran {r['ran_jobs']} jobs / {r['ran_steps']} steps  "
            f"shadow keeps {r['kept_jobs']} jobs / {r['kept_steps']} steps ({ratio})"
            + ("  RUN_ALL" if r["run_all"] else "")
            + f"  skipped-failures MISS {tally['MISS']}, pre-existing "
            f"{tally['pre-existing']}, flake {tally['flake']}"
        )
        for f in r["skipped_failures"]:
            shard = f"#{f['shard']}" if f["shard"] is not None else ""
            lines.append(
                f"    {f['verdict']:<12} {f['step_key']}{shard}  {f['why']}".rstrip()
            )
    scored = [r for r in rows if "skip" not in r]
    if scored:
        ran = sum(r["ran_jobs"] for r in scored)
        kept = sum(r["kept_jobs"] for r in scored)
        miss = sum(r["miss"] for r in scored)
        lines.append(
            f"\nTOTAL over {len(scored)} builds: ran {ran} jobs, shadow keeps {kept}"
            + (f" ({kept / ran:.3f}x)" if ran else "")
            + f"; {miss} MISS, each still to be judged"
        )
    return "\n".join(lines)


def add_args(parser: argparse.ArgumentParser) -> None:
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--builds", type=int, nargs="+", help="PR build numbers")
    which.add_argument("--prs", type=int, nargs="+", help="vLLM PR numbers")
    parser.add_argument("--org", default="vllm")
    parser.add_argument("--pipeline", default="ci")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument(
        "--repo", type=Path, help="vLLM checkout to read base commit times from"
    )
    parser.add_argument(
        "--window-hours",
        type=int,
        default=DEFAULT_WINDOW_HOURS,
        help="how far either side of the base commit a main failure counts",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path.home() / ".cache" / "vllm-ci-selector" / "shadow",
        help="where settled days of main builds are kept",
    )
    parser.add_argument("--rate", type=float, default=140.0, help="API calls/minute")


def run(args, transport: Transport | None = None) -> int:
    if transport is None:
        token = (
            os.environ.get("BUILDKITE_TOKEN")
            or os.environ.get("BK_TOKEN")
            or os.environ.get("BUILDKITE_API_TOKEN")
        )
        if not token:
            print(
                "set BUILDKITE_TOKEN or BK_TOKEN (read_builds, read_artifacts)",
                file=sys.stderr,
            )
            return 2
        transport = HttpTransport(token, Throttle(args.rate))
    pipeline_url = f"{API}/organizations/{args.org}/pipelines/{args.pipeline}"
    history = MainHistory(transport, pipeline_url, args.cache)

    numbers: list[tuple[int | None, int | None]] = []
    if args.builds:
        numbers = [(n, None) for n in args.builds]
    else:
        numbers = [
            (_latest_build_for_pr(transport, pipeline_url, pr), pr) for pr in args.prs
        ]

    rows = []
    for number, pr in numbers:
        if number is None:
            rows.append({"build": None, "pr": pr, "skip": "no finished build found"})
            continue
        build_url = f"{pipeline_url}/builds/{number}"
        build = transport.get_json(build_url)
        if build is None:
            rows.append({"build": number, "pr": pr, "skip": "no such build"})
            continue
        selection = shadow_selection(transport, build_url)
        base = (selection or {}).get("base")
        base_time = commit_time(base, args.repo) if base else None
        row = score_build(build, selection, history, base_time, args.window_hours)
        if pr and not row.get("pr"):
            row["pr"] = pr
        rows.append(row)

    print(render(rows))
    if args.json_out:
        args.json_out.write_text(json.dumps(rows, indent=1))
    return 0
