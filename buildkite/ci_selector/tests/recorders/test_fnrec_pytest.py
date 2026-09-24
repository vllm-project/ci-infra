# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""The pytest plugin that stands in for the job log, read by the log parser.

Each test runs a real pytest in a subprocess with the plugin loaded, then reads
what it wrote with `coverage/joblog.read_counts`, the parser the table builder
uses on Buildkite logs. The strongest check reads pytest's own terminal output
with the same parser and requires the two to agree.
"""

from __future__ import annotations

import gzip
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from ci_selector.coverage.joblog import read_counts

FNREC = Path(__file__).resolve().parents[2] / "recorders" / "fnrec"

MIXED = textwrap.dedent(
    """\
    import pytest

    @pytest.fixture
    def broken():
        raise RuntimeError("setup fails")

    def test_pass(): pass
    def test_pass_too(): pass
    def test_fail(): assert False
    def test_skip(): pytest.skip("no")
    @pytest.mark.xfail
    def test_xfail(): assert False
    @pytest.mark.xfail
    def test_xpass(): pass
    def test_error(broken): pass
    def test_deselect_me(): pass
    """
)


def _pytest(tmp_path, body, *args, fnrec_dir=True, plugin_arg=True, extra_path=None):
    tests = tmp_path / "t"
    tests.mkdir(exist_ok=True)
    (tests / "test_sample.py").write_text(body)
    out = tmp_path / "out"
    env = {k: v for k, v in os.environ.items() if k != "FNREC_OUT"}
    if fnrec_dir:
        env["FNREC_OUT"] = str(out)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(extra_path or FNREC), env.get("PYTHONPATH", "")) if p
    )
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", str(tests), *args]
    if plugin_arg:
        cmd[3:3] = ["-p", "fnrec_pytest"]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=tmp_path)
    return proc, out


def _plugin_counts(out: Path, tmp_path: Path):
    files = sorted(out.glob("pytest.*.txt"))
    log = tmp_path / "synthetic.log.gz"
    with gzip.open(log, "wt") as fh:
        for f in files:
            fh.write(f.read_text())
    return files, read_counts(log)


def _terminal_counts(proc, tmp_path: Path):
    log = tmp_path / "terminal.log"
    log.write_text(proc.stdout)
    return read_counts(log)


def test_plugin_agrees_with_pytests_own_summary(tmp_path):
    proc, out = _pytest(tmp_path, MIXED, "-k", "not deselect_me")
    files, mine = _plugin_counts(out, tmp_path)
    theirs = _terminal_counts(proc, tmp_path)
    assert len(files) == 1, proc.stdout + proc.stderr
    assert theirs.counts.invocations == 1, proc.stdout
    assert mine.counts == theirs.counts
    c = mine.counts
    assert (c.passed, c.failed, c.skipped, c.xfailed, c.xpassed, c.errors) == (
        2,
        1,
        1,
        1,
        1,
        1,
    )
    assert c.deselected == 1 and c.collected == 8


def test_all_skipped_reads_as_ran_nothing(tmp_path):
    body = "import pytest\n\ndef test_a(): pytest.skip('x')\ndef test_b(): pytest.skip('y')\n"
    proc, out = _pytest(tmp_path, body)
    _, mine = _plugin_counts(out, tmp_path)
    assert mine.counts == _terminal_counts(proc, tmp_path).counts
    assert mine.counts.ran_nothing


def test_nothing_collected_reads_as_ran_nothing(tmp_path):
    proc, out = _pytest(tmp_path, "X = 1\n")
    _, mine = _plugin_counts(out, tmp_path)
    assert mine.counts == _terminal_counts(proc, tmp_path).counts
    assert mine.counts.invocations == 1 and mine.counts.ran_nothing


def test_run_killed_before_its_summary_reads_as_unparsed(tmp_path):
    body = "import os, signal\n\ndef test_a(): pass\ndef test_die(): os.kill(os.getpid(), signal.SIGKILL)\n"
    proc, out = _pytest(tmp_path, body)
    assert proc.returncode != 0
    _, mine = _plugin_counts(out, tmp_path)
    assert mine.counts.collected == 2
    assert mine.counts.summary_unparsed, "a killed run must keep the row thin"


def test_inert_without_fnrec_dir(tmp_path):
    proc, out = _pytest(tmp_path, "def test_a(): pass\n", fnrec_dir=False)
    assert proc.returncode == 0
    assert not out.exists()


def test_entry_point_install_loads_it_without_a_flag(tmp_path):
    """The shape ci_setup.sh installs: module plus a dist-info with a pytest11
    entry point, found on the path with no -p argument."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "fnrec_pytest.py").write_text((FNREC / "fnrec_pytest.py").read_text())
    di = site / "fnrec_pytest-0.dist-info"
    di.mkdir()
    (di / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: fnrec-pytest\nVersion: 0\n"
    )
    (di / "entry_points.txt").write_text("[pytest11]\nfnrec = fnrec_pytest\n")
    proc, out = _pytest(
        tmp_path, "def test_a(): pass\n", plugin_arg=False, extra_path=site
    )
    files, mine = _plugin_counts(out, tmp_path)
    assert files, proc.stdout + proc.stderr
    assert mine.counts.passed == 1 and mine.counts.invocations == 1


@pytest.mark.parametrize("src", ["fnrec_pytest.py"])
def test_imports_only_the_standard_library(src):
    """A plugin that fails to import aborts pytest, and with it the step."""
    text = (FNREC / src).read_text()
    imports = [
        line.split()[1].split(".")[0]
        for line in text.splitlines()
        if line.startswith(("import ", "from ")) and "__future__" not in line
    ]
    assert set(imports) <= {"os", "time"}, imports


def test_the_host_installer_arms_the_plugin_for_pytest(tmp_path):
    """host_install.py, the plugin-less path: the plugin, its entry point and
    the marker land in the per-job directory, and a pytest started with that
    directory on PYTHONPATH writes its counts."""
    payload = tmp_path / "payload"
    payload.mkdir()
    for name in ("fnrec.py", "host_install.py", "fnrec_pytest.py"):
        (payload / name).write_text((FNREC / name).read_text())
    lib, out = tmp_path / "lib", tmp_path / "out"
    out.mkdir()
    env = {**os.environ, "FNREC_LIB": str(lib), "FNREC_OUT": str(out)}
    proc = subprocess.run(
        [sys.executable, str(payload / "host_install.py")],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert (lib / "fnrec_pytest.py").is_file()
    assert (
        "fnrec_pytest"
        in (lib / "fnrec_pytest-0.dist-info" / "entry_points.txt").read_text()
    )
    assert (out / "pytest.installed").exists()

    proc, _ = _pytest(
        tmp_path, "def test_a(): pass\n", plugin_arg=False, extra_path=lib
    )
    files, mine = _plugin_counts(out, tmp_path)
    assert files, proc.stdout + proc.stderr
    assert mine.counts.passed == 1


def test_an_installer_without_the_plugin_still_installs_the_recorder(tmp_path):
    payload = tmp_path / "payload"
    payload.mkdir()
    for name in ("fnrec.py", "host_install.py"):
        (payload / name).write_text((FNREC / name).read_text())
    lib, out = tmp_path / "lib", tmp_path / "out"
    out.mkdir()
    env = {**os.environ, "FNREC_LIB": str(lib), "FNREC_OUT": str(out)}
    proc = subprocess.run(
        [sys.executable, str(payload / "host_install.py")],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert (lib / "fnrec.py").is_file() and (lib / "sitecustomize.py").is_file()
    assert not (out / "pytest.installed").exists()
