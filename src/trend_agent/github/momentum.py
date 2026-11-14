"""Repository attention from *repeated* observations (external developer activity).

Rules, all enforced here:

* A delta needs two stored observations of the same repository; one cumulative count
  never yields growth. Rates use the actual interval between the two collections and are
  only reported when that interval is at least ``min_interval_hours`` long.
* Stars and forks are account actions, not unique people, and say nothing about
  YouTube search demand. Every result carries that label and the definitions.
* Missing values are omitted, never filled in.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from trend_agent.github.models import MEASUREMENT_DEFINITIONS, SIGNAL_LABEL

SIGNAL_CLASS = "DEVELOPER_ACTIVITY"
CAVEATS = (
    "GitHub stars and forks are cumulative account actions, not unique people.",
    "Changes are measured only between stored observations, over their actual interval.",
    "This is external developer activity. It does not measure YouTube search interest "
    "or search volume and cannot confirm a YouTube Search surge.",
)


@dataclass(frozen=True)
class Observation:
    observed_at: datetime
    stargazers_count: int | None
    forks_count: int | None
    open_issues_count: int | None = None
    pushed_at: datetime | None = None
    latest_release_at: datetime | None = None


@dataclass(frozen=True)
class MomentumReport:
    status: str  # ok / insufficient_observations / interval_too_short
    observations: int
    first_observed_at: datetime | None
    last_observed_at: datetime | None
    interval_hours: float | None
    star_delta: int | None
    fork_delta: int | None
    stars_per_day: float | None
    forks_per_day: float | None
    pushed_during_interval: bool | None
    repository_age_days: float | None
    days_since_release: float | None
    independent_corroborations: int
    notes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        def iso(v: datetime | None) -> str | None:
            return v.isoformat() if v else None

        return {
            "signal_class": SIGNAL_CLASS,
            "signal_label": SIGNAL_LABEL,
            "status": self.status,
            "observations": self.observations,
            "first_observed_at": iso(self.first_observed_at),
            "last_observed_at": iso(self.last_observed_at),
            "interval_hours": self.interval_hours,
            "star_delta": self.star_delta,
            "fork_delta": self.fork_delta,
            "stars_per_day": self.stars_per_day,
            "forks_per_day": self.forks_per_day,
            "pushed_during_interval": self.pushed_during_interval,
            "repository_age_days": self.repository_age_days,
            "days_since_release": self.days_since_release,
            "independent_corroborations": self.independent_corroborations,
            "notes": list(self.notes),
            "caveats": list(CAVEATS),
            "definitions": {
                k: MEASUREMENT_DEFINITIONS[k]
                for k in ("stargazers_count", "forks_count", "star_delta", "stars_per_day")
            },
        }


def compute_momentum(
    observations: Sequence[Observation],
    *,
    now: datetime,
    created_at: datetime | None,
    latest_release_at: datetime | None = None,
    corroborations: int = 0,
    min_interval_hours: float = 6.0,
) -> MomentumReport:
    obs = sorted(observations, key=lambda o: o.observed_at)
    # Collapse observations with identical timestamps (idempotent re-ingestion).
    unique: list[Observation] = []
    for o in obs:
        if unique and unique[-1].observed_at == o.observed_at:
            continue
        unique.append(o)
    age = round((now - created_at).total_seconds() / 86400, 2) if created_at else None
    since_release = (
        round((now - latest_release_at).total_seconds() / 86400, 2) if latest_release_at else None
    )
    notes: list[str] = []
    base: dict[str, Any] = {
        "observations": len(unique),
        "repository_age_days": age,
        "days_since_release": since_release,
        "independent_corroborations": corroborations,
    }
    if len(unique) < 2:
        notes.append("one observation: a cumulative count is not growth; no change reported")
        return MomentumReport(
            status="insufficient_observations",
            first_observed_at=unique[0].observed_at if unique else None,
            last_observed_at=unique[-1].observed_at if unique else None,
            interval_hours=None,
            star_delta=None,
            fork_delta=None,
            stars_per_day=None,
            forks_per_day=None,
            pushed_during_interval=None,
            notes=tuple(notes),
            **base,
        )
    first, last = unique[0], unique[-1]
    hours = (last.observed_at - first.observed_at).total_seconds() / 3600
    star_delta = (
        last.stargazers_count - first.stargazers_count
        if first.stargazers_count is not None and last.stargazers_count is not None
        else None
    )
    fork_delta = (
        last.forks_count - first.forks_count
        if first.forks_count is not None and last.forks_count is not None
        else None
    )
    pushed = last.pushed_at > first.observed_at if last.pushed_at is not None else None
    if hours < min_interval_hours:
        notes.append(
            f"observations {hours:.1f} h apart (< {min_interval_hours:g} h): deltas shown, "
            "no rate computed"
        )
        return MomentumReport(
            status="interval_too_short",
            first_observed_at=first.observed_at,
            last_observed_at=last.observed_at,
            interval_hours=round(hours, 2),
            star_delta=star_delta,
            fork_delta=fork_delta,
            stars_per_day=None,
            forks_per_day=None,
            pushed_during_interval=pushed,
            notes=tuple(notes),
            **base,
        )
    days = hours / 24
    if star_delta is not None and star_delta < 0:
        notes.append("net star count fell between observations (unstars exceed stars)")
    return MomentumReport(
        status="ok",
        first_observed_at=first.observed_at,
        last_observed_at=last.observed_at,
        interval_hours=round(hours, 2),
        star_delta=star_delta,
        fork_delta=fork_delta,
        stars_per_day=round(star_delta / days, 2) if star_delta is not None else None,
        forks_per_day=round(fork_delta / days, 2) if fork_delta is not None else None,
        pushed_during_interval=pushed,
        notes=tuple(notes),
        **base,
    )
