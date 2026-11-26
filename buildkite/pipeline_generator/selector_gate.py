"""Let the test selector (buildkite/ci_selector) choose a PR build's steps.

On by default. `VLLM_CI_SELECTOR` overrides it for one build:

  on      (the default) the selector's step keys replace the
          source_file_dependencies rules, for every step but AMD ones
          (native and `amd-` mirrors), which keep those rules
  shadow  the selector runs and its decision is recorded; nothing changes
  off     the selector does not run; today's rules, and no record

PR, branch and main builds are selected; a main build diffs against its
parent commit, as today's rules do there. Release-branch, nightly, AITER
nightly, run-all (the scheduled full CI runs), docs-only and AMD-only
(`VLLM_CI_PLATFORM=amd`) builds, and builds that already name their steps
(`VLLM_CI_ONLY_STEP_KEYS`),
keep today's rules. Any failure keeps them too: the selector cannot be fetched or run, it
times out, it declines to name steps for another reason, or it names a step
this generator does not know. When it declines because it cannot narrow the
diff at all (its own fail-open, "run-all: ..."), every non-optional step
runs instead, as a run_all_patterns match would; AMD keeps today's rules.
The selector never makes a build fail.

Every armed build writes one JSON record, `ci_selector_decision.json`, and
uploads it as a build artifact, so the rollout can be evaluated afterwards from
the Buildkite API alone: what the selector chose, what today's rules would
have run, what was scheduled, and which recording it decided from.

Python 3.9: the generator runs on the small CPU queue's python3. The selector
needs 3.12 or later and its own dependencies, so it runs through uv from a
shallow checkout of ci-infra at `VLLM_CI_BRANCH`, as the collect steps do,
under the Python that made the function record (`record_python`).
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
import zlib
from typing import Any, Callable, Dict, FrozenSet, Iterable, Iterator, List, Optional

SELECTOR_ENV_VAR = "VLLM_CI_SELECTOR"
DECISION_FILE = "ci_selector_decision.json"
RECORD_VERSION = 1
CI_INFRA_REPO = "https://github.com/vllm-project/ci-infra.git"
RECORD_URL = (
    "https://vllm-ci-selector.s3.us-west-2.amazonaws.com/ci/{recorder}/latest.json"
)
# Tells ci-select which function table to download instead of following
# latest.json itself (ci_selector/scripts/fetch_functions.py reads it).
FNREC_COMMIT_ENV = "CI_SELECTOR_FUNCTION_RECORD_COMMIT"
TABLE_URL = "https://vllm-ci-selector.s3.us-west-2.amazonaws.com/ci/fnrec/{commit}/table.json.gz"
# ci-select reads a function record only under the Python minor that recorded
# it: names differ across minors (3.14 builds annotations in `__annotate__`
# functions, PEP 649), so on any other it drops nothing. It runs under the
# record's own Python, read off the table; this one when the table does not
# say, which is the oldest ci-select supports.
DEFAULT_SELECTOR_PYTHON = "3.12"
# Every row's stamp names its interpreters, and the first row sits a few KB
# into the table, so the head of the download is enough.
_INTERPRETER = re.compile(rb'"interpreters":\s*\[\s*"(3\.\d+)\.\d+')
_TABLE_HEAD_BYTES = 1 << 16
# The whole selector run, fetches included. Past it the build keeps today's
# rules rather than wait.
SELECTOR_TIMEOUT_SECONDS = 20 * 60


def mode() -> str:
    """ "on" (the default), "shadow", or "" for off. An unknown value reads as
    the default, so a typo cannot quietly switch selection off."""
    value = (os.getenv(SELECTOR_ENV_VAR) or "on").strip().lower()
    if value == "off":
        return ""
    return value if value in ("on", "shadow") else "on"


def skip_reason(
    global_config: Dict[str, Any], base: Optional[str] = None
) -> Optional[str]:
    """Why this build keeps today's rules without asking the selector.
    `base` is the build's diff_base, when the caller already has it."""
    # A branch build diffs against its merge base with main, as a PR build
    # does; a main build against its parent commit. Release branches are not
    # selected.
    branch = global_config.get("branch") or ""
    if not branch:
        return "no branch"
    if branch.startswith("releases/"):
        return "release branch build"
    if global_config.get("nightly") == "1":
        return "nightly build (runs every step)"
    if global_config.get("aiter_nightly") == "1":
        return "AITER nightly build (runs every AMD step)"
    if global_config.get("run_all"):
        return "run-all build (label or run_all_patterns)"
    if global_config.get("only_step_keys") is not None:
        return "steps named explicitly by VLLM_CI_ONLY_STEP_KEYS"
    if global_config.get("platform") == "amd":
        return "AMD-only build (the selector does not pick AMD steps)"
    if not (base or diff_base(global_config)) or not global_config.get("commit"):
        return "no base or commit to diff"
    return None


def diff_base(global_config: Dict[str, Any]) -> Optional[str]:
    """The commit this build's diff starts from. On main the merge base is the
    build's own commit, so it is the parent instead, which is what the
    generator's own list_file_diff uses there (HEAD~1)."""
    if global_config.get("branch") != "main":
        return global_config.get("merge_base_commit")
    commit = global_config.get("commit")
    if not commit:
        return None
    try:
        out = subprocess.run(
            ["git", "rev-parse", f"{commit}^"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def base_record(global_config: Dict[str, Any], gate_mode: str) -> Dict[str, Any]:
    return {
        "version": RECORD_VERSION,
        "mode": gate_mode,
        "build_number": os.getenv("BUILDKITE_BUILD_NUMBER"),
        "build_url": os.getenv("BUILDKITE_BUILD_URL"),
        "pipeline": global_config.get("name"),
        "pull_request": global_config.get("pull_request"),
        "branch": global_config.get("branch"),
        "base": diff_base(global_config),
        "head": global_config.get("commit"),
        "ci_infra_branch": os.getenv("VLLM_CI_BRANCH") or "main",
        "changed_files": list(global_config.get("list_file_diff") or []),
        "skipped": None,
        "applied": False,
        "fallback": None,
        "selector": None,
        "selector_keys": None,
        "explain": None,
        "legacy_keys": None,
        # Steps added by `/ci run --include` (VLLM_CI_INCLUDE_STEP_KEYS) with
        # their prerequisites: scheduled, but in neither the selector's keys
        # nor today's rules.
        "included_keys": None,
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
    why = skip_reason(global_config, record["base"])
    if why:
        record["skipped"] = why
        return record
    run_selector = run_selector or run_ci_select
    started = time.time()
    try:
        result = run_selector(
            repo=os.getcwd(),
            base=record["base"],
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
    # Per step: the map's reasons and what each record did. What a miss is
    # diagnosed from later, without re-running the selector.
    record["explain"] = result.get("explain")
    record["ci_infra_commit"] = result.get("ci_infra_commit")
    record["selector_python"] = result.get("python")
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
    if record["applied"] and record.get("selector_run_all"):
        text = (
            ":dart: ci-selector cannot narrow this change, so every test step "
            f"runs: {record['selector_run_all']}"
        )
    elif record["applied"]:
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
            [
                "buildkite-agent",
                "annotate",
                text,
                "--context",
                "ci-selector",
                "--style",
                "info",
            ],
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


def _latest(recorder: str) -> Dict[str, Any]:
    """The recorder's `latest.json`: which recording to use, and the metadata
    the decision records for it. Raises if it cannot be read."""
    with urllib.request.urlopen(RECORD_URL.format(recorder=recorder), timeout=30) as r:
        pointer = json.loads(r.read())
    if not isinstance(pointer, dict) or not pointer.get("commit"):
        raise ValueError(f"{recorder} latest.json names no commit: {pointer!r}")
    return pointer


def record_python(commit: str) -> str:
    """The Python minor that made the function record at `commit`, as
    `ci-select` must run it: read from the first row of the table, which takes
    the first few KB of a ~130 MB download. DEFAULT_SELECTOR_PYTHON when the
    table cannot be read or names a Python ci-select does not support.

    vLLM's CI moved to Python 3.14 (vllm#68457), and the records with it. A
    gate pinned to 3.12 read every row as another Python's and dropped nothing
    on coverage evidence: build 105155 (vllm#69001) ran 35 test steps for a
    GLM5-Next change, 32 of them only through the model registry's imports."""
    head = b""
    try:
        with urllib.request.urlopen(TABLE_URL.format(commit=commit), timeout=30) as r:
            gz = zlib.decompressobj(16 + zlib.MAX_WBITS)
            while len(head) < _TABLE_HEAD_BYTES:
                chunk = r.read(8192)
                if not chunk:
                    break
                head += gz.decompress(chunk)
    except (OSError, ValueError, zlib.error):
        return DEFAULT_SELECTOR_PYTHON
    m = _INTERPRETER.search(head)
    if not m:
        return DEFAULT_SELECTOR_PYTHON
    minor = m.group(1).decode()
    supported = tuple(int(x) for x in minor.split(".")) >= tuple(
        int(x) for x in DEFAULT_SELECTOR_PYTHON.split(".")
    )
    return minor if supported else DEFAULT_SELECTOR_PYTHON


def run_ci_select(repo: str, base: str, head: str, pipeline: str) -> Dict[str, Any]:
    """Run `ci-select --emit-keys` on base...head with the latest published
    recordings; returns the emission, the recordings' metadata and the log.

    Each recording's pointer is read once, first, and every download is pinned
    to it, so the record names what was loaded even if a publish lands mid-run.
    """
    deadline = time.time() + SELECTOR_TIMEOUT_SECONDS
    left = lambda: max(30.0, deadline - time.time())  # noqa: E731
    records = {"fnrec": _latest("fnrec"), "kernrec": _latest("kernrec")}
    workdir = tempfile.mkdtemp(prefix="ci-selector-")
    branch = os.getenv("VLLM_CI_BRANCH") or "main"
    checkout = os.path.join(workdir, "ci-infra")
    _run(
        [
            "git",
            "clone",
            "--quiet",
            "--depth",
            "1",
            "--branch",
            branch,
            CI_INFRA_REPO,
            checkout,
        ],
        timeout=left(),
    )
    ci_infra_commit = _run(["git", "-C", checkout, "rev-parse", "HEAD"]).strip()
    project = os.path.join(checkout, "buildkite", "ci_selector")
    uv = _uv(workdir)
    python = record_python(records["fnrec"]["commit"])
    run = [uv, "run", "--quiet", "--no-dev", "--python", python, "--project", project]
    data = os.path.join(workdir, "data")
    explain_path = os.path.join(workdir, "explain.json")
    _run(
        run
        + [
            "ci-fetch-kernel-record",
            "--commit",
            records["kernrec"]["commit"],
            "--out",
            data,
        ],
        timeout=left(),
    )
    # No function table is passed: ci-select downloads the pinned one itself
    # and parses it once (about 1 GB of JSON), instead of a separate fetch
    # step that parsed it a first time only to check it.
    out = subprocess.run(
        run
        + [
            "ci-select",
            "--repo",
            repo,
            "--diff",
            f"{base}...{head}",
            "--emit-keys",
            "--pipeline",
            pipeline,
            "--kernel-table",
            os.path.join(data, "kernel_table.json.gz"),
            "--kernel-symbol-map",
            os.path.join(data, "kernel_symbol_map.json.gz"),
            "--explain",
            explain_path,
        ],
        env={**os.environ, FNREC_COMMIT_ENV: records["fnrec"]["commit"]},
        capture_output=True,
        text=True,
        timeout=left(),
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ci-select exited {out.returncode}: {out.stderr[-1500:]}")
    explain = None
    try:
        with open(explain_path) as f:
            explain = json.load(f)
    except (OSError, ValueError):
        pass  # informational; the decision stands without it
    return {
        "emission": json.loads(out.stdout),
        "explain": explain,
        "records": records,
        "log": out.stderr[-4000:],
        "ci_infra_commit": ci_infra_commit,
        "python": python,
    }


def _keyed_steps(steps: Iterable[Any]) -> Iterator[Any]:
    """Each keyed step, then its AMD mirror as the generator decides whether
    to run it, so a mirror's own fields (its optional) are the ones read."""
    from buildkite_step import amd_mirror_step

    for step in steps:
        if not step.key:
            continue
        yield step
        mirror = amd_mirror_step(step)
        if mirror is not None:
            yield mirror


def known_keys(steps: Iterable[Any]) -> FrozenSet[str]:
    """Every key the generator can schedule: step keys and AMD mirror keys."""
    return frozenset(step.key for step in _keyed_steps(steps))


def amd_keys(steps: Iterable[Any]) -> FrozenSet[str]:
    """Keys of AMD steps: native ones and the `amd-` mirrors. These keep the
    source_file_dependencies rules; the selector decides the rest."""
    from amd import is_amd_device

    return frozenset(
        step.key for step in _keyed_steps(steps) if is_amd_device(step.device)
    )


def optional_keys(steps: Iterable[Any]) -> FrozenSet[str]:
    """Keys of optional steps and mirrors: a PR build leaves these to a
    manual unblock or the nightly, whatever the selector says. A mirror's
    optional may differ from its step's."""
    return frozenset(step.key for step in _keyed_steps(steps) if step.optional)
