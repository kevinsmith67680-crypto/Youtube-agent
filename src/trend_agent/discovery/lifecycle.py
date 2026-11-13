"""Keyword lifecycle: explicit states, validated transitions, logged history.

DISCOVERED -> VALIDATED -> PRIORITIZED -> MONITORING -> DORMANT, with REJECTED reachable
from any non-terminal state. Reaching VALIDATED or PRIORITIZED says the candidate is
coherent and worth watching; it says nothing about search demand. Only the Phase 3
monitoring actor may move a keyword into MONITORING, and the discovery engine can never
set ``KeywordOrigin.VERIFIED_INTEREST``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trend_agent.discovery.models import (
    TARGET_CATEGORIES,
    CandidateStatus,
    Category,
    EntityStatus,
    KeywordOrigin,
)

S = CandidateStatus

ALLOWED: dict[CandidateStatus, frozenset[CandidateStatus]] = {
    S.DISCOVERED: frozenset({S.VALIDATED, S.REJECTED}),
    S.VALIDATED: frozenset({S.PRIORITIZED, S.DORMANT, S.REJECTED}),
    S.PRIORITIZED: frozenset({S.VALIDATED, S.MONITORING, S.DORMANT, S.REJECTED}),
    S.MONITORING: frozenset({S.PRIORITIZED, S.DORMANT, S.REJECTED}),
    S.DORMANT: frozenset({S.VALIDATED, S.PRIORITIZED, S.REJECTED}),
    S.REJECTED: frozenset(),
}
MONITORING_ACTOR = "monitoring"
DISCOVERY_ACTOR = "discovery"


class InvalidTransitionError(ValueError):
    pass


@dataclass(frozen=True)
class Transition:
    from_status: CandidateStatus | None
    to_status: CandidateStatus
    reason: str
    actor: str
    at: datetime


def transition(
    current: CandidateStatus | None,
    target: CandidateStatus,
    *,
    reason: str,
    actor: str,
    at: datetime,
) -> Transition:
    if current is None:
        if target is not S.DISCOVERED:
            raise InvalidTransitionError(f"new keywords start as discovered, not {target.value}")
    elif target not in ALLOWED[current]:
        raise InvalidTransitionError(f"{current.value} -> {target.value} is not allowed")
    if target is S.MONITORING and actor != MONITORING_ACTOR:
        raise InvalidTransitionError("only the monitoring system may start monitoring a keyword")
    if not reason:
        raise InvalidTransitionError("a transition needs a reason")
    return Transition(current, target, reason, actor, at)


def check_origin_change(old: KeywordOrigin | None, new: KeywordOrigin, actor: str) -> None:
    if new is KeywordOrigin.VERIFIED_INTEREST and actor == DISCOVERY_ACTOR:
        raise InvalidTransitionError(
            "discovery cannot mark a keyword as verified interest; that requires real "
            "search-interest observations (Phase 3/4)"
        )
    if old is KeywordOrigin.OBSERVED_QUERY and new is KeywordOrigin.GENERATED_HYPOTHESIS:
        raise InvalidTransitionError("an observed query cannot be downgraded to a hypothesis")


@dataclass(frozen=True)
class ValidationOutcome:
    valid: bool
    reason: str


def validate_keyword(
    *,
    canonical: str,
    origin: KeywordOrigin,
    category: Category,
    entity_status: EntityStatus | None,
    max_words: int,
) -> ValidationOutcome:
    """Is the candidate coherent, relevant and grounded? (Not: is it in demand.)"""
    words = canonical.split()
    if len(words) < 2:
        return ValidationOutcome(False, "too short to be a specific search query")
    if len(words) > max_words:
        return ValidationOutcome(False, f"longer than {max_words} words")
    if category not in TARGET_CATEGORIES:
        return ValidationOutcome(False, "outside target categories (ai/technology/gaming)")
    if entity_status is EntityStatus.REJECTED:
        return ValidationOutcome(False, "linked entity was rejected (ungrounded)")
    if origin is KeywordOrigin.GENERATED_HYPOTHESIS and entity_status is not EntityStatus.VERIFIED:
        return ValidationOutcome(False, "generated from an entity that is not verified")
    if origin is KeywordOrigin.OBSERVED_QUERY:
        return ValidationOutcome(True, "observed by a query-level provider")
    return ValidationOutcome(True, "grounded in a verified entity")


def status_after_scoring(
    current: CandidateStatus, priority: float, threshold: float
) -> CandidateStatus:
    """VALIDATED <-> PRIORITIZED by threshold; other states are left alone."""
    if current is S.VALIDATED and priority >= threshold:
        return S.PRIORITIZED
    if current is S.PRIORITIZED and priority < threshold:
        return S.VALIDATED
    if current is S.DORMANT:
        return S.PRIORITIZED if priority >= threshold else S.VALIDATED
    return current
