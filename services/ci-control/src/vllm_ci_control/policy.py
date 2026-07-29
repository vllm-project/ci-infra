"""Reviewed policy values and authorization decisions."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field

from .models import (
    RepositoryPermission,
    stable_id_set,
    validate_stable_id,
)


class PolicyValidationError(ValueError):
    """Policy input is invalid or unsafe."""


@dataclass(frozen=True, slots=True)
class RetryLimit:
    """Maximum retries after the initial attempt; ``None`` means unlimited."""

    maximum: int | None

    def __post_init__(self) -> None:
        if self.maximum is not None and (
            isinstance(self.maximum, bool)
            or not isinstance(self.maximum, int)
            or self.maximum < 0
        ):
            raise PolicyValidationError(
                "retry limit must be a non-negative integer or inf"
            )

    @classmethod
    def infinite(cls) -> RetryLimit:
        """Return an unbounded retry-depth policy."""

        return cls(None)

    @classmethod
    def parse(cls, value: object) -> RetryLimit:
        """Parse a trusted configuration value."""

        if value == "inf":
            return cls.infinite()
        if isinstance(value, int) and not isinstance(value, bool):
            return cls(value)
        raise PolicyValidationError(
            "retry limit must be a non-negative integer or 'inf'"
        )

    @property
    def is_infinite(self) -> bool:
        return self.maximum is None

    def allows(self, completed_retries: int) -> bool:
        """Return whether one additional retry is permitted."""

        if completed_retries < 0:
            raise PolicyValidationError("completed retries cannot be negative")
        return self.maximum is None or completed_retries < self.maximum


_PERMISSION_RANK = {
    RepositoryPermission.NONE: 0,
    RepositoryPermission.READ: 1,
    RepositoryPermission.TRIAGE: 2,
    RepositoryPermission.WRITE: 3,
    RepositoryPermission.MAINTAIN: 4,
    RepositoryPermission.ADMIN: 5,
}
_RETRY_STATES = frozenset({"failed", "timed_out", "expired"})


@dataclass(frozen=True, slots=True)
class Policy:
    """Immutable policy loaded from the reviewed vLLM overlay."""

    api_version: int = 1
    catalog_groups: frozenset[str] = frozenset({"upstream", "cpu", "amd"})
    catalog_aliases: tuple[tuple[str, str], ...] = ()
    catalog_tombstones: tuple[str, ...] = ()
    catalog_area_aliases: tuple[tuple[str, str], ...] = ()
    catalog_area_tombstones: tuple[str, ...] = ()
    native_amd_selection: str = "whole_lane"
    minimum_compute_permission: RepositoryPermission = RepositoryPermission.WRITE
    minimum_refresh_permission: RepositoryPermission = RepositoryPermission.WRITE
    minimum_credit_grant_permission: RepositoryPermission = (
        RepositoryPermission.MAINTAIN
    )
    committer_teams: tuple[str, ...] = ("vllm-committers",)
    credit_admin_teams: tuple[str, ...] = ("vllm-ci-admins",)
    initial_credits: int = 300
    credit_reset: str = "none"
    retry_limit: RetryLimit = field(default_factory=RetryLimit.infinite)
    retry_states: frozenset[str] = _RETRY_STATES
    confirmation_distinct_shas: int = 2
    resolution_clean_distinct_shas: int = 3
    evidence_max_age_hours: int = 72

    def __post_init__(self) -> None:
        if self.api_version != 1:
            raise PolicyValidationError("unsupported policy api_version")

        groups = stable_id_set(self.catalog_groups, label="catalog group")
        if not groups:
            raise PolicyValidationError(
                "catalog groups must contain at least one group"
            )
        aliases = tuple(sorted(self.catalog_aliases))
        alias_names: set[str] = set()
        for alias, target in aliases:
            validate_stable_id(alias, label="catalog alias")
            validate_stable_id(target, label="catalog alias target")
            if alias in alias_names:
                raise PolicyValidationError("catalog aliases must be unique")
            alias_names.add(alias)
        tombstones = tuple(sorted(self.catalog_tombstones))
        for tombstone in tombstones:
            validate_stable_id(tombstone, label="catalog tombstone")
        if len(tombstones) != len(set(tombstones)):
            raise PolicyValidationError("catalog tombstones must be unique")
        if alias_names & set(tombstones):
            raise PolicyValidationError(
                "catalog aliases and tombstones share one job namespace"
            )
        area_aliases = tuple(sorted(self.catalog_area_aliases))
        area_alias_names: set[str] = set()
        for alias, target in area_aliases:
            validate_stable_id(alias, label="catalog area alias")
            validate_stable_id(target, label="catalog area alias target")
            if alias in area_alias_names:
                raise PolicyValidationError("catalog area aliases must be unique")
            area_alias_names.add(alias)
        area_tombstones = tuple(sorted(self.catalog_area_tombstones))
        for tombstone in area_tombstones:
            validate_stable_id(tombstone, label="catalog area tombstone")
        if len(area_tombstones) != len(set(area_tombstones)):
            raise PolicyValidationError("catalog area tombstones must be unique")
        if area_alias_names & set(area_tombstones):
            raise PolicyValidationError(
                "catalog area aliases and tombstones share one namespace"
            )
        if self.native_amd_selection != "whole_lane":
            raise PolicyValidationError("native_amd_selection must be 'whole_lane'")

        _validate_minimum_permission(
            self.minimum_compute_permission,
            label="minimum compute permission",
        )
        _validate_minimum_permission(
            self.minimum_refresh_permission,
            label="minimum refresh permission",
        )
        _validate_minimum_permission(
            self.minimum_credit_grant_permission,
            label="minimum credit grant permission",
        )
        committer_teams = _normalized_team_slugs(
            self.committer_teams,
            label="committer teams",
        )
        credit_admin_teams = _normalized_team_slugs(
            self.credit_admin_teams,
            label="credit admin teams",
        )

        _non_negative_int(self.initial_credits, label="initial grant")
        if self.credit_reset != "none":
            raise PolicyValidationError("credit reset must be 'none'")

        retry_states = frozenset(self.retry_states)
        if not retry_states or not retry_states <= _RETRY_STATES:
            raise PolicyValidationError(
                "retry states must contain only failed, timed_out, and expired"
            )
        _positive_int(
            self.confirmation_distinct_shas,
            label="confirmation distinct SHAs",
        )
        _positive_int(
            self.resolution_clean_distinct_shas,
            label="resolution clean distinct SHAs",
        )
        _positive_int(
            self.evidence_max_age_hours,
            label="evidence max age hours",
        )

        object.__setattr__(self, "catalog_groups", groups)
        object.__setattr__(self, "catalog_aliases", aliases)
        object.__setattr__(self, "catalog_tombstones", tombstones)
        object.__setattr__(self, "catalog_area_aliases", area_aliases)
        object.__setattr__(
            self,
            "catalog_area_tombstones",
            area_tombstones,
        )
        object.__setattr__(self, "committer_teams", committer_teams)
        object.__setattr__(
            self,
            "credit_admin_teams",
            credit_admin_teams,
        )
        object.__setattr__(self, "retry_states", retry_states)

    @classmethod
    def from_toml(cls, document: str) -> Policy:
        """Parse one complete reviewed TOML policy document."""

        if not isinstance(document, str):
            raise PolicyValidationError("policy TOML must be text")
        try:
            value = tomllib.loads(document)
        except tomllib.TOMLDecodeError as exc:
            raise PolicyValidationError(f"invalid policy TOML: {exc}") from exc
        return cls.from_mapping(value)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Policy:
        """Parse the exact nested shape of ``.buildkite/ci_control.toml``."""

        root = _exact_mapping(
            value,
            required={
                "api_version",
                "catalog",
                "authorization",
                "credits",
                "retry",
                "main_status",
            },
            label="policy",
        )
        catalog = _exact_mapping(
            root["catalog"],
            required={
                "groups",
                "aliases",
                "tombstones",
                "area_aliases",
                "area_tombstones",
                "native_amd_selection",
            },
            label="catalog",
        )
        authorization = _exact_mapping(
            root["authorization"],
            required={
                "minimum_compute_permission",
                "minimum_refresh_permission",
                "minimum_credit_grant_permission",
                "committer_teams",
                "credit_admin_teams",
            },
            label="authorization",
        )
        credits = _exact_mapping(
            root["credits"],
            required={"initial_grant", "reset"},
            label="credits",
        )
        retry = _exact_mapping(
            root["retry"],
            required={"failures_limit", "include_states"},
            label="retry",
        )
        main_status = _exact_mapping(
            root["main_status"],
            required={
                "confirmation_distinct_shas",
                "resolution_clean_distinct_shas",
                "evidence_max_age_hours",
            },
            label="main_status",
        )

        raw_aliases = _mapping(catalog["aliases"], label="catalog aliases")
        aliases: list[tuple[str, str]] = []
        for alias, target in raw_aliases.items():
            if not isinstance(alias, str) or not isinstance(target, str):
                raise PolicyValidationError(
                    "catalog aliases must map strings to strings"
                )
            aliases.append((alias, target))
        raw_area_aliases = _mapping(
            catalog["area_aliases"],
            label="catalog area aliases",
        )
        area_aliases: list[tuple[str, str]] = []
        for alias, target in raw_area_aliases.items():
            if not isinstance(alias, str) or not isinstance(target, str):
                raise PolicyValidationError(
                    "catalog area aliases must map strings to strings"
                )
            area_aliases.append((alias, target))

        return cls(
            api_version=_integer(root["api_version"], label="api_version"),
            catalog_groups=frozenset(
                _string_list(catalog["groups"], label="catalog groups")
            ),
            catalog_aliases=tuple(aliases),
            catalog_tombstones=tuple(
                _string_list(
                    catalog["tombstones"],
                    label="catalog tombstones",
                )
            ),
            catalog_area_aliases=tuple(area_aliases),
            catalog_area_tombstones=tuple(
                _string_list(
                    catalog["area_tombstones"],
                    label="catalog area tombstones",
                )
            ),
            native_amd_selection=_string(
                catalog["native_amd_selection"],
                label="native_amd_selection",
            ),
            minimum_compute_permission=_permission(
                authorization["minimum_compute_permission"],
                label="minimum compute permission",
            ),
            minimum_refresh_permission=_permission(
                authorization["minimum_refresh_permission"],
                label="minimum refresh permission",
            ),
            minimum_credit_grant_permission=_permission(
                authorization["minimum_credit_grant_permission"],
                label="minimum credit grant permission",
            ),
            committer_teams=tuple(
                _string_list(
                    authorization["committer_teams"],
                    label="committer teams",
                )
            ),
            credit_admin_teams=tuple(
                _string_list(
                    authorization["credit_admin_teams"],
                    label="credit admin teams",
                )
            ),
            initial_credits=_integer(
                credits["initial_grant"],
                label="initial grant",
            ),
            credit_reset=_string(credits["reset"], label="credit reset"),
            retry_limit=RetryLimit.parse(retry["failures_limit"]),
            retry_states=frozenset(
                _string_list(
                    retry["include_states"],
                    label="retry include states",
                )
            ),
            confirmation_distinct_shas=_integer(
                main_status["confirmation_distinct_shas"],
                label="confirmation distinct SHAs",
            ),
            resolution_clean_distinct_shas=_integer(
                main_status["resolution_clean_distinct_shas"],
                label="resolution clean distinct SHAs",
            ),
            evidence_max_age_hours=_integer(
                main_status["evidence_max_age_hours"],
                label="evidence max age hours",
            ),
        )

    def can_mutate(
        self,
        permission: RepositoryPermission,
        teams: frozenset[str] = frozenset(),
    ) -> bool:
        """Return whether a caller may dispatch or retry compute."""

        return _permission_at_least(
            permission,
            self.minimum_compute_permission,
        ) or bool(set(teams) & set(self.committer_teams))

    def can_refresh(
        self,
        permission: RepositoryPermission,
        teams: frozenset[str] = frozenset(),
    ) -> bool:
        """Return whether a caller may refresh main-branch evidence."""

        return _permission_at_least(
            permission,
            self.minimum_refresh_permission,
        ) or bool(set(teams) & set(self.committer_teams))

    def can_receive_compute_credits(
        self,
        permission: RepositoryPermission,
        teams: frozenset[str] = frozenset(),
    ) -> bool:
        """Return whether an account may be opened or topped up."""

        return self.can_mutate(permission, teams)

    def can_top_up(
        self,
        permission: RepositoryPermission,
        teams: frozenset[str] = frozenset(),
    ) -> bool:
        """Return whether a caller may grant audited credit top-ups."""

        return _permission_at_least(
            permission,
            self.minimum_credit_grant_permission,
        ) or bool(set(teams) & set(self.credit_admin_teams))


def _permission_at_least(
    actual: RepositoryPermission,
    minimum: RepositoryPermission,
) -> bool:
    return _PERMISSION_RANK[actual] >= _PERMISSION_RANK[minimum]


def _validate_minimum_permission(
    value: RepositoryPermission,
    *,
    label: str,
) -> None:
    if _PERMISSION_RANK[value] < _PERMISSION_RANK[RepositoryPermission.WRITE]:
        raise PolicyValidationError(f"{label} must be write or higher")


def _normalized_team_slugs(
    values: tuple[str, ...],
    *,
    label: str,
) -> tuple[str, ...]:
    result = tuple(sorted(values))
    if len(result) != len(set(result)):
        raise PolicyValidationError(f"{label} must be unique")
    for value in result:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 100
            or value.strip() != value
            or not all(
                character.isascii()
                and (character.islower() or character.isdigit() or character == "-")
                for character in value
            )
        ):
            raise PolicyValidationError(
                f"{label} must contain normalized GitHub team slugs"
            )
    return result


def _exact_mapping(
    value: object,
    *,
    required: set[str],
    label: str,
) -> Mapping[str, object]:
    result = _mapping(value, label=label)
    keys = set(result)
    missing = required - keys
    unknown = keys - required
    if missing:
        raise PolicyValidationError(
            f"{label} is missing fields: " + ", ".join(sorted(missing))
        )
    if unknown:
        raise PolicyValidationError(
            f"{label} has unknown fields: " + ", ".join(sorted(unknown))
        )
    return result


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PolicyValidationError(f"{label} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise PolicyValidationError(f"{label} keys must be strings")
    return value


def _string_list(value: object, *, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PolicyValidationError(f"{label} must be a list of strings")
    return value


def _string(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise PolicyValidationError(f"{label} must be text")
    return value


def _integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyValidationError(f"{label} must be an integer")
    return value


def _positive_int(value: object, *, label: str) -> int:
    result = _integer(value, label=label)
    if result < 1:
        raise PolicyValidationError(f"{label} must be positive")
    return result


def _non_negative_int(value: object, *, label: str) -> int:
    result = _integer(value, label=label)
    if result < 0:
        raise PolicyValidationError(f"{label} must be non-negative")
    return result


def _permission(
    value: object,
    *,
    label: str,
) -> RepositoryPermission:
    text = _string(value, label=label)
    try:
        return RepositoryPermission(text)
    except ValueError as exc:
        raise PolicyValidationError(f"{label} is not recognized") from exc
