# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""The Python function recorder, read back through the real reader.

Each test runs the recorder in a subprocess against a throwaway `vllm`
package, then parses what it wrote with `coverage/model.read_process`, the
same code the table builder uses. A producer/reader disagreement fails here
rather than as an empty table in production.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from ci_selector.coverage.model import read_process

FNREC = Path(__file__).resolve().parents[2] / "fnrec" / "fnrec.py"

PACKAGE = {
    "__init__.py": "",
    "mod.py": textwrap.dedent(
        """\
        CONSTANT = 1

        def plain():
            return CONSTANT

        class Holder:
            def method(self):
                return plain()
        """
    ),
    "other.py": "def elsewhere():\n    return 0\n",
}


@pytest.fixture
def fake_vllm(tmp_path: Path) -> Path:
    pkg = tmp_path / "site" / "vllm"
    pkg.mkdir(parents=True)
    for name, text in PACKAGE.items():
        (pkg / name).write_text(text)
    return pkg


def _run(
    tmp_path: Path, fake_vllm: Path, script: str, *, env_extra: dict | None = None
):
    out = tmp_path / "out"
    env = {
        **os.environ,
        "FNREC_DIR": str(out),
        "FNREC_ROOT": str(fake_vllm) + "/",
        "PYTHONPATH": str(fake_vllm.parent) + os.pathsep + str(FNREC.parent),
        "BUILDKITE_JOB_ID": "job-77",
        "BUILDKITE_RETRY_COUNT": "0",
    }
    env.update(env_extra or {})
    for k in ("FNREC_DIR", "FNREC_ROOT"):
        if env.get(k) is None:
            env.pop(k, None)
    proc = subprocess.run(
        [sys.executable, "-c", "import fnrec\n" + textwrap.dedent(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return out, proc


def _records(out: Path):
    return {p.name: read_process(p) for p in sorted(out.glob("fn.*.txt"))}


def test_records_entered_functions_in_the_readers_shape(tmp_path, fake_vllm):
    out, _ = _run(
        tmp_path,
        fake_vllm,
        """
        import vllm.mod as m
        m.plain(); m.plain(); m.Holder().method()
        """,
    )
    recs = _records(out)
    assert len(recs) == 1
    rec = next(iter(recs.values()))
    assert rec is not None
    assert rec.functions == {
        "vllm/__init__.py": {"<module>"},
        "vllm/mod.py": {"<module>", "plain", "Holder", "Holder.method"},
    }
    assert "vllm/other.py" not in rec.functions, "never imported, never recorded"
    assert rec.clean_exit and rec.errors == 0 and rec.malformed == 0
    assert rec.job == "job-77" and rec.retry == "0"
    assert rec.py == ".".join(map(str, sys.version_info[:3]))
    # DISABLE: plain() ran three times (twice directly, once via method) and
    # is one line; the counter matches the lines written.
    assert rec.data_lines == 5 and rec.counter == 5 and not rec.lost_lines
    assert rec.outside_root == 0, (
        "stdlib and the script itself are counted, not written"
    )


def test_forked_child_writes_its_own_file(tmp_path, fake_vllm):
    out, _ = _run(
        tmp_path,
        fake_vllm,
        """
        import os, sys
        import vllm.mod as m
        m.plain()
        pid = os.fork()
        if pid == 0:
            import vllm.other as o
            o.elsewhere()
            os._exit(0)
        os.waitpid(pid, 0)
        """,
    )
    recs = _records(out)
    assert len(recs) == 2, sorted(recs)
    parent = next(r for r in recs.values() if r.clean_exit)
    child = next(r for r in recs.values() if not r.clean_exit)  # os._exit skips atexit
    assert "vllm/other.py" not in parent.functions
    assert child.functions.get("vllm/other.py") == {"<module>", "elsewhere"}
    assert "plain" not in child.functions.get("vllm/mod.py", set()), (
        "disabled in the parent stays disabled in the child"
    )


def test_off_without_fnrec_dir(tmp_path, fake_vllm):
    out, proc = _run(
        tmp_path,
        fake_vllm,
        "import vllm.mod as m; m.plain(); print(fnrec.install())",
        env_extra={"FNREC_DIR": None},
    )
    assert proc.stdout.strip() == "False"
    assert not out.exists()


def test_takes_another_tool_slot_when_the_profiler_id_is_busy(tmp_path, fake_vllm):
    """Without FNREC_DIR the import installs nothing; claim the profiler slot
    the way another tool would, then install by hand and expect a free slot."""
    out, proc = _run(
        tmp_path,
        fake_vllm,
        """
        import sys, os
        sys.monitoring.use_tool_id(sys.monitoring.PROFILER_ID, "someone-else")
        assert fnrec.install(%r)
        print("tool", fnrec._state["tool"])
        import vllm.mod as m
        m.plain()
        """
        % str(tmp_path / "out"),
        env_extra={"FNREC_DIR": None},
    )
    assert proc.stdout.strip() in ("tool 3", "tool 4")
    rec = next(iter(_records(out).values()))
    assert "plain" in rec.functions["vllm/mod.py"]


def test_root_found_lazily_when_vllm_is_imported_later(tmp_path, fake_vllm):
    """No FNREC_ROOT and vllm not importable at start: the root is picked up
    from sys.modules once something imports it, and the #root line follows."""
    out, _ = _run(
        tmp_path,
        fake_vllm,
        """
        import sys
        sys.path.insert(0, %r)
        import vllm.mod as m
        m.plain()
        """
        % str(fake_vllm.parent),
        env_extra={"FNREC_ROOT": None, "PYTHONPATH": str(FNREC.parent)},
    )
    rec = next(iter(_records(out).values()))
    assert rec.root == str(fake_vllm) + "/"
    assert "plain" in rec.functions["vllm/mod.py"]


def test_stat_lines_every_thousand_records(tmp_path, fake_vllm):
    big = "\n".join(f"def f{i}():\n    return {i}\n" for i in range(1200))
    (fake_vllm / "big.py").write_text(
        big
        + "\nfor _f in list(globals().values()):\n    if callable(_f) and getattr(_f, '__name__', '').startswith('f'):\n        _f()\n"
    )
    out, _ = _run(tmp_path, fake_vllm, "import vllm.big")
    text = next(out.glob("fn.*.txt")).read_text()
    assert text.count("#stat\troot=1000\t") == 1
    rec = read_process(next(out.glob("fn.*.txt")))
    assert rec.data_lines >= 1201 and rec.counter == rec.data_lines
