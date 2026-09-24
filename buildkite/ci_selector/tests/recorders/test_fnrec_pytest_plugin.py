"""The plugin's numbers have to be pytest's numbers.

They also have to match what `coverage/joblog.py` reads off the job log, so
these run real pytest in a subprocess and read what the plugin wrote, rather
than assert against a handwritten JSON line.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from ci_selector.coverage.joblog import SESSION_GLOB, read_session_counts

FNREC = Path(__file__).resolve().parents[2] / "recorders" / "fnrec"

MIXED = """\
import pytest


def test_ok():
    pass


def test_also_ok():
    pass


@pytest.mark.skip(reason="nope")
def test_skipped():
    pass


def test_bad():
    assert False


@pytest.mark.xfail
def test_xf():
    assert False
"""

ALL_SKIPPED = """\
import pytest

pytestmark = pytest.mark.skip(reason="capability gated")


def test_a():
    pass


def test_b():
    pass
"""


def run(tmp_path: Path, name: str, body: str):
    """One pytest session with the plugin loaded the way fnrec loads it."""
    proj = tmp_path / name
    proj.mkdir()
    (proj / f"test_{name}.py").write_text(body)
    (proj / "fnrec_pytest.py").write_text((FNREC / "fnrec_pytest.py").read_text())
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"],
        cwd=proj,
        env={
            "PATH": "/usr/bin:/bin",
            "FNREC_OUT": str(out),
            "PYTEST_PLUGINS": "fnrec_pytest",
            "PYTHONPATH": str(proj),
        },
        capture_output=True,
        text=True,
    )
    return proc, out


@pytest.fixture(scope="module")
def sessions(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("plugin")
    mixed, out = run(tmp, "mixed", MIXED)
    skipped, _ = run(tmp, "allskip", ALL_SKIPPED)
    rows = []
    for path in sorted(out.glob(SESSION_GLOB)):
        rows += [json.loads(line) for line in path.read_text().splitlines()]
    sessions = [r for r in rows if r.get("event") == "session"]
    collected = [r for r in rows if r.get("event") == "collected"]
    assert len(sessions) == 2, (mixed.stdout, skipped.stdout)
    assert len(collected) == 2, "the count is written before the tests run"
    return sessions, mixed, skipped


def test_counts_match_pytests_own_summary(sessions):
    rows, mixed, _ = sessions
    row = rows[0]
    assert (row["passed"], row["failed"], row["skipped"], row["xfailed"]) == (
        2,
        1,
        1,
        1,
    ), mixed.stdout
    assert row["selected"] == 5
    assert row["testsfailed"] == 1
    assert row["exitstatus"] != 0


def test_an_all_skipped_run_is_visible_despite_exiting_zero(sessions):
    rows, _, skipped = sessions
    assert skipped.returncode == 0, "the premise: pytest reports success"
    row = rows[1]
    assert row["exitstatus"] == 0
    assert row["skipped"] == 2
    assert row["selected"] == 2
    assert row["passed"] == row["failed"] == row["errors"] == 0


def test_the_reader_calls_that_run_nothing(sessions, tmp_path):
    """End to end: what the plugin wrote is what the table builder reads."""
    rows, _, _ = sessions
    d = tmp_path / "fnrec"
    d.mkdir()
    (d / "pytest.1.jsonl").write_text(json.dumps(rows[1]) + "\n")
    counts = read_session_counts(d).counts
    assert counts.executed == 0
    assert counts.ran_nothing, "a row built from this must never allow a drop"
    assert not counts.summary_unparsed


def test_a_payload_without_the_plugin_still_installs_the_recorder(tmp_path):
    """Naming a plugin pytest cannot import aborts startup, so a partial
    payload still has to install the recorder."""
    payload = tmp_path / "payload"
    payload.mkdir()
    for name in ("fnrec.py", "host_install.py"):
        (payload / name).write_text((FNREC / name).read_text())
    lib = tmp_path / "lib"
    proc = subprocess.run(
        [sys.executable, str(payload / "host_install.py")],
        env={"PATH": "/usr/bin:/bin", "FNREC_LIB": str(lib)},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert (lib / "fnrec.py").is_file(), "the recorder still installs"
    assert not (lib / "fnrec_pytest.py").exists()


def test_a_killed_session_still_leaves_its_collected_count(tmp_path):
    """Why the count is written at collection: with no line at all, a killed
    job reads as one that started no pytest, which looks healthy."""
    proj = tmp_path / "killed"
    proj.mkdir()
    (proj / "test_slow.py").write_text(
        "import os, signal\n"
        "def test_one(): os.kill(os.getpid(), signal.SIGKILL)\n"
        "def test_two(): pass\n"
    )
    (proj / "fnrec_pytest.py").write_text((FNREC / "fnrec_pytest.py").read_text())
    out = tmp_path / "out"
    out.mkdir()
    subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"],
        cwd=proj,
        env={
            "PATH": "/usr/bin:/bin",
            "FNREC_OUT": str(out),
            "PYTEST_PLUGINS": "fnrec_pytest",
            "PYTHONPATH": str(proj),
        },
        capture_output=True,
    )
    counts = read_session_counts(out).counts
    assert counts.collected == 2, "collection finished, so the count is there"
    assert counts.invocations == 0, "the session never finished"
    assert counts.summary_unparsed, "which is what keeps the row too thin to drop"


def test_the_installer_marks_that_the_plugin_was_there(tmp_path):
    """Without the marker, a job that wrote no session file looks the same as
    one whose plugin never installed."""
    payload = tmp_path / "payload"
    payload.mkdir()
    for name in ("fnrec.py", "fnrec_pytest.py", "host_install.py"):
        (payload / name).write_text((FNREC / name).read_text())
    out = tmp_path / "out"
    out.mkdir()
    proc = subprocess.run(
        [sys.executable, str(payload / "host_install.py")],
        env={
            "PATH": "/usr/bin:/bin",
            "FNREC_LIB": str(tmp_path / "lib"),
            "FNREC_OUT": str(out),
        },
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert (out / "pytest.installed").is_file()
