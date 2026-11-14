"""Discussion momentum from repeated observations of one story.

* Two stored observations of the same story are required; one cumulative score is never
  growth. Rates use the actual elapsed time and are reported only when the observations
  are at least ``min_interval_minutes`` apart.
* ``rising`` is a label for strong *community* activity. It is not a YouTube Search
  surge, not views and not search demand, and nothing downstream may treat it as one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from trend_agent.hackernews.models import SIGNAL_CLASS, SIGNAL_LABEL

CAVEATS = (
    "Hacker News points and comments are cumulative community actions, not views or unique people.",
    "Changes are measured only between stored observations, over their actual interval.",
    "This is community discussion activity. It does not measure YouTube search interest "
    "and cannot confirm a YouTube Search surge.",
)


@dataclass(frozen=True)
class StoryObservation:
    observed_at: datetime
    score: int | None
    comments: int | None


@dataclass(frozen=True)
class DiscussionMomentum:
    status: str  # ok | insufficient_observations | interval_too_short
    state: str  # rising | steady | unknown
    observations: int
    interval_hours: float | None
    score_delta: int | None
    comments_delta: int | None
    score_per_hour: float | None
    comments_per_hour: float | None
    notes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal_class": SIGNAL_CLASS,
            "signal_label": SIGNAL_LABEL,
            "status": self.status,
            "state": self.state,
            "observations": self.observations,
            "interval_hours": self.interval_hours,
            "score_delta": self.score_delta,
            "comments_delta": self.comments_delta,
            "score_per_hour": self.score_per_hour,
            "comments_per_hour": self.comments_per_hour,
            "notes": list(self.notes),
            "caveats": list(CAVEATS),
        }


def _delta(a: int | None, b: int | None) -> int | None:
    return b - a if a is not None and b is not None else None


def compute_momentum(
    observations: Sequence[StoryObservation],
    *,
    min_interval_minutes: int = 10,
    rising_score_per_hour: float = 20.0,
    rising_comments_per_hour: float = 15.0,
    window: int = 2,
) -> DiscussionMomentum:
    """Momentum over the latest ``window``+ observations spanning the minimum interval.

    The comparison uses the most recent observation and the most recent earlier one that
    is at least ``min_interval_minutes`` older (so frequent polling does not shrink the
    interval into noise); if none is old enough, deltas are shown without rates.
    """
    obs: list[StoryObservation] = []
    for o in sorted(observations, key=lambda x: x.observed_at):
        if obs and obs[-1].observed_at == o.observed_at:
            continue  # idempotent re-ingestion of the same snapshot
        obs.append(o)
    if len(obs) < max(window, 2):
        return DiscussionMomentum(
            "insufficient_observations", "unknown", len(obs), None, None, None, None, None,
            ("one observation: a cumulative score is not growth; no change reported",),
        )  # fmt: skip
    last = obs[-1]
    earlier = [
        o
        for o in obs[:-1]
        if (last.observed_at - o.observed_at).total_seconds() >= 60 * min_interval_minutes
    ]
    base = earlier[-1] if earlier else obs[-2]
    hours = (last.observed_at - base.observed_at).total_seconds() / 3600
    sd, cd = _delta(base.score, last.score), _delta(base.comments, last.comments)
    if not earlier:
        return DiscussionMomentum(
            "interval_too_short", "unknown", len(obs), round(hours, 3), sd, cd, None, None,
            (f"observations {hours * 60:.0f} min apart (< {min_interval_minutes} min): "
             "deltas shown, no rate computed",),
        )  # fmt: skip
    sph = round(sd / hours, 2) if sd is not None else None
    cph = round(cd / hours, 2) if cd is not None else None
    rising = (sph is not None and sph >= rising_score_per_hour) or (
        cph is not None and cph >= rising_comments_per_hour
    )
    notes: list[str] = []
    if sd is not None and sd < 0:
        notes.append("score fell between observations (Hacker News adjusts scores)")
    return DiscussionMomentum(
        "ok", "rising" if rising else "steady", len(obs), round(hours, 3), sd, cd, sph, cph,
        tuple(notes),
    )  # fmt: skip
