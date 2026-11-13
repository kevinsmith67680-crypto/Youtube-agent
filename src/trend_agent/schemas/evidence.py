"""Strict evidence classification for surge decisions.

The measurement category of the series decides what a detected increase may be called:

| Evidence category (Phase 3)       | Surge classification               | Highest alert stage |
|-----------------------------------|------------------------------------|---------------------|
| DIRECT_YOUTUBE_SEARCH_INTEREST    | DIRECT_SEARCH_SURGE                | CONFIRMED           |
| EXTERNAL_SEARCH_INTEREST          | EXTERNAL_SEARCH_SURGE              | CANDIDATE           |
| VIDEO_ACTIVITY                    | PROXY_ATTENTION_SIGNAL             | CANDIDATE           |
| SEARCH_SUGGESTION_PRESENCE        | PROXY_ATTENTION_SIGNAL             | CANDIDATE           |
| OFFICIAL_EVENT_SIGNAL             | PROXY_ATTENTION_SIGNAL             | CANDIDATE           |
| COMMUNITY_ACTIVITY                | PROXY_ATTENTION_SIGNAL             | CANDIDATE           |
| mixed indirect signals (target)   | POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY | CANDIDATE         |

Only YouTube-specific search-interest evidence can ever be CONFIRMED. Synthetic
observations are classified by the category they *simulate* but always carry
``synthetic=True`` and are excluded from production alert listings. A relative index is
never converted into a search count.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from trend_agent.schemas.monitoring import MeasurementCategory

M = MeasurementCategory


class SurgeClassification(StrEnum):
    DIRECT_SEARCH_SURGE = "direct_search_surge"
    EXTERNAL_SEARCH_SURGE = "external_search_surge"
    POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY = "potential_youtube_demand_opportunity"
    PROXY_ATTENTION_SIGNAL = "proxy_attention_signal"


class AlertStage(StrEnum):
    WATCH = "watch"  # preliminary unusual activity; needs more evidence
    CANDIDATE = "candidate"  # statistical evidence of a possible upward change
    CONFIRMED = "confirmed"  # validated DIRECT YouTube search-interest surge
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"  # data cannot support a conclusion
    REJECTED = "rejected"  # apparent anomaly failed validation
    RESOLVED = "resolved"  # previously detected surge no longer active


OPEN_STAGES = frozenset({AlertStage.WATCH, AlertStage.CANDIDATE, AlertStage.CONFIRMED})
CLOSED_STAGES = frozenset({AlertStage.RESOLVED, AlertStage.REJECTED})
STAGE_RANK = {AlertStage.WATCH: 1, AlertStage.CANDIDATE: 2, AlertStage.CONFIRMED: 3}

_CLASSIFICATION: dict[MeasurementCategory, SurgeClassification] = {
    M.DIRECT_YOUTUBE_SEARCH_INTEREST: SurgeClassification.DIRECT_SEARCH_SURGE,
    M.EXTERNAL_SEARCH_INTEREST: SurgeClassification.EXTERNAL_SEARCH_SURGE,
    M.VIDEO_ACTIVITY: SurgeClassification.PROXY_ATTENTION_SIGNAL,
    M.SEARCH_SUGGESTION_PRESENCE: SurgeClassification.PROXY_ATTENTION_SIGNAL,
    M.OFFICIAL_EVENT_SIGNAL: SurgeClassification.PROXY_ATTENTION_SIGNAL,
    M.COMMUNITY_ACTIVITY: SurgeClassification.PROXY_ATTENTION_SIGNAL,
}

STAGE_CEILING: dict[SurgeClassification, AlertStage] = {
    SurgeClassification.DIRECT_SEARCH_SURGE: AlertStage.CONFIRMED,
    SurgeClassification.EXTERNAL_SEARCH_SURGE: AlertStage.CANDIDATE,
    SurgeClassification.POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY: AlertStage.CANDIDATE,
    SurgeClassification.PROXY_ATTENTION_SIGNAL: AlertStage.CANDIDATE,
}

# What each category measures, used verbatim by the explanation templates.
MEANING: dict[MeasurementCategory, str] = {
    M.DIRECT_YOUTUBE_SEARCH_INTEREST: (
        "The source identifies the signal as YouTube Search interest. This is a relative "
        "index, not an absolute number of searches."
    ),
    M.EXTERNAL_SEARCH_INTEREST: (
        "The signal is search interest on another search property (e.g. Google Web Search), "
        "not YouTube Search. It is a relative index, not a number of searches."
    ),
    M.VIDEO_ACTIVITY: (
        "The signal is video activity (publications or engagement). It indicates attention "
        "or supply, not measured search demand."
    ),
    M.SEARCH_SUGGESTION_PRESENCE: (
        "The signal counts appearances in a search-suggestion source. Appearing in "
        "suggestions is not proof of increased search volume."
    ),
    M.OFFICIAL_EVENT_SIGNAL: (
        "The signal counts official announcements, releases or events. It explains why "
        "interest might rise; it is not measured search demand."
    ),
    M.COMMUNITY_ACTIVITY: (
        "The signal counts community mentions or interactions on other platforms. It "
        "indicates attention, not measured search demand."
    ),
    M.SYNTHETIC_TEST_OBSERVATION: "Synthetic test data; not a measurement of anything real.",
}


class EvidenceRuleError(ValueError):
    """An attempted classification or stage violates the evidence rules."""


def effective_category(
    category: MeasurementCategory, simulated: MeasurementCategory | None
) -> MeasurementCategory:
    """The category a value stands for (synthetic values stand for what they simulate)."""
    if category is M.SYNTHETIC_TEST_OBSERVATION:
        if simulated is None or simulated is M.SYNTHETIC_TEST_OBSERVATION:
            raise EvidenceRuleError("synthetic observation without a simulated category")
        return simulated
    if simulated is not None:
        raise EvidenceRuleError("only synthetic observations may carry a simulated category")
    return category


def classify(category: MeasurementCategory, *, is_proxy: bool = False) -> SurgeClassification:
    """Classification of an increase in a single series of ``category``."""
    if category is M.SYNTHETIC_TEST_OBSERVATION:
        raise EvidenceRuleError("classify the simulated category of synthetic data")
    result = _CLASSIFICATION[category]
    if is_proxy and result is not SurgeClassification.PROXY_ATTENTION_SIGNAL:
        # A proxy-flagged series is never search interest, whatever its label says.
        return SurgeClassification.PROXY_ATTENTION_SIGNAL
    return result


def combine(categories: Iterable[MeasurementCategory]) -> SurgeClassification | None:
    """Target-level classification from the categories that currently show increases.

    Direct YouTube search evidence dominates. Without it, external search interest plus
    any other independent category, or two or more indirect categories, form a
    POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY - which is never described as measured YouTube
    search growth. A single category keeps its own classification.
    """
    present = set(categories)
    if not present:
        return None
    if M.DIRECT_YOUTUBE_SEARCH_INTEREST in present:
        return SurgeClassification.DIRECT_SEARCH_SURGE
    if len(present) >= 2:
        return SurgeClassification.POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY
    return classify(next(iter(present)))


def ceiling(classification: SurgeClassification) -> AlertStage:
    return STAGE_CEILING[classification]


def cap_stage(stage: AlertStage, classification: SurgeClassification) -> AlertStage:
    """Lower ``stage`` to what the evidence category allows."""
    top = STAGE_CEILING[classification]
    if stage in STAGE_RANK and STAGE_RANK[stage] > STAGE_RANK[top]:
        return top
    return stage


def validate_stage(stage: AlertStage, classification: SurgeClassification) -> None:
    """Raise if ``stage`` is not allowed for ``classification`` (defence in depth)."""
    top = STAGE_CEILING[classification]
    if stage in STAGE_RANK and STAGE_RANK[stage] > STAGE_RANK[top]:
        raise EvidenceRuleError(
            f"{classification.value} evidence cannot reach {stage.value}; "
            f"highest allowed stage is {top.value}"
        )
