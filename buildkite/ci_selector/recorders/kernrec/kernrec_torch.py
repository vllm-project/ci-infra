"""Hand CUPTI to torch.profiler before it starts.

ci_setup.sh installs this next to a kernrec_torch.pth, so it loads at every
interpreter start in a recording job. CUPTI allows one activity client per
process, so a CUDA torch.profiler that starts while libkernrec.so holds CUPTI
records nothing. This patches torch.autograd.profiler.profile._prepare_trace,
which both torch.profiler.profile and the autograd profiler go through, to call
kernrec_release() first. Nothing here may break the workload: every failure
falls back to doing nothing.
"""

import os
import sys

_TARGET = "torch.autograd.profiler"


def _release() -> None:
    path = os.environ.get("CUDA_INJECTION64_PATH")
    if not path:
        return
    import ctypes

    try:
        lib = ctypes.CDLL(path, mode=os.RTLD_NOLOAD)
    except OSError:
        # CUDA is not initialised in this process yet, so the recorder has not
        # loaded. Tell it not to arm here; children have other pids.
        os.environ["KERNREC_RELEASED_PID"] = str(os.getpid())
        return
    release = getattr(lib, "kernrec_release", None)
    if release is not None:
        release()


def _profiles_cuda(profiler) -> bool:
    activities = getattr(profiler, "kineto_activities", None)
    if activities is None:
        return True
    return any(getattr(a, "name", "") == "CUDA" for a in activities)


def _patch(module) -> None:
    profile = getattr(module, "profile", None)
    original = getattr(profile, "_prepare_trace", None)
    if original is None or getattr(original, "_kernrec", False):
        return

    def _prepare_trace(self, *args, **kwargs):
        try:
            if _profiles_cuda(self):
                _release()
        except Exception:
            pass
        return original(self, *args, **kwargs)

    _prepare_trace._kernrec = True
    profile._prepare_trace = _prepare_trace


class _Finder:
    """Patches the profiler module right after it is first executed."""

    def find_spec(self, name, path=None, target=None):
        if name != _TARGET:
            return None
        sys.meta_path.remove(self)
        try:
            import importlib.util

            spec = importlib.util.find_spec(name)
            loader = getattr(spec, "loader", None)
            exec_module = getattr(loader, "exec_module", None)
            if exec_module is None:
                return None

            def exec_and_patch(module):
                exec_module(module)
                try:
                    _patch(module)
                except Exception:
                    pass

            loader.exec_module = exec_and_patch
            return spec
        except Exception:
            return None


def install() -> None:
    if not os.environ.get("CUDA_INJECTION64_PATH"):
        return
    module = sys.modules.get(_TARGET)
    if module is not None:
        _patch(module)
    elif not any(isinstance(f, _Finder) for f in sys.meta_path):
        sys.meta_path.insert(0, _Finder())


try:
    install()
except Exception:
    pass
