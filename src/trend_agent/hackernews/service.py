"""Hacker News integration services: recording, assessment, refresh, connection test and
read models.

Evidence classes shown to users (never mixed up):

* ``COMMUNITY_ACTIVITY``              Hacker News stories, points and comments and their
                                      measured changes (community discussion activity)
* ``KEYWORD_HYPOTHESIS``              a search phrase *generated* from a verified entity;
                                      never an observed search query
* ``EXTERNAL_ATTENTION``              independent sources outside Hacker News mentioning
                                      the same entity (reposts count once)
* ``DIRECT_YOUTUBE_SEARCH_INTEREST``  only from a live-verified direct provider; Hacker
                                      News never produces it
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from trend_agent.db.discovery_models import DiscoverySourceRow
from trend_agent.db.hackernews_models import HNStoryRow
from trend_agent.db.repositories.hackernews import ORDERS, HackerNewsRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.discovery.config import DiscoveryConfig
from trend_agent.discovery.models import TARGET_CATEGORIES, Category, DiscoveryItem, DiscoverySource
from trend_agent.discovery.service import DiscoveryRunSummary, DiscoveryService
from trend_agent.github.service import direct_interest_available
from trend_agent.hackernews.classify import classify_story
from trend_agent.hackernews.client import HackerNewsClient
from trend_agent.hackernews.config import HackerNewsSettings
from trend_agent.hackernews.models import (
    LIST_LABEL,
    LISTS,
    MEASUREMENT_DEFINITIONS,
    SIGNAL_CLASS,
    SIGNAL_LABEL,
)
from trend_agent.hackernews.momentum import StoryObservation, compute_momentum
from trend_agent.hackernews.priority import PriorityInputs, assess
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import ProviderError
from trend_agent.utils.time import Clock, utc_now

PROVIDER = "hackernews_firebase"
EVIDENCE_CLASSES = {
    "COMMUNITY_ACTIVITY": "Hacker News stories, points and comments, and their measured "
    "changes between stored observations (community discussion activity). Not search "
    "interest, not views, not demand.",
    "KEYWORD_HYPOTHESIS": "A search phrase generated from a verified entity and templates. "
    "A hypothesis to test, never an observed search query.",
    "EXTERNAL_ATTENTION": "Independent sources outside Hacker News mentioning the same "
    "entity (official announcements, GitHub, YouTube video metadata). Reposts count once.",
    "DIRECT_YOUTUBE_SEARCH_INTEREST": "Relative interest in YouTube Search from a "
    "live-verified direct provider. Hacker News data never produces this class.",
}
NOT_SEARCH_DEMAND = (
    "Hacker News attention is community discussion activity. It is not YouTube search "
    "volume or search interest and cannot confirm a YouTube Search surge."
)


# ---------------------------------------------------------------------------- assessment


def assess_story(
    session: Session, row: HNStoryRow, settings: HackerNewsSettings, *, now: datetime
) -> dict[str, Any]:
    """Recompute momentum, priority and refresh tier from stored observations."""
    repo = HackerNewsRepository(session)
    obs = repo.observations(row.story_id)
    m = settings.momentum
    momentum = compute_momentum(
        [StoryObservation(o.observed_at, o.score, o.comments) for o in obs],
        min_interval_minutes=m.min_interval_minutes,
        rising_score_per_hour=m.rising_score_per_hour,
        rising_comments_per_hour=m.rising_comments_per_hour,
    )
    entity = repo.entity(row.entity_id)
    corroboration = repo.corroboration(row.entity_id)
    repost_of = repo.repost_of(row)
    result = assess(
        PriorityInputs(
            relevance=row.relevance,
            subject=row.subject,
            published_at=row.published_at,
            score=row.latest_score,
            comments=row.latest_comments,
            momentum=momentum,
            lists=tuple(row.lists or ()),
            corroborating_publishers=int(corroboration["independent_publishers"]),
            entity_status=entity.status.value if entity else None,
            repost_of=repost_of,
        ),
        settings,
        now=now,
    )
    components = {
        **result.components,
        "_meta": {
            "computed_at": now.isoformat(),
            "repost_of": repost_of,
            "stop_reason": result.stop_reason,
            "note": "heuristic ranking for what to look at; not demand or search interest",
        },
    }
    repo.set_assessment(
        row.story_id,
        priority=result.score,
        components=components,
        momentum=momentum.as_dict(),
        momentum_state=momentum.state,
        tier=result.tier,
        monitoring_state=result.monitoring_state,
        next_refresh_at=result.next_refresh_at,
    )
    return {"priority": result.score, "tier": result.tier, "momentum": momentum.state}


# ---------------------------------------------------------------------------- recording


class HackerNewsObservationRecorder:
    """Discovery observer: keeps story rows and one observation per collection."""

    def __init__(
        self,
        settings: HackerNewsSettings,
        retention_days: int | None,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._settings = settings
        self._retention = timedelta(days=retention_days) if retention_days else None
        self._clock = clock
        self._provider_ids: dict[int, int | None] = {}
        self._touched: dict[uuid.UUID, set[int]] = {}

    def _provider_id(self, session: Session, source_id: int) -> int | None:
        if source_id not in self._provider_ids:
            row = session.get(DiscoverySourceRow, source_id)
            self._provider_ids[source_id] = row.provider_id if row else None
        return self._provider_ids[source_id]

    def observe(
        self,
        session: Session,
        *,
        run_id: uuid.UUID,
        source: DiscoverySource,
        item: DiscoveryItem,
        item_id: int,
        source_id: int,
    ) -> None:
        if item.provider_name != PROVIDER:
            return
        meta = item.metadata.get("hackernews")
        if not isinstance(meta, dict) or not str(meta.get("story_id", "")).isdigit():
            return
        repo = HackerNewsRepository(session)
        expires = item.collected_at + self._retention if self._retention else None
        previous = repo.story(int(meta["story_id"]))
        # A story on several lists is read once per cache window: a second list in the same
        # poll would only repeat the cached record, so it adds no observation.
        repeat = (
            previous is not None
            and previous.last_observed_at is not None
            and timedelta(0)
            < item.collected_at - previous.last_observed_at
            < timedelta(seconds=self._settings.client.item_cache_seconds)
        )
        story_id = repo.upsert_story(
            meta,
            title=item.title,
            source_key=source.key,
            run_id=run_id,
            item_id=item_id,
            provider_id=self._provider_id(session, source_id),
            seen_at=item.collected_at,
            expires_at=expires,
            is_synthetic=item.is_synthetic,
        )
        if not repeat:
            repo.add_observation(
                story_id,
                observed_at=item.collected_at,
                score=meta.get("score"),
                comments=meta.get("comments"),
                origin="list",
                list_name=meta.get("list"),
                rank=meta.get("rank"),
                source_key=source.key,
                run_id=run_id,
                item_id=item_id,
                expires_at=expires,
                is_synthetic=item.is_synthetic,
            )
        self._touched.setdefault(run_id, set()).add(story_id)

    def finish(self, session: Session, *, run_id: uuid.UUID, item_ids: Sequence[int]) -> None:
        repo = HackerNewsRepository(session)
        repo.link_entities(item_ids)
        now = self._clock()
        for story_id in sorted(self._touched.pop(run_id, set())):
            row = repo.story(story_id)
            if row is not None:
                assess_story(session, row, self._settings, now=now)


# ---------------------------------------------------------------------------- refresh


class HackerNewsRefresher:
    """Incremental re-observation of tracked stories.

    One ``/updates.json`` request says which items changed recently; tracked stories in it
    are pulled forward (never re-read sooner than the momentum minimum interval). Stories
    due by their tier are refreshed regardless (the updates feed covers only a short
    window, so absence from it does not prove a story is unchanged). At most
    ``refresh.max_per_run`` item requests per run; stories older than
    ``refresh.stop_after_hours`` stop being monitored.
    """

    def __init__(
        self,
        client: HackerNewsClient,
        factory: SessionFactory,
        settings: HackerNewsSettings,
        *,
        retention_days: int | None,
        clock: Clock = utc_now,
        enabled: bool = True,
    ) -> None:
        self.client = client
        self._factory = factory
        self._settings = settings
        self._retention = timedelta(days=retention_days) if retention_days else None
        self._clock = clock
        self._enabled = enabled

    async def run(
        self, *, max_items: int | None = None, use_updates: bool = True
    ) -> dict[str, Any]:
        if not self._enabled:
            return {"skipped": "Hacker News provider disabled"}
        r = self._settings.refresh
        limit = max(1, min(max_items or r.max_per_run, r.max_per_run))
        now = self._clock()
        ctx = RequestContext()
        report: dict[str, Any] = {
            "started_at": now.isoformat(),
            "requests": 0,
            "updates_feed": None,
            "selected": 0,
            "observed": 0,
            "deactivated": {},
            "failed": {},
            "errors": [],
        }
        with transaction(self._factory) as s:
            aged = HackerNewsRepository(s).deactivate_older_than(
                now - timedelta(hours=r.stop_after_hours)
            )
        if aged:
            report["deactivated"]["older_than_stop_after"] = aged
        changed: set[int] = set()
        if use_updates:
            try:
                report["requests"] += 1
                changed = await self.client.updates(ctx)
                report["updates_feed"] = {"changed_items": len(changed)}
            except ProviderError as exc:
                report["updates_feed"] = {"error": exc.to_dict()}
                report["errors"].append(f"updates: {exc.message}")
        with transaction(self._factory) as s:
            rows = HackerNewsRepository(s).refresh_candidates(
                now,
                changed=changed,
                min_gap=timedelta(minutes=self._settings.momentum.min_interval_minutes),
                limit=limit,
            )
            ids = [row.story_id for row in rows]
            pulled_forward = sum(
                1 for row in rows if row.next_refresh_at is None or row.next_refresh_at > now
            )
        report["selected"] = len(ids)
        report["pulled_forward_by_updates"] = pulled_forward
        if not ids:
            return report
        report["requests"] += len(ids)
        batch = await self.client.items(ids, ctx)
        observed_at = self._clock()
        expires = observed_at + self._retention if self._retention else None
        with transaction(self._factory) as s:
            repo = HackerNewsRepository(s)
            for story_id in ids:
                item = batch.items.get(story_id)
                if item is not None:
                    c = classify_story(item, self._settings.classifier)
                    repo.add_observation(
                        story_id,
                        observed_at=observed_at,
                        score=item.score,
                        comments=item.descendants,
                        origin="refresh",
                        expires_at=expires,
                    )
                    title = None if "prompt_injection_suspected" in c.flags else c.clean_title
                    repo.record_refresh(
                        story_id,
                        observed_at=observed_at,
                        score=item.score,
                        comments=item.descendants,
                        title=title,
                    )
                    row = repo.story(story_id)
                    if row is not None:
                        assess_story(s, row, self._settings, now=observed_at)
                    report["observed"] += 1
                elif story_id in batch.skipped:
                    # deleted, dead, missing (null) or no longer a story: stop monitoring
                    reason = batch.skipped[story_id]
                    repo.deactivate(story_id, reason)
                    report["deactivated"][reason] = report["deactivated"].get(reason, 0) + 1
                else:
                    reason = batch.failed.get(story_id, "unknown")
                    # retry at the normal cadence, not in a tight loop
                    repo.defer(story_id, now + timedelta(minutes=r.normal_priority_minutes))
                    report["failed"][reason] = report["failed"].get(reason, 0) + 1
        return report


# ---------------------------------------------------------------------------- discovery


async def run_list_discovery(
    service: DiscoveryService,
    sources: Sequence[DiscoverySource],
    *,
    config: DiscoveryConfig,
    max_fetches: int | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> list[DiscoveryRunSummary]:
    """Run Hacker News list sources through the normal discovery pipeline.

    The general planner gives a single-category run ``ceil(total * share)`` slots; HN
    list sources are few and their category only groups them, so each category is run
    on its own with a total sized to give one slot per source (bounded by
    ``max_fetches`` overall). Minimum polling intervals and quotas still apply unless
    ``force``.
    """
    by_category: dict[Category, list[DiscoverySource]] = {}
    for source in sources:
        by_category.setdefault(source.category, []).append(source)
    remaining = max_fetches if max_fetches is not None else len(sources)
    out: list[DiscoveryRunSummary] = []
    for category in TARGET_CATEGORIES:
        group = by_category.get(category)
        if not group or remaining <= 0:
            continue
        slots = min(len(group), remaining)
        share = config.category_budgets.share(category)
        total = math.ceil(slots / share) if share > 0 else slots
        summary = await service.run(
            [category], max_fetches=total, dry_run=dry_run, sources=group[:slots], force=force
        )
        remaining -= sum(1 for s in summary.sources if s.status != "skipped")
        out.append(summary)
    return out


# ---------------------------------------------------------------------------- connection


async def connection_test(client: HackerNewsClient, *, sample_items: int = 3) -> dict[str, Any]:
    """Bounded live check of every endpoint used: ``/maxitem.json``, ``/updates.json``,
    the five story lists and a few ``/item/{id}.json`` records (about 10 requests).

    Returns a report; ``error`` holds the typed error of the first failing call.
    """
    ctx = RequestContext()
    report: dict[str, Any] = {
        "ok": False,
        "base_url": client.base_url,
        "endpoints": {},
        "lists": {},
        "sample": [],
        "error": None,
    }

    async def step(name: str, coro: Any) -> Any:
        try:
            value = await coro
            report["endpoints"][name] = "ok"
            return value
        except ProviderError as exc:
            report["endpoints"][name] = f"failed: {exc.reason or exc.__class__.__name__}"
            if report["error"] is None:
                report["error"] = exc
                report["error_detail"] = exc.to_dict()
            return None

    report["max_item"] = await step("/maxitem.json", client.max_item(ctx))
    changed = await step("/updates.json", client.updates(ctx))
    report["updates_changed_items"] = len(changed) if changed is not None else None
    top: list[int] = []
    for name in LISTS:
        ids = await step(f"/{name}.json", client.story_ids(name, ctx))
        report["lists"][name] = len(ids) if ids is not None else None
        if name == "topstories" and ids:
            top = list(ids[:sample_items])
    if top:
        batch = await step("/item/{id}.json", client.items(top, ctx))
        if batch is not None:
            for story_id in top:
                item = batch.items.get(story_id)
                if item is None:
                    continue
                c = classify_story(item, HackerNewsSettings().classifier)
                report["sample"].append(
                    {
                        "story_id": item.id,
                        "title": c.clean_title,
                        "discussion_url": item.discussion_url,
                        "linked_url": item.url,
                        "author": item.by,
                        "score": item.score,
                        "comments": item.descendants,
                        "published_at": item.published_at.isoformat()
                        if item.published_at
                        else None,
                        "category": c.primary.value,
                    }
                )
            if batch.skipped or batch.failed:
                report["sample_skipped"] = dict(batch.skipped)
                report["sample_failed"] = dict(batch.failed)
    report["ok"] = report["error"] is None and bool(report["sample"])
    report["requests_sent"] = client.requests_sent
    return report


# ---------------------------------------------------------------------------- read models


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def story_view(row: HNStoryRow, now: datetime) -> dict[str, Any]:
    c = row.classification or {}
    mom = row.momentum or {}
    return {
        "story_id": row.story_id,
        "title": row.title,
        "linked_url": row.linked_url,
        "discussion_url": row.discussion_url,
        "domain": row.domain,
        "author": row.author,
        "kind": row.kind,
        "subject": row.subject,
        "primary_category": row.primary_category,
        "secondary_category": row.secondary_category,
        "ambiguous": row.ambiguous,
        "relevance": row.relevance,
        "project_name": c.get("project_name"),
        "potential_intents": list(c.get("potential_intents") or []),
        "flags": list(c.get("flags") or []),
        "lists": [LIST_LABEL.get(x, x) for x in (row.lists or [])],
        "published_at": _iso(row.published_at),
        "age_hours": round((now - row.published_at).total_seconds() / 3600, 1),
        "score": row.latest_score,
        "comments": row.latest_comments,
        "last_observed_at": _iso(row.last_observed_at),
        "priority_score": round(row.priority_score, 2),
        "momentum_state": row.momentum_state,
        "score_per_hour": mom.get("score_per_hour"),
        "comments_per_hour": mom.get("comments_per_hour"),
        "momentum_status": mom.get("status"),
        "monitoring_state": row.monitoring_state,
        "refresh_tier": row.refresh_tier,
        "next_refresh_at": _iso(row.next_refresh_at),
        "entity_id": row.entity_id,
        "first_seen_at": _iso(row.first_seen_at),
        "freshness_hours": round((now - row.last_seen_at).total_seconds() / 3600, 1),
        "source_key": row.first_source_key,
        "item_id": row.item_id,
        "evidence_class": SIGNAL_CLASS,
        "signal_label": SIGNAL_LABEL,
        "synthetic": row.is_synthetic,
    }


def source_health(session: Session) -> list[dict[str, Any]]:
    rows = (
        session.execute(
            sa.select(DiscoverySourceRow)
            .where(DiscoverySourceRow.provider_name == PROVIDER)
            .order_by(DiscoverySourceRow.key)
        )
        .scalars()
        .all()
    )
    return [
        {
            "source_key": r.key,
            "list": r.source_reference,
            "enabled": r.enabled,
            "poll_interval_minutes": r.min_poll_interval_minutes,
            "health": r.health_status.value,
            "last_checked_at": _iso(r.last_checked_at),
            "last_success_at": _iso(r.last_success_at),
            "last_new_items_at": _iso(r.last_new_items_at),
            "consecutive_failures": r.consecutive_failures,
            "last_error": r.last_error,
            "cursor": r.etag if r.etag and r.etag.startswith("hn:last=") else None,
        }
        for r in rows
    ]


class HackerNewsReadService:
    """Database read models for the CLI and the dashboard API."""

    def __init__(self, factory: SessionFactory, *, clock: Clock = utc_now) -> None:
        self._factory = factory
        self._clock = clock

    def stories(
        self,
        *,
        days: int | None = None,
        seen_since: datetime | None = None,
        category: str | None = None,
        list_name: str | None = None,
        kind: str | None = None,
        momentum_state: str | None = None,
        limit: int = 50,
        offset: int = 0,
        order: str = "published",
        include_synthetic: bool = False,
    ) -> dict[str, Any]:
        if order not in ORDERS:
            raise ValueError(f"order must be one of {', '.join(ORDERS)}")
        now = self._clock()
        since = now - timedelta(days=days) if days else None
        with transaction(self._factory) as s:
            rows, total = HackerNewsRepository(s).stories(
                since=since,
                seen_since=seen_since,
                category=category,
                list_name=list_name,
                kind=kind,
                momentum_state=momentum_state,
                include_synthetic=include_synthetic,
                limit=limit,
                offset=offset,
                order=order,
            )
            items = [story_view(r, now) for r in rows]
        return {
            "total": total,
            "items": items,
            "since": _iso(since),
            "signal_label": SIGNAL_LABEL,
            "caveat": NOT_SEARCH_DEMAND,
        }

    def trending(self, *, hours: int = 48, limit: int = 20) -> dict[str, Any]:
        """Stories with the fastest *measured* discussion growth. Stories with fewer than
        two observations far enough apart are counted, never ranked on a guess."""
        now = self._clock()
        since = now - timedelta(hours=hours)
        with transaction(self._factory) as s:
            repo = HackerNewsRepository(s)
            rows, _ = repo.stories(since=since, order="momentum", limit=500)
            measured = [r for r in rows if (r.momentum or {}).get("status") == "ok"]
            waiting = len(rows) - len(measured)
            items = [story_view(r, now) for r in measured[:limit]]
        return {
            "window_hours": hours,
            "items": items,
            "measured": len(measured),
            "awaiting_repeat_observation": waiting,
            "basis": "points per hour between stored observations at least the minimum "
            "interval apart; 'rising' is a community-activity label",
            "evidence_class": SIGNAL_CLASS,
            "caveat": NOT_SEARCH_DEMAND,
        }

    def story_detail(self, story_id: int) -> dict[str, Any] | None:
        now = self._clock()
        with transaction(self._factory) as s:
            repo = HackerNewsRepository(s)
            row = repo.story(story_id)
            if row is None:
                return None
            view = story_view(row, now)
            obs = repo.observations(story_id)
            entity = repo.entity(row.entity_id)
            corroboration = repo.corroboration(row.entity_id)
            keywords = repo.keywords_for(row.item_id, row.entity_id)
            reposts = repo.reposts(row)
            direct = direct_interest_available(s)
            view.update(
                {
                    "classification": row.classification,
                    "priority_components": row.priority_components,
                    "momentum": row.momentum,
                    "observations": [
                        {
                            "observed_at": o.observed_at.isoformat(),
                            "score": o.score,
                            "comments": o.comments,
                            "origin": o.origin,
                            "list": LIST_LABEL.get(o.list_name or "", o.list_name),
                            "rank": o.rank,
                            "source_key": o.source_key,
                            "run_id": str(o.run_id) if o.run_id else None,
                        }
                        for o in obs
                    ],
                    "entity": {
                        "id": entity.id,
                        "name": entity.canonical_name,
                        "type": entity.entity_type.value,
                        "status": entity.status.value,
                    }
                    if entity
                    else None,
                    "reposts": [
                        {
                            "story_id": sid,
                            "discussion_url": f"https://news.ycombinator.com/item?id={sid}",
                        }
                        for sid in reposts
                    ],
                    "keyword_candidates": keywords,
                    "external_attention": {"evidence_class": "EXTERNAL_ATTENTION", **corroboration},
                    "direct_youtube_search_interest": {
                        "evidence_class": "DIRECT_YOUTUBE_SEARCH_INTEREST",
                        "available": direct,
                        "detail": "not measured: no live-verified direct YouTube Search "
                        "provider has data"
                        if not direct
                        else "see the trends page for monitored queries; Hacker News "
                        "activity is never used as this signal",
                    },
                    "caveat": NOT_SEARCH_DEMAND,
                }
            )
        return view

    def summary(self) -> dict[str, Any]:
        now = self._clock()
        with transaction(self._factory) as s:
            stats = HackerNewsRepository(s).stats()
            health = source_health(s)
        last = stats.get("last_observed_at")
        return {
            **{k: v.isoformat() if isinstance(v, datetime) else v for k, v in stats.items()},
            "freshness_hours": round((now - last).total_seconds() / 3600, 1) if last else None,
            "sources": health,
            "evidence_classes": EVIDENCE_CLASSES,
            "definitions": MEASUREMENT_DEFINITIONS,
            "signal_label": SIGNAL_LABEL,
            "caveat": NOT_SEARCH_DEMAND,
        }
