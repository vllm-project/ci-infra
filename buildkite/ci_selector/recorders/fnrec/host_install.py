import importlib.util
import os
import pathlib


# ci_setup.sh fetches fnrec.py next to this script.
FNREC_SOURCE = (pathlib.Path(__file__).parent / "fnrec.py").read_text()


# A plugin-less step runs on the agent host, where a .pth in site-packages
# would load for every later job on that machine. Install per job instead: a
# directory on PYTHONPATH, gone when the checkout is cleaned.
#
# sitecustomize, not a .pth, because only real site directories read .pth files.
# Both load at interpreter start, so subprocesses are covered either way.
target = pathlib.Path(os.environ["FNREC_LIB"])
target.mkdir(parents=True, exist_ok=True)
(target / "fnrec.py").write_text(FNREC_SOURCE)

# Python imports only the first sitecustomize on sys.path, so shadowing the
# agent's own would disable it. Delegate to it first, then load the recorder.
(target / "sitecustomize.py").write_text(
    "import os, sys\n"
    "_here = os.path.dirname(os.path.abspath(__file__))\n"
    "_saved = sys.path[:]\n"
    "try:\n"
    "    sys.path[:] = [p for p in sys.path if os.path.abspath(p) != _here]\n"
    "    _self = sys.modules.pop('sitecustomize', None)\n"
    "    try:\n"
    "        import sitecustomize  # the host's own, if it has one\n"
    "    except Exception:\n"
    "        pass\n"
    "finally:\n"
    "    sys.path[:] = _saved\n"
    "    sys.modules.setdefault('sitecustomize', _self)\n"
    "try:\n"
    "    import fnrec\n"
    "except Exception:\n"
    "    pass\n"
)

# Same as the container installer: print where vllm's code is.
spec = importlib.util.find_spec("vllm")
print(pathlib.Path(spec.origin).parent if spec is not None and spec.origin else target / "vllm")
