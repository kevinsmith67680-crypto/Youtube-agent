"""Monitoring-target state machine (separate from the Phase 2 discovery lifecycle).

The discovery lifecycle says whether a *keyword* is worth watching; this one says what the
*monitoring system* is doing about it. They are linked at exactly two points, both logged
in ``keyword_status_history`` with actor ``monitoring``: a keyword becomes MONITORING when
one of its non-proxy targets becomes ACTIVE, and leaves MONITORING when none remains active.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trend_agent.schemas.monitoring import MonitoringStatus

M = MonitoringStatus

ALLOWED: dict[MonitoringStatus, frozenset[MonitoringStatus]] = {
    M.PENDING: frozenset({M.ACTIVE, M.PROVIDER_UNAVAILABLE, M.DORMANT, M.DISABLED, M.PAUSED}),
    M.ACTIVE: frozenset(
        {M.PAUSED, M.PROVIDER_UNAVAILABLE, M.QUOTA_BLOCKED, M.STALE, M.DORMANT, M.DISABLED}
    ),
    M.PAUSED: frozenset({M.ACTIVE, M.DISABLED, M.PENDING}),
    M.PROVIDER_UNAVAILABLE: frozenset({M.ACTIVE, M.PENDING, M.DORMANT, M.DISABLED, M.PAUSED}),
    M.QUOTA_BLOCKED: frozenset(
        {M.ACTIVE, M.PROVIDER_UNAVAILABLE, M.PAUSED, M.DISABLED, M.DORMANT, M.STALE}
    ),
    M.STALE: frozenset(
        {M.ACTIVE, M.PROVIDER_UNAVAILABLE, M.QUOTA_BLOCKED, M.DORMANT, M.DISABLED, M.PAUSED}
    ),
    M.DORMANT: frozenset({M.ACTIVE, M.PENDING, M.DISABLED, M.PROVIDER_UNAVAILABLE}),
    M.DISABLED: frozenset({M.PENDING}),  # only by explicit re-enable
}

MANUAL_ACTOR = "manual"
MANAGER_ACTOR = "target_manager"
EXECUTOR_ACTOR = "executor"
PLANNER_ACTOR = "planner"


class InvalidMonitoringTransitionError(ValueError):
    pass


@dataclass(frozen=True)
class MonitoringTransition:
    from_status: MonitoringStatus | None
    to_status: MonitoringStatus
    reason: str
    actor: str
    at: datetime


def monitoring_transition(
    current: MonitoringStatus | None,
    target: MonitoringStatus,
    *,
    reason: str,
    actor: str,
    at: datetime,
) -> MonitoringTransition:
    if not reason:
        raise InvalidMonitoringTransitionError("a transition needs a reason")
    if current is None:
        if target not in (M.PENDING, M.PROVIDER_UNAVAILABLE):
            raise InvalidMonitoringTransitionError(
                f"new targets start as pending or provider_unavailable, not {target.value}"
            )
    elif target not in ALLOWED[current]:
        raise InvalidMonitoringTransitionError(f"{current.value} -> {target.value} is not allowed")
    if current is M.DISABLED and actor != MANUAL_ACTOR:
        raise InvalidMonitoringTransitionError("only a manual action re-enables a disabled target")
    if target is M.PAUSED and actor != MANUAL_ACTOR:
        raise InvalidMonitoringTransitionError("only a manual action pauses a target")
    if current is M.PAUSED and actor != MANUAL_ACTOR:
        raise InvalidMonitoringTransitionError("only a manual action resumes a paused target")
    return MonitoringTransition(current, target, reason, actor, at)
