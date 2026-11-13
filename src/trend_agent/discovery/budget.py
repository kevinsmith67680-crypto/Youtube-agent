"""Discovery budget allocation and source scheduling.

A deliberately simple, explainable scheduler (no bandits / RL):

1. Split the run's fetch slots between categories by the configured shares (largest
   remainder), so effort follows AI 45% / technology 25% / gaming 30%.
2. Within a category, reserve ``exploration_share`` of the slots for exploration sources
   (new entities, never-fetched sources); unused slots flow to the other pool.
3. Skip sources that are disabled, whose provider is not ready, that were polled within
   their minimum interval (doubled per consecutive failure, capped), or whose provider
   lacks quota. Skips are recorded with reasons.
4. Rank the rest by reliability x expected yield x staleness.

Slots that cannot be filled stay unused: no candidates are invented to meet a share.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trend_agent.discovery.config import DiscoveryConfig
from trend_agent.discovery.models import (
    TARGET_CATEGORIES,
    Category,
    DiscoverySource,
    SourceHealth,
    SourcePool,
)


@dataclass(frozen=True)
class SourceState:
    key: str
    last_checked_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    health: SourceHealth = SourceHealth.UNKNOWN
    expected_yield: float = 0.0  # EWMA of new candidates per fetch


@dataclass(frozen=True)
class PlannedFetch:
    source: DiscoverySource
    pool: SourcePool
    score: float


@dataclass(frozen=True)
class SkippedSource:
    source_key: str
    category: Category
    reason: str


@dataclass
class DiscoveryPlan:
    fetches: list[PlannedFetch] = field(default_factory=list)
    skipped: list[SkippedSource] = field(default_factory=list)
    slots: dict[Category, int] = field(default_factory=dict)
    exploration_slots: dict[Category, int] = field(default_factory=dict)
    unused_slots: dict[Category, int] = field(default_factory=dict)

    def for_category(self, category: Category) -> list[PlannedFetch]:
        return [f for f in self.fetches if f.source.category is category]


def allocate_slots(
    total: int, shares: Mapping[Category, float], categories: Iterable[Category]
) -> dict[Category, int]:
    """Largest-remainder split of ``total`` slots over the selected categories.

    For a single-category run the category receives its own share of the total (at least
    one slot), so running categories one at a time cannot exceed the combined budget.
    """
    selected = [c for c in TARGET_CATEGORIES if c in set(categories)]
    if not selected:
        return {}
    if len(selected) < len(TARGET_CATEGORIES):
        return {c: max(1, math.ceil(total * shares[c])) for c in selected}
    raw = {c: total * shares[c] for c in selected}
    slots = {c: math.floor(v) for c, v in raw.items()}
    remaining = total - sum(slots.values())
    for c in sorted(selected, key=lambda c: (-(raw[c] - slots[c]), TARGET_CATEGORIES.index(c))):
        if remaining <= 0:
            break
        slots[c] += 1
        remaining -= 1
    return slots


def poll_interval(
    source: DiscoverySource, state: SourceState | None, max_multiplier: int
) -> timedelta:
    failures = state.consecutive_failures if state else 0
    multiplier = min(2**failures, max_multiplier)
    return timedelta(minutes=source.min_poll_interval_minutes * multiplier)


def _score(
    source: DiscoverySource, state: SourceState | None, now: datetime, interval: timedelta
) -> float:
    yield_factor = 1.0 + min((state.expected_yield if state else 0.0) / 5.0, 1.0)
    if state is None or state.last_checked_at is None:
        staleness = 3.0
    else:
        staleness = min((now - state.last_checked_at) / interval, 3.0)
    return round(source.reliability * yield_factor * staleness, 4)


ReadyCheck = Callable[[DiscoverySource], tuple[bool, str]]
_Candidate = tuple[DiscoverySource, float, SourcePool]


def _take(
    pool: list[_Candidate],
    limit: int,
    chosen: list[_Candidate],
    slots: int,
    category: Category,
    plan: DiscoveryPlan,
    quota_ok: ReadyCheck,
) -> None:
    """Move up to ``limit`` candidates from ``pool`` into ``chosen`` (quota permitting)."""
    while pool and len(chosen) < slots and limit > 0:
        candidate = pool.pop(0)
        ok, why = quota_ok(candidate[0])
        if not ok:
            plan.skipped.append(SkippedSource(candidate[0].key, category, why))
            continue
        chosen.append(candidate)
        limit -= 1


def build_plan(
    sources: Iterable[DiscoverySource],
    states: Mapping[str, SourceState],
    *,
    categories: Iterable[Category],
    config: DiscoveryConfig,
    now: datetime,
    provider_ready: ReadyCheck,
    quota_ok: ReadyCheck,
    max_fetches: int | None = None,
    respect_intervals: bool = True,
) -> DiscoveryPlan:
    selected = list(categories)
    total = max_fetches if max_fetches is not None else config.run.max_fetches_per_run
    shares = {c: config.category_budgets.share(c) for c in TARGET_CATEGORIES}
    plan = DiscoveryPlan(slots=allocate_slots(total, shares, selected))
    by_category: dict[Category, list[DiscoverySource]] = {c: [] for c in selected}
    for source in sources:
        if source.category in by_category:
            by_category[source.category].append(source)

    for category in selected:
        slots = plan.slots.get(category, 0)
        eligible: list[tuple[DiscoverySource, float, SourcePool]] = []
        for source in sorted(by_category[category], key=lambda s: s.key):
            state = states.get(source.key)
            if not source.enabled:
                plan.skipped.append(
                    SkippedSource(source.key, category, "disabled in configuration")
                )
                continue
            ready, why = provider_ready(source)
            if not ready:
                plan.skipped.append(SkippedSource(source.key, category, why))
                continue
            interval = poll_interval(source, state, config.run.max_failure_backoff_multiplier)
            if (
                respect_intervals
                and state
                and state.last_checked_at
                and now - state.last_checked_at < interval
            ):
                next_at = (state.last_checked_at + interval).isoformat()
                plan.skipped.append(
                    SkippedSource(source.key, category, f"polled recently; next eligible {next_at}")
                )
                continue
            never_fetched = state is None or state.last_checked_at is None
            pool = (
                SourcePool.EXPLORATION
                if source.pool is SourcePool.EXPLORATION or never_fetched
                else SourcePool.EXPLOITATION
            )
            eligible.append((source, _score(source, state, now, interval), pool))

        explore = sorted(
            (e for e in eligible if e[2] is SourcePool.EXPLORATION), key=lambda e: (-e[1], e[0].key)
        )
        exploit = sorted(
            (e for e in eligible if e[2] is SourcePool.EXPLOITATION),
            key=lambda e: (-e[1], e[0].key),
        )
        explore_slots = round(slots * config.exploration_share)
        if slots >= 2 and explore and explore_slots == 0 and config.exploration_share > 0:
            explore_slots = 1
        plan.exploration_slots[category] = explore_slots

        chosen: list[_Candidate] = []
        args = (chosen, slots, category, plan, quota_ok)
        _take(explore, explore_slots, *args)
        _take(exploit, slots - len(chosen), *args)
        _take(explore, slots - len(chosen), *args)  # unused exploitation -> exploration
        for source, score, pool in chosen:
            plan.fetches.append(PlannedFetch(source, pool, score))
        for source, _score_value, _pool in explore + exploit:
            plan.skipped.append(SkippedSource(source.key, category, "category budget exhausted"))
        plan.unused_slots[category] = slots - len(chosen)
    return plan
