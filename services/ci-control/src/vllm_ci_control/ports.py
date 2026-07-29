"""Storage and external-service ports for the pure domain package."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .catalog import Catalog
from .credits import CreditAccount
from .models import RepositoryPermission


@runtime_checkable
class CatalogPort(Protocol):
    """Load an immutable catalog revision from trusted storage."""

    def load_catalog(self, version: str | None = None) -> Catalog:
        """Return the requested catalog, or the current trusted revision."""


@runtime_checkable
class CreditAccountPort(Protocol):
    """Persist credit state with optimistic compare-and-swap semantics."""

    def load_account(self, user_id: str) -> CreditAccount | None:
        """Return one account if it already exists."""

    def compare_and_swap(
        self,
        *,
        user_id: str,
        expected_version: int | None,
        account: CreditAccount,
    ) -> bool:
        """Atomically persist state when the stored version still matches."""


@runtime_checkable
class PermissionPort(Protocol):
    """Read live authorization from a repository-bound adapter."""

    def repository_permission(
        self,
        username: str,
    ) -> RepositoryPermission:
        """Return the caller's current base repository permission."""


@runtime_checkable
class TeamMembershipPort(Protocol):
    """Read live team membership from a repository-bound adapter."""

    def is_team_member(
        self,
        team_slug: str,
        username: str,
    ) -> bool:
        """Return whether the caller is an active member of the named team."""
