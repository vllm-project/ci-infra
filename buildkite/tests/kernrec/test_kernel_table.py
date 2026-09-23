"""kernel_table.py: fold recordings into per-step rows and query them.

The module is a standalone script (it also runs on the agents' Python 3.9),
so it is loaded by path rather than imported as a package.
"""

import gzip
import importlib.util
import json
from pathlib import Path

import pytest

KERNREC = Path(__file__).resolve().parents[2] / "ci_selector" / "recorders" / "kernrec"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, KERNREC / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kernel_table = _load("kernel_table")


def _recording(path: Path, names, dropped=0, end=True):
    lines = ["# kernrec v1 pid=1 ppid=0 exe=python"] + list(names)
    if dropped:
        lines.append(f"# dropped={dropped}")
    if end:
        lines.append(f"# end records=10 unique={len(names)} dropped={dropped}")
    path.write_text("\n".join(lines) + "\n")


def _run(argv):
    import sys

    old = sys.argv
    sys.argv = ["kernel_table.py", *argv]
    try:
        return kernel_table.main()
    finally:
        sys.argv = old


def _read(path: Path) -> dict:
    with gzip.open(path, "rt") as f:
        return json.load(f)


def test_kernrec_layout_folds_jobs_into_step_rows(tmp_path):
    fn = tmp_path / ".kernrec"
    (fn / "job-a").mkdir(parents=True)
    (fn / "job-b").mkdir()
    (fn / "job-c").mkdir()
    _recording(fn / "job-a" / "kern.1.txt", ["kernA", "kernB"])
    (fn / "job-a" / "kernrec.json").write_text(
        json.dumps({"step_key": "step-x", "exit_status": 0})
    )
    _recording(fn / "job-b" / "kern.2.txt", ["kernC"], dropped=2)
    (fn / "job-b" / "kernrec.json").write_text(
        json.dumps({"step_key": "step-x", "exit_status": 1})
    )
    # job-c died before its shell exited: recordings, no sidecar, no step key
    _recording(fn / "job-c" / "kern.3.txt", ["kernZ"], end=False)

    out = tmp_path / "t.json.gz"
    assert (
        _run(
            [
                "build",
                "--kernrec",
                str(fn),
                "--build",
                "7",
                "--commit",
                "abc",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    t = _read(out)
    assert t["source"]["build"] == 7 and t["source"]["commit"] == "abc"
    assert set(t["rows"]) == {"step-x"}
    row = t["rows"]["step-x"]
    assert row["jobs"] == 2 and row["processes"] == 2
    assert row["passed"] is False, "one job exited 1"
    assert row["complete"] is True, "no shard fields: a non-parallel step"
    assert row["dropped"] == 2
    assert sorted(t["names"][i] for i in row["kernels"]) == ["kernA", "kernB", "kernC"]
    assert "kernZ" not in t["names"], "an unfiled job must not leak into any row"


def test_kernrec_layout_without_sidecar_but_with_api_states(tmp_path):
    fn = tmp_path / ".kernrec"
    (fn / "job-a").mkdir(parents=True)
    _recording(fn / "job-a" / "kern.1.txt", ["kernA"])
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps([{"id": "job-a", "state": "passed", "step_key": "s"}]))
    out = tmp_path / "t.json.gz"
    _run(
        [
            "build",
            "--kernrec",
            str(fn),
            "--build",
            "1",
            "--jobs",
            str(jobs),
            "--out",
            str(out),
        ]
    )
    t = _read(out)
    assert t["rows"]["s"]["passed"] is True


def test_step_layout_still_works(tmp_path):
    rec = tmp_path / "rec"
    (rec / "step-y" / "job-1").mkdir(parents=True)
    _recording(rec / "step-y" / "job-1" / "kern.1.txt", ["k1", "k2"])
    out = tmp_path / "t.json.gz"
    _run(["build", str(rec), "--build", "1", "--out", str(out)])
    t = _read(out)
    assert t["rows"]["step-y"]["passed"] is True
    assert len(t["rows"]["step-y"]["kernels"]) == 2


def test_build_needs_an_input(tmp_path):
    with pytest.raises(SystemExit):
        _run(["build", "--build", "1", "--out", str(tmp_path / "x.gz")])


def _symbol_map(path: Path, objects, **extra):
    d = {"version": 1, "commit": "abc", "objects": objects, "stats": {}}
    d.update(extra)
    with gzip.open(path, "wt") as f:
        json.dump(d, f)


def test_query_selects_rows_that_launched_a_symbol_from_the_file(tmp_path, capsys):
    out = tmp_path / "t.json.gz"
    rec = tmp_path / "rec"
    for step, names in {"a": ["kA"], "b": ["kB"], "c": ["kA", "kB"]}.items():
        (rec / step / "j").mkdir(parents=True)
        _recording(rec / step / "j" / "kern.1.txt", names)
    _run(["build", str(rec), "--build", "1", "--commit", "abc", "--out", str(out)])
    m = tmp_path / "map.json.gz"
    _symbol_map(
        m,
        [
            {
                "source": "csrc/a.cu",
                "target": "_C",
                "object": "o",
                "device": True,
                "symbols": ["kA"],
                "deps": ["csrc/a.cu", "csrc/shared.h"],
            },
            {
                "source": "csrc/b.cu",
                "target": "_C",
                "object": "o2",
                "device": True,
                "symbols": ["kB"],
                "deps": ["csrc/b.cu", "csrc/shared.h"],
            },
        ],
    )
    _run(
        [
            "query",
            str(out),
            str(m),
            "--file",
            "csrc/a.cu",
            "--file",
            "csrc/shared.h",
            "--file",
            "csrc/none.cu",
        ]
    )
    text = capsys.readouterr().out
    a, shared, none = text.split("\n--- ")[1:]
    assert "SELECT   (2): a, c" in a and "drop     (1)" in a
    assert "SELECT   (3): a, b, c" in shared, (
        "a header selects everything compiled with it"
    )
    assert "static rule" in none


def test_query_falls_back_for_every_file_when_the_map_is_empty(tmp_path, capsys):
    out = tmp_path / "t.json.gz"
    rec = tmp_path / "rec"
    (rec / "a" / "j").mkdir(parents=True)
    _recording(rec / "a" / "j" / "kern.1.txt", ["kA"])
    _run(["build", str(rec), "--build", "1", "--out", str(out)])
    m = tmp_path / "map.json.gz"
    _symbol_map(m, [], reason="1 objects unreadable by cuobjdump", incomplete=True)
    _run(["query", str(out), str(m), "--file", "csrc/a.cu"])
    text = capsys.readouterr().out
    assert "map is empty" in text and "SELECT" not in text


def _sidecar(job_dir: Path, step="step-x", exit_status=0, shard=None, count=None):
    d = {"step_key": step, "exit_status": exit_status}
    if shard is not None:
        d["parallel_job"] = str(shard)
        d["parallel_job_count"] = str(count)
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "kernrec.json").write_text(json.dumps(d))


def _build_kernrec(tmp_path, fn):
    out = tmp_path / "t.json.gz"
    _run(
        [
            "build",
            "--kernrec",
            str(fn),
            "--build",
            "1",
            "--commit",
            "abc",
            "--out",
            str(out),
        ]
    )
    return _read(out), out


def test_failed_shard_without_recordings_still_counts(tmp_path):
    """Codex P1: a shard that died before launching anything has a sidecar
    but no kern files; it must fail the row, not vanish."""
    fn = tmp_path / ".kernrec"
    _recording((fn / "j0").mkdir(parents=True) or fn / "j0" / "kern.1.txt", ["kA"])
    _sidecar(fn / "j0", exit_status=0, shard=0, count=2)
    _sidecar(fn / "j1", exit_status=1, shard=1, count=2)  # no recordings
    t, _ = _build_kernrec(tmp_path, fn)
    row = t["rows"]["step-x"]
    assert row["jobs"] == 2
    assert row["passed"] is False
    assert row["complete"] is True, "both shards reported in"
    assert kernel_table.usable(row) is False


def test_missing_shard_makes_the_row_incomplete(tmp_path):
    """One of two shards never produced anything (agent lost, never ran)."""
    fn = tmp_path / ".kernrec"
    (fn / "j0").mkdir(parents=True)
    _recording(fn / "j0" / "kern.1.txt", ["kA"])
    _sidecar(fn / "j0", exit_status=0, shard=0, count=2)
    t, _ = _build_kernrec(tmp_path, fn)
    row = t["rows"]["step-x"]
    assert row["passed"] is True and row["complete"] is False
    assert row["shards"] == {"expected": 2, "seen": 1}
    assert kernel_table.usable(row) is False


def test_killed_shard_with_setup_sidecar_is_not_passed(tmp_path):
    """ci_setup.sh writes the sidecar at setup with exit_status null; a job
    SIGKILLed later keeps its step key and must read as not passed."""
    fn = tmp_path / ".kernrec"
    (fn / "j0").mkdir(parents=True)
    _recording(fn / "j0" / "kern.1.txt", ["kA"], end=False)
    _sidecar(fn / "j0", exit_status=None)
    t, _ = _build_kernrec(tmp_path, fn)
    row = t["rows"]["step-x"]
    assert row["jobs"] == 1 and row["passed"] is False
    assert kernel_table.usable(row) is False


def test_query_keeps_incomplete_rows_instead_of_dropping(tmp_path, capsys):
    fn = tmp_path / ".kernrec"
    (fn / "j0").mkdir(parents=True)
    _recording(fn / "j0" / "kern.1.txt", ["kOther"])
    _sidecar(fn / "j0", exit_status=0, shard=0, count=2)  # shard 1 missing
    _, table = _build_kernrec(tmp_path, fn)
    m = tmp_path / "map.json.gz"
    _symbol_map(
        m,
        [
            {
                "source": "csrc/a.cu",
                "target": "_C",
                "object": "o",
                "device": True,
                "symbols": ["kA"],
                "deps": ["csrc/a.cu"],
            }
        ],
    )
    _run(["query", str(table), str(m), "--file", "csrc/a.cu"])
    text = capsys.readouterr().out
    assert "drop     (0)" in text
    assert "keep, row not usable (1): step-x" in text
