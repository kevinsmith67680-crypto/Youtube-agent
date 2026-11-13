"""Cost-aware analysis queue: priorities, category allocation and budgets (pure logic).

Each candidate gets a priority from its Phase 4 demand basis, an urgency boost for
time-sensitive categories (new AI models, time-limited game events, technology
announcements), its severity and how recently it was detected. Category allocation
(AI 45 %, technology 25 %, gaming 30 % by default) is a *priority ordering*, not a hard
quota: unused share flows to the other categories. Selection stops when the daily
search-request budget would be exceeded; when the LLM token budget is short the analysis
still runs, in reduced-capability (deterministic-only) mode. Unchanged inputs inside the
re-analysis window are skipped - that is the cache-invalidation rule.

Nothing here browses autonomously: candidates come only from Phase 4 events and explicit
requests.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trend_agent.content_analysis.config import ContentAnalysisConfig
from trend_agent.content_analysis.entity_verification import QueryEntities
from trend_agent.content_analysis.intent_classifier import classify
from trend_agent.schemas.content_opportunity import DemandBasis
from trend_agent.schemas.search_intent import PrimaryIntent, SubIntent

_GAME_EVENT = frozenset(
    {
        SubIntent.EVENT_GUIDE,
        SubIntent.ITEM_LOCATION,
        SubIntent.CHALLENGE_COMPLETION,
        SubIntent.REWARD_UNLOCK,
        SubIntent.GAME_UPDATE,
    }
)
_NEW_MODEL = frozenset(
    {
        SubIntent.RELEASE_EXPLANATION,
        SubIntent.MODEL_INSTALLATION,
        SubIntent.LOCAL_MODEL_SETUP,
        SubIntent.MODEL_COMPARISON,
        SubIntent.BENCHMARK,
    }
)


@dataclass(frozen=True)
class QueueItem:
    key: str
    query: str
    category: str | None
    basis: DemandBasis
    event_id: int | None = None
    target_id: int | None = None
    query_id: int | None = None
    severity: float = 0.0
    detected_at: datetime | None = None
    updated_at: datetime | None = None
    last_analysed_at: datetime | None = None
    last_versions: tuple[str, str] | None = None  # (analysis, configuration) of last run
    last_expires_at: datetime | None = None
    synthetic: bool = False


@dataclass(frozen=True)
class Planned:
    item: QueueItem
    priority: float
    urgency: str | None
    use_llm: bool


@dataclass
class QueuePlan:
    selected: list[Planned] = field(default_factory=list)
    skipped: list[tuple[QueueItem, str]] = field(default_factory=list)
    search_budget_left: int = 0
    llm_tokens_left: int = 0


def urgency(query: str, category: str | None, config: ContentAnalysisConfig) -> str | None:
    intent = classify(query, QueryEntities(), config, category=category)
    sub = intent.sub_intent
    if intent.category == "ai" and sub in _NEW_MODEL:
        return "new_ai_model"
    if intent.category == "gaming" and sub in _GAME_EVENT:
        return "time_sensitive_game_event"
    if intent.category == "technology" and intent.primary_intent is PrimaryIntent.NEWS:
        return "technology_announcement"
    return None


def priority(
    item: QueueItem, config: ContentAnalysisConfig, now: datetime
) -> tuple[float, str | None]:
    sch = config.scheduling
    p = sch.basis_priority.get(item.basis, 0.0)
    why = urgency(item.query, item.category, config)
    if why:
        p += sch.urgency_boost.get(why, 0.0)
    p += min(max(item.severity, 0.0), 5.0) * 2.0
    if item.detected_at is not None:
        age = (now - item.detected_at).total_seconds() / 86400.0
        p += 10.0 * max(0.0, 1.0 - age / 7.0)
    return round(p, 3), why


def is_unchanged(
    item: QueueItem, *, analysis_version: str, configuration_version: str, now: datetime,
    reanalyse_after: timedelta,
) -> bool:  # fmt: skip
    """True when the last completed analysis is still valid for this item."""
    if item.last_analysed_at is None:
        return False
    if item.last_versions != (analysis_version, configuration_version):
        return False
    if item.last_expires_at is not None and item.last_expires_at <= now:
        return False
    if item.updated_at is not None and item.updated_at > item.last_analysed_at:
        return False
    return now - item.last_analysed_at < reanalyse_after


def plan(
    items: Sequence[QueueItem],
    config: ContentAnalysisConfig,
    *,
    now: datetime,
    analysis_version: str,
    configuration_version: str,
    search_requests_used: int,
    llm_tokens_used: int,
    llm_enabled: bool,
    limit: int | None = None,
    force: bool = False,
) -> QueuePlan:
    sch = config.scheduling
    out = QueuePlan(
        search_budget_left=max(0, sch.daily_search_requests - search_requests_used),
        llm_tokens_left=max(0, sch.daily_llm_tokens - llm_tokens_used),
    )
    # One canonical request plus at most one variant (see the orchestrator).
    per_analysis = 1 + min(1, config.retrieval.max_variants)
    per_llm = config.llm.max_tokens + 8_000  # output cap + a generous prompt estimate
    cap = min(limit or sch.max_per_run, sch.max_per_run)
    pool: dict[str, list[tuple[float, str | None, QueueItem]]] = {}
    seen: set[str] = set()
    for it in items:
        if it.key in seen:
            out.skipped.append((it, "duplicate"))
            continue
        seen.add(it.key)
        if it.basis is DemandBasis.INSUFFICIENT_EVIDENCE and not force:
            out.skipped.append((it, "Phase 4 evidence is insufficient; not worth search quota"))
            continue
        if not force and is_unchanged(
            it,
            analysis_version=analysis_version,
            configuration_version=configuration_version,
            now=now,
            reanalyse_after=sch.reanalyse_after,
        ):
            out.skipped.append((it, "unchanged since the last analysis (cached)"))
            continue
        p, why = priority(it, config, now)
        cat = it.category if it.category in sch.category_allocation else "other"
        pool.setdefault(cat, []).append((p, why, it))
    for cat in pool:
        pool[cat].sort(key=lambda t: (-t[0], t[2].key))
    counts = dict.fromkeys(pool, 0)
    budget = out.search_budget_left
    tokens = out.llm_tokens_left
    while any(pool.values()) and len(out.selected) < cap:
        n = len(out.selected) + 1
        # Category whose allocated share is furthest ahead of its selections; ties by
        # the priority of its best remaining item. "other" only when nothing else is left.
        cat = max(
            (c for c, q in pool.items() if q),
            key=lambda c: (
                sch.category_allocation.get(c, 0.0) * n - counts[c],
                pool[c][0][0],
            ),
        )
        p, why, it = pool[cat].pop(0)
        if budget < per_analysis:
            out.skipped.append((it, "daily search-request budget exhausted"))
            continue
        use_llm = llm_enabled and tokens >= per_llm
        if use_llm:
            tokens -= per_llm
        budget -= per_analysis
        counts[cat] += 1
        out.selected.append(Planned(it, p, why, use_llm))
    for q in pool.values():
        for _, _, it in q:
            out.skipped.append((it, "over this run's analysis limit"))
    return out
