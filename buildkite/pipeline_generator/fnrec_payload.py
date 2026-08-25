"""Payload shipped into the test container to record vLLM function entries.

Sources are kept verbatim and encoded at generation time rather than stored
pre-encoded, so the two can never drift apart.

Base64 is not cosmetic. `_prepare_commands` rewrites every apostrophe in a generated
command to a double quote, and `buildkite-agent pipeline upload` expands a single `$`
against the bootstrap agent's environment. The base64 alphabet contains neither
character, so a blob survives both passes byte for byte. A heredoc does not.
"""

import base64
import gzip
import os

# Read by the generator, not by the container.
FNREC_ENV_VAR = "FNREC"

INSTALL_PY = '''import importlib.util
import os
import pathlib
import site


def site_dir():
    """Where a .pth will actually be executed.

    Only accept vllm's parent if it is itself a site directory. Under an editable
    install that parent is the repo root, where a .pth is never read and the job
    would record nothing while looking like it ran fine.
    """
    dirs = [p for p in site.getsitepackages() if os.path.isdir(p)]
    spec = importlib.util.find_spec("vllm")
    if spec is not None and spec.origin:
        parent = str(pathlib.Path(spec.origin).parent.parent)
        if parent in dirs:
            return parent
    return dirs[0]


target = pathlib.Path(site_dir())
(target / "fnrec.py").write_text(FNREC_SOURCE)
# The .pth is what loads fnrec at every interpreter start, including the engine
# and worker subprocesses a plain PYTHONPATH would miss.
(target / "fnrec.pth").write_text("import fnrec\\n")

# FNREC_ROOT has to be where vllm's code is, which under an editable install is
# nowhere near the install target.
spec = importlib.util.find_spec("vllm")
print(pathlib.Path(spec.origin).parent if spec is not None and spec.origin else target / "vllm")
'''

# Runs from an EXIT trap, so it must never fail the step or alter its exit status.
# Everything fnrec produces lands under this directory, relative to the Buildkite
# checkout, so the agent's artifact phase -- which runs on the host after the
# container is gone -- can see it.
FNREC_CHECKOUT_DIR = ".fnrec"

# The globs that deliver a job's recording. The agent evaluates these, so nothing
# inside the container can pre-empt them and `docker run --rm` cannot eat them.
FNREC_ARTIFACT_PATHS = (
    # The packed common case: one artifact per job.
    f"{FNREC_CHECKOUT_DIR}/*.tar.gz",
    # The raw per-process records, for when packing never ran: a step killed
    # mid-run, or an abort before the last command. Not `*.txt`, because
    # install.err is the only witness separating a failed install from a job that
    # genuinely ran no vLLM code.
    f"{FNREC_CHECKOUT_DIR}/*/*",
)

# Marker `fnrec_artifact_paths` greps for. Keyed off the emitted commands rather
# than re-deriving the enablement predicate, which lives in four places.
FNREC_OUT_MARKER = "export FNREC_OUT="

PACK_SH = """#!/usr/bin/env bash
# Pack this job's recording into one tarball, in place. Nothing else.
#
# Delivery is the step's `artifact_paths`, evaluated by the agent on the host
# after the container exits, so this uploads nothing and its failure costs only
# the packing: the raw files stay where the second glob also looks. It runs as
# an ordinary command, so it must never fail the step -- but it must say what it
# did on every path. Four silent `exit 0`s are how a total delivery failure
# looked exactly like success for a whole rollout.
set -u

_fnrec_say() { echo "fnrec: $*" || :; }

if [ -z "${FNREC_OUT:-}" ]; then
    _fnrec_say "FNREC_OUT is unset; setup never ran or the environment was reset. Nothing packed."
    exit 0
fi

if [ ! -d "${FNREC_OUT}" ]; then
    _fnrec_say "no recording directory at ${FNREC_OUT}. Nothing packed."
    exit 0
fi

BASE="$(dirname "${FNREC_OUT}")"
JOB="$(basename "${FNREC_OUT}")"
COUNT="$(find "${FNREC_OUT}" -type f 2>/dev/null | wc -l | tr -d ' ')"

# Build under a name no artifact glob matches, then rename. A step killed
# mid-tar would otherwise publish a truncated tarball that reads downstream as a
# complete, nearly empty recording -- a wrong answer, not a missing one.
if ! tar -czf "${BASE}/${JOB}.tar.gz.part" -C "${BASE}" "${JOB}"; then
    _fnrec_say "tar failed for ${FNREC_OUT} (${COUNT} files); leaving the raw files for artifact_paths."
    rm -f "${BASE}/${JOB}.tar.gz.part" || :
    exit 0
fi

if ! mv "${BASE}/${JOB}.tar.gz.part" "${BASE}/${JOB}.tar.gz"; then
    _fnrec_say "could not move the tarball into place; leaving the raw files for artifact_paths."
    rm -f "${BASE}/${JOB}.tar.gz.part" || :
    exit 0
fi

# The agent user has to be able to unlink this during the next job's git clean.
chmod 0666 "${BASE}/${JOB}.tar.gz" || :

if rm -rf "${FNREC_OUT}"; then
    _fnrec_say "packed ${COUNT} files into ${BASE}/${JOB}.tar.gz"
else
    _fnrec_say "packed ${COUNT} files into ${BASE}/${JOB}.tar.gz but could not remove ${FNREC_OUT}; both will upload."
fi
exit 0
"""

FNREC_PY = r'''"""Record which vLLM functions were entered. Nothing else.

The CI filter only ever asks one bit per function: did this job enter it?
Line coverage answers that, but records every executed line to do so, which is
where the 2-3.3x slowdown comes from. sys.monitoring can answer it directly:
subscribe to PY_START, and return DISABLE from the callback so that code object
never reports again. Every function then costs exactly one event, once.

Activated by FNREC_OUT (output directory) and FNREC_ROOT (the vllm package
path). It starts on the first `vllm` import rather than at interpreter startup,
so third-party infrastructure such as Ray's dashboard is left alone.

Everything beyond that bare recording exists because this runs once, on
infrastructure we cannot reach, and every artifact has to be diagnosable
offline. A process that recorded nothing must still say so, and say why: silence
is the one outcome we can never interpret afterwards.
"""

import os
import sys
import threading

_OUT = os.environ.get("FNREC_OUT")
_ROOT_ENV = os.environ.get("FNREC_ROOT")

# Only names that are safe in a public build artifact. Never a prefix match:
# BUILDKITE_* would capture BUILDKITE_AGENT_ACCESS_TOKEN, HF_* would capture
# HF_TOKEN, and this file is uploaded.
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
    """Where vllm actually is, preferring the live module over the env var.

    Returns None while neither source is usable, which keeps the callback armed
    so it can try again. That matters because _begin() runs from inside
    find_spec, before the import machinery has put vllm in sys.modules -- the
    module only becomes available once its body starts executing, which is the
    first event we receive.

    A FNREC_ROOT that does not exist is rejected rather than used. It is a path
    baked into a CI template, so it is wrong for any install layout the template
    did not anticipate, and accepting it would match nothing while looking
    perfectly healthy.
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

    Write each function the moment it is first seen rather than batching. With
    DISABLE, a function is recorded exactly once, so this is a few thousand
    small writes per run -- nothing. Batching cost us the teardown functions:
    they run in the last moments before vLLM kills a process, so anything still
    sitting in a buffer was lost, and they showed up as false "never executed".

    The handle is buffered, not raw. A raw FileIO write is one syscall and may
    write only part of the line, which would corrupt the record under exactly
    the disk-full conditions that make the counters interesting.
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

    Not at startup, and not in the fork hook: multiprocessing's _bootstrap
    clears the inherited finalizer registry in the child after the fork hooks
    have run, so anything registered before that point is silently dropped.
    Doing it on first write puts it after _bootstrap in every process.
    """
    global _hooks_pid
    if _hooks_pid == pid:
        return
    _hooks_pid = pid
    import atexit

    atexit.register(_end)
    try:
        # A fork child ends via os._exit() and never runs atexit. It does run
        # multiprocessing's own exit function, which is what Finalize hangs on.
        from multiprocessing.util import Finalize

        Finalize(None, _end, exitpriority=5)
    except Exception:
        pass


_SECRETISH = ("key", "token", "secret", "passwd", "password")


def _argv():
    """Enough of the command line to tell processes apart, and no more.

    Drops the interpreter's directory rather than the arguments: it is the
    longest and least informative part, and telling `vllm serve <model>` from
    `pytest <path>` is what the census is for. argv can carry keys and signed
    URLs that no env allowlist would catch, so secret-looking values go too.
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

    Registered through both atexit and multiprocessing's Finalize because
    neither covers the cases we care about alone: a fork child ends via
    os._exit() and never runs atexit at all, and fork is vLLM's default worker
    method. Nothing survives SIGKILL, which is the point -- a file with no
    #end is a process kill_process_tree got to.
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
            # vllm is mid-import and has no __path__ yet. Stay armed, but not
            # forever: if the import raises, vllm leaves sys.modules and the root
            # never resolves, and an armed callback runs on every call for the
            # life of the process.
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
            # The header is written before the root is knowable, so record the
            # value actually in force. Reading a record means knowing what it
            # was filtered against.
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
                # Losing the write loses the function: DISABLE has already been
                # promised and the code object will not report again. Count it
                # so a thinned record is visibly thin rather than silently so.
                _stats["errors"] += 1
                _stats["last_error"] = repr(exc)[:200].replace("\t", " ")
    return sys.monitoring.DISABLE


def _after_in_child():
    """Give the child its own identity, its own lock, and its own events.

    Three inherited things break a forked child. Its copy of the parent's file
    handle would interleave into the parent's file. A lock held by some other
    thread at the instant of the fork is held forever in the child, which then
    deadlocks on its first vLLM function. And sys.monitoring's DISABLE state is
    per code object and survives fork, so without restart_events the child
    reports nothing the parent already touched -- the union stays right, but
    the child looks empty, which is exactly the signature the trust gate reads
    as a lost worker.
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


def _claim_tool_id():
    """Prefer the ids nobody asks for by name.

    0, 1, 2 and 5 are DEBUGGER, COVERAGE, PROFILER and OPTIMIZER. Taking one of
    those makes a later tool that demands its own id fail with a ValueError
    pointing at the wrong component. 3 and 4 are unnamed, so try those first.
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
            # This runs inside `import vllm`. Raising here would fail the
            # import and take the whole job with it.
            pass
        return None


if _OUT and _ROOT_ENV:
    # Nothing in this block may raise: it executes at interpreter start in every
    # Python process in the image, including the image-build smoke test.
    try:
        import socket

        _host = socket.gethostname().split(".")[0][:32]
        os.makedirs(_OUT, exist_ok=True)
        sys.meta_path.insert(0, _VllmImportTrigger())
    except Exception:
        pass
'''


def fnrec_enabled():
    return os.getenv(FNREC_ENV_VAR, "0") == "1"


def _blob(text):
    """Gzip then base64. Decode with `base64 -d | gunzip`.

    Gzipped because the blob repeats on every instrumented step, and 200+ of
    them is megabytes in one `pipeline upload`. mtime=0 because the default
    stamps the current time in, making the blob differ between calls.
    """
    return base64.b64encode(gzip.compress(text.encode(), 9, mtime=0)).decode()


def install_blob():
    """Installer with the recorder source inlined."""
    # repr() rather than wrapping in another raw string: that only held because
    # the recorder happens to contain no triple-quote and not to end in a
    # backslash, and breaking either would emit a SyntaxError inside a container.
    return _blob(f"FNREC_SOURCE = {FNREC_PY!r}\n{INSTALL_PY}")


def pack_blob():
    return _blob(PACK_SH)


def fnrec_artifact_paths(commands):
    """The globs that deliver whatever fnrec injected into `commands`.

    Read off the emitted commands rather than re-deriving the enablement
    predicate. That predicate lives in several places, and a second copy is a
    copy that drifts; drift here means a step that records into a directory
    nothing uploads, which is precisely the bug this delivery path replaced.
    """
    if not any(FNREC_OUT_MARKER in command for command in commands):
        return []
    return list(FNREC_ARTIFACT_PATHS)
