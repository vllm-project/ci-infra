"""The recorders that feed the test selector, and the switches that arm them.

Payloads live in `buildkite/ci_selector/recorders/`; this is the generator side.
Each recorder is opt-in per build, so PR jobs never pay for either, and the two
are independent: the records have different producers and either can be turned
on alone.

A module of its own rather than a corner of `constants`, which holds enums and
knows nothing about the environment, and rather than `buildkite_step`, which
the docker plugin cannot import back into.
"""

import os

KERNREC_ENV_VAR = "VLLM_CI_KERNREC"
KERNREC_AMD_ENV_VAR = "VLLM_CI_KERNREC_AMD"
FNREC_ENV_VAR = "VLLM_CI_FNREC"


def _enabled(env_var: str) -> bool:
    """`env_var` is "1" and this is not an AITER nightly build.

    The AITER nightly tests an unreleased AITER, so its records would
    mislead the selector.
    """
    return os.getenv(env_var, "") == "1" and os.getenv("AITER_NIGHTLY", "") != "1"


def kernrec_enabled() -> bool:
    return _enabled(KERNREC_ENV_VAR)


def kernrec_amd_enabled() -> bool:
    return _enabled(KERNREC_AMD_ENV_VAR)


def fnrec_enabled() -> bool:
    return _enabled(FNREC_ENV_VAR)
