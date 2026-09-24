# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Where the coverage table comes from.

The only module that knows. Callers take a table and never a path, so moving
the table somewhere else means changing `fetch_table` and nothing else.
"""

from __future__ import annotations

import os
from pathlib import Path

from .table import Table, load

# Data rather than code, so it sits outside the importable package and is
# gitignored.
# TODO: read the table from the database instead of a file on disk. Only
# `fetch_table` should need to change.
COVERAGE_DIR = Path(__file__).resolve().parents[2] / "coverage-data"
TABLE_NAME = "table.json.gz"
# The kernel record's two halves, published together by the recording build
# (`recorders/kernrec/collect.sh`) and fetched by `ci-fetch-kernel-record`.
KERNEL_TABLE_NAME = "kernel_table.json.gz"
KERNEL_MAP_NAME = "kernel_symbol_map.json.gz"
KERNEL_TABLE_ENV = "CI_SELECTOR_KERNEL_TABLE"
KERNEL_MAP_ENV = "CI_SELECTOR_KERNEL_SYMBOL_MAP"


def table_path() -> Path:
    """Where a local table is expected. `CI_SELECTOR_TABLE` overrides it."""
    override = os.environ.get("CI_SELECTOR_TABLE")
    return Path(override) if override else COVERAGE_DIR / TABLE_NAME


def fetch_table(path: Path | None = None) -> Table:
    """The coverage table, or an empty one that changes nothing.

    A missing table is not an error. The reason rides along on the table, so
    callers need no special case and the selector falls back to the code map.
    """
    target = path or table_path()
    if not target.is_file():
        return Table(
            None,
            unavailable=(
                f"no coverage table at {target}. Put one there (see the README) "
                f"or set CI_SELECTOR_TABLE. Running on the code map alone."
            ),
        )
    return load(target)


def kernel_paths() -> tuple[Path, Path]:
    """Where the kernel table and symbol map are expected. The two environment
    variables override them one at a time."""
    t = os.environ.get(KERNEL_TABLE_ENV)
    m = os.environ.get(KERNEL_MAP_ENV)
    return (
        Path(t) if t else COVERAGE_DIR / KERNEL_TABLE_NAME,
        Path(m) if m else COVERAGE_DIR / KERNEL_MAP_NAME,
    )


def fetch_kernel_evidence(table_path: Path | None = None, map_path: Path | None = None):
    """The kernel record, or evidence that authorizes nothing.

    Missing files are not an error: the reason rides along and the selector
    routes csrc on the code map alone, as it did before this record existed.
    """
    from .kernels import (
        KernelEvidence,
        KernelTable,
        SymbolMap,
        load_symbol_map,
        load_table,
    )

    default_t, default_m = kernel_paths()
    t = table_path or default_t
    m = map_path or default_m
    table = (
        load_table(t)
        if t.is_file()
        else KernelTable(
            None,
            f"no kernel table at {t}. Run ci-fetch-kernel-record, or set "
            f"{KERNEL_TABLE_ENV}. csrc runs on the code map alone.",
        )
    )
    symbol_map = (
        load_symbol_map(m)
        if m.is_file()
        else SymbolMap(
            None,
            unavailable=f"no kernel symbol map at {m}. Run ci-fetch-kernel-record, "
            f"or set {KERNEL_MAP_ENV}. csrc runs on the code map alone.",
        )
    )
    return KernelEvidence(table, symbol_map)
