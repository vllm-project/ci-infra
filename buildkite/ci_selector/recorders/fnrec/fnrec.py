"""Record which vLLM functions were entered, and which libraries.

The selector only asks one bit per function: did this job enter it? Line
coverage answers that but records every line to do so, which is where its
slowdown comes from. sys.monitoring answers it directly: subscribe to
PY_START and return DISABLE, so each function costs one event, once.

Two roots are recorded by function: the vllm package, and the checkout's
tests/ package once something imports it. A test helper changes as often as
the code it tests, and without its names the selector can only route it by
file. Everything else is recorded by top-level package only, one `#pkg` line
each. Entering a package says little: vLLM imports most of them at startup.

A short list of libraries is recorded by call as well: the functions in them
that code outside the library called at runtime, one `#lib` line each. That
is what a dependency bump routes on, the jobs that called flashinfer rather
than every job in the image.

Needs FNREC_OUT and FNREC_ROOT. Starts on the first `vllm` or `aiter` import
rather than at interpreter startup, leaving other infrastructure alone.

The rest exists because this runs on machines we cannot reach, so every
artifact has to be diagnosable afterwards. A process that recorded nothing
must still say so, and why: silence is the one outcome we cannot read.
"""

import os
import sys
import threading

_OUT = os.environ.get("FNREC_OUT")
_ROOT_ENV = os.environ.get("FNREC_ROOT")

# Named one by one, never a prefix: this file is uploaded, and BUILDKITE_*
# or HF_* would sweep up access tokens.
_ENV_KEYS = (
    "BUILDKITE_JOB_ID",
    "BUILDKITE_PIPELINE_SLUG",
    "BUILDKITE_STEP_KEY",
    "BUILDKITE_LABEL",
    "BUILDKITE_RETRY_COUNT",
    "BUILDKITE_PARALLEL_JOB",
    "BUILDKITE_PARALLEL_JOB_COUNT",
    "VLLM_WORKER_MULTIPROC_METHOD",
    "CUDA_VISIBLE_DEVICES",
    "ROCR_VISIBLE_DEVICES",
    "HIP_VISIBLE_DEVICES",
    "VLLM_ROCM_USE_AITER",
    "VLLM_ROCM_USE_AITER_RMSNORM",
    "VLLM_ROCM_USE_AITER_LINEAR",
    "VLLM_ROCM_USE_AITER_MOE",
    "VLLM_ROCM_USE_AITER_MLA",
    "VLLM_ROCM_USE_AITER_MHA",
    "VLLM_ROCM_USE_AITER_UNIFIED_ATTENTION",
    "VLLM_ROCM_USE_AITER_CUSTOM_AR",
    "VLLM_ROCM_USE_AITER_TRITON_GEMM",
    "VLLM_ROCM_USE_AITER_TRITON_ROPE",
    "AITER_TRITON_ONLY",
    "AITER_AOT_IMPORT",
)

_STAT_EVERY = 500
_CO_OPTIMIZED = 0x0001  # inspect.CO_OPTIMIZED: set on functions, not bodies
_MAX_ROOT_TRIES = 1000

_root = None
_root_tries = 0
_seen = set()
_lock = threading.Lock()
_fh = None
_fh_pid = None
_nonce = os.urandom(4).hex()
_host = ""
_tool_id = None
_hooks_pid = None
_origin = "import"
_root_logged = False
_tests_root = None
_tests_logged = False
_packages = set()
_libcalls = set()
_stats = {"root": 0, "other": 0, "errors": 0, "last_error": ""}
_ended = False

# Where installed libraries live. A path under one names its package by the
# next segment: .../site-packages/flashinfer/sampling.py is flashinfer.
_SITE_DIRS = (os.sep + "site-packages" + os.sep, os.sep + "dist-packages" + os.sep)

# Libraries recorded by call, by top-level package. Torch is left out on
# purpose: a torch bump runs everything. This file imports nothing from
# ci_selector, so a test pins the list to handwritten.LIBRARY_PINS.
_LIBS = ("deep_gemm", "flash_attn", "flashinfer", "triton")
# vLLM also vendors some of them under its own tree, where they are gitignored
# build output the table cannot name, so they are recorded as the library.
_VENDORED = "third_party" + os.sep
_IMPORTING = "<frozen importlib._bootstrap"


def _now():
    import time

    return round(time.time(), 3)


def _resolve_root():
    """Where vllm is, preferring the live module over the env var.

    Returns None while neither works, leaving the callback armed to try
    again: _begin() runs inside find_spec, before vllm is in sys.modules.

    A FNREC_ROOT that does not exist is rejected rather than used. It would
    match nothing while looking healthy.
    """
    mod = sys.modules.get("vllm")
    path = getattr(mod, "__path__", None)
    if path:
        try:
            return os.path.join(list(path)[0], "")
        except Exception:
            pass
    if _ROOT_ENV and os.path.isdir(_ROOT_ENV):
        return os.path.join(_ROOT_ENV, "")
    return None


def _resolve_tests_root():
    """Where the checkout's tests package is, once imported, else None.

    Asked of the live module, like the vllm root: pytest imports the package
    before any file in it runs, so its __path__ is set by then. One under a
    site directory is some wheel's stray `tests` package, not ours.
    """
    mod = sys.modules.get("tests")
    path = getattr(mod, "__path__", None)
    if not path:
        return None
    try:
        # A namespace package can span several directories.
        for entry in list(path):
            root = os.path.join(entry, "")
            if not any(d in root for d in _SITE_DIRS):
                return root
    except Exception:
        pass
    return None


def _package_of(filename):
    """The top-level package an installed file belongs to, or None for the
    standard library, frozen modules, and anything not installed."""
    # AITER CI commonly installs from a source checkout, outside site-packages.
    aiter = sys.modules.get("aiter")
    for root in getattr(aiter, "__path__", ()):
        if filename.startswith(os.path.join(os.path.abspath(root), "")):
            return "aiter"
    for marker in _SITE_DIRS:
        i = filename.rfind(marker)
        if i >= 0:
            top = filename[i + len(marker) :].split(os.sep, 1)[0]
            if top.endswith(".py"):
                top = top[:-3]
            # .pth hooks, dist-info and the like carry no code anyone calls.
            if top and top.isidentifier():
                return top
            return None
    return None


def _out():
    """Line-buffered handle for this process, created with a header.

    Written as each function is first seen, not batched. With DISABLE that is
    one write per function. Batching lost the teardown functions, which run
    just before a process is killed, and they read as never executed.

    Buffered, not raw: a raw write can land partially and corrupt the record
    under the disk-full conditions that make the counters worth having.
    """
    global _fh, _fh_pid
    pid = os.getpid()
    if _fh is None or _fh_pid != pid:
        name = f"fn.{_host}.{_nonce}.{pid}.txt"
        _fh = open(os.path.join(_OUT, name), "a", buffering=1)
        _fh_pid = pid
        _fh.write(_header(pid))
        _arm_exit_hooks(pid)
    return _fh


def _arm_exit_hooks(pid):
    """Register the clean-exit marker in whichever process we are now in.

    Not at startup or in the fork hook: multiprocessing clears the inherited
    finalizer registry in the child after fork hooks run, so anything earlier
    is dropped. Doing it on first write lands after that in every process.
    """
    global _hooks_pid
    if _hooks_pid == pid:
        return
    _hooks_pid = pid
    import atexit

    atexit.register(_end)
    try:
        # A fork child exits through os._exit() and never runs atexit, but
        # does run multiprocessing's exit function, which Finalize hangs on.
        from multiprocessing.util import Finalize

        Finalize(None, _end, exitpriority=5)
    except Exception:
        pass


_SECRETISH = ("key", "token", "secret", "passwd", "password")


def _argv():
    """Enough of the command line to tell processes apart, and no more.

    Drops the interpreter's directory, the longest and least useful part.
    argv can carry keys and signed URLs, so secret-looking values go too.
    """
    parts = [os.path.basename(sys.argv[0])] if sys.argv else []
    redact_next = False
    for arg in sys.argv[1:7]:
        if redact_next:
            parts.append("<redacted>")
            redact_next = False
            continue
        low = arg.lower()
        if any(s in low for s in _SECRETISH):
            parts.append(arg.split("=", 1)[0] + "=<redacted>" if "=" in arg else arg)
            redact_next = "=" not in arg
            continue
        parts.append(arg[:48])
    return " ".join(parts)[:160]


def _header(pid):
    fields = [
        "#start",
        f"pid={pid}",
        f"ppid={os.getppid()}",
        f"host={_host}",
        f"nonce={_nonce}",
        f"origin={_origin}",
        f"root={_root or ''}",
        f"root_env={_ROOT_ENV or ''}",
        f"tool={_tool_id}",
        # Which libraries this recorder watches. A reader seeing none knows
        # the process predates `#lib` lines, so their absence proves nothing.
        f"libs={','.join(_LIBS)}",
        f"py={sys.version.split()[0]}",
        f"exe={sys.executable}",
        f"argv={_argv()!r}",
        f"t={_now()}",
    ]
    fields += [f"{k}={os.environ.get(k, '')}" for k in _ENV_KEYS]
    return "\t".join(fields) + "\n"


def _stat_line(tag):
    return (
        f"{tag}\troot={_stats['root']}\tother={_stats['other']}"
        f"\terrors={_stats['errors']}\tlast_error={_stats['last_error']}"
        f"\tt={_now()}\n"
    )


def _end():
    """Mark a clean exit, so its absence means the process was killed.

    Both atexit and multiprocessing's Finalize, because neither covers every
    case: a fork child exits through os._exit() and never runs atexit, and
    fork is vLLM's default. Nothing survives SIGKILL, which is the point.
    """
    global _ended
    if _ended or _fh is None:
        return
    _ended = True
    try:
        _fh.write(_stat_line("#end"))
        _fh.flush()
    except Exception:
        pass


def _on_py_start(code, instruction_offset):
    global _root, _root_logged, _root_tries, _tests_root
    if _root is None:
        _root = _resolve_root()
        if _root is None:
            # vllm is mid-import and has no __path__ yet. Stay armed, but
            # not forever: if the import raises the root never resolves and
            # the callback would run on every call for the rest of the run.
            _root_tries += 1
            if _root_tries < _MAX_ROOT_TRIES:
                return None
            return sys.monitoring.DISABLE
    filename = code.co_filename
    if not filename.startswith(_root):
        if _tests_root is None:
            _tests_root = _resolve_tests_root()
        if _tests_root is None or not filename.startswith(_tests_root):
            _stats["other"] += 1
            if _note_package(filename) in _LIBS:
                return _note_libcall(_library_of(filename), code, sys._getframe(1))
            return sys.monitoring.DISABLE
    elif filename.startswith(_VENDORED, len(_root)):
        lib = _library_of(filename)
        if lib is not None:
            return _note_libcall(lib, code, sys._getframe(1))
    key = f"{filename}\t{code.co_qualname}\t{code.co_firstlineno}"
    with _lock:
        if _tests_root is not None and not _tests_logged:
            _log_tests_root()
        if not _root_logged:
            # The header goes out before the root is known, so record the
            # value in force. A record is unreadable without it.
            _root_logged = True
            try:
                _out().write(f"#root\t{_root}\tt={_now()}\n")
            except Exception:
                pass
        if key not in _seen:
            _seen.add(key)
            _stats["root"] += 1
            try:
                fh = _out()
                fh.write(key + "\n")
                if _stats["root"] % _STAT_EVERY == 0:
                    fh.write(_stat_line("#stat"))
            except Exception as exc:
                # Losing the write loses the function: DISABLE is already
                # promised and it will not report again. Count it so a thin
                # record is visibly thin.
                _stats["errors"] += 1
                _stats["last_error"] = repr(exc)[:200].replace("\t", " ")
    return sys.monitoring.DISABLE


def _log_tests_root():
    """Once per process, before its first tests/ line, so the reader can map
    those lines. Two fields: an older reader skips it as malformed rather
    than reading it as a function. Called under the lock."""
    global _tests_logged
    _tests_logged = True
    try:
        _out().write(f"#tests\t{_tests_root}\n")
    except Exception:
        pass


def _note_package(filename):
    """One `#pkg` line per installed package this process enters. Returns
    the package, or None."""
    pkg = _package_of(filename)
    if pkg is None or pkg in _packages:
        return pkg
    with _lock:
        if pkg in _packages:
            return pkg
        _packages.add(pkg)
        try:
            _out().write(f"#pkg\t{pkg}\n")
        except Exception as exc:
            _stats["errors"] += 1
            _stats["last_error"] = repr(exc)[:200].replace("\t", " ")
    return pkg


def _note_libcall(lib, code, frame):
    """One `#lib` line per function of a watched library that code outside it
    called at runtime: the entry points, not the library's own internals.

    Three outcomes, and only one records. A call from inside the library, or a
    module or class body, is never an entry: DISABLE. A call made while a
    module is being imported is a startup side effect, not use, so the event
    stays armed for a later runtime call. Anything else is an entry, recorded
    once. Either way the steady state costs nothing: every code object ends
    disabled on its first runtime call.
    """
    pkg, rel = lib or (None, "")
    caller = frame.f_back
    if pkg is None or not code.co_flags & _CO_OPTIMIZED or caller is None:
        return sys.monitoring.DISABLE
    inside = _library_of(caller.f_code.co_filename)
    if inside is not None and inside[0] == pkg:
        return sys.monitoring.DISABLE
    f = caller
    while f is not None:
        if f.f_code.co_filename.startswith(_IMPORTING):
            return None
        f = f.f_back
    name = f"{_module_of(rel)}.{code.co_qualname}"
    with _lock:
        if (pkg, name) not in _libcalls:
            _libcalls.add((pkg, name))
            try:
                _out().write(f"#lib\t{pkg}\t{name}\n")
            except Exception as exc:
                _stats["errors"] += 1
                _stats["last_error"] = repr(exc)[:200].replace("\t", " ")
    return sys.monitoring.DISABLE


def _library_of(filename):
    """(watched library, the file's path from the directory holding it), or
    None. Vendored first: vLLM itself may be installed under site-packages."""
    rel = None
    under_root = _root and filename.startswith(_root)
    if under_root and filename.startswith(_VENDORED, len(_root)):
        rel = filename[len(_root) + len(_VENDORED) :]
    else:
        for marker in _SITE_DIRS:
            i = filename.rfind(marker)
            if i >= 0:
                rel = filename[i + len(marker) :]
                break
    if rel is None:
        return None
    pkg = rel.split(os.sep, 1)[0]
    return (pkg, rel) if pkg in _LIBS else None


def _module_of(rel):
    """`flashinfer.sampling` for flashinfer/sampling.py, spelled the same
    whether the library is installed or vendored."""
    if rel.endswith(".py"):
        rel = rel[:-3]
    parts = rel.split(os.sep)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _after_in_child():
    """Give the child its own identity, lock, and events.

    Three inherited things break a forked child: the parent's file handle
    would interleave writes, a lock held by another thread at fork time stays
    held forever and deadlocks, and DISABLE state survives fork so without
    restart_events the child records nothing the parent already saw and reads
    as an empty worker.
    """
    global _fh, _fh_pid, _seen, _lock, _nonce, _origin, _stats, _ended, _hooks_pid
    global _root_logged, _root_tries, _tests_logged, _packages, _libcalls
    _fh, _fh_pid, _hooks_pid = None, None, None
    _root_logged = False
    _tests_logged = False
    _root_tries = 0
    _packages = set()
    _libcalls = set()
    _seen = set()
    _lock = threading.Lock()
    _nonce = os.urandom(4).hex()
    _origin = f"fork:{os.getppid()}"
    _stats = {"root": 0, "other": 0, "errors": 0, "last_error": ""}
    _ended = False
    try:
        sys.monitoring.restart_events()
    except Exception:
        pass
    _out()


def _claim_tool_id():
    """Prefer the ids nobody asks for by name.

    0, 1, 2 and 5 are DEBUGGER, COVERAGE, PROFILER and OPTIMIZER; taking one
    makes a later tool fail pointing at the wrong component. 3 and 4 are
    unnamed, so try those first.
    """
    for candidate in (3, 4, 0, 5):
        try:
            sys.monitoring.use_tool_id(candidate, "fnrec")
        except ValueError:
            continue
        return candidate
    return None


def _note_no_tool_id():
    try:
        occupants = [sys.monitoring.get_tool(i) for i in range(6)]
        path = os.path.join(_OUT, f"fn.{_host}.{_nonce}.{os.getpid()}.txt")
        with open(path, "a", buffering=1) as fh:
            fh.write(f"#error\tno_tool_id\ttools={occupants}\tt={_now()}\n")
    except Exception:
        pass


def _begin():
    global _tool_id, _root
    _tool_id = _claim_tool_id()
    if _tool_id is None:
        _note_no_tool_id()
        return
    _root = _resolve_root()
    mon = sys.monitoring
    mon.register_callback(_tool_id, mon.events.PY_START, _on_py_start)
    mon.set_events(_tool_id, mon.events.PY_START)

    _out()  # Announce this process even if it goes on to record nothing.
    os.register_at_fork(after_in_child=_after_in_child)


def _arm_pytest_plugin():
    """Have pytest load fnrec_pytest, which records each session's outcome.

    PYTEST_PLUGINS is read after this module loads and is inherited by
    subprocesses. Naming a module pytest cannot import aborts its startup, so
    only set it once the file is there.
    """
    name = "fnrec_pytest"
    if not os.path.exists(os.path.join(os.path.dirname(__file__), name + ".py")):
        return
    existing = os.environ.get("PYTEST_PLUGINS", "")
    if name in existing.split(","):
        return
    os.environ["PYTEST_PLUGINS"] = f"{existing},{name}" if existing else name


class _VllmImportTrigger:
    fired = False

    def find_spec(self, fullname, path=None, target=None):
        if _VllmImportTrigger.fired:
            return None
        if fullname.partition(".")[0] not in ("vllm", "aiter"):
            return None
        _VllmImportTrigger.fired = True
        try:
            sys.meta_path.remove(self)
        except ValueError:
            pass
        try:
            _begin()
        except Exception:
            # Runs inside an application import; raising would fail it and
            # take the job with it.
            pass
        return None


if _OUT and _ROOT_ENV:
    # Nothing here may raise: it runs at the start of every Python process
    # in the image.
    try:
        import socket

        _host = socket.gethostname().split(".")[0][:32]
        os.makedirs(_OUT, exist_ok=True)
        os.chmod(_OUT, 0o777)
        sys.meta_path.insert(0, _VllmImportTrigger())
        _arm_pytest_plugin()
    except Exception:
        pass
