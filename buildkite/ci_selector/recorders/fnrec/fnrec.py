"""Record which vLLM functions were entered. Nothing else.

The selector only asks one bit per function: did this job enter it? Line
coverage answers that but records every line to do so, which is where its
slowdown comes from. sys.monitoring answers it directly: subscribe to
PY_START and return DISABLE, so each function costs one event, once.

Needs FNREC_OUT and FNREC_ROOT. Starts on the first `vllm` import rather
than at interpreter startup, leaving other infrastructure alone.

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
)

_STAT_EVERY = 500
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
_stats = {"root": 0, "other": 0, "errors": 0, "last_error": ""}
_ended = False


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
    global _root, _root_logged, _root_tries
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
        _stats["other"] += 1
        return sys.monitoring.DISABLE
    key = f"{filename}\t{code.co_qualname}\t{code.co_firstlineno}"
    with _lock:
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


def _after_in_child():
    """Give the child its own identity, lock, and events.

    Three inherited things break a forked child: the parent's file handle
    would interleave writes, a lock held by another thread at fork time stays
    held forever and deadlocks, and DISABLE state survives fork so without
    restart_events the child records nothing the parent already saw and reads
    as an empty worker.
    """
    global _fh, _fh_pid, _seen, _lock, _nonce, _origin, _stats, _ended, _hooks_pid
    global _root_logged, _root_tries
    _fh, _fh_pid, _hooks_pid = None, None, None
    _root_logged = False
    _root_tries = 0
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


class _VllmImportTrigger:
    fired = False

    def find_spec(self, fullname, path=None, target=None):
        if _VllmImportTrigger.fired:
            return None
        if fullname != "vllm" and not fullname.startswith("vllm."):
            return None
        _VllmImportTrigger.fired = True
        try:
            sys.meta_path.remove(self)
        except ValueError:
            pass
        try:
            _begin()
        except Exception:
            # Runs inside `import vllm`; raising would fail the import and
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
    except Exception:
        pass
