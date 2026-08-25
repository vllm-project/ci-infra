# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

import base64
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

import test_selection.collector.nvtx_test_ranges as nvtx_test_ranges
import test_selection.collector.pytest_trace_plugin as pytest_trace_plugin
import test_selection.collector.run_job_trace as run_job_trace
from test_selection.collector_bundle import bundle_bytes
from test_selection.collector.nvtx_test_ranges import _configured_nvtx
from test_selection.collector.run_job_trace import decode_commands


def _payload(commands: list[str]) -> str:
    return base64.b64encode(json.dumps(commands).encode()).decode()


def _module_root() -> Path:
    return Path(__file__).resolve().parents[3] / "pipeline_generator"


def test_decode_commands_round_trip():
    commands = ["pytest -q tests/test_one.py", "python -m pytest tests/test_two.py"]

    assert decode_commands(_payload(commands)) == commands


@pytest.mark.parametrize("document", [[], [""], {"command": "pytest"}, [1]])
def test_decode_commands_rejects_invalid_documents(document):
    with pytest.raises(SystemExit):
        decode_commands(base64.b64encode(json.dumps(document).encode()).decode())


def test_python_only_nvtx_gate_does_not_initialize_cuda(monkeypatch):
    def fail_if_called():
        raise AssertionError("python-only collection touched CUDA")

    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=fail_if_called))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setenv("VLLM_CI_TEST_SELECTION_NVTX", "0")

    assert _configured_nvtx() is None


@pytest.mark.parametrize("failure", ["push", "pop"])
def test_nvtx_tooling_failure_does_not_escape_pytest_hook(monkeypatch, failure):
    class BrokenNvtx:
        def range_push(self, _label):
            if failure == "push":
                raise RuntimeError("broken push")

        def range_pop(self):
            if failure == "pop":
                raise RuntimeError("broken pop")

    monkeypatch.setattr(nvtx_test_ranges, "_nvtx", BrokenNvtx())
    wrapper = nvtx_test_ranges._wrap("call", SimpleNamespace(nodeid="test_node"))

    next(wrapper)
    with pytest.raises(StopIteration):
        next(wrapper)


def test_node_export_failure_does_not_change_pytest_result(
    tmp_path: Path, monkeypatch, capsys
):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("block", encoding="utf-8")
    monkeypatch.setenv("VLLM_CI_TEST_SELECTION_NODEIDS", str(blocker / "nodes.json"))

    pytest_trace_plugin.pytest_sessionfinish(SimpleNamespace(), 0)

    assert "node/outcome export failed" in capsys.readouterr().err


def test_python_only_job_collects_unordered_lines_and_exact_collector(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    source = repo / "vllm" / "sample.py"
    test_file = repo / "tests" / "test_sample.py"
    source.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    (repo / "vllm" / "__init__.py").write_text("", encoding="utf-8")
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")
    test_file.write_text(
        "from vllm.sample import answer\n\n"
        "def test_answer():\n"
        "    assert answer() == 42\n",
        encoding="utf-8",
    )
    output = tmp_path / "trace"
    collector_sha256 = "c" * 64
    environment = dict(os.environ)
    environment.update(
        {
            "BUILDKITE_COMMIT": "a" * 40,
            "BUILDKITE_RETRY_COUNT": "2",
            "PATH": f"{Path(sys.executable).parent}:{environment['PATH']}",
            "PYTHONPATH": str(_module_root()),
            "VLLM_CI_TEST_SELECTION_COLLECTOR_SHA256": collector_sha256,
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "test_selection.collector.run_job_trace",
            "--output-dir",
            str(output),
            "--job-key",
            "unit",
            "--represented-job-key",
            "unit",
            "--commands-base64",
            _payload(["pytest -q tests/test_sample.py"]),
            "--repo-root",
            str(repo),
            "--python-only",
        ],
        cwd=repo,
        env=environment,
        check=False,
    )

    assert result.returncode == 0
    shard = output / "commands" / "000"
    trace_rows = [
        json.loads(line)
        for line in (shard / "python-trace.jsonl").read_text().splitlines()
    ]
    assert {row["file"] for row in trace_rows} == {"vllm/sample.py"}
    assert {row["test_id"] for row in trace_rows} == {
        "tests/test_sample.py::test_answer"
    }
    summary = json.loads((output / "trace-job.json").read_text())
    assert summary["capture_mode"] == "python-only"
    assert summary["collector_sha256"] == collector_sha256
    assert summary["healthy"] is True
    assert summary["retry_count"] == 2
    assert json.loads((shard / "job.json").read_text())["retry_count"] == 2


def test_one_command_merges_every_sequential_pytest_invocation(tmp_path: Path):
    repo = tmp_path / "repo"
    package = repo / "vllm"
    tests = repo / "tests"
    package.mkdir(parents=True)
    tests.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "sample.py").write_text(
        "def answer():\n    return 42\n", encoding="utf-8"
    )
    for name in ("first", "second"):
        (tests / f"test_{name}.py").write_text(
            "from vllm.sample import answer\n\n"
            f"def test_{name}():\n"
            "    assert answer() == 42\n",
            encoding="utf-8",
        )
    output = tmp_path / "trace"
    environment = dict(os.environ)
    environment.update(
        {
            "BUILDKITE_COMMIT": "a" * 40,
            "PATH": f"{Path(sys.executable).parent}:{environment['PATH']}",
            "PYTHONPATH": str(_module_root()),
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "test_selection.collector.run_job_trace",
            "--output-dir",
            str(output),
            "--job-key",
            "unit",
            "--represented-job-key",
            "unit",
            "--commands-base64",
            _payload(
                ["pytest -q tests/test_first.py && pytest -q tests/test_second.py"]
            ),
            "--repo-root",
            str(repo),
            "--python-only",
        ],
        cwd=repo,
        env=environment,
        check=False,
    )

    assert result.returncode == 0
    shard = output / "commands/000"
    manifest = json.loads((shard / "job.json").read_text(encoding="utf-8"))
    assert manifest["healthy"] is True
    assert manifest["pytest_invocations_started"] == 2
    assert manifest["pytest_invocations_exported"] == 2
    assert manifest["pytest_node_exports_complete"] is True
    assert set(manifest["node_ids"]) == {
        "tests/test_first.py::test_first",
        "tests/test_second.py::test_second",
    }
    assert (shard / "pytest-nodes.000.json").is_file()
    assert (shard / "pytest-nodes.001.json").is_file()


def test_command_local_pythonpath_keeps_pytest_plugins_importable(tmp_path: Path):
    repo = tmp_path / "repo"
    package = repo / "vllm"
    tests = repo / "tests"
    package.mkdir(parents=True)
    tests.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "sample.py").write_text(
        "def answer():\n    return 42\n", encoding="utf-8"
    )
    (tests / "test_first.py").write_text(
        "from vllm.sample import answer\n\n"
        "def test_first():\n    assert answer() == 42\n",
        encoding="utf-8",
    )
    (tests / "test_second.py").write_text(
        "from vllm.sample import answer\n\n"
        "def test_second():\n    assert answer() == 42\n",
        encoding="utf-8",
    )
    output = tmp_path / "trace"
    collector_root = tmp_path / "collector"
    with ZipFile(io.BytesIO(bundle_bytes())) as archive:
        archive.extractall(collector_root)
    environment = dict(os.environ)
    environment.update(
        {
            "BUILDKITE_COMMIT": "b" * 40,
            "PATH": f"{Path(sys.executable).parent}:{environment['PATH']}",
            "PYTHONPATH": str(collector_root),
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ci_test_selection.run_job_trace",
            "--output-dir",
            str(output),
            "--job-key",
            "unit",
            "--represented-job-key",
            "unit",
            "--commands-base64",
            _payload(
                [
                    "pytest -q tests/test_first.py",
                    f"PYTHONPATH={repo} pytest -q tests/test_second.py",
                ]
            ),
            "--repo-root",
            str(repo),
            "--python-only",
            "--preserve-command-exit-code",
        ],
        cwd=repo,
        env=environment,
        check=False,
    )

    assert result.returncode == 0
    summary = json.loads((output / "trace-job.json").read_text())
    assert summary["healthy"] is True
    assert summary["failure_reason"] is None
    assert [row["healthy"] for row in summary["command_results"]] == [True, True]
    assert [row["fallback_uninstrumented"] for row in summary["command_results"]] == [
        False,
        False,
    ]


@pytest.mark.parametrize(
    "command,expected_status",
    [("echo original-command-ran", 0), ("exit 7", 7)],
)
def test_in_place_collection_preserves_original_command_status(
    tmp_path: Path, command: str, expected_status: int
):
    repo = tmp_path / "repo"
    package = repo / "vllm"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    output = tmp_path / "trace"
    environment = dict(os.environ)
    environment["BUILDKITE_COMMIT"] = "c" * 40
    environment["PYTHONPATH"] = str(_module_root())

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "test_selection.collector.run_job_trace",
            "--output-dir",
            str(output),
            "--job-key",
            "unit",
            "--represented-job-key",
            "unit",
            "--commands-base64",
            _payload([command]),
            "--repo-root",
            str(repo),
            "--python-only",
            "--preserve-command-exit-code",
        ],
        cwd=repo,
        env=environment,
        check=False,
    )

    assert result.returncode == expected_status
    summary = json.loads((output / "trace-job.json").read_text())
    assert summary["command_results"][0]["command_exit_code"] == expected_status
    assert summary["healthy"] is False


def test_in_place_collection_falls_back_only_before_command_start(tmp_path: Path):
    repo = tmp_path / "repo"
    package = repo / "vllm"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "raise RuntimeError('preflight failure')\n", encoding="utf-8"
    )
    output = tmp_path / "trace"
    environment = dict(os.environ)
    environment["BUILDKITE_COMMIT"] = "d" * 40
    environment["PYTHONPATH"] = str(_module_root())

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "test_selection.collector.run_job_trace",
            "--output-dir",
            str(output),
            "--job-key",
            "unit",
            "--represented-job-key",
            "unit",
            "--commands-base64",
            _payload(["touch original-ran"]),
            "--repo-root",
            str(repo),
            "--python-only",
            "--preserve-command-exit-code",
        ],
        cwd=repo,
        env=environment,
        check=False,
    )

    assert result.returncode == 0
    assert (repo / "original-ran").is_file()
    summary = json.loads((output / "trace-job.json").read_text())
    assert summary["command_results"][0]["fallback_uninstrumented"] is True
    assert summary["command_results"][0]["failure_reason"] == (
        "collector_import_failed"
    )
    assert summary["failure_reason"] == "collector_import_failed"


def test_finished_command_is_not_rerun_after_collector_failure(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    output = tmp_path / "trace"

    def fake_run_command(*_args, command_index, output_dir, **_kwargs):
        command_output = output_dir / "commands" / f"{command_index:03d}"
        command_output.mkdir(parents=True)
        (command_output / "command-status.json").write_text(
            json.dumps({"command_executed": True, "exit_code": 0, "phase": "finished"}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess([], 13)

    def unexpected_fallback(*_args, **_kwargs):
        raise AssertionError("a completed command was rerun")

    monkeypatch.setattr(run_job_trace, "_run_command", fake_run_command)
    monkeypatch.setattr(run_job_trace, "_run_uninstrumented", unexpected_fallback)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_job_trace",
            "--output-dir",
            str(output),
            "--job-key",
            "unit",
            "--represented-job-key",
            "unit",
            "--commands-base64",
            _payload(["pytest tests/test_one.py"]),
            "--repo-root",
            str(repo),
            "--python-only",
            "--preserve-command-exit-code",
        ],
    )

    assert run_job_trace.main() == 0
    summary = json.loads((output / "trace-job.json").read_text())
    assert summary["command_results"][0]["collector_exit_code"] == 13
    assert summary["command_results"][0]["command_exit_code"] == 0
    assert summary["command_results"][0]["fallback_uninstrumented"] is False


def _trace_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    package = repo / "vllm"
    tests = repo / "tests"
    package.mkdir(parents=True)
    tests.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "sample.py").write_text(
        "def answer():\n    return 42\n", encoding="utf-8"
    )
    for name in ("first", "second"):
        (tests / f"test_{name}.py").write_text(
            "from vllm.sample import answer\n\n"
            f"def test_{name}():\n"
            "    assert answer() == 42\n",
            encoding="utf-8",
        )
    return repo


def _run_traced(tmp_path: Path, repo: Path, command: str) -> dict:
    output = tmp_path / "trace"
    environment = dict(os.environ)
    environment.update(
        {
            "BUILDKITE_COMMIT": "a" * 40,
            "PATH": f"{Path(sys.executable).parent}:{environment['PATH']}",
            "PYTHONPATH": str(_module_root()),
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "test_selection.collector.run_job_trace",
            "--output-dir",
            str(output),
            "--job-key",
            "unit",
            "--represented-job-key",
            "unit",
            "--commands-base64",
            _payload([command]),
            "--repo-root",
            str(repo),
            "--python-only",
        ],
        cwd=repo,
        env=environment,
        check=False,
    )
    assert result.returncode == 0
    return json.loads((output / "commands" / "000" / "job.json").read_text())


def test_python_m_pytest_allocates_invocation_ids(tmp_path: Path):
    """Harnesses calling `python -m pytest` bypass the PATH launcher shim;
    the plugin itself must allocate so exports never land unsuffixed."""

    repo = _trace_repo(tmp_path)
    manifest = _run_traced(
        tmp_path,
        repo,
        "python3 -m pytest -q tests/test_first.py && "
        "python3 -m pytest -q tests/test_second.py",
    )
    assert manifest["healthy"] is True
    assert manifest["pytest_invocations_started"] == 2
    assert manifest["pytest_invocations_exported"] == 2
    assert manifest["pytest_node_exports_complete"] is True
    shard = tmp_path / "trace" / "commands" / "000"
    assert (shard / "pytest-nodes.000.json").is_file()
    assert (shard / "pytest-nodes.001.json").is_file()
    assert set(manifest["node_ids"]) == {
        "tests/test_first.py::test_first",
        "tests/test_second.py::test_second",
    }


def test_python_m_pytest_single_invocation_accounts(tmp_path: Path):
    repo = _trace_repo(tmp_path)
    manifest = _run_traced(tmp_path, repo, "python3 -m pytest -q tests/test_first.py")
    assert manifest["pytest_invocations_started"] == 1
    assert manifest["pytest_invocations_exported"] == 1
    assert manifest["pytest_node_exports_complete"] is True


def test_mixed_launcher_and_module_invocations_never_collide(tmp_path: Path):
    repo = _trace_repo(tmp_path)
    manifest = _run_traced(
        tmp_path,
        repo,
        "pytest -q tests/test_first.py && python3 -m pytest -q tests/test_second.py",
    )
    assert manifest["healthy"] is True
    assert manifest["pytest_invocations_started"] == 2
    assert manifest["pytest_invocations_exported"] == 2
    assert manifest["pytest_node_exports_complete"] is True
