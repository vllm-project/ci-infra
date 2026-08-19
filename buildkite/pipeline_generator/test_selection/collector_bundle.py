# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Build the exact minimal runtime collector shipped by pipeline generation."""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

TRACE_COLLECTOR_DOWNLOAD_ATTEMPTS = 12
TRACE_COLLECTOR_DOWNLOAD_INTERVAL_SECONDS = 5


def download_prelude(directory: str) -> str:
    """Return a bounded artifact poll that never becomes a Buildkite edge."""

    archive = f"{directory}/test-selection-collector.zip"
    attempts = " ".join(
        str(attempt) for attempt in range(1, TRACE_COLLECTOR_DOWNLOAD_ATTEMPTS + 1)
    )
    return (
        "TRACE_COLLECTOR_READY=0; "
        "if command -v buildkite-agent >/dev/null 2>&1; then "
        f"for TRACE_COLLECTOR_ATTEMPT in {attempts}; do "
        f'rm -f "{archive}"; '
        'if buildkite-agent artifact download "test-selection-collector.zip" '
        f'"{directory}"; then TRACE_COLLECTOR_READY=1; break; fi; '
        'if [ "$$TRACE_COLLECTOR_ATTEMPT" -lt '
        f"{TRACE_COLLECTOR_DOWNLOAD_ATTEMPTS} ]; then "
        f"sleep {TRACE_COLLECTOR_DOWNLOAD_INTERVAL_SECONDS}; fi; "
        "done; fi; "
    )


def bundle_bytes() -> bytes:
    source = Path(__file__).with_name("collector")
    files = sorted(
        path
        for path in source.iterdir()
        if path.suffix == ".py" or path.name.endswith(".sh")
    )
    if not files:
        raise RuntimeError("test-selection collector package is empty")

    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for path in files:
            info = ZipInfo(
                filename=f"ci_test_selection/{path.name}",
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    return output.getvalue()


def bundle_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
