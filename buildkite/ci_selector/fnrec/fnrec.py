# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""The Python function recorder: which vLLM functions did this process enter?

The producing half of the coverage record that `ci_selector/coverage`
reads. It subscribes to CPython's `sys.monitoring` PY_START event and
returns DISABLE from the callback, so every code object costs one event in
the life of the process and a step's row is the set of functions it ran.

It is loaded into every Python process of a CI step by a `.pth` file that
`ci_setup.sh` drops into site-packages (one line: `import fnrec`), so the
engine core, tensor-parallel workers, Ray workers and pytest itself all
record, each into its own file. Nothing here may break the process it rides
in: every failure is counted, written as an `#error` line where possible,
and otherwise ignored.

Switched on by `FNREC_DIR`. Unset, importing this module does nothing, so a
PR build with the `.pth` in place still pays nothing.

Output, `$FNREC_DIR/fn.<pid>.txt`, in the shape `coverage/model.py` reads:

    #start  pid=4242  root=/usr/local/lib/python3.12/dist-packages/vllm/  py=3.12.13  BUILDKITE_JOB_ID=...  BUILDKITE_RETRY_COUNT=0
    #root   /usr/local/lib/python3.12/dist-packages/vllm/  t=<epoch>
    /usr/local/lib/python3.12/dist-packages/vllm/engine/llm_engine.py  LLMEngine.step  1
    ...
    #stat   root=<records so far>  other=<events outside the root>  errors=<n>  last_error=  t=<epoch>
    #end    root=<records>  other=<...>  errors=<...>  last_error=  t=<epoch>

`root` is the installed vLLM package directory. Only functions under it are
written; everything else (stdlib, torch, tests) is counted in `other`. The
reader turns `<root>/engine/llm_engine.py` into `vllm/engine/llm_engine.py`.
`#stat` every 1000 records is a floor the reader checks `data_lines` against;
`#end` is the clean-exit marker, which a SIGKILLed engine core never writes,
and the stamp counts how many processes did.

Environment:
    FNREC_DIR     where to write; required, or nothing happens
    FNREC_ROOT    the package directory to record under; default: wherever
                  `vllm` is importable from, found without importing it
"""

from __future__ import annotations

import atexit
import os
import platform
import sys
import threading
import time

STAT_EVERY = 1000
MAX_ERROR_LINES = 20

# Re-entrant, and guarded: PY_START fires for the recorder's own helpers too,
# and a plain lock taken inside the callback deadlocks the first time a helper
# runs under it. The guard also keeps our own code objects out of the count.
_lock = threading.RLock()
_local = threading.local()
_state: dict = {}


def _find_vllm_root() -> str | None:
    """The vLLM package directory, without importing vLLM (importing it here
    would run the whole package at interpreter start)."""
    override = os.environ.get("FNREC_ROOT")
    if override:
        return override if override.endswith("/") else override + "/"
    mod = sys.modules.get("vllm")
    file = getattr(mod, "__file__", None)
    if file:
        return os.path.dirname(file) + "/"
    try:
        import importlib.util

        spec = importlib.util.find_spec("vllm")
    except Exception:  # noqa: BLE001 - a broken finder must not break the process
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    return list(spec.submodule_search_locations)[0].rstrip("/") + "/"


def _open(directory: str, root: str | None) -> None:
    """Start a fresh file for this pid. Called at install and after a fork."""
    os.makedirs(directory, exist_ok=True)
    try:
        os.chmod(directory, 0o777)
    except OSError:
        pass
    pid = os.getpid()
    path = os.path.join(directory, f"fn.{pid}.txt")
    fh = open(path, "a", buffering=1, encoding="utf-8", errors="replace")  # noqa: SIM115
    try:
        os.chmod(path, 0o666)
    except OSError:
        pass
    _state.update(
        pid=pid,
        fh=fh,
        root=root,
        root_written=False,
        n_root=0,
        n_other=0,
        errors=0,
        last_error="",
        error_lines=0,
    )
    fh.write(
        "\t".join(
            [
                "#start",
                f"pid={pid}",
                f"root={root or ''}",
                f"py={platform.python_version()}",
                f"BUILDKITE_JOB_ID={os.environ.get('BUILDKITE_JOB_ID', '')}",
                f"BUILDKITE_RETRY_COUNT={os.environ.get('BUILDKITE_RETRY_COUNT', '')}",
            ]
        )
        + "\n"
    )
    if root:
        _write_root(root)


def _write_root(root: str) -> None:
    _state["fh"].write(f"#root\t{root}\tt={int(time.time())}\n")
    _state["root_written"] = True


def _counters(tag: str) -> str:
    s = _state
    return (
        f"{tag}\troot={s['n_root']}\tother={s['n_other']}\terrors={s['errors']}"
        f"\tlast_error={s['last_error']}\tt={int(time.time())}\n"
    )


def _on_start(code, instruction_offset):
    """PY_START for a code object we have not seen: record it, then disable
    the event for it. The return value is what makes this cheap."""
    if getattr(_local, "busy", False):
        return sys.monitoring.DISABLE  # one of our own helpers, called from below
    _local.busy = True
    try:
        s = _state
        if s.get("pid") != os.getpid():
            return sys.monitoring.DISABLE  # after a fork, before the hook re-opened
        root = s["root"]
        if root is None:
            root = s["root"] = _find_vllm_root()
            if root is not None:
                with _lock:
                    _write_root(root)
        filename = code.co_filename
        with _lock:
            if root is not None and filename.startswith(root):
                s["fh"].write(f"{filename}\t{code.co_qualname}\t1\n")
                s["n_root"] += 1
                if s["n_root"] % STAT_EVERY == 0:
                    s["fh"].write(_counters("#stat"))
            else:
                s["n_other"] += 1
    except Exception as exc:  # noqa: BLE001 - never propagate into the workload
        try:
            with _lock:
                _state["errors"] += 1
                _state["last_error"] = type(exc).__name__
                if _state["error_lines"] < MAX_ERROR_LINES:
                    _state["error_lines"] += 1
                    _state["fh"].write(f"#error\t{type(exc).__name__}: {exc}\n")
        except Exception:  # noqa: BLE001
            pass
    finally:
        _local.busy = False
    return sys.monitoring.DISABLE


def _at_exit() -> None:
    try:
        s = _state
        if s.get("pid") == os.getpid() and s.get("fh"):
            with _lock:
                s["fh"].write(_counters("#end"))
                s["fh"].close()
                s["fh"] = None
    except Exception:  # noqa: BLE001
        pass


def _after_fork_in_child() -> None:
    """A forked child inherits the parent's open file and the parent's pid
    stamp; give it its own file. Functions the parent already disabled stay
    disabled in the child, which is fine: the step's row is the union."""
    try:
        s = _state
        directory = s.get("dir")
        if not directory:
            return
        if s.get("fh"):
            try:
                s["fh"].close()  # the parent's handle; the parent keeps its own
            except Exception:  # noqa: BLE001
                pass
        _open(directory, s.get("root"))
    except Exception:  # noqa: BLE001
        pass


def _claim_tool_id():
    mon = sys.monitoring
    for tool in (mon.PROFILER_ID, 3, 4):
        try:
            mon.use_tool_id(tool, "fnrec")
            return tool
        except ValueError:
            continue
    return None


def install(directory: str | None = None) -> bool:
    """Start recording into `directory` (default `$FNREC_DIR`). Returns False,
    having done nothing, when there is nowhere to write, the interpreter is
    too old, or no monitoring tool slot is free."""
    directory = directory or os.environ.get("FNREC_DIR")
    if not directory or _state.get("fh"):
        return False
    if sys.version_info < (3, 12) or not hasattr(sys, "monitoring"):
        return False
    tool = _claim_tool_id()
    if tool is None:
        return False
    try:
        _state["dir"] = directory
        _open(directory, _find_vllm_root())
    except OSError:
        return False
    mon = sys.monitoring
    mon.register_callback(tool, mon.events.PY_START, _on_start)
    mon.set_events(tool, mon.events.PY_START)
    _state["tool"] = tool
    atexit.register(_at_exit)
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=_after_fork_in_child)
    return True


if os.environ.get("FNREC_DIR"):
    try:
        install()
    except Exception:  # noqa: BLE001 - imported from a .pth; never break startup
        pass
