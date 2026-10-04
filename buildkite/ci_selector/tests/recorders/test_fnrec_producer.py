"""The recorder's output has to be readable by the reader that consumes it.

`coverage/model.py` parses these files and `scripts/build.py` folds them into
the table. Nothing else checks that the two agree: the recorder ships as a
standalone payload fetched at step time, so a format change on either side
would be found by a nightly sweep that quietly produced empty rows.

This runs the real recorder in a subprocess against a throwaway `vllm` package
and reads the result back with the real `read_process`.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from ci_selector.coverage.model import read_process

FNREC = Path(__file__).resolve().parents[2] / "recorders" / "fnrec"

VLLM_STUB = """\
def top_level():
    return 1


class Engine:
    def start(self):
        return top_level()


def branchy(rare):
    if rare:
        return 1
    return 2
"""
# Lines of VLLM_STUB: branchy's untaken branch, and the line it does run.
UNTAKEN_LINE = 12
TAKEN_LINE = 13


TESTS_STUB = """\
def helper():
    return 2
"""

# vLLM code calling a watched library: once while being imported, which is
# not use, and at runtime, which is. Plus a copy vendored under vllm/.
USES_LIBS_STUB = """\
import flashinfer.sampling

from vllm.third_party import deep_gemm

flashinfer.sampling.at_import()
flashinfer.sampling.at_both()


def run():
    flashinfer.sampling.top_k(1)
    flashinfer.sampling.at_both()
    deep_gemm.fp8_gemm_nt()
"""

FLASHINFER_INIT = """\
from . import sampling


def _setup():
    return 0


_setup()
"""

FLASHINFER_SAMPLING = """\
def _helper(x):
    return x


def top_k(x):
    return _helper(x)


def at_import():
    return 5


def at_both():
    return 6
"""

DEEP_GEMM_STUB = """\
def _inner():
    return 7


def fp8_gemm_nt():
    return _inner()
"""

PROGRAM = (
    "import vllm, vllm.uses_libs, tests.helpers, flashlib; "
    "tests.helpers.helper(); flashlib.go(); vllm.Engine().start(); "
    "vllm.branchy(False); "
    "vllm.uses_libs.run()"
)


@pytest.fixture(scope="module")
def recorded(tmp_path_factory):
    """Install the recorder the way a plugin-less step does, and run it once.

    Host mode rather than container mode: it installs into a directory on
    PYTHONPATH, so this needs no second interpreter, and it is the path that is
    otherwise only exercised on an agent.
    """
    root = tmp_path_factory.mktemp("fnrec")
    (root / "vllm").mkdir()
    (root / "vllm" / "__init__.py").write_text(VLLM_STUB)
    (root / "vllm" / "uses_libs.py").write_text(USES_LIBS_STUB)
    (root / "vllm" / "third_party" / "deep_gemm").mkdir(parents=True)
    (root / "vllm" / "third_party" / "__init__.py").write_text("")
    (root / "vllm" / "third_party" / "deep_gemm" / "__init__.py").write_text(
        DEEP_GEMM_STUB
    )
    # The checkout's tests package, and an installed library.
    (root / "tests").mkdir()
    (root / "tests" / "__init__.py").write_text("")
    (root / "tests" / "helpers.py").write_text(TESTS_STUB)
    site = root / "env" / "site-packages"
    (site / "flashlib").mkdir(parents=True)
    (site / "flashlib" / "__init__.py").write_text("def go():\n    return 3\n")
    (site / "flashinfer").mkdir()
    (site / "flashinfer" / "__init__.py").write_text(FLASHINFER_INIT)
    (site / "flashinfer" / "sampling.py").write_text(FLASHINFER_SAMPLING)
    lib, out = root / "lib", root / "rec"
    out.mkdir()

    staging = root / "payload"
    staging.mkdir()
    for name in ("fnrec.py", "fnrec_pytest.py", "host_install.py"):
        (staging / name).write_bytes((FNREC / name).read_bytes())
    installed = subprocess.run(
        [sys.executable, str(staging / "host_install.py")],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "FNREC_LIB": str(lib), "PYTHONPATH": str(root)},
    )
    assert installed.returncode == 0, installed.stderr
    assert (lib / "fnrec.py").is_file() and (lib / "sitecustomize.py").is_file()

    run = subprocess.run(
        [sys.executable, "-c", PROGRAM],
        capture_output=True,
        text=True,
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": f"{lib}:{root}:{site}",
            "FNREC_OUT": str(out),
            "FNREC_ROOT": str(root / "vllm"),
        },
    )
    assert run.returncode == 0, run.stderr
    assert not run.stderr, f"the recorder wrote to stderr: {run.stderr}"
    files = sorted(out.glob("fn.*.txt"))
    assert files, "the recorder wrote nothing"
    return files


def test_the_reader_parses_what_the_recorder_writes(recorded):
    record = read_process(recorded[0])
    assert record is not None, "read_process rejected the recorder's own output"
    assert record.clean_exit, "no #end marker, so the reader counts this as killed"
    assert record.malformed == 0
    assert record.errors == 0


def test_every_function_entered_is_recorded(recorded):
    record = read_process(recorded[0])
    names = {name for names in record.functions.values() for name in names}
    # The module body, the class body, the method and the plain function. The
    # method is qualified, so fifty different `forward`s stay distinct.
    assert {"<module>", "Engine", "Engine.start", "top_level"} <= names


def test_recorded_paths_are_relative_to_the_recorder_scope(recorded):
    """The table is keyed by these paths, so a leading `vllm/` is load-bearing
    for every lookup the selector does."""
    record = read_process(recorded[0])
    assert record.functions, "nothing under the root was recorded"
    assert all(path.startswith(("vllm/", "tests/")) for path in record.functions), (
        record.functions
    )
    assert "vllm/__init__.py" in record.functions


def test_the_tests_package_is_recorded_by_function(recorded):
    """A test helper changes as often as the code it tests; without its names
    the selector can only route it by file."""
    record = read_process(recorded[0])
    assert record.functions.get("tests/helpers.py", set()) >= {"<module>", "helper"}
    assert record.outside_root == 0, "the reader could not place a tests/ line"


def test_installed_libraries_are_recorded_by_package_only(recorded):
    record = read_process(recorded[0])
    assert "flashlib" in record.packages
    assert not any("flashlib" in path for path in record.functions)
    assert record.malformed == 0


def test_watched_libraries_are_recorded_by_the_calls_into_them(recorded):
    """The entry points code outside the library called at runtime. Not its
    internals, not its module bodies, and not what ran during an import."""
    record = read_process(recorded[0])
    assert record.libcalls["flashinfer"] == {
        "flashinfer.sampling.top_k",
        "flashinfer.sampling.at_both",
    }
    # The vendored copy under vllm/ spells its names the same way, and is not
    # also recorded as vLLM code: it is build output the table cannot name.
    assert record.libcalls["deep_gemm"] == {"deep_gemm.fp8_gemm_nt"}
    assert not any("third_party/deep_gemm" in path for path in record.functions)
    # Watched and never called still says so.
    assert record.libcalls["triton"] == set()
    assert "flashinfer" in record.packages
    assert record.malformed == 0


def test_the_recorder_watches_the_libraries_the_selector_routes():
    """The recorder cannot import ci_selector, so the list is written twice."""
    from ci_selector.coverage.libraries import LIBRARY_PINS

    assert set(_load("fnrec")._LIBS) == set(LIBRARY_PINS)


def test_the_counter_is_a_floor_on_what_was_recorded(recorded):
    record = read_process(recorded[0])
    written = sum(len(names) for names in record.functions.values())
    assert record.counter is not None, "no #end counter, so no floor to check against"
    assert record.counter >= written


def _load(name):
    """The recorder is a standalone payload, so load it by path."""
    spec = importlib.util.spec_from_file_location(name, FNREC / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "argv,expected",
    [
        (
            ["/venv/bin/pytest", "-v", "-s", "v1/kv_connector"],
            "pytest -v -s v1/kv_connector",
        ),
        (["/venv/bin/vllm", "serve", "Qwen/Qwen3-0.6B"], "vllm serve Qwen/Qwen3-0.6B"),
        (
            ["/x/vllm", "serve", "--api-key", "sk-real-secret"],
            "vllm serve --api-key <redacted>",
        ),
        (["/x/vllm", "--hf-token=hf_real_secret"], "vllm --hf-token=<redacted>"),
    ],
)
def test_recorded_argv_identifies_the_process_without_leaking(
    monkeypatch, argv, expected
):
    """The header goes into a public build artifact, and argv is the only field
    whose content is unbounded: an env allowlist cannot catch a key passed on a
    command line. It still has to tell `vllm serve` from `pytest`."""
    fnrec = _load("fnrec")
    monkeypatch.setattr(fnrec.sys, "argv", argv)
    assert fnrec._argv() == expected


def test_the_recorded_env_cannot_carry_a_credential():
    """Names are listed one by one, never by prefix: this file is uploaded, and
    a prefix match on BUILDKITE_ or HF_ would sweep up access tokens."""
    fnrec = _load("fnrec")
    assert fnrec._ENV_KEYS, "the allowlist is empty, so nothing is being recorded"
    assert all("*" not in key for key in fnrec._ENV_KEYS), "a prefix is not a name"
    for dangerous in (
        "BUILDKITE_AGENT_ACCESS_TOKEN",
        "HF_TOKEN",
        "CODECOV_TOKEN",
        "BUILDKITE_ANALYTICS_TOKEN",
    ):
        assert dangerous not in fnrec._ENV_KEYS, dangerous


def test_the_recorder_arms_only_when_both_variables_are_set(tmp_path):
    """A missing root would record against the wrong tree while every other
    signal looked healthy, so the recorder refuses to start instead."""
    out = tmp_path / "rec"
    out.mkdir()
    for env in ({"FNREC_OUT": str(out)}, {"FNREC_ROOT": str(tmp_path)}, {}):
        subprocess.run(
            [sys.executable, str(FNREC / "fnrec.py")],
            capture_output=True,
            env={"PATH": "/usr/bin:/bin", **env},
            check=False,
        )
        assert not list(out.glob("fn.*.txt")), f"armed with only {sorted(env)}"


def test_the_lines_a_process_ran_are_recorded(recorded):
    """A changed line a job never ran is one the job cannot break."""
    record = read_process(recorded[0])
    assert record.records_lines and not record.lost_line_records
    ran = record.lines["vllm/__init__.py"]
    assert {2, 7, TAKEN_LINE} <= ran, ran
    assert UNTAKEN_LINE not in ran
    assert "tests/helpers.py" in record.lines
    assert not any("flashlib" in path for path in record.lines)
