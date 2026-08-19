# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Pytest plugin: wrap every test phase in an NVTX range named by nodeid.

nsys records these ranges alongside CUDA launch activity; the parser joins
kernels to tests by launch-site timestamp within the range on the same
thread. Setup/teardown get their own prefixed ranges so fixture-launched
kernels are attributed explicitly instead of falling outside every range.

Zero hard dependencies: if torch or a CUDA context is unavailable the plugin
is inert, so the same pytest command runs unchanged outside the trace step.

Usage: pytest -p nvtx_test_ranges ... (with this file on PYTHONPATH), or
copy next to conftest.py and add to plugins.
"""

import os
from contextlib import suppress

import pytest


def _configured_nvtx():
    if os.environ.get("VLLM_CI_TEST_SELECTION_NVTX") != "1":
        return None
    try:
        import torch

        return torch.cuda.nvtx if torch.cuda.is_available() else None
    except Exception:
        return None


_nvtx = _configured_nvtx()

PREFIX = {
    "setup": "citest-setup::",
    "call": "citest::",
    "teardown": "citest-teardown::",
}


def _wrap(phase, item):
    if _nvtx is None:
        yield
        return
    pushed = False
    try:
        _nvtx.range_push(PREFIX[phase] + item.nodeid)
        pushed = True
    except Exception:
        # NVTX is evidence-only. A driver/tooling failure must never change
        # the pytest phase's correctness result.
        pass
    try:
        yield
    finally:
        if pushed:
            with suppress(Exception):
                _nvtx.range_pop()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    yield from _wrap("setup", item)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    yield from _wrap("call", item)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    yield from _wrap("teardown", item)
