# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Record the exact pytest node IDs and outcomes in a trace pilot run."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_STATE: dict[str, Any] = {"collected": [], "outcomes": {}}


def _collector_warning(component: str, error: Exception) -> None:
    print(
        f"test-selection collector {component} failed: {type(error).__name__}: {error}",
        file=sys.stderr,
    )


def _allocate_invocation_id() -> str | None:
    """Allocate a unique pytest invocation id when the launcher shim didn't.

    The collector's PATH-shadowing shim allocates for bare `pytest` calls;
    harnesses invoking `python -m pytest` bypass it entirely. Without an id
    those exports write the unsuffixed "direct" node file and can overwrite
    each other. Same mkdir-lock counter protocol as the shim, so the
    started/exported accounting holds for every invocation path.
    """

    node_file = os.environ.get("VLLM_CI_TEST_SELECTION_NODEIDS")
    if not node_file:
        return None
    if os.environ.get("VLLM_CI_TEST_SELECTION_PYTEST_INVOCATION"):
        return None
    counter_file = Path(node_file + ".invocations")
    lock_dir = Path(str(counter_file) + ".lock")
    import time

    for _attempt in range(500):
        try:
            lock_dir.mkdir()
            break
        except FileExistsError:
            time.sleep(0.01)
    else:
        _collector_warning(
            "invocation id allocation", RuntimeError("lock timeout")
        )
        return None
    try:
        count = 0
        if counter_file.is_file():
            try:
                count = int(counter_file.read_text(encoding="utf-8").strip())
            except ValueError:
                _collector_warning(
                    "invocation id allocation",
                    RuntimeError("invalid pytest invocation counter"),
                )
                return None
        temporary = counter_file.with_suffix(counter_file.suffix + f".tmp.{os.getpid()}")
        temporary.write_text(f"{count + 1}\n", encoding="utf-8")
        temporary.replace(counter_file)
        return f"{count:03d}"
    finally:
        try:
            lock_dir.rmdir()
        except OSError:
            pass


def pytest_configure(config: Any) -> None:
    allocated = _allocate_invocation_id()
    if allocated is not None:
        os.environ["VLLM_CI_TEST_SELECTION_PYTEST_INVOCATION"] = allocated


def pytest_collection_finish(session: Any) -> None:
    _STATE["collected"] = sorted(item.nodeid for item in session.items)


def pytest_runtest_logreport(report: Any) -> None:
    outcomes = _STATE["outcomes"].setdefault(report.nodeid, {})
    outcomes[report.when] = report.outcome


def _node_output_path(output: str) -> Path:
    path = Path(output)
    invocation = os.environ.get("VLLM_CI_TEST_SELECTION_PYTEST_INVOCATION")
    if invocation:
        path = path.with_name(f"{path.stem}.{invocation}{path.suffix}")
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker:
        path = path.with_name(f"{path.stem}.{worker}{path.suffix}")
    return path


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    output = os.environ.get("VLLM_CI_TEST_SELECTION_NODEIDS")
    if not output:
        return

    try:
        document = {
            "collected": _STATE["collected"],
            "exit_status": int(exitstatus),
            "outcomes": {
                nodeid: _STATE["outcomes"][nodeid]
                for nodeid in sorted(_STATE["outcomes"])
            },
        }
        path = _node_output_path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except Exception as error:
        _collector_warning("node/outcome export", error)
