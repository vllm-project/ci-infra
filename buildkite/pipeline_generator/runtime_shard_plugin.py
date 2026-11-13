"""pytest plugin for one parallel job of a runtime-sharded step.

The pipeline generator planned the step's shards (runtime_shard.py) and put
the plan in RUNTIME_SHARD_PLAN; each parallel job runs the step's pytest
commands unchanged, with this plugin loaded (`-p runtime_shard_plugin`). In a
command the plan covers, it:

  - skips collecting the files planned for other shards,
  - keeps this job's tests: its files, and its part of each file split by
    test. A collected file the plan doesn't name (one the generator could
    not see) runs in the first shard that runs the command, so no test can
    drop out of every shard,
  - opens a Buildkite log group per file, opens the group of a file that
    fails, and puts the failures and summary in their own open group,
  - turns "no tests ran" into success when every test went to other shards,
  - records what it ran for the build's coverage check
    (runtime_shard_coverage.py): the command's first shard also records the
    whole list of tests the command should run, collected by a separate pytest
    it starts on the same worker, so its own run never imports the other
    shards' files.

Each job collects the same tests, so the parts of a split file add up to the
file. Elsewhere (a setup command's pytest, a pytest a test starts) it does
nothing. Standard library and pytest only.

Run as a script (`python3 runtime_shard_plugin.py`), it copies itself into the
site-packages of python3 and of the python that runs `pytest`, where a command
that sets its own PYTHONPATH still finds it.
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zlib

PLAN_ENV = "RUNTIME_SHARD_PLAN"
COMMAND_ENV = "RUNTIME_SHARD_COMMAND"
COVERAGE_DIR_ENV = "RUNTIME_SHARD_COVERAGE_DIR"
# The file the job's guard checks after a planned command: written once the
# plugin has applied the plan, so a pytest that ran without it fails the job.
APPLIED_ENV = "RUNTIME_SHARD_APPLIED"
# Set by the recording shard on the pytest it starts to list the command's
# tests: the path that run writes the list to.
INVENTORY_ENV = "RUNTIME_SHARD_INVENTORY"


def _pytest_python():
    """The python the `pytest` on PATH runs, from its shebang, or None."""
    script = shutil.which("pytest")
    if not script:
        return None
    try:
        with open(script) as f:
            lines = f.read(4096).splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if not lines or not lines[0].startswith("#!"):
        return None
    words = lines[0][2:].split()
    if words and os.path.basename(words[0]) == "env":
        words = words[1:]
    if words and words[0] in ("/bin/sh", "/usr/bin/sh"):
        # pip's form for long paths: #!/bin/sh, then '''exec' <python> "$0" ...
        exec_line = next(
            (line for line in lines[1:3] if line.startswith("'''exec' ")), ""
        )
        words = exec_line.split()[1:2]
    if not words or "python" not in os.path.basename(words[0]):
        return None
    return shutil.which(words[0]) or words[0]


def _install():
    """Best effort, each python on its own: a failed copy only matters to a
    command that also sets its own PYTHONPATH, so it warns."""
    # Not deduplicated by realpath: a venv's python links to the one it was
    # made from, but has its own site-packages.
    for python in dict.fromkeys(p for p in (sys.executable, _pytest_python()) if p):
        try:
            site = subprocess.run(
                [
                    python,
                    "-c",
                    "import sysconfig; print(sysconfig.get_paths()['purelib'])",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            ).stdout.strip()
            shutil.copy(os.path.abspath(__file__), site)
        except Exception as error:
            print(
                f"runtime-shard: couldn't install the plugin for {python} ({error});"
                " a command that sets its own PYTHONPATH won't find it",
                file=sys.stderr,
            )


if __name__ == "__main__":
    _install()
    sys.exit(0)

import pytest  # noqa: E402  (the install above runs without pytest)


def pytest_configure(config):
    # Popped, so a pytest that a test itself starts runs as written.
    inventory = os.environ.pop(INVENTORY_ENV, None)
    if inventory:
        # A recording shard's collecting subprocess: no shard plan here, and
        # it writes no coverage record of its own.
        config.pluginmanager.register(_Inventory(inventory), "runtime_shard_inventory")
        return
    packed = os.environ.pop(PLAN_ENV, None)
    if not packed:
        return
    plan = json.loads(zlib.decompress(base64.b64decode(packed)))
    args = _without_plugins(config.invocation_params.args)
    matches = [c for c in plan["commands"] if _without_plugins(c["args"]) == args]
    if not matches:
        return
    # Commands that differ only in env vars have the same args; the job's
    # guard exports the position of the one running.
    position = os.environ.get(COMMAND_ENV)
    command = next(
        (c for c in matches if str(c.get("position")) == position), matches[0]
    )
    shard = int(os.environ.get("BUILDKITE_PARALLEL_JOB", "0"))
    config.pluginmanager.register(
        ShardPlugin(command, shard, plan["shards"], plan.get("key")),
        "runtime_shard_job",
    )
    applied = os.environ.get(APPLIED_ENV)
    if applied:
        os.makedirs(os.path.dirname(applied), exist_ok=True)
        open(applied, "w").close()


def _without_plugins(args):
    """A command's args less its `-p` plugins.

    A wrapper may load its own plugin: vLLM's CI OTel shim runs
    `pytest -p ci_otel <args>`. That is still the planned command.
    """
    kept, args = [], list(args)
    while args:
        arg = args.pop(0)
        if arg == "-p":
            args[:1] = []
        elif not arg.startswith("-p"):
            kept.append(arg)
    return kept


class _Inventory:
    """Writes the collected tests' IDs to the path in INVENTORY_ENV: the whole
    list a recording shard's subprocess collected for its command."""

    def __init__(self, path):
        self.path = path

    @pytest.hookimpl(trylast=True)  # after -m, -k and conftest hooks
    def pytest_collection_modifyitems(self, session, config, items):
        with open(self.path, "w") as f:
            json.dump([item.nodeid for item in items], f)


class ShardPlugin:
    def __init__(self, command, shard, shards, step_key=None):
        self.files = command["files"]  # file from rootdir -> its shard
        self.split = command["split"]  # file -> {"shards", "assign"}
        self.prefix = command["prefix"]
        self.label = command["label"]
        self.fallback = command.get("fallback", 0)
        self.shard = shard
        self.shards = shards
        self.reporter = None
        self.counts = {}  # file -> its tests here
        self.totals = {}  # file -> its tests in all shards
        self.unplanned = []
        self.elsewhere = {}  # other shard -> its tests collected here
        self.elsewhere_files = {}  # other shard -> its files not collected here
        self.dropped = 0
        self.current = None
        self.expanded = False
        self.step_key = step_key
        self.position = command.get("position")
        self.coverage_dir = os.environ.get(COVERAGE_DIR_ENV)
        # The first shard that runs the command says what the command should
        # run: listed by a separate pytest it starts (_expected_inventory), on
        # the job's hardware, at the job's commit.
        self.records_expected = bool(self.coverage_dir) and shard == self.fallback
        self.expected = None
        self.planned = []  # the tests kept here
        self.ran = set()  # the tests pytest reported on here

    def pytest_ignore_collect(self, collection_path, config):
        """A file planned whole for another shard isn't even imported. The
        recording shard's inventory comes from its own pytest, so no shard's
        run imports another shard's file."""
        relative = os.path.relpath(str(collection_path), str(config.rootpath))
        shard = self.files.get(relative.replace(os.sep, "/"))
        if shard is not None and shard != self.shard:
            self.dropped += 1
            self.elsewhere_files.setdefault(shard, set()).add(relative)
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
        return self.fallback

    @pytest.hookimpl(trylast=True)  # after -m, -k and conftest hooks
    def pytest_collection_modifyitems(self, session, config, items):
        keep, drop = [], []
        if self.records_expected and not config.getoption("collectonly"):
            self.expected = self._expected_inventory(config)
        for item in items:
            file = item.nodeid.split("::")[0]
            self.totals[file] = self.totals.get(file, 0) + 1
            owner = self._owner(item, file)
            if owner == self.shard:
                keep.append(item)
                self.counts[file] = self.counts.get(file, 0) + 1
            else:
                drop.append(item)
                self.elsewhere[owner] = self.elsewhere.get(owner, 0) + 1
        if drop:
            self.dropped += len(drop)
            config.hook.pytest_deselected(items=drop)
            items[:] = keep
        self.planned = [item.nodeid for item in items]

    def _expected_inventory(self, config):
        """The command's full test list, collected by a second pytest on this
        worker: the same python, arguments and environment as this run, but
        with no shard plan, so a file of another shard cannot change what this
        shard's own tests see, and the subprocess runs no shard plan and writes
        no record of its own.

        A failure — the collection could not run, or any file had an error —
        leaves the list unknown (None): the coverage check then reports the
        command unverified rather than compare against a partial list.
        """
        fd, path = tempfile.mkstemp(prefix="runtime-shard-inventory-", suffix=".json")
        os.close(fd)
        env = {
            name: value
            for name, value in os.environ.items()
            if name not in (PLAN_ENV, COMMAND_ENV, COVERAGE_DIR_ENV, APPLIED_ENV)
        }
        env[INVENTORY_ENV] = path
        try:
            run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    *config.invocation_params.args,
                    "--collect-only",
                    "--continue-on-collection-errors",
                ],
                env=env,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            # The spawn itself failed (say, the worker is out of processes):
            # no list, and the shard's own run must go on without it.
            os.unlink(path)
            print(
                "runtime-shard: couldn't collect the command's full test list "
                f"({error}); the coverage check will report the command "
                "unverified",
                file=sys.stderr,
            )
            return None
        try:
            with open(path) as f:
                inventory = json.load(f)
        except (OSError, ValueError):
            inventory = None
        finally:
            if os.path.exists(path):
                os.unlink(path)
        collected = run.returncode in (
            pytest.ExitCode.OK,
            pytest.ExitCode.NO_TESTS_COLLECTED,
        )
        if collected and isinstance(inventory, list):
            return inventory
        print(
            "runtime-shard: couldn't collect the command's full test list "
            f"(exit {run.returncode}); the coverage check will report the "
            "command unverified",
            file=sys.stderr,
        )
        return None

    def pytest_collection_finish(self, session):
        if session.config.getoption("collectonly"):
            self.coverage_dir = None
            return
        # Before any test: a shard that dies leaves this, marked unfinished.
        self._write_record(finished=False)
        # Looked up here: it registers after -p plugins are configured.
        self.reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if self.reporter is None:
            return
        self._say(self._where())
        if self.unplanned and self.shard == self.fallback:
            self._say(
                f"runtime-shard: not in the plan, so run in shard {self.fallback + 1}: "
                + ", ".join(self.unplanned)
            )

    def _write_record(self, finished):
        """One file per job per command, for the build's coverage check. A
        failure here costs the check a record, never the job its tests.

        Directories are 0777: a job writes as root into a checkout the agent
        owns, and the agent must be able to delete them for the next job.
        """
        if not self.coverage_dir:
            return
        job_id = os.environ.get("BUILDKITE_JOB_ID", "no-job-id")
        job_dir = os.path.join(self.coverage_dir, job_id)
        record = {
            "step": self.step_key,
            "position": self.position,
            "shard": self.shard,
            "retry": int(os.environ.get("BUILDKITE_RETRY_COUNT") or 0),
            "finished": finished,
            "finished_at": time.time(),
            "expected": self.expected,
            "planned": self.planned,
            "ran": sorted(self.ran),
        }
        try:
            os.makedirs(job_dir, exist_ok=True)
            os.chmod(self.coverage_dir, 0o777)
            os.chmod(job_dir, 0o777)
            path = os.path.join(job_dir, f"{self.position}.json")
            # Written aside, then swapped in whole: a job killed mid-write
            # keeps its earlier record. The .tmp name isn't an artifact.
            with open(f"{path}.tmp", "w") as f:
                json.dump(record, f)
            os.replace(f"{path}.tmp", path)
        except OSError as error:
            print(f"runtime-shard: no coverage record: {error}")

    def _where(self):
        """Which of the command's tests run here, and where the rest run, so
        a shard that runs few or none of them doesn't read as tests skipped."""
        tests, files = sum(self.counts.values()), len(self.counts)
        here = f"runtime-shard: shard {self.shard + 1}/{self.shards}"
        rest = []
        for shard in sorted(set(self.elsewhere) | set(self.elsewhere_files)):
            parts = []
            if self.elsewhere.get(shard):
                parts.append(_plural(self.elsewhere[shard], "test"))
            if self.elsewhere_files.get(shard):
                parts.append(_plural(len(self.elsewhere_files[shard]), "file"))
            rest.append(f"shard {shard + 1} ({' and '.join(parts)})")
        elsewhere = ", ".join(rest)
        if not tests:
            return f"{here} runs none of this command's tests; they run in {elsewhere}"
        line = f"{here} runs {_plural(tests, 'test')} in {_plural(files, 'file')}"
        return f"{line}; the rest run in {elsewhere}" if rest else line

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
            title += (
                f"   ({self.counts[file]} of {self.totals[file]} tests,"
                " split by test; the rest run in other shards)"
            )
        self._say(title)

    @pytest.hookimpl(trylast=True)  # after -v prints FAILED or ERROR
    def pytest_runtest_logreport(self, report):
        self.ran.add(report.nodeid)
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
        # -x, --maxfail or a Ctrl-C stopped the run: tests here may not have run.
        stopped = session.shouldstop or session.shouldfail or exitstatus in (
            pytest.ExitCode.INTERRUPTED,
            pytest.ExitCode.INTERNAL_ERROR,
        )
        self._write_record(finished=not stopped)


def _plural(count, noun):
    return f"{count} {noun}{'' if count == 1 else 's'}"
