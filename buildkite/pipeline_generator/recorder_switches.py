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
FNREC_ENV_VAR = "VLLM_CI_FNREC"
# Not a recorder, but armed the same way: the selector's shadow run on PR
# builds, which reads the records and gates nothing.
SELECTOR_SHADOW_ENV_VAR = "VLLM_CI_SELECTOR_SHADOW"


def kernrec_enabled() -> bool:
    return os.getenv(KERNREC_ENV_VAR, "") == "1"


def fnrec_enabled() -> bool:
    return os.getenv(FNREC_ENV_VAR, "") == "1"


def selector_shadow_enabled() -> bool:
    return os.getenv(SELECTOR_SHADOW_ENV_VAR, "") == "1"
