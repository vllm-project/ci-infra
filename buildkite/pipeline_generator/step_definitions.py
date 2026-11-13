"""Which steps an edit to a step yaml redefines. Python 3.9 (generator)."""

import subprocess
from functools import lru_cache
from typing import Any, Dict, FrozenSet, Optional

import yaml

AMD_MIRROR = "amd"


def changed_step_keys(
    base_text: Optional[str], head_text: str
) -> Optional[FrozenSet[str]]:
    """Keys whose entry changed, plus `amd-<key>` if it has a `mirror.amd`
    block (only that for a mirror-only change). None if the edit can't be
    pinned to steps."""
    if base_text is None:
        return None
    try:
        base, head = yaml.safe_load(base_text), yaml.safe_load(head_text)
    except yaml.YAMLError:
        return None
    base_steps, head_steps = _steps_by_key(base), _steps_by_key(head)
    if base_steps is None or head_steps is None:
        return None
    if _without(base, "steps") != _without(head, "steps"):
        return None
    changed = set()
    for key, entry in head_steps.items():
        old = base_steps.get(key)
        own = old is None or _without(old, "mirror") != _without(entry, "mirror")
        if own:
            changed.add(key)
        amd = _amd_block(entry)
        if amd is not None and (own or _amd_block(old) != amd):
            changed.add(f"amd-{key}")
    return frozenset(changed)


@lru_cache(maxsize=None)
def changed_step_keys_in_file(
    path: str, base_ref: Optional[str]
) -> Optional[FrozenSet[str]]:
    """`changed_step_keys` for `path` between `base_ref` and the checkout."""
    keys = None
    if base_ref:
        try:
            base_text = subprocess.run(
                ["git", "show", f"{base_ref}:{path}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            with open(path, encoding="utf-8") as f:
                head_text = f.read()
            keys = changed_step_keys(base_text, head_text)
        except (OSError, UnicodeDecodeError, subprocess.CalledProcessError):
            pass
    if keys is None:
        print(f"{path}: edit not pinned to steps; every AMD step in it counts it")
    else:
        print(f"{path}: AMD steps count the edit only if redefined: {sorted(keys)}")
    return keys


def _steps_by_key(doc: Any) -> Optional[Dict[str, Dict[str, Any]]]:
    if not isinstance(doc, dict) or not isinstance(doc.get("steps"), list):
        return None
    steps: Dict[str, Dict[str, Any]] = {}
    for entry in doc["steps"]:
        if not isinstance(entry, dict):
            return None
        key = entry.get("key")
        if not isinstance(key, str) or not key or key in steps:
            return None
        steps[key] = entry
    return steps


def _without(entry: Dict[str, Any], field: str) -> Dict[str, Any]:
    return {name: value for name, value in entry.items() if name != field}


def _amd_block(entry: Dict[str, Any]) -> Any:
    mirror = entry.get("mirror")
    return mirror.get(AMD_MIRROR) if isinstance(mirror, dict) else None
