"""Deterministic historical replay without look-ahead bias.

Replay re-creates, for a sequence of simulated times T, exactly the observations that were
*available* at T, ordered by event time (observation window). An observation (or a
revision of it) becomes available at:

* ``publication`` mode (default): the provider's stated publication time; if the provider
  did not state one, the conservative bound ``max(collected_at, ingested_at)``.
  Example: Monday's value published Wednesday is invisible on a simulated Tuesday.
* ``agent`` mode: always ``max(collected_at, ingested_at)`` - what this system itself
  actually knew at T (backfilled history becomes visible only when it was backfilled).

Earlier availability is never inferred without evidence. A revision replaces the value it
supersedes only from its own availability time; until then the earlier revision is what
was known. Conflict rows (values contradicting an immutable source) never become visible
values. Late-arriving data simply appears at its availability time.

The same stored data and parameters always produce the same snapshots and digests.
Phase 4 detectors plug in through ``ReplayConsumer``; none is implemented here.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from trend_agent.schemas.time_series import StoredPoint

ENGINE_VERSION = "replay-v1"


class AvailabilityMode(StrEnum):
    PUBLICATION = "publication"
    AGENT = "agent"


def availability(point: StoredPoint, mode: AvailabilityMode) -> tuple[datetime, str]:
    known = max(point.collected_at, point.ingested_at)
    if mode is AvailabilityMode.AGENT:
        return known, "agent_ingested_at"
    if point.provider_published_at is not None:
        return point.provider_published_at, "provider_published_at"
    return known, "conservative_collected_or_ingested_at"


@dataclass(frozen=True)
class ReplayEvent:
    available_at: datetime
    basis: str
    point: StoredPoint


@dataclass(frozen=True)
class ReplaySnapshot:
    """What was knowable at ``as_of``. Series are keyed by series id (one scale each)."""

    as_of: datetime
    series: dict[int, tuple[StoredPoint, ...]]
    newly_available: tuple[StoredPoint, ...]
    revisions_applied: tuple[StoredPoint, ...]
    withheld_not_yet_available: int


class ReplayConsumer(Protocol):
    """Phase 4 hook: called once per replay step, in time order."""

    def on_step(self, snapshot: ReplaySnapshot) -> None: ...


@dataclass
class StepSummary:
    as_of: datetime
    visible: int
    newly_available: int
    revisions_applied: int
    withheld_not_yet_available: int
    latest_visible_period_end: datetime | None
    digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "visible": self.visible,
            "newly_available": self.newly_available,
            "revisions_applied": self.revisions_applied,
            "withheld_not_yet_available": self.withheld_not_yet_available,
            "latest_visible_period_end": (
                self.latest_visible_period_end.isoformat()
                if self.latest_visible_period_end
                else None
            ),
            "digest": self.digest,
        }


@dataclass
class ReplayReport:
    mode: AvailabilityMode
    start: datetime
    end: datetime
    step: timedelta
    events: int
    series: int
    late_arrivals: int
    basis_counts: dict[str, int]
    steps: list[StepSummary] = field(default_factory=list)
    final_digest: str = ""
    engine: str = ENGINE_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "mode": self.mode.value,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "step_seconds": int(self.step.total_seconds()),
            "events": self.events,
            "series": self.series,
            "late_arrivals": self.late_arrivals,
            "availability_basis": self.basis_counts,
            "final_digest": self.final_digest,
            "steps": [s.as_dict() for s in self.steps],
        }


def _stable_series(p: StoredPoint) -> str:
    """Series identity independent of database ids (digests reproduce across databases)."""
    return "|".join(
        [
            p.provider_name,
            p.effective_category.value,
            p.region,
            p.language or "",
            p.granularity.value,
            p.unit,
            p.normalization_key,
        ]
    )


def _digest(state: dict[tuple[int, datetime], StoredPoint]) -> str:
    rows = sorted(
        (_stable_series(p), start.isoformat(), p.period_end.isoformat(), p.value, p.revision)
        for (_sid, start), p in state.items()
    )
    return hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()[:16]


def build_events(points: Iterable[StoredPoint], mode: AvailabilityMode) -> list[ReplayEvent]:
    events = []
    for p in points:
        if p.revision_kind == "conflict":
            continue  # never an accepted value
        at, basis = availability(p, mode)
        events.append(ReplayEvent(at, basis, p))
    # Deterministic total order: availability, then event time, then revision, then id.
    events.sort(
        key=lambda e: (
            e.available_at,
            e.point.series_id,
            e.point.period_start,
            e.point.revision,
            e.point.id,
        )
    )
    return events


def replay(
    points: Sequence[StoredPoint],
    *,
    start: datetime,
    end: datetime,
    step: timedelta,
    mode: AvailabilityMode = AvailabilityMode.PUBLICATION,
    consumers: Sequence[ReplayConsumer] = (),
    late_after: timedelta = timedelta(days=2),
) -> ReplayReport:
    if step <= timedelta(0):
        raise ValueError("step must be positive")
    if end < start:
        raise ValueError("end before start")
    events = build_events(points, mode)
    basis = Counter(e.basis for e in events)
    late = sum(1 for e in events if e.available_at - e.point.period_end > late_after)
    report = ReplayReport(
        mode=mode,
        start=start,
        end=end,
        step=step,
        events=len(events),
        series=len({e.point.series_id for e in events}),
        late_arrivals=late,
        basis_counts=dict(sorted(basis.items())),
    )
    state: dict[tuple[int, datetime], StoredPoint] = {}
    cursor = 0
    as_of = start
    while as_of <= end:
        new: list[StoredPoint] = []
        revised: list[StoredPoint] = []
        while cursor < len(events) and events[cursor].available_at <= as_of:
            p = events[cursor].point
            key = (p.series_id, p.period_start)
            previous = state.get(key)
            if previous is None:
                state[key] = p
                new.append(p)
            elif p.revision > previous.revision:
                state[key] = p
                revised.append(p)
            cursor += 1
        withheld = sum(
            1 for e in events[cursor:] if e.point.period_end <= as_of
        )  # windows already over, but not yet published/known at as_of
        by_series: dict[int, list[StoredPoint]] = {}
        for (sid, _), p in state.items():
            by_series.setdefault(sid, []).append(p)
        snapshot = ReplaySnapshot(
            as_of=as_of,
            series={
                sid: tuple(sorted(v, key=lambda p: p.period_start))
                for sid, v in sorted(by_series.items())
            },
            newly_available=tuple(new),
            revisions_applied=tuple(revised),
            withheld_not_yet_available=withheld,
        )
        for consumer in consumers:
            consumer.on_step(snapshot)
        latest = max((p.period_end for p in state.values()), default=None)
        report.steps.append(
            StepSummary(as_of, len(state), len(new), len(revised), withheld, latest, _digest(state))
        )
        as_of += step
    report.final_digest = report.steps[-1].digest if report.steps else _digest({})
    return report


class CollectingConsumer:
    """Reference consumer used by tests and the demo: records what each step could see."""

    def __init__(self) -> None:
        self.snapshots: list[ReplaySnapshot] = []

    def on_step(self, snapshot: ReplaySnapshot) -> None:
        self.snapshots.append(snapshot)

    def visible_periods(self, as_of: datetime) -> set[tuple[int, datetime]]:
        for snap in self.snapshots:
            if snap.as_of == as_of:
                return {(sid, p.period_start) for sid, pts in snap.series.items() for p in pts}
        raise KeyError(as_of)
