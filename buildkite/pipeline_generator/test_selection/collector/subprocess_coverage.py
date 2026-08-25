# SPDX-License-Identifier: Apache-2.0

"""Subprocess coverage capture for shell-harness test jobs.

Shell-harness jobs (PD-accuracy, disaggregated serving) run the code under
test in `vllm serve` subprocesses, which a pytest plugin can never see. This
module implements the coverage.py subprocess recipe so every Python
interpreter below the job shell records coverage:

- `enable()` installs a `.pth` file into the first writable site-packages;
  the `.pth` line imports this module and calls `boot()` at interpreter
  startup;
- `boot()` is a no-op unless `COVERAGE_PROCESS_START` is set, in which case
  `coverage.process_startup()` starts coverage with the rc settings
  (parallel=true, sigterm=true);
- the caller owns `COVERAGE_FILE` (the run_trace per-command shard) and
  combines the parallel data files after the command finishes.

Serve-side rows carry the static `harness-subprocess` context; pytest client
rows stack as `harness-subprocess|<nodeid>|<phase>` when the hook is active
under pytest-cov. `run_trace.coverage_rows` maps both shapes.

Only enroll jobs on ephemeral runtimes (elastic EC2 / Kubernetes pod): the
`.pth` lives in site-packages and must not leak onto persistent CI hosts.
"""

from __future__ import annotations

import json
import re
import site
import sys
import sysconfig
from pathlib import Path

CONTEXT_LABEL = "harness-subprocess"

RC_TEXT = """\
# coverage.py configuration for CI test-selection subprocess capture.
# Installed per traced command by ci_test_selection.subprocess_coverage.
[run]
# Per-process unique data file suffixes (hostname.pid.random); combined later.
parallel = true
# Flush on SIGTERM so harness teardown (pkill -TERM "vllm serve") does not
# discard server-side evidence. SIGKILL can never flush.
sigterm = true
concurrency = thread,multiprocessing
# Serve-side rows carry this static context; pytest client rows stack as
# harness-subprocess|<nodeid>|<phase> when pytest-cov piggybacks on the
# already-active coverage instance.
context = harness-subprocess
source =
    vllm
"""

RC_NAME = "coverage_subprocess.rc"
PTH_NAME = "zz_vllm_ci_subprocess_coverage.pth"
HOOK_STATE_NAME = "subprocess-hook.json"


def _package() -> str:
    return __package__ or "ci_test_selection"


def _pth_line() -> str:
    return f"import {_package()}.subprocess_coverage as _vss; _vss.boot()\n"


def _site_packages() -> list[Path]:
    candidates: list[Path] = []
    try:
        candidates.extend(Path(p) for p in site.getsitepackages())
    except Exception:
        pass
    purelib = sysconfig.get_paths().get("purelib")
    if purelib:
        candidates.append(Path(purelib))
    seen: set[Path] = set()
    unique = []
    for path in candidates:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def write_rc(directory: Path) -> Path:
    """Materialize the rc into the command's output dir and return its path.

    Never write next to this module: in a source checkout that pollutes the
    tree (and the pristine-checkout proof). The caller owns the location.
    """

    target = directory / RC_NAME
    target.write_text(RC_TEXT, encoding="utf-8")
    return target


def baseline_file_name(image_digest: str) -> str:
    """The bundled worktree-shape baseline for an image digest."""

    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest or ""):
        raise ValueError(f"invalid image digest reference: {image_digest!r}")
    return f"worktree-baseline-{image_digest.split(':', 1)[1][:12]}.json"


def _write_state(output_dir: Path, document: dict) -> None:
    try:
        (output_dir / HOOK_STATE_NAME).write_text(
            json.dumps(document, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def enable(output_dir: Path) -> bool:
    """Install the auto-start hook. Always writes a state sentinel.

    Returns True when the hook was installed. A missing coverage module or
    unwritable site-packages records `installed: false` with the reason, so
    the collector can distinguish "hook skipped" from "hook ran, no data".
    """

    try:
        import coverage  # noqa: F401
    except ImportError:
        _write_state(output_dir, {"installed": False, "reason": "no coverage"})
        print("test-selection: coverage not installed; subprocess hook skipped",
              file=sys.stderr)
        return False
    for directory in _site_packages():
        target = directory / PTH_NAME
        try:
            if not directory.is_dir():
                continue
            target.write_text(_pth_line(), encoding="utf-8")
            _write_state(
                output_dir, {"installed": True, "path": str(target)}
            )
            print(f"test-selection: subprocess coverage hook at {target}")
            return True
        except OSError:
            continue
    _write_state(
        output_dir,
        {"installed": False, "reason": "no writable site-packages"},
    )
    print("test-selection: no writable site-packages; hook skipped",
          file=sys.stderr)
    return False


def disable() -> bool:
    """Remove the auto-start hook (idempotent)."""

    removed = False
    for directory in _site_packages():
        target = directory / PTH_NAME
        try:
            if target.is_file():
                target.unlink()
                removed = True
        except OSError:
            pass
    return removed


def boot() -> None:
    """`.pth` entry point: start coverage when the job env requests it.

    Also writes a per-process receipt (`subprocess-hook-ran.<pid>.json`)
    identifying the interpreter that actually started, so the collector can
    prove a `vllm serve` runtime was hooked — and read `sys.executable` /
    `sys.prefix` directly instead of inferring site-packages mismatches.
    """

    import os

    if not os.environ.get("COVERAGE_PROCESS_START"):
        return
    marker_dir = os.environ.get("VLLM_CI_TEST_SELECTION_HOOK_DIR")
    if marker_dir:
        try:
            import time

            document = {
                "argv": sys.argv,
                "executable": sys.executable,
                "pid": os.getpid(),
                "prefix": sys.prefix,
                "time": time.time(),
            }
            path = Path(marker_dir) / f"subprocess-hook-ran.{os.getpid()}.json"
            path.write_text(
                json.dumps(document, sort_keys=True) + "\n", encoding="utf-8"
            )
        except Exception:
            pass
    try:
        import coverage

        coverage.process_startup()
    except Exception:
        # Never break the process under test because of instrumentation.
        pass
