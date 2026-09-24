# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""fnrec_pytest: every pytest run's outcome, written next to the recordings.

The table builder has to know whether a job's tests ran. A job whose tests all
skipped recorded little more than its imports, and its row must not be read as
coverage. Offline that comes off the Buildkite log (coverage/joblog.py). The
collect step at the end of a recording build has no API token and so no logs,
so this plugin writes the two lines of the log that parser reads, in pytest's
own format, and the same parser reads them:

    collected 12 items                               end of collection
    ========== 10 passed, 2 skipped in 3.21s ==========  end of the session

to $FNREC_OUT/pytest.<pid>.txt. The collection line goes out on its own and
first, so a run killed before its summary leaves exactly the shape joblog calls
`summary_unparsed`, which keeps the row thin, as a killed run should.

Loaded by a `pytest11` entry point that install.py or host_install.py writes
next to fnrec.py, so every pytest the image's interpreter starts loads it and nothing
else does. Inert without FNREC_OUT and in an xdist worker (the controller
reports for it). Only the standard library at import, and nothing here may
raise into pytest: a plugin that fails breaks the step it was meant to watch.
"""

from __future__ import annotations

import os
import time

# pytest's order in its own summary line.
_ORDER = ("failed", "passed", "skipped", "deselected", "xfailed", "xpassed", "error")


def _category(report) -> str | None:
    """The summary bucket pytest itself would put this report in."""
    if hasattr(report, "wasxfail"):
        if report.skipped:
            return "xfailed"
        if report.passed:
            return "xpassed"
    if report.when == "call":
        return report.outcome
    if report.failed:
        return "error"
    if report.skipped:
        return "skipped"
    return None


def _word(key: str, n: int) -> str:
    return "errors" if key == "error" and n != 1 else key


class _Recorder:
    def __init__(self, path: str):
        self.path = path
        self.start = time.time()
        self.counts = dict.fromkeys(_ORDER, 0)

    def _write(self, line: str) -> None:
        try:
            new = not os.path.exists(self.path)
            with open(self.path, "a") as fh:
                fh.write(line + "\n")
            if new:
                # Root writes here under the docker plugin; the agent user has
                # to be able to clean the checkout afterwards.
                os.chmod(self.path, 0o666)
        except Exception:
            pass

    def pytest_deselected(self, items):
        try:
            self.counts["deselected"] += len(items)
        except Exception:
            pass

    def pytest_collection_finish(self, session):
        try:
            # pytest counts before deselection; session.items is after it.
            n = len(session.items) + self.counts["deselected"]
            self._write(f"collected {n} item{'' if n == 1 else 's'}")
        except Exception:
            pass

    def pytest_runtest_logreport(self, report):
        try:
            key = _category(report)
            if key in self.counts:
                self.counts[key] += 1
        except Exception:
            pass

    def pytest_sessionfinish(self, session, exitstatus):
        try:
            parts = [f"{n} {_word(k, n)}" for k in _ORDER if (n := self.counts[k])]
            body = ", ".join(parts) or "no tests ran"
            elapsed = time.time() - self.start
            self._write(f"{'=' * 10} {body} in {elapsed:.2f}s {'=' * 10}")
        except Exception:
            pass


def pytest_configure(config):
    try:
        directory = os.environ.get("FNREC_OUT")
        if not directory or hasattr(config, "workerinput"):
            return
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"pytest.{os.getpid()}.txt")
        # One recorder per session: a test that calls pytest.main in-process
        # gets its own counts, appended to the same file as its own summary.
        config.pluginmanager.register(_Recorder(path), f"fnrec-recorder-{id(config)}")
    except Exception:
        pass
