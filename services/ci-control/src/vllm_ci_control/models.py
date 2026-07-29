"""Shared immutable domain values."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

_STABLE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._:-]{0,99}")


class DomainValidationError(ValueError):
    """A domain value does not satisfy the public contract."""


def validate_stable_id(value: object, *, label: str = "identifier") -> str:
    """Validate a bounded, lowercase identifier used by the public API."""

    if not isinstance(value, str) or _STABLE_ID_PATTERN.fullmatch(value) is None:
        raise DomainValidationError(
            f"{label} must match {_STABLE_ID_PATTERN.pattern!r}"
        )
    return value


def stable_id_set(
    values: Iterable[str],
    *,
    label: str,
) -> frozenset[str]:
    """Return validated identifiers as an immutable set."""

    result = frozenset(values)
    for value in result:
        validate_stable_id(value, label=label)
    return result


class RepositoryPermission(StrEnum):
    """GitHub's repository permission levels."""

    NONE = "none"
    READ = "read"
    TRIAGE = "triage"
    WRITE = "write"
    MAINTAIN = "maintain"
    ADMIN = "admin"


MUTATION_PERMISSIONS = frozenset(
    {
        RepositoryPermission.WRITE,
        RepositoryPermission.MAINTAIN,
        RepositoryPermission.ADMIN,
    }
)


@dataclass(frozen=True, slots=True)
class Selector:
    """Normalized selector expression used by planning and execution."""

    all_jobs: bool = False
    groups: frozenset[str] = frozenset()
    areas: frozenset[str] = frozenset()
    jobs: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "groups",
            stable_id_set(self.groups, label="group"),
        )
        object.__setattr__(
            self,
            "areas",
            stable_id_set(self.areas, label="area"),
        )
        object.__setattr__(
            self,
            "jobs",
            stable_id_set(self.jobs, label="job"),
        )
        dimensions = self.groups or self.areas or self.jobs
        if self.all_jobs and dimensions:
            raise DomainValidationError("all cannot be combined with another selector")
        if not self.all_jobs and not dimensions:
            raise DomainValidationError("at least one selector is required")

    @classmethod
    def all(cls) -> Selector:
        """Select every active catalog job."""

        return cls(all_jobs=True)
