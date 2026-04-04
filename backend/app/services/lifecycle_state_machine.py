from __future__ import annotations

from dataclasses import dataclass

ACTIVE = "ACTIVE"
MANAGED_ONLY = "MANAGED_ONLY"
INACTIVE = "INACTIVE"

ENTRY_ELIGIBLE_STATUSES = {ACTIVE}
EXIT_ELIGIBLE_STATUSES = {ACTIVE, MANAGED_ONLY}


@dataclass(frozen=True)
class LifecycleTransition:
    state: str
    allows_entry: bool
    allows_exit: bool
    operator_note: str


def normalize_lifecycle_state(status: str | None) -> str:
    normalized = str(status or "").strip().upper()
    if normalized in {ACTIVE, MANAGED_ONLY, INACTIVE}:
        return normalized
    return normalized or INACTIVE


def describe_lifecycle(status: str | None) -> LifecycleTransition:
    state = normalize_lifecycle_state(status)
    if state == MANAGED_ONLY:
        return LifecycleTransition(
            state=state,
            allows_entry=False,
            allows_exit=True,
            operator_note="Managed-only: keep supervising exits, but do not allow new entries.",
        )
    if state == ACTIVE:
        return LifecycleTransition(
            state=state,
            allows_entry=True,
            allows_exit=True,
            operator_note="Active: symbol is eligible for monitoring, entries, and exits.",
        )
    return LifecycleTransition(
        state=state,
        allows_entry=False,
        allows_exit=False,
        operator_note="Inactive: symbol is not eligible for new activity.",
    )
