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


def pytest_collection_finish(session: Any) -> None:
    _STATE["collected"] = sorted(item.nodeid for item in session.items)


def pytest_runtest_logreport(report: Any) -> None:
    outcomes = _STATE["outcomes"].setdefault(report.nodeid, {})
    outcomes[report.when] = report.outcome


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
        path = Path(output)
        worker = os.environ.get("PYTEST_XDIST_WORKER")
        if worker:
            path = path.with_name(f"{path.stem}.{worker}{path.suffix}")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except Exception as error:
        _collector_warning("node/outcome export", error)
