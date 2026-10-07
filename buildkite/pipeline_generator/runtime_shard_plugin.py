"""pytest plugin for one parallel job of a runtime-sharded step.

The pipeline generator planned the step's shards (runtime_shard.py) and put
the plan in RUNTIME_SHARD_PLAN; each parallel job runs the step's pytest
commands unchanged, with this plugin loaded (`-p runtime_shard_plugin`). In a
command the plan covers, it:

  - skips collecting the files planned for other shards,
  - keeps this job's tests: its files, and its part of each file split by
    test. A collected file the plan doesn't name (one the generator could
    not see) runs in the first shard, so no test can drop out of every shard,
  - opens a Buildkite log group per file, opens the group of a file that
    fails, and puts the failures and summary in their own open group,
  - turns "no tests ran" into success when every test went to other shards.

Each job collects the same tests, so the parts of a split file add up to the
file. Elsewhere (a setup command's pytest, a pytest a test starts) it does
nothing. Standard library and pytest only.
"""

import base64
import json
import os
import sys
import zlib

import pytest

PLAN_ENV = "RUNTIME_SHARD_PLAN"


def pytest_configure(config):
    # Popped, so a pytest that a test itself starts runs as written.
    packed = os.environ.pop(PLAN_ENV, None)
    if not packed:
        return
    plan = json.loads(zlib.decompress(base64.b64decode(packed)))
    args = list(config.invocation_params.args)
    for command in plan["commands"]:
        if command["args"] == args:
            shard = int(os.environ.get("BUILDKITE_PARALLEL_JOB", "0"))
            config.pluginmanager.register(
                ShardPlugin(command, shard, plan["shards"]), "runtime_shard_job"
            )
            return


class ShardPlugin:
    def __init__(self, command, shard, shards):
        self.files = command["files"]  # file from rootdir -> its shard
        self.split = command["split"]  # file -> {"shards", "assign"}
        self.prefix = command["prefix"]
        self.label = command["label"]
        self.shard = shard
        self.shards = shards
        self.reporter = None
        self.counts = {}  # file -> its tests here
        self.totals = {}  # file -> its tests in all shards
        self.unplanned = []
        self.dropped = 0
        self.current = None
        self.expanded = False

    def pytest_ignore_collect(self, collection_path, config):
        """A file planned whole for another shard isn't even imported."""
        relative = os.path.relpath(str(collection_path), str(config.rootpath))
        shard = self.files.get(relative.replace(os.sep, "/"))
        if shard is not None and shard != self.shard:
            self.dropped += 1
            return True
        return None

    def _owner(self, item, file):
        """The shard of one collected test."""
        if file in self.split:
            split = self.split[file]
            part = split["assign"].get(item.nodeid)
            if part is None:  # a test main hasn't timed: its function's part
                function = item.nodeid.split("[")[0]
                for nodeid, known in split["assign"].items():
                    if nodeid.split("[")[0] == function:
                        part = known
                        break
            if part is None:  # a new function: a part by its name, cases together
                part = zlib.crc32(function.encode()) % len(split["shards"])
            return split["shards"][str(part)]
        if file in self.files:
            return self.files[file]
        if file not in self.unplanned:
            self.unplanned.append(file)
        return 0

    @pytest.hookimpl(trylast=True)  # after -m, -k and conftest hooks
    def pytest_collection_modifyitems(self, session, config, items):
        keep, drop = [], []
        for item in items:
            file = item.nodeid.split("::")[0]
            self.totals[file] = self.totals.get(file, 0) + 1
            if self._owner(item, file) == self.shard:
                keep.append(item)
                self.counts[file] = self.counts.get(file, 0) + 1
            else:
                drop.append(item)
        if drop:
            self.dropped += len(drop)
            config.hook.pytest_deselected(items=drop)
            items[:] = keep

    def pytest_collection_finish(self, session):
        if session.config.getoption("collectonly"):
            return
        # Looked up here: it registers after -p plugins are configured.
        self.reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if self.reporter is None:
            return
        tests = sum(self.counts.values())
        self._say(
            f"runtime-shard: shard {self.shard + 1}/{self.shards} runs {tests} "
            f"of {sum(self.totals.values())} tests in {len(self.counts)} files"
        )
        if self.unplanned and self.shard == 0:
            self._say(
                "runtime-shard: not in the plan, so run in shard 1: "
                + ", ".join(self.unplanned)
            )

    def _say(self, line):
        # Buildkite reads a group header only at the start of a line.
        self.reporter.ensure_newline()
        self.reporter.write_line(line)
        sys.stdout.flush()

    @pytest.hookimpl(tryfirst=True)  # before -v names the test
    def pytest_runtest_logstart(self, nodeid, location):
        file = nodeid.split("::")[0]
        if self.reporter is None or file == self.current:
            return
        self.current, self.expanded = file, False
        shown = file[len(self.prefix) :] if file.startswith(self.prefix) else file
        number = list(self.counts).index(file) + 1 if file in self.counts else 0
        # Collapsed, so a shard's log is one line per file until opened.
        title = (
            f"--- :test_tube: {self.label}, file {number}/{len(self.counts)}: {shown}"
        )
        if self.counts.get(file, 0) < self.totals.get(file, 0):
            title += f"   ({self.counts[file]} of {self.totals[file]} tests)"
        self._say(title)

    @pytest.hookimpl(trylast=True)  # after -v prints FAILED or ERROR
    def pytest_runtest_logreport(self, report):
        if self.reporter is not None and report.failed and not self.expanded:
            self.expanded = True
            self._say("^^^ +++")  # Buildkite: open this file's group

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtestloop(self, session):
        yield
        if self.reporter is not None and self.current is not None:
            # The failures and the summary get their own open group, not the
            # last file's.
            self._say(f"+++ :test_tube: {self.label}: results")

    def pytest_sessionfinish(self, session, exitstatus):
        # Every test of the command went to other shards: nothing to run here
        # is not a failure. A command that collects nothing at all still is.
        if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED and self.dropped:
            session.exitstatus = pytest.ExitCode.OK
