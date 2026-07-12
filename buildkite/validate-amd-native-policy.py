#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml


AMD_POOL_PATTERN = re.compile(r"^mi(250|300|325|355)_([1248])$")


def validate_config(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    for index, step in enumerate(data.get("steps", []), start=1):
        if not isinstance(step, dict) or "mirror_hardwares" not in step:
            continue

        label = step.get("label", f"step {index}")
        pool = step.get("agent_pool")
        match = AMD_POOL_PATTERN.fullmatch(str(pool or ""))
        if not match:
            errors.append(f"{label}: invalid or missing AMD agent_pool {pool!r}")
            continue

        expected_native = match.group(1) == "300"
        native = step.get("native_ci", False)
        if not isinstance(native, bool):
            errors.append(f"{label}: native_ci must be a boolean")
        elif native != expected_native:
            expected = "native" if expected_native else "legacy DinD"
            errors.append(f"{label}: {pool} must use {expected}")

        if native and (step.get("num_nodes") or 1) > 1:
            errors.append(f"{label}: native AMD jobs cannot be multi-node")
        if native and step.get("no_plugin"):
            errors.append(f"{label}: native AMD jobs cannot use no_plugin")

        configured_gpus = step.get("num_gpus", 1)
        expected_gpus = int(match.group(2))
        if configured_gpus != expected_gpus:
            errors.append(
                f"{label}: num_gpus={configured_gpus} does not match "
                f"{pool} ({expected_gpus})"
            )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the legacy AMD pipeline's native/DinD device policy."
    )
    parser.add_argument("config", type=Path)
    args = parser.parse_args()

    data = yaml.safe_load(args.config.read_text())
    errors = validate_config(data)
    if errors:
        parser.error("Invalid AMD native CI policy:\n- " + "\n- ".join(errors))

    print("AMD native CI policy validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
