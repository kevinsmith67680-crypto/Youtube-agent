"""Story priority (0-100) with every component preserved, and the refresh tier it implies.

``priority = 100 * sum(w_i * x_i) / sum(w_i)`` with each ``x_i`` in [0, 1]:

* ``relevance``            classifier relevance to AI / developer technology / gaming
* ``novelty``              1.0, or 0.2 when an earlier story linked the same article (a
                           repost of one announcement is not new, independent attention)
* ``recency``              ``0.5 ** (age_hours / 12)``
* ``discussion_activity``  cumulative points/comments strength, blended with the measured
                           per-hour rates when two observations far enough apart exist
* ``launch_significance``  launch 1.0, release 0.9, announcement 0.8, discussion 0.35,
                           question 0.25
* ``corroboration``        independent publishers outside Hacker News mentioning the same
                           entity, ``min(1, n / 3)``
* ``entity_verification``  verified entity 1.0, unverified 0.4, none 0
* ``source_reliability``   the most reliable list the story appeared on (configured)

The weights are configurable heuristics, not validated against outcomes. Priority decides
what to look at and how often to refresh it; it is not demand and not search interest.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from trend_agent.hackernews.classify import activity_strength
from trend_agent.hackernews.config import HackerNewsSettings
from trend_agent.hackernews.momentum import DiscussionMomentum

LAUNCH_SIGNIFICANCE = {
    "product_launch": 1.0,
    "release": 0.9,
    "announcement": 0.8,
    "discussion": 0.35,
    "question": 0.25,
}
RECENCY_HALF_LIFE_HOURS = 12.0
REPOST_NOVELTY = 0.2


@dataclass(frozen=True)
class PriorityInputs:
    relevance: float
    subject: str
    published_at: datetime
    score: int | None
    comments: int | None
    momentum: DiscussionMomentum | None
    lists: Sequence[str]
    corroborating_publishers: int
    entity_status: str | None  # "verified" | "needs_verification" | None
    repost_of: int | None  # an earlier story ID linking the same article


@dataclass(frozen=True)
class StoryAssessment:
    score: float
    components: dict[str, Any]
    tier: str  # high | normal | low
    monitoring_state: str  # active | inactive
    next_refresh_at: datetime | None
    stop_reason: str | None


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def story_priority(
    inputs: PriorityInputs, settings: HackerNewsSettings, *, now: datetime
) -> tuple[float, dict[str, Any]]:
    w = settings.priority
    m = settings.momentum
    age_h = max(0.0, (now - inputs.published_at).total_seconds() / 3600)
    strength = activity_strength(inputs.score, inputs.comments)
    mom = inputs.momentum
    if mom is not None and mom.status == "ok":
        rate = max(
            (mom.score_per_hour or 0) / (2 * m.rising_score_per_hour),
            (mom.comments_per_hour or 0) / (2 * m.rising_comments_per_hour),
        )
        activity = 0.5 * strength + 0.5 * _clip(rate)
        activity_basis = (
            f"cumulative strength {strength:.2f} blended with measured rates "
            f"({mom.score_per_hour} points/h, {mom.comments_per_hour} comments/h)"
        )
    else:
        activity = strength
        activity_basis = f"cumulative points/comments only ({strength:.2f}); no measured rate yet"
    reliability = max(
        (settings.list_reliability.get(name, 0.5) for name in inputs.lists), default=0.5
    )
    values: dict[str, tuple[float, float, str]] = {
        "relevance": (
            w.relevance,
            _clip(inputs.relevance),
            "deterministic classifier relevance (title, domain, sanitised text)",
        ),
        "novelty": (
            w.novelty,
            REPOST_NOVELTY if inputs.repost_of else 1.0,
            f"repost: story {inputs.repost_of} linked the same article earlier"
            if inputs.repost_of
            else "first Hacker News story linking this article",
        ),
        "recency": (
            w.recency,
            0.5 ** (age_h / RECENCY_HALF_LIFE_HOURS),
            f"published {age_h:.1f} h ago (half-life {RECENCY_HALF_LIFE_HOURS:.0f} h)",
        ),
        "discussion_activity": (w.discussion_activity, _clip(activity), activity_basis),
        "launch_significance": (
            w.launch_significance,
            LAUNCH_SIGNIFICANCE.get(inputs.subject, 0.35),
            f"subject: {inputs.subject}",
        ),
        "corroboration": (
            w.corroboration,
            _clip(inputs.corroborating_publishers / 3),
            f"{inputs.corroborating_publishers} independent publisher(s) outside Hacker News",
        ),
        "entity_verification": (
            w.entity_verification,
            {"verified": 1.0, "needs_verification": 0.4}.get(inputs.entity_status or "", 0.0),
            f"entity status: {inputs.entity_status or 'no entity linked'}",
        ),
        "source_reliability": (
            w.source_reliability,
            _clip(reliability),
            "most reliable list seen on: " + (", ".join(sorted(inputs.lists)) or "none"),
        ),
    }
    total_w = sum(weight for weight, _, _ in values.values()) or 1.0
    components: dict[str, dict[str, Any]] = {
        name: {
            "value": round(x, 3),
            "weight": weight,
            "contribution": round(100 * weight * x / total_w, 2),
            "basis": basis,
        }
        for name, (weight, x, basis) in values.items()
    }
    score = round(sum(c["contribution"] for c in components.values()), 2)
    return max(0.0, min(100.0, score)), components


def assess(
    inputs: PriorityInputs, settings: HackerNewsSettings, *, now: datetime
) -> StoryAssessment:
    score, components = story_priority(inputs, settings, now=now)
    r = settings.refresh
    rising = inputs.momentum is not None and inputs.momentum.state == "rising"
    if score >= r.high_priority_threshold or rising:
        tier, minutes = "high", r.high_priority_minutes
    elif score >= r.normal_priority_threshold:
        tier, minutes = "normal", r.normal_priority_minutes
    else:
        tier, minutes = "low", r.low_priority_minutes
    if now - inputs.published_at >= timedelta(hours=r.stop_after_hours):
        return StoryAssessment(
            score, components, tier, "inactive", None, f"older than {r.stop_after_hours} h"
        )
    return StoryAssessment(
        score, components, tier, "active", now + timedelta(minutes=minutes), None
    )
