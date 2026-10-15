"""Let the test selector (buildkite/ci_selector) choose a PR build's steps.

Off unless `VLLM_CI_SELECTOR` is set:

  on      the selector's step keys replace the source_file_dependencies rules
  shadow  the selector runs and its decision is recorded; nothing changes

Only PR and branch builds are selected. Main, release-branch, nightly, run-all and docs-only builds, and
builds that already name their steps (`VLLM_CI_ONLY_STEP_KEYS`), keep today's
rules. Any failure keeps them too: the selector cannot be fetched or run, it
times out, it declines to name steps (its own fail-open), or it names a step
this generator does not know. The selector never makes a build fail.

Every armed build writes one JSON record, `ci_selector_decision.json`, and
uploads it as a build artifact, so the rollout can be evaluated afterwards from
the Buildkite API alone: what the selector chose, what today's rules would
have run, what was scheduled, and which recording it decided from.

Python 3.9: the generator runs on the small CPU queue's python3. The selector
needs 3.12 and its own dependencies, so it runs through uv from a shallow
checkout of ci-infra at `VLLM_CI_BRANCH`, as the collect steps do.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional

SELECTOR_ENV_VAR = "VLLM_CI_SELECTOR"
DECISION_FILE = "ci_selector_decision.json"
RECORD_VERSION = 1
CI_INFRA_REPO = "https://github.com/vllm-project/ci-infra.git"
RECORD_URL = "https://vllm-ci-selector.s3.us-west-2.amazonaws.com/ci/{recorder}/latest.json"
# The whole selector run, fetches included. Past it the build keeps today's
# rules rather than wait.
SELECTOR_TIMEOUT_SECONDS = 20 * 60


def mode() -> str:
    """"on", "shadow", or "" (off)."""
    value = (os.getenv(SELECTOR_ENV_VAR) or "").strip().lower()
    return value if value in ("on", "shadow") else ""


def skip_reason(global_config: Dict[str, Any]) -> Optional[str]:
    """Why this build keeps today's rules without asking the selector."""
    # A branch build diffs against its merge base with main, as a PR build
    # does, so it is selected the same way. Main and release branches are not.
    branch = global_config.get("branch") or ""
    if not branch or branch == "main":
        return "main branch build"
    if branch.startswith("releases/"):
        return "release branch build"
    if global_config.get("nightly") == "1":
        return "nightly build (runs every step)"
    if global_config.get("run_all"):
        return "run-all build (label or run_all_patterns)"
    if global_config.get("only_step_keys") is not None:
        return "steps named explicitly by VLLM_CI_ONLY_STEP_KEYS"
    if not global_config.get("merge_base_commit") or not global_config.get("commit"):
        return "no merge base or commit to diff"
    return None


def base_record(global_config: Dict[str, Any], gate_mode: str) -> Dict[str, Any]:
    return {
        "version": RECORD_VERSION,
        "mode": gate_mode,
        "build_number": os.getenv("BUILDKITE_BUILD_NUMBER"),
        "build_url": os.getenv("BUILDKITE_BUILD_URL"),
        "pipeline": global_config.get("name"),
        "pull_request": global_config.get("pull_request"),
        "branch": global_config.get("branch"),
        "base": global_config.get("merge_base_commit"),
        "head": global_config.get("commit"),
        "ci_infra_branch": os.getenv("VLLM_CI_BRANCH") or "main",
        "changed_files": list(global_config.get("list_file_diff") or []),
        "skipped": None,
        "applied": False,
        "fallback": None,
        "selector": None,
        "selector_keys": None,
        "legacy_keys": None,
        "scheduled_keys": None,
        "records": None,
        "seconds": None,
    }


def decide(
    global_config: Dict[str, Any],
    legacy_keys: Iterable[str],
    run_selector: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """The record for this build; sets `selector_keys` when the selector
    named steps. The caller applies them, and records what it scheduled."""
    gate_mode = mode()
    record = base_record(global_config, gate_mode)
    record["legacy_keys"] = sorted(set(legacy_keys))
    why = skip_reason(global_config)
    if why:
        record["skipped"] = why
        return record
    run_selector = run_selector or run_ci_select
    started = time.time()
    try:
        result = run_selector(
            repo=os.getcwd(),
            base=global_config["merge_base_commit"],
            head=global_config["commit"],
            pipeline=global_config["name"],
        )
    except Exception as error:  # noqa: BLE001 - any failure keeps today's rules
        record["fallback"] = f"selector failed: {type(error).__name__}: {error}"[:2000]
        return record
    finally:
        record["seconds"] = round(time.time() - started, 1)
    record["selector"] = result.get("emission")
    record["records"] = result.get("records")
    record["selector_log"] = result.get("log")
    record["ci_infra_commit"] = result.get("ci_infra_commit")
    emission = result.get("emission") or {}
    if emission.get("omit", True) or not emission.get("keys"):
        record["fallback"] = "selector declined: " + (
            emission.get("reason") or "no keys"
        )
        return record
    record["selector_keys"] = sorted(emission["keys"])
    return record


def write_and_upload(record: Dict[str, Any], output_dir: str) -> str:
    """Write the record next to the pipeline and upload it as an artifact.
    Upload failures are printed, never raised."""
    path = os.path.join(output_dir or ".", DECISION_FILE)
    with open(path, "w") as f:
        json.dump(record, f, indent=1, sort_keys=True)
    print(f"ci-selector decision written to {path}")
    try:
        subprocess.run(
            ["buildkite-agent", "artifact", "upload", path], check=False, timeout=120
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"ci-selector: artifact upload skipped ({error})")
    return path


def annotate(record: Dict[str, Any]) -> None:
    if record["applied"]:
        n, m = len(record["scheduled_keys"] or []), len(record["legacy_keys"] or [])
        text = (
            f":dart: ci-selector chose {len(record['selector_keys'])} test steps "
            f"({n} scheduled with their prerequisites); today's rules: {m}."
        )
    elif record["mode"] == "shadow" and record["selector_keys"]:
        text = (
            f":dart: ci-selector (shadow) would choose "
            f"{len(record['selector_keys'])} test steps; today's rules ran "
            f"{len(record['legacy_keys'] or [])}."
        )
    else:
        text = (
            ":dart: ci-selector not applied: "
            f"{record['skipped'] or record['fallback'] or 'shadow mode'}"
        )
    try:
        subprocess.run(
            ["buildkite-agent", "annotate", text, "--context", "ci-selector",
             "--style", "info"],
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


# ---- running the selector -------------------------------------------------


def _run(cmd: List[str], cwd: Optional[str] = None, timeout: float = 600) -> str:
    out = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if out.returncode != 0:
        raise RuntimeError(
            f"{' '.join(cmd[:4])} exited {out.returncode}: {out.stderr[-1500:]}"
        )
    return out.stdout


def _uv(workdir: str) -> str:
    found = shutil.which("uv")
    if found:
        return found
    install_dir = os.path.join(workdir, "uv-bin")
    _run(
        [
            "sh",
            "-c",
            "curl -LsSf --retry 3 https://astral.sh/uv/install.sh | "
            f"env UV_UNMANAGED_INSTALL='{install_dir}' sh",
        ],
        timeout=300,
    )
    return os.path.join(install_dir, "uv")


def _latest(recorder: str) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(RECORD_URL.format(recorder=recorder), timeout=30) as r:
            return json.loads(r.read())
    except Exception as error:  # noqa: BLE001 - informational only
        return {"error": str(error)}


def run_ci_select(repo: str, base: str, head: str, pipeline: str) -> Dict[str, Any]:
    """Run `ci-select --emit-keys` on base...head with the latest published
    recordings; returns the emission, the recordings' metadata and the log."""
    deadline = time.time() + SELECTOR_TIMEOUT_SECONDS
    left = lambda: max(30.0, deadline - time.time())  # noqa: E731
    workdir = tempfile.mkdtemp(prefix="ci-selector-")
    branch = os.getenv("VLLM_CI_BRANCH") or "main"
    checkout = os.path.join(workdir, "ci-infra")
    _run(
        ["git", "clone", "--quiet", "--depth", "1", "--branch", branch,
         CI_INFRA_REPO, checkout],
        timeout=left(),
    )
    ci_infra_commit = _run(["git", "-C", checkout, "rev-parse", "HEAD"]).strip()
    project = os.path.join(checkout, "buildkite", "ci_selector")
    uv = _uv(workdir)
    run = [uv, "run", "--quiet", "--no-dev", "--python", "3.12", "--project", project]
    data = os.path.join(workdir, "data")
    _run(run + ["ci-fetch-function-record", "--out", data], timeout=left())
    _run(run + ["ci-fetch-kernel-record", "--out", data], timeout=left())
    out = subprocess.run(
        run
        + [
            "ci-select",
            "--repo", repo,
            "--diff", f"{base}...{head}",
            "--emit-keys",
            "--pipeline", pipeline,
            "--table", os.path.join(data, "table.json.gz"),
            "--kernel-table", os.path.join(data, "kernel_table.json.gz"),
            "--kernel-symbol-map", os.path.join(data, "kernel_symbol_map.json.gz"),
        ],
        capture_output=True,
        text=True,
        timeout=left(),
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ci-select exited {out.returncode}: {out.stderr[-1500:]}")
    return {
        "emission": json.loads(out.stdout),
        "records": {"fnrec": _latest("fnrec"), "kernrec": _latest("kernrec")},
        "log": out.stderr[-4000:],
        "ci_infra_commit": ci_infra_commit,
    }


def known_keys(steps: Iterable[Any]) -> FrozenSet[str]:
    """Every key the generator can schedule: step keys and AMD mirror keys."""
    keys = set()
    for step in steps:
        if step.key:
            keys.add(step.key)
            if (step.mirror or {}).get("amd"):
                keys.add(f"amd-{step.key}")
    return frozenset(keys)
