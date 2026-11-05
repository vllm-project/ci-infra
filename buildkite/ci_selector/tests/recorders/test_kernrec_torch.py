"""kernrec_torch.py: the recorder steps aside before a CUDA torch.profiler.

Runs each case in a fresh interpreter against a stand-in torch package, the way
the .pth loads it in CI: before torch is imported.
"""

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

KERNREC = Path(__file__).resolve().parents[2] / "recorders" / "kernrec"

FAKE_PROFILER = """
from enum import Enum

class ProfilerActivity(Enum):
    CPU = 0
    CUDA = 1

class profile:
    def __init__(self, activities=(ProfilerActivity.CPU, ProfilerActivity.CUDA)):
        self.kineto_activities = set(activities)
        self.prepared = False

    def _prepare_trace(self):
        self.prepared = True
"""

CASE = """
import json, os, sys
import kernrec_torch  # what kernrec_torch.pth runs at startup
if os.environ.get("PRELOAD"):
    import ctypes
    ctypes.CDLL(os.environ["PRELOAD"])  # the driver's dlopen at cuInit
from torch.autograd import profiler
acts = [profiler.ProfilerActivity[a] for a in os.environ["ACTIVITIES"].split(",")]
prof = profiler.profile(acts)
prof._prepare_trace()
print(json.dumps({
    "prepared": prof.prepared,
    "patched": getattr(profiler.profile._prepare_trace, "_kernrec", False),
    "released_pid": os.environ.get("KERNREC_RELEASED_PID"),
    "pid": str(os.getpid()),
}))
"""


@pytest.fixture
def fake_torch(tmp_path):
    pkg = tmp_path / "torch" / "autograd"
    pkg.mkdir(parents=True)
    (tmp_path / "torch" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "profiler.py").write_text(FAKE_PROFILER)
    return tmp_path


def _run(fake_torch, activities="CPU,CUDA", injection="", preload=""):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"CUDA_INJECTION64_PATH", "KERNREC_RELEASED_PID"}
    }
    env.update(
        PYTHONPATH=f"{KERNREC}{os.pathsep}{fake_torch}",
        ACTIVITIES=activities,
        PRELOAD=preload,
    )
    if injection:
        env["CUDA_INJECTION64_PATH"] = injection
    out = subprocess.run(
        [sys.executable, "-c", CASE],
        env=env,
        cwd=fake_torch,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_unarmed_job_is_untouched(fake_torch):
    r = _run(fake_torch)
    assert r["prepared"] and not r["patched"] and r["released_pid"] is None


def test_profiler_before_cuda_init_keeps_recorder_off_in_this_process(fake_torch):
    r = _run(fake_torch, injection=str(fake_torch / "libkernrec-not-loaded.so"))
    assert r["prepared"] and r["patched"]
    assert r["released_pid"] == r["pid"]


def test_cpu_only_profiler_keeps_recording(fake_torch):
    r = _run(fake_torch, activities="CPU", injection=str(fake_torch / "x.so"))
    assert r["prepared"] and r["patched"] and r["released_pid"] is None


@pytest.mark.skipif(shutil.which("cc") is None, reason="needs a C compiler")
def test_loaded_recorder_is_released(fake_torch, tmp_path):
    marker = tmp_path / "released"
    src = tmp_path / "rec.c"
    src.write_text(
        textwrap.dedent(
            f"""
            #include <stdio.h>
            int kernrec_release(void) {{
              FILE *f = fopen("{marker}", "a");
              if (f) {{ fputs("x", f); fclose(f); }}
              return 1;
            }}
            """
        )
    )
    lib = tmp_path / "librec.so"
    subprocess.run(["cc", "-shared", "-fPIC", str(src), "-o", str(lib)], check=True)
    r = _run(fake_torch, injection=str(lib), preload=str(lib))
    assert r["prepared"] and r["patched"] and r["released_pid"] is None
    assert marker.read_text() == "x"
