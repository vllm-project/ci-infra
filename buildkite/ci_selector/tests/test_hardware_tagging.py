"""Hardware-convention tagging: which of its steps the record may drop."""

from ci_selector.codemap.step_refs import hardware_steps_held

AMD = {"vllm_ci:engine:amd", "vllm_ci:lora:amd"}


def test_a_python_model_file_holds_nothing():
    """vllm#58689: a ROCm-only .py rename held all 106 AMD mirror steps. The
    Python record has AMD rows and sees calls into a .py file."""
    assert hardware_steps_held("vllm/models/x/amd/rocm.py", AMD) == set()


def test_compiled_code_holds_every_step():
    """Kernels reach a family's jobs where no recording sees."""
    assert hardware_steps_held("csrc/rocm/attention.cu", AMD) == AMD


def test_a_platform_module_holds_every_step():
    """Loaded by a qualname string the graph cannot follow, and its
    import-time code runs in every job of the family."""
    assert hardware_steps_held("vllm/platforms/rocm.py", AMD) == AMD
