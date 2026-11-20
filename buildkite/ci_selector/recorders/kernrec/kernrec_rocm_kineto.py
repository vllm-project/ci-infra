"""Let Kineto register with a ROCProfiler SDK that the recorder initializes.

With the recorder in ROCP_TOOL_LIBRARIES, the SDK initializes and collects
its clients when torch import loads the HIP runtime. Kineto's
rocprofiler_configure (libtorch_cpu.so) is only found if libtorch_cpu.so is
already in the global symbol scope then, so torch.profiler sees no GPU
activity. Loading libtorch_cpu.so RTLD_GLOBAL just before torch's __init__
runs fixes that, as vllm/env_override.py does when vllm is imported first.
Recording is unchanged. Every failure falls back to doing nothing.
"""

import os
import sys

_TARGET = "torch"


def _promote(torch_root: str) -> None:
    import ctypes
    import glob

    lib = os.path.join(torch_root, "lib", "libtorch_cpu.so")
    if not os.path.exists(lib):
        return
    try:
        ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
    except OSError:
        # ROCm wheels can leave a stale RUNPATH; satisfy that dependency first.
        site_root = os.path.dirname(torch_root)
        deps = glob.glob(
            os.path.join(
                site_root, "_rocm_sdk_*", "lib", "*", "lib", "librocm-openblas.so.*"
            )
        )
        if not deps:
            return
        ctypes.CDLL(deps[0], mode=ctypes.RTLD_LOCAL)
        ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)


class _Finder:
    """Promotes libtorch_cpu.so right before torch's __init__ executes."""

    _busy = False

    def find_spec(self, name, path=None, target=None):
        if name != _TARGET or self._busy or _TARGET in sys.modules:
            return None
        self._busy = True
        try:
            import importlib.util

            spec = importlib.util.find_spec(name)
        except Exception:
            return None
        finally:
            self._busy = False
        loader = getattr(spec, "loader", None)
        exec_module = getattr(loader, "exec_module", None)
        if exec_module is None or getattr(exec_module, "_kernrec", False):
            return spec

        def promote_and_exec(module):
            if self in sys.meta_path:
                sys.meta_path.remove(self)
            try:
                _promote(os.path.dirname(spec.origin))
            except Exception:
                pass
            exec_module(module)

        promote_and_exec._kernrec = True
        loader.exec_module = promote_and_exec
        return spec


def install() -> None:
    if not os.environ.get("ROCP_TOOL_LIBRARIES") or _TARGET in sys.modules:
        return
    if not any(isinstance(f, _Finder) for f in sys.meta_path):
        sys.meta_path.insert(0, _Finder())


try:
    install()
except Exception:
    pass
