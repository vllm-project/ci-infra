import importlib.util
import os
import pathlib
import sys


# ci_setup.sh fetches fnrec.py next to this script.
FNREC_SOURCE = (pathlib.Path(__file__).parent / "fnrec.py").read_text()


# A plugin-less step runs on the agent host, where a .pth in site-packages
# would load for every later job on that machine. Install per job instead: a
# directory on PYTHONPATH, gone when the checkout is cleaned.
#
# sitecustomize, not a .pth, because only real site directories read .pth files.
# Both load at interpreter start, so subprocesses are covered either way.
def install_pytest_plugin(target: pathlib.Path) -> None:
    """Test outcomes without the job log: fnrec_pytest.py as a pytest11 entry
    point beside the recorder, so every pytest this interpreter starts writes
    its collected count and summary line into FNREC_OUT. The marker tells the
    collect step the plugin was in place, so a job with no pytest.*.txt ran no
    pytest rather than lost its counts. Optional: the recorder works without it.
    """
    source = pathlib.Path(__file__).parent / "fnrec_pytest.py"
    out = os.environ.get("FNREC_OUT")
    if not source.is_file() or not out:
        return
    try:
        (target / "fnrec_pytest.py").write_text(source.read_text())
        dist = target / "fnrec_pytest-0.dist-info"
        dist.mkdir(exist_ok=True)
        metadata = "Metadata-Version: 2.1\nName: fnrec-pytest\nVersion: 0\n"
        (dist / "METADATA").write_text(metadata)
        (dist / "entry_points.txt").write_text("[pytest11]\nfnrec = fnrec_pytest\n")
        marker = pathlib.Path(out) / "pytest.installed"
        marker.write_text("")
        marker.chmod(0o666)
    except Exception as exc:  # noqa: BLE001 - never fail the install over the plugin
        print(f"fnrec: no pytest plugin: {exc}", file=sys.stderr)


target = pathlib.Path(os.environ["FNREC_LIB"])
target.mkdir(parents=True, exist_ok=True)
(target / "fnrec.py").write_text(FNREC_SOURCE)
install_pytest_plugin(target)

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
