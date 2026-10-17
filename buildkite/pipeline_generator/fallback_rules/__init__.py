"""Named queue health rules: fallback_rules/{common,nvidia,amd}.py.

A rule takes a queue's recent finished REST jobs and its parameters from the
entry's close_when, and returns True when the queue should close. A platform
module can add rules or override common ones by name.
"""

from typing import Callable, Dict

from fallback_rules import amd, common, nvidia

PLATFORMS = ("nvidia", "amd")
_PLATFORM_RULES = {"nvidia": nvidia.RULES, "amd": amd.RULES}


def rules_for(platform: str) -> Dict[str, Callable[..., bool]]:
    return {**common.RULES, **_PLATFORM_RULES[platform]}
