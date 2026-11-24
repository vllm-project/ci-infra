"""pytest plugin for one parallel job of a runtime-sharded step.

The pipeline generator planned the step's shards (runtime_shard.py) and put
the plan in RUNTIME_SHARD_PLAN; each parallel job runs the step's pytest
commands unchanged, with this plugin loaded (`-p runtime_shard_plugin`). In a
command the plan covers, it:

  - skips collecting the files planned for other shards,
  - keeps this job's tests: its files, and its part of each file split by
    test. A collected file the plan doesn't name (one the generator could
    not see) runs in the least-loaded owner that runs the command, so no test can
    drop out of every shard,
  - opens a Buildkite log group per file, opens the group of a file that
    fails, and puts the failures and summary in their own open group,
  - turns "no tests ran" into success when every test went to other shards,
  - records what it ran for the build's coverage check
    (runtime_shard_coverage.py): the command's least-loaded owner also records the
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
import signal
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
# A recording shard's inventory collection is bounded: a hung collection
# must not hang the shard's own run.
INVENTORY_TIMEOUT_SECONDS = 300
# A failed inventory prints only this much of its output: enough to name the
# file that broke, not enough to bury the job's own log.
INVENTORY_TAIL_LINES = 40
# Captured before conftest and package imports can change sys.path.
_INITIAL_SYS_PATH_ENTRY = os.path.realpath(sys.path[0] or ".")


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
            retry_helper = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "pytest_retry.py"
            )
            if os.path.isfile(retry_helper):
                shutil.copy(retry_helper, site)
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
import pytest_retry  # noqa: E402

RETRY_IN_ENV = pytest_retry.RETRY_IN_ENV


def pytest_configure(config):
    # Popped, so a pytest that a test itself starts runs as written.
    inventory = os.environ.pop(INVENTORY_ENV, None)
    if inventory:
        # A recording shard's collecting subprocess: no shard plan here, and
        # it writes no coverage record of its own.
        config.pluginmanager.register(_Inventory(inventory), "runtime_shard_inventory")
        return
    packed = os.environ.pop(PLAN_ENV, None)
    retry_in = os.environ.pop(RETRY_IN_ENV, None)
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
        ShardPlugin(
            command,
            shard,
            plan["shards"],
            plan.get("key"),
            retry_in=retry_in,
            single_job=bool(plan.get("single_job")),
        ),
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


def _cancel_inventory(signum, _frame):
    """Let the inventory's finally run when the live job is canceled."""
    raise SystemExit(128 + signum)


def _stop_inventory_process(process):
    """Stop the collection group and close its pipes without another read."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as error:
        print(
            "runtime-shard: couldn't stop the inventory process group "
            f"({error})",
            file=sys.stderr,
        )
        try:
            process.kill()
        except ProcessLookupError:
            pass
    if process.poll() is None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print(
                "runtime-shard: inventory process did not stop after SIGKILL",
                file=sys.stderr,
            )
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


class ShardPlugin:
    def __init__(
        self, command, shard, shards, step_key=None, retry_in=None, single_job=False
    ):
        self.files = command["files"]  # file from rootdir -> its shard
        self.split = command["split"]  # file -> {"shards", "assign"}
        self.prefix = command["prefix"]
        self.label = command["label"]
        self.fallback = command.get("fallback", 0)
        self.shard = shard
        self.shards = shards
        self.single_job = bool(single_job) or shards <= 1
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
        self.retry_count = int(os.environ.get("BUILDKITE_RETRY_COUNT") or 0)
        self.mode = "full"
        self.retry_passed = set()
        self.retry_skip_files = set()
        self.retry_skipped_files = set()
        self.retry_skipped_tests = 0
        self.retry_has_expected = False
        self.retry_base_planned = None
        self.retry_missing_outstanding = []
        if (
            retry_in
            and os.path.isdir(retry_in)
            and self.retry_count > 0
            and self.step_key
            and self.position is not None
        ):
            prior_records = pytest_retry.read_records(retry_in)
            retry_plan = pytest_retry.build_retry_plan(
                prior_records,
                self.step_key,
                self.position,
                self.shard,
                build_id=os.environ.get("BUILDKITE_BUILD_ID"),
                current_retry=self.retry_count,
            )
            if retry_plan["active"]:
                self.mode = "test-retry"
                self.retry_passed = retry_plan["passed"]
                self.retry_skip_files = retry_plan["skip_files"]
                self.retry_has_expected = retry_plan["has_expected"]
                self.retry_base_planned = retry_plan["base_planned"]
        # The least-loaded owner that runs the command says what the command
        # should run: listed by a separate pytest it starts
        # (_expected_inventory), on the job's hardware, at the job's commit.
        # Skipped on single-job steps and on test retries when the active chain
        # already captured a valid expected list.
        self.records_expected = (
            bool(self.coverage_dir)
            and not self.single_job
            and shard == self.fallback
            and not self.retry_has_expected
        )
        self.expected = None
        self.planned = None  # tests kept here after collection; None if unknown
        self.collection_failed = False
        self.ran = set()  # the tests pytest reported on here
        self._phases = {}  # nodeid -> {when: (outcome, wasxfail)}
        self._active_nodeid = None
        self._subtest_failed = set()
        self.record_path = None

    def pytest_ignore_collect(self, collection_path, config):
        """A directory-discovered file planned whole for another shard, or
        whose tests all passed earlier in the active retry chain, isn't even
        imported (explicit file CLI arguments bypass this hook in pytest and
        are deselected in `pytest_collection_modifyitems`)."""
        relative = os.path.relpath(str(collection_path), str(config.rootpath))
        relative_posix = relative.replace(os.sep, "/")
        if not self.single_job:
            shard = self.files.get(relative_posix)
            if shard is not None and shard != self.shard:
                self.dropped += 1
                self.elsewhere_files.setdefault(shard, set()).add(relative)
                return True
        if relative_posix in self.retry_skip_files:
            self.dropped += 1
            self.retry_skipped_files.add(relative_posix)
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
        if (
            self.records_expected
            and not self.collection_failed
            and not config.getoption("collectonly")
        ):
            self.expected = self._expected_inventory(config)
        # Before deselecting already-passed tests, check whether this shard
        # collected any new test or extra duplicate occurrence (even of a
        # previously passing test) relative to the base full attempt.
        if self.mode == "test-retry" and isinstance(self.retry_base_planned, list):
            base_counts = {}
            for nodeid in self.retry_base_planned:
                base_counts[nodeid] = base_counts.get(nodeid, 0) + 1
            seen_counts = {}
            grew = False
            for item in items:
                file = item.nodeid.split("::")[0]
                if not self.single_job and self._owner(item, file) != self.shard:
                    continue
                seen_counts[item.nodeid] = seen_counts.get(item.nodeid, 0) + 1
                if seen_counts[item.nodeid] > base_counts.get(item.nodeid, 0):
                    grew = True
                    break
            if grew:
                self.retry_passed = set()
                if not self.retry_skipped_files:
                    self.mode = "full"
                    self.retry_base_planned = None
        for item in items:
            file = item.nodeid.split("::")[0]
            self.totals[file] = self.totals.get(file, 0) + 1
            if not self.single_job:
                owner = self._owner(item, file)
                if owner != self.shard:
                    drop.append(item)
                    self.elsewhere[owner] = self.elsewhere.get(owner, 0) + 1
                    continue
            if item.nodeid in self.retry_passed:
                drop.append(item)
                self.retry_skipped_tests += 1
                continue
            keep.append(item)
            self.counts[file] = self.counts.get(file, 0) + 1
        if drop:
            self.dropped += len(drop)
            config.hook.pytest_deselected(items=drop)
            items[:] = keep
        if not self.collection_failed:
            # An empty assignment is meaningful only when collection completed.
            self.planned = [item.nodeid for item in items]
            if self.mode == "test-retry" and isinstance(self.retry_base_planned, list):
                required_counts = {}
                for nodeid in self.retry_base_planned:
                    if nodeid not in self.retry_passed:
                        required_counts[nodeid] = required_counts.get(nodeid, 0) + 1
                kept_counts = {}
                for nodeid in self.planned:
                    kept_counts[nodeid] = kept_counts.get(nodeid, 0) + 1
                self.retry_missing_outstanding = sorted(
                    nodeid
                    for nodeid, count in required_counts.items()
                    if kept_counts.get(nodeid, 0) < count
                )

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_failed = True

    def _expected_inventory(self, config):
        """The command's full test list, collected by a second pytest on this
        worker: the same python, arguments and environment as this run, but
        with no shard plan, so a file of another shard cannot change what this
        shard's own tests see, and the subprocess runs no shard plan and writes
        no record of its own.

        A failure — the collection could not run, or any file had an error —
        leaves the list unknown (None): the coverage check then reports the
        command unverified rather than compare against a partial list. The
        inventory repeats collection-time option and plugin side effects,
        including report writing. Recorder metadata and trace IDs are stripped
        so the inventory cannot alter the job's own test counts or timings.
        """
        fd, path = tempfile.mkstemp(prefix="runtime-shard-inventory-", suffix=".json")
        os.close(fd)
        env = {
            name: value
            for name, value in os.environ.items()
            if name not in (
                PLAN_ENV,
                COMMAND_ENV,
                COVERAGE_DIR_ENV,
                APPLIED_ENV,
                RETRY_IN_ENV,
                "CI_INFRA_TRACE_ID",
                "CI_INFRA_COMMAND_SPAN_ID",
                "CUDA_INJECTION64_PATH",
                "ROCP_TOOL_LIBRARIES",
                "BUILDKITE_ANALYTICS_TOKEN",
                "COVERAGE_PROCESS_START",
            )
            and not name.startswith(("FNREC_", "KERNREC_", "COV_CORE_"))
        }
        env[INVENTORY_ENV] = path
        # Match the live invocation: -m pytest prepends the working directory
        # to sys.path; the console script does not. Suppress it only if the
        # live invocation did not already have it there.
        if _INITIAL_SYS_PATH_ENTRY != os.path.realpath(
            config.invocation_params.dir
        ):
            env["PYTHONSAFEPATH"] = "1"
        process = None
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _cancel_inventory)
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    *config.invocation_params.args,
                    "--collect-only",
                ],
                env=env,
                cwd=config.invocation_params.dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            stdout, stderr = process.communicate(timeout=INVENTORY_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            os.unlink(path)
            print(
                "runtime-shard: couldn't collect the command's full test list "
                f"(timed out after {INVENTORY_TIMEOUT_SECONDS} seconds); the "
                "coverage check will report the command unverified"
                + _output_tail(error.output, error.stderr),
                file=sys.stderr,
            )
            return None
        except OSError as error:
            os.unlink(path)
            print(
                "runtime-shard: couldn't collect the command's full test list "
                f"({error}); the coverage check will report the command "
                "unverified",
                file=sys.stderr,
            )
            return None
        finally:
            if process is not None:
                _stop_inventory_process(process)
            signal.signal(signal.SIGTERM, previous_sigterm)
        try:
            with open(path) as f:
                inventory = json.load(f)
        except (OSError, ValueError):
            inventory = None
        finally:
            if os.path.exists(path):
                os.unlink(path)
        collected = process.returncode in (
            pytest.ExitCode.OK,
            pytest.ExitCode.NO_TESTS_COLLECTED,
        )
        if collected and isinstance(inventory, list):
            return inventory
        print(
            "runtime-shard: couldn't collect the command's full test list "
            f"(exit {process.returncode}); the coverage check will report the "
            "command unverified" + _output_tail(stdout, stderr),
            file=sys.stderr,
        )
        return None

    def pytest_collection_finish(self, session):
        if session.config.getoption("collectonly"):
            self.coverage_dir = None
            return
        # Before any test: a shard that dies leaves this header + flushed tests.
        self._start_record()
        # Looked up here: it registers after -p plugins are configured.
        self.reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if self.reporter is None:
            return
        if not self.single_job:
            self._say(self._where())
            if self.unplanned and self.shard == self.fallback:
                self._say(
                    f"runtime-shard: not in the plan, so run in shard {self.fallback + 1}: "
                    + ", ".join(self.unplanned)
                )
        if self.mode == "test-retry" and (
            self.retry_skipped_files or self.retry_skipped_tests
        ):
            self._say(
                "pytest-retry: skipped "
                f"{_plural(len(self.retry_skipped_files), 'passed file')} and "
                f"{_plural(self.retry_skipped_tests, 'passed test')} from earlier attempts"
            )

    def _start_record(self):
        """Write line 1 (`start` header) of the command's flushed JSONL record."""
        if not self.coverage_dir:
            return
        job_id = os.environ.get("BUILDKITE_JOB_ID", "no-job-id")
        self.record_path = pytest_retry.write_jsonl_start(
            coverage_dir=self.coverage_dir,
            job_id=job_id,
            step=self.step_key,
            position=self.position,
            shard=self.shard,
            retry=self.retry_count,
            build_id=os.environ.get("BUILDKITE_BUILD_ID"),
            mode=self.mode,
            expected=self.expected,
            planned=self.planned,
        )

    def _finish_record(self, finished, exitstatus=None):
        """Flush any unfinished phase records and append the `finish` trailer."""
        for nodeid in sorted(self._phases):
            pytest_retry.append_jsonl_event(
                self.record_path,
                {
                    "event": "test",
                    "nodeid": nodeid,
                    "passed": False,
                    "outcome": "error",
                },
            )
        self._phases.clear()
        payload = {
            "event": "finish",
            "finished": finished,
            "finished_at": time.time(),
        }
        if exitstatus is not None:
            payload["exitstatus"] = int(exitstatus)
        pytest_retry.append_jsonl_event(self.record_path, payload)

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
        self._active_nodeid = nodeid
        if self.single_job:
            return
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
        # Runtime subtests can report IDs that are absent from collection, so
        # exclude their IDs from coverage `ran`, but still mark the parent test
        # failed so a failing subtest is never skipped on retry.
        if isinstance(report, getattr(pytest, "SubtestReport", ())):
            if report.failed and self._active_nodeid:
                self._subtest_failed.add(self._active_nodeid)
        else:
            self.ran.add(report.nodeid)
            phases = self._phases.setdefault(report.nodeid, {})
            phases[report.when] = (
                report.outcome,
                hasattr(report, "wasxfail"),
            )
            if report.when == "teardown":
                passed, outcome = _classify_phases(phases)
                if report.nodeid in self._subtest_failed:
                    passed, outcome = False, "failed"
                    self._subtest_failed.discard(report.nodeid)
                pytest_retry.append_jsonl_event(
                    self.record_path,
                    {
                        "event": "test",
                        "nodeid": report.nodeid,
                        "passed": passed,
                        "outcome": outcome,
                    },
                )
                self._phases.pop(report.nodeid, None)
        if (
            not self.single_job
            and self.reporter is not None
            and report.failed
            and not self.expanded
        ):
            self.expanded = True
            self._say("^^^ +++")  # Buildkite: open this file's group

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtestloop(self, session):
        yield
        if not self.single_job and self.reporter is not None and self.current is not None:
            # The failures and the summary get their own open group, not the
            # last file's.
            self._say(f"+++ :test_tube: {self.label}: results")

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_sessionfinish(self, session, exitstatus):
        # Every test of the command went to other shards or already passed on an
        # earlier attempt: nothing to run here is not a failure. A command that
        # collects nothing at all, failed collection, or lost an outstanding
        # unpassed test from the retry chain still fails.
        if (
            exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED
            and self.dropped
            and not self.collection_failed
            and not self.retry_missing_outstanding
        ):
            session.exitstatus = pytest.ExitCode.OK
        if self.retry_missing_outstanding:
            print(
                "pytest-retry: outstanding unpassed test(s) disappeared from collection: "
                + ", ".join(self.retry_missing_outstanding),
                file=sys.stderr,
            )
            if session.exitstatus == pytest.ExitCode.OK:
                session.exitstatus = pytest.ExitCode.TESTS_FAILED
        outcome = yield
        final_status = int(session.exitstatus)
        if outcome.excinfo is not None and final_status == int(pytest.ExitCode.OK):
            final_status = int(pytest.ExitCode.INTERNAL_ERROR)
        # -x, --maxfail or a Ctrl-C stopped the run: tests here may not have run.
        stopped = session.shouldstop or session.shouldfail or final_status in (
            int(pytest.ExitCode.INTERRUPTED),
            int(pytest.ExitCode.INTERNAL_ERROR),
        )
        self._finish_record(finished=not stopped, exitstatus=final_status)


def _classify_phases(phases):
    """Classify a completed test from its setup/call/teardown phase outcomes.

    Returns:
        (passed: bool, outcome: str). `passed` is True only when setup, call
        and teardown all passed without xfail.
    """
    setup = phases.get("setup")
    call = phases.get("call")
    teardown = phases.get("teardown")
    if (
        setup == ("passed", False)
        and call == ("passed", False)
        and teardown == ("passed", False)
    ):
        return True, "passed"
    if (setup and setup[0] == "failed") or (teardown and teardown[0] == "failed"):
        return False, "error"
    if call and call[0] == "failed":
        return False, "failed"
    if (setup and setup[1]) or (call and call[1]):
        return False, "xfailed"
    if (setup and setup[0] == "skipped") or (call and call[0] == "skipped"):
        return False, "skipped"
    return False, "failed"


def _plural(count, noun):
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _output_tail(stdout, stderr):
    """The end of a failed inventory collection's output, bounded: pytest
    names the file that broke at the end of its traceback."""
    parts = []
    for label, text in (("output", stdout), ("error output", stderr)):
        if not text:
            continue
        if isinstance(text, bytes):
            text = text.decode(errors="replace")
        lines = text.splitlines()
        if len(lines) > INVENTORY_TAIL_LINES:
            lines = ["..."] + lines[-INVENTORY_TAIL_LINES:]
        parts.append(f"\n--- last lines of its {label} ---\n" + "\n".join(lines))
    return "".join(parts)
