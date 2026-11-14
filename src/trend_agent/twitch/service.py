"""Twitch services: polling and recording, attention assessment, promotion of emerging games
into Phase 2, read models for the CLI/API/dashboard, and a bounded connection test.

Evidence classes (never merged into one number):

* ``TWITCH_ACTIVITY``    rankings and live streams on Twitch (external gaming attention)
* ``OBSERVED_VIEWERS``   summed concurrent viewers of the streams actually returned
* ``PARTIAL_SAMPLE``     a collection that stopped before the API's list ended
* ``RANK_CHANGE``        position change in Get Top Games between stored observations
* ``PROXY_OPPORTUNITY``  a possible YouTube opportunity (hypothesis), never demand
* ``DIRECT_YOUTUBE_SEARCH_INTEREST``  only from a live-verified direct provider; Twitch never
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from trend_agent.db.content_models import ContentGapRow, ContentOpportunityRow
from trend_agent.db.forecast_models import ForecastPredictionRow
from trend_agent.db.recommendation_models import VideoRecommendationRow
from trend_agent.db.repositories.twitch import TwitchRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.db.twitch_models import (
    TwitchGameRow,
    TwitchRankObservationRow,
    TwitchStreamObservationRow,
    TwitchViewerObservationRow,
)
from trend_agent.discovery.config import DiscoveryConfig
from trend_agent.discovery.models import Category
from trend_agent.discovery.providers.twitch import game_source
from trend_agent.github.service import direct_interest_available
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import ProviderError
from trend_agent.twitch import attention as att
from trend_agent.twitch.client import TwitchClient
from trend_agent.twitch.config import POLICY_CACHE_HOURS, TwitchSettings
from trend_agent.twitch.models import (
    EVIDENCE_CLASSES,
    MEASUREMENT_DEFINITIONS,
    SIGNAL_CLASS,
    SIGNAL_LABEL,
    TwitchGame,
)
from trend_agent.utils.time import Clock, utc_now

NOT_SEARCH_DEMAND = (
    "Twitch data is livestream activity on another platform (external gaming attention). "
    "It is not YouTube search volume or search interest and cannot confirm a YouTube "
    "Search surge."
)
RISING = ("rising", "sustained_rise")

# Candidate video formats -> the existing Phase 2 templates that ground them. A format is
# offered only when one of its templates produced a hypothesis for the verified game;
# update/event formats additionally need a verified official update/event entity.
FORMATS: dict[str, tuple[tuple[str, ...], str]] = {
    "new_game_introduction": (("game.gameplay", "game.review"), "verified game entity"),
    "gameplay_tutorial": (("game.how_to_play",), "verified game entity"),
    "beginner_guide": (("game.beginner",), "verified game entity"),
    "strategy_demonstration": (("game.gameplay", "event.strategy"), "verified game entity"),
    "performance_optimization": (("game.settings",), "a grounded 'performance' attribute"),
    "update_explanation": (
        ("update.whats_new", "update.changes", "update.bugs", "update.crash"),
        "a verified official update entity",
    ),
    "verified_event_walkthrough": (
        ("event.guide", "event.complete", "event.challenges", "event.rewards", "event.end"),
        "a verified official event entity",
    ),
    "game_comparison": (("comparison.vs",), "a competitor grounded in evidence"),
    "ai_gaming_experiment": ((), "no keyword template: a concept format only"),
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class TwitchMonitor:
    """Polls Twitch at the configured cadence and keeps every observation's provenance."""

    def __init__(
        self,
        client: TwitchClient,
        factory: SessionFactory,
        settings: TwitchSettings,
        *,
        clock: Clock = utc_now,
        enabled: bool = True,
        trigger: str = "scheduler",
    ) -> None:
        self.client = client
        self._factory = factory
        self.settings = settings
        self._clock = clock
        self.enabled = enabled
        self.trigger = trigger

    def _expires(self, at: datetime) -> datetime:
        return at + timedelta(hours=self.settings.retention.hours)

    # ------------------------------------------------------------------ polls
    async def poll_top_games(self, ctx: RequestContext | None = None) -> dict[str, Any]:
        ctx = ctx or RequestContext()
        observed_at = self._clock()
        before = self.client.requests_sent
        res = await self.client.top_games(ctx, count=self.settings.collection.top_games)
        exp = self._expires(observed_at)
        with transaction(self._factory) as s:
            repo = TwitchRepository(s)
            repo.upsert_games(
                [g for _, g in res.ranked],
                seen_at=observed_at,
                expires_at=exp,
                non_games=self.settings.monitor.non_game_categories,
            )
            sid = repo.add_snapshot(
                kind="top_games",
                game_id=None,
                languages=(),
                observed_at=observed_at,
                completed_at=self._clock(),
                collection=res,
                items=len(res.ranked),
                viewer_sum=None,  # Get Top Games returns positions, never viewer numbers
                stream_count=None,
                requests=self.client.requests_sent - before,
                rate_limit_remaining=self.client.rate.remaining,
                trigger=self.trigger,
                expires_at=exp,
            )
            repo.add_ranks(sid, res.ranked, observed_at=observed_at, expires_at=exp)
        return {
            "snapshot_id": sid,
            "games": len(res.ranked),
            "sample_status": res.status,
            "reason": res.reason,
            "game_ids": [g.id for _, g in res.ranked],
        }

    async def poll_top_streams(self, ctx: RequestContext | None = None) -> dict[str, Any]:
        ctx = ctx or RequestContext()
        c = self.settings.collection
        observed_at = self._clock()
        before = self.client.requests_sent
        res = await self.client.streams(ctx, languages=c.languages, max_pages=c.top_stream_pages)
        exp = self._expires(observed_at)
        # Per-game aggregates from the global top-streams sample: always a partial view of
        # each game (only its streams that made the global top N), unless the whole list was
        # read. Games not in the sample get no row at all (unknown, not zero).
        per: dict[str, dict[str, Any]] = {}
        names: dict[str, str] = {}
        for st in res.streams:
            if not st.game_id:
                continue
            g = per.setdefault(
                st.game_id,
                {
                    "game_id": st.game_id,
                    "streams": 0,
                    "viewers": 0,
                    "top_stream_viewers": 0,
                    "status": "complete" if res.status == "complete" else "partial",
                    "basis": "top_streams",
                    "languages": res.languages,
                    "cap": res.page_size * res.pages_requested,
                },
            )
            g["streams"] += 1
            g["viewers"] += st.viewer_count
            g["top_stream_viewers"] = max(g["top_stream_viewers"], st.viewer_count)
            names.setdefault(st.game_id, st.game_name or st.game_id)
        if res.status == "unknown":
            for g in per.values():
                g["status"] = "unknown"
        with transaction(self._factory) as s:
            repo = TwitchRepository(s)
            for gid, name in names.items():
                existing = repo.game(gid)
                repo.upsert_game(
                    gid,
                    name,
                    igdb_id=existing.igdb_id if existing else None,
                    box_art_url=None,
                    seen_at=observed_at,
                    expires_at=exp,
                    non_games=self.settings.monitor.non_game_categories,
                )
            sid = repo.add_snapshot(
                kind="top_streams",
                game_id=None,
                languages=res.languages,
                observed_at=observed_at,
                completed_at=self._clock(),
                collection=res,
                items=len(res.streams),
                viewer_sum=res.viewer_sum,
                stream_count=len(res.streams),
                requests=self.client.requests_sent - before,
                rate_limit_remaining=self.client.rate.remaining,
                trigger=self.trigger,
                expires_at=exp,
            )
            repo.add_streams(
                sid,
                res.streams,
                observed_at=observed_at,
                expires_at=exp,
                title_chars=c.stream_title_chars,
            )
            repo.add_viewers(sid, list(per.values()), observed_at=observed_at, expires_at=exp)
        return {
            "snapshot_id": sid,
            "streams": len(res.streams),
            "games": len(per),
            "sample_status": res.status,
            "reason": res.reason,
            "duplicates": res.duplicates,
            "skipped": dict(res.skipped),
            "game_ids": list(per),
        }

    async def poll_game(self, game_id: str, ctx: RequestContext | None = None) -> dict[str, Any]:
        """All live streams of one game, up to the page limit (complete when the list ends)."""
        ctx = ctx or RequestContext()
        c = self.settings.collection
        observed_at = self._clock()
        before = self.client.requests_sent
        res = await self.client.streams(ctx, game_ids=[game_id], max_pages=c.game_stream_pages)
        moved = [st for st in res.streams if st.game_id != game_id]
        if moved:
            # a channel switched category while the list was being read
            res.streams = [st for st in res.streams if st.game_id == game_id]
            res.skipped["category_changed_during_collection"] += len(moved)
        exp = self._expires(observed_at)
        with transaction(self._factory) as s:
            repo = TwitchRepository(s)
            sid = repo.add_snapshot(
                kind="game_streams",
                game_id=game_id,
                languages=res.languages,
                observed_at=observed_at,
                completed_at=self._clock(),
                collection=res,
                items=len(res.streams),
                viewer_sum=res.viewer_sum,
                stream_count=len(res.streams),
                requests=self.client.requests_sent - before,
                rate_limit_remaining=self.client.rate.remaining,
                trigger=self.trigger,
                expires_at=exp,
            )
            repo.add_streams(
                sid,
                res.streams,
                observed_at=observed_at,
                expires_at=exp,
                title_chars=c.stream_title_chars,
            )
            if repo.game(game_id) is not None:
                repo.add_viewers(
                    sid,
                    [
                        {
                            "game_id": game_id,
                            "streams": len(res.streams),
                            # a complete read with no live streams is a measured zero
                            "viewers": res.viewer_sum,
                            "top_stream_viewers": max(
                                (st.viewer_count for st in res.streams), default=0
                            ),
                            "status": res.status,
                            "basis": "game_streams",
                            "languages": res.languages,
                            "cap": res.page_size * res.pages_requested,
                        }
                    ],
                    observed_at=observed_at,
                    expires_at=exp,
                )
        return {
            "snapshot_id": sid,
            "game_id": game_id,
            "streams": len(res.streams),
            "viewers_observed": res.viewer_sum,
            "sample_status": res.status,
            "reason": res.reason,
            "duplicates": res.duplicates,
            "skipped": dict(res.skipped),
        }

    # ------------------------------------------------------------------ games
    async def resolve(self, name: str, ctx: RequestContext | None = None) -> TwitchGame | None:
        """Exact name through Get Games; a case-insensitive match among stored games is
        retried by ID (Get Games needs the exact title)."""
        ctx = ctx or RequestContext()
        games, _ = await self.client.games(ctx, names=[name.strip()])
        if not games:
            with transaction(self._factory) as s:
                row = TwitchRepository(s).game_by_name(name)
                stored = row.game_id if row else None
            if stored:
                games, _ = await self.client.games(ctx, ids=[stored])
        if not games:
            return None
        game = games[0]
        now = self._clock()
        with transaction(self._factory) as s:
            TwitchRepository(s).upsert_game(
                game.id,
                game.name,
                igdb_id=game.igdb_id,
                box_art_url=game.box_art_url,
                seen_at=now,
                expires_at=self._expires(now),
                non_games=self.settings.monitor.non_game_categories,
            )
        return game

    async def monitor(
        self,
        name: str,
        *,
        tier: str = "high",
        origin: str = "manual",
        ctx: RequestContext | None = None,
    ) -> TwitchGame | None:
        game = await self.resolve(name, ctx)
        if game is None:
            return None
        now = self._clock()
        with transaction(self._factory) as s:
            TwitchRepository(s).set_monitored(
                game.id,
                tier=tier,
                origin=origin,
                requested_name=name.strip()[:200],
                reason=f"{origin} selection",
                now=now,
                expires_at=None,
            )
        return game

    async def ensure_configured_games(self, ctx: RequestContext) -> list[str]:
        """Resolve ``monitor.games`` once (a stored selection is kept, not re-resolved)."""
        wanted = [g for g in self.settings.monitor.games if g.strip()]
        if not wanted:
            return []
        with transaction(self._factory) as s:
            have = {
                (m.requested_name or "").casefold()
                for m in TwitchRepository(s).monitored()
                if m.origin in ("config", "manual")
            }
        added: list[str] = []
        for name in wanted:
            if name.casefold() in have:
                continue
            game = await self.monitor(name, tier="high", origin="config", ctx=ctx)
            if game is not None:
                added.append(game.id)
        return added

    # ------------------------------------------------------------------ assessment
    def assess(self, session: Session, game_ids: Sequence[str], now: datetime) -> int:
        repo = TwitchRepository(session)
        since = now - timedelta(hours=self.settings.attention.window_hours)
        tops = repo.snapshot_times("top_games", since)
        done = 0
        for gid in dict.fromkeys(game_ids):
            if repo.game(gid) is None:
                continue
            ranks = [att.RankPoint(r.observed_at, r.rank) for r in repo.rank_history(gid, since)]
            viewers = [
                att.ViewerPoint(
                    v.observed_at,
                    int(v.viewers_observed),
                    v.streams_observed,
                    v.sample_status,
                    v.sample_basis,
                    v.sample_cap,
                    tuple(v.languages or ()),
                )
                for v in repo.viewer_history(gid, since)
            ]
            # Viewer momentum uses only the game's own stream list. Its share of the global
            # top-streams sample depends on other games' streams entering and leaving that
            # sample, so it is stored and shown (partial) but never treated as the game's
            # growth. Game-level candidates come from Twitch's own ranking instead.
            own = [v for v in viewers if v.basis == "game_streams"]
            result = att.assess(ranks, tops, own, self.settings.attention)
            repo.set_attention(
                gid,
                result=result.as_dict() | {"assessed_at": now.isoformat()},
                state=result.state,
                score=result.score,
                version=result.version,
            )
            done += 1
        return done

    def emerging(self, session: Session, now: datetime) -> list[TwitchGameRow]:
        p = self.settings.promotion
        rows = session.execute(
            sa.select(TwitchGameRow)
            .where(
                TwitchGameRow.attention_state.in_(RISING),
                TwitchGameRow.attention_score >= p.min_attention_score,
                TwitchGameRow.category_kind == "game"
                if p.require_igdb_id
                else TwitchGameRow.category_kind != "non_game",
                TwitchGameRow.is_synthetic.is_(False),
                sa.or_(
                    TwitchGameRow.promoted_at.is_(None),
                    TwitchGameRow.promoted_at <= now - timedelta(hours=p.cooldown_hours),
                ),
            )
            .order_by(TwitchGameRow.attention_score.desc())
            .limit(p.max_per_run)
        ).scalars()
        return list(rows)

    # ------------------------------------------------------------------ cycle
    async def run_due(self, *, max_game_polls: int | None = None) -> dict[str, Any]:
        """One scheduler cycle: purge, poll what is due, assess, auto-monitor, pick
        promotion candidates (promotion itself runs through the discovery service)."""
        if not self.enabled:
            return {"skipped": "Twitch provider disabled or not configured"}
        now = self._clock()
        cad = self.settings.cadence
        ctx = RequestContext()
        report: dict[str, Any] = {"started_at": now.isoformat(), "polls": {}, "errors": []}
        with transaction(self._factory) as s:
            report["purged"] = TwitchRepository(s).purge_expired(now)
        try:
            report["configured_games_added"] = await self.ensure_configured_games(ctx)
        except ProviderError as exc:
            report["errors"].append(f"resolve configured games: {exc.error_type}: {exc.message}")
        touched: set[str] = set()

        def due(kind: str, minutes: int) -> bool:
            with transaction(self._factory) as s:
                last = TwitchRepository(s).latest_snapshot(kind)
                return last is None or now - last.observed_at >= timedelta(minutes=minutes)

        for kind, minutes, poll in (
            ("top_games", cad.top_games_minutes, self.poll_top_games),
            ("top_streams", cad.top_streams_minutes, self.poll_top_streams),
        ):
            if not due(kind, minutes):
                continue
            try:
                out = await poll(ctx)
                touched.update(out.pop("game_ids"))
                report["polls"][kind] = out
            except ProviderError as exc:
                report["errors"].append(f"{kind}: {exc.error_type}: {exc.message}")
        limit = max_game_polls or self.settings.monitor.max_monitored_games
        with transaction(self._factory) as s:
            # read the clock again: selections made earlier in this cycle are due now
            due_games = [
                (m.game_id, m.tier) for m in TwitchRepository(s).due_monitored(self._clock(), limit)
            ]
        game_polls: list[dict[str, Any]] = []
        for gid, tier in due_games:
            minutes = (
                cad.high_priority_game_minutes if tier == "high" else cad.low_priority_game_minutes
            )
            try:
                out = await self.poll_game(gid, ctx)
                game_polls.append(out)
                touched.add(gid)
                next_at = now + timedelta(minutes=minutes)
            except ProviderError as exc:
                report["errors"].append(f"game {gid}: {exc.error_type}: {exc.message}")
                next_at = now + timedelta(minutes=max(minutes, cad.low_priority_game_minutes))
            with transaction(self._factory) as s:
                TwitchRepository(s).mark_polled(gid, now=now, next_at=next_at)
        report["polls"]["game_streams"] = game_polls
        with transaction(self._factory) as s:
            report["entities_resolved"] = TwitchRepository(s).link_existing_entities()
            report["assessed"] = self.assess(s, sorted(touched), now)
            report["auto_monitored"] = self._auto_monitor(s, now)
            report["candidates"] = [
                {"game_id": g.game_id, "name": g.name, "score": g.attention_score}
                for g in self.emerging(s, now)
            ]
        report["requests"] = self.client.requests_sent
        report["rate_limit"] = self.client.rate.as_dict()
        return report

    def _auto_monitor(self, session: Session, now: datetime) -> list[str]:
        m = self.settings.monitor
        if not m.auto_monitor:
            return []
        repo = TwitchRepository(session)
        current = repo.monitored()
        room = m.max_monitored_games - len(current)
        if room <= 0:
            return []
        have = {r.game_id for r in current}
        rows = session.execute(
            sa.select(TwitchGameRow)
            .where(
                TwitchGameRow.attention_state.in_(RISING),
                TwitchGameRow.category_kind == "game",
                TwitchGameRow.game_id.not_in(have) if have else sa.true(),
            )
            .order_by(TwitchGameRow.attention_score.desc())
            .limit(room)
        ).scalars()
        added = []
        for g in rows:
            repo.set_monitored(
                g.game_id,
                tier="low",
                origin="auto",
                requested_name=None,
                reason=f"attention {g.attention_state} (score {g.attention_score:.0f})",
                now=now,
                expires_at=now + timedelta(hours=m.auto_monitor_hours),
            )
            added.append(g.game_id)
        return added


# ---------------------------------------------------------------------------- promotion


def attention_summary(row: TwitchGameRow) -> dict[str, Any]:
    a = row.attention or {}
    return {
        "state": row.attention_state,
        "score": row.attention_score,
        "version": row.attention_version,
        "rank": row.latest_rank,
        "window_positions_gained": (a.get("rank") or {}).get("window_positions_gained"),
        "viewers_pct_per_hour": (a.get("viewers") or {}).get("pct_per_hour"),
        "sample_status": (a.get("viewers") or {}).get("latest_sample_status"),
        "assessed_at": a.get("assessed_at"),
        "signal_class": SIGNAL_CLASS,
    }


async def promote(
    discovery: Any,
    monitor: TwitchMonitor,
    config: DiscoveryConfig,
    *,
    game_ids: Sequence[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Send emerging (or the given) games to the Phase 2 pipeline as ``game_catalog``
    sources: Twitch ID re-verified, entity resolved, keyword hypotheses generated."""
    now = monitor._clock()
    with transaction(monitor._factory) as s:
        repo = TwitchRepository(s)
        rows = (
            [r for r in (repo.game(g) for g in game_ids) if r is not None]
            if game_ids
            else monitor.emerging(s, now)
        )
        targets = [(r.game_id, r.name, attention_summary(r)) for r in rows]
    if not targets or not monitor.settings.promotion.enabled:
        return {"promoted": [], "skipped": "no eligible games" if not targets else "disabled"}
    sources = [
        game_source(gid, name, summary, monitor.settings.promotion.cooldown_hours)
        for gid, name, summary in targets
    ]
    share = config.category_budgets.share(Category.GAMING)
    total = math.ceil(len(sources) / share) if share > 0 else len(sources)
    summary = await discovery.run(
        [Category.GAMING], max_fetches=total, dry_run=dry_run, sources=sources, force=True
    )
    promoted = []
    if not dry_run:
        with transaction(monitor._factory) as s:
            repo = TwitchRepository(s)
            for gid, name, _ in targets:
                entity_id = repo.link_promoted(gid, now=now)
                promoted.append({"game_id": gid, "name": name, "entity_id": entity_id})
    return {
        "promoted": promoted,
        "run": {
            "status": summary.status.value,
            "sources": [
                {"key": o.key, "status": o.status, "items_new": o.items_new, "warnings": o.warnings}
                for o in summary.sources
            ],
            "entities_new": sum(c.entities_new for c in summary.categories.values()),
            "candidates_new": sum(c.candidates_new for c in summary.categories.values()),
        },
    }


# ---------------------------------------------------------------------------- connection


async def connection_test(client: TwitchClient) -> dict[str, Any]:
    """Bounded live test (about 5 Helix requests + 1 token request):
    token -> top 20 games -> 20 live streams -> resolve Fortnite -> 20 Fortnite streams."""
    ctx = RequestContext()
    report: dict[str, Any] = {
        "ok": False,
        "steps": {},
        "top_games": [],
        "streams": [],
        "fortnite": None,
        "fortnite_streams": [],
        "error": None,
    }

    async def step(name: str, coro: Any) -> Any:
        try:
            value = await coro
            report["steps"][name] = "ok"
            return value
        except ProviderError as exc:
            report["steps"][name] = f"failed: {exc.error_type}"
            if report["error"] is None:
                report["error"] = exc
                report["error_detail"] = exc.to_dict()
            return None

    token = await step("app_access_token", client.tokens.validate())
    if token is None:
        report["token"] = client.tokens.state()
        return report
    top = await step("GET /helix/games/top", client.top_games(ctx, count=20))
    if top is not None:
        report["top_games"] = [
            {"rank": r, "game_id": g.id, "name": g.name, "igdb_id": g.igdb_id}
            for r, g in top.ranked
        ]
        report["top_games_sample_status"] = top.status
    streams = await step("GET /helix/streams", client.streams(ctx, max_pages=1, first=20))
    if streams is not None:
        report["streams"] = [
            {
                "stream_id": st.id,
                "user_login": st.user_login,
                "game_name": st.game_name,
                "language": st.language,
                "viewer_count": st.viewer_count,
                "started_at": st.started_at.isoformat(),
            }
            for st in streams.streams
        ]
        report["streams_sample_status"] = streams.status
    found = await step("GET /helix/games?name=Fortnite", client.games(ctx, names=["Fortnite"]))
    if found is not None and found[0]:
        g = found[0][0]
        report["fortnite"] = {"game_id": g.id, "name": g.name, "igdb_id": g.igdb_id}
        fs = await step(
            "GET /helix/streams?game_id=",
            client.streams(ctx, game_ids=[g.id], max_pages=1, first=20),
        )
        if fs is not None:
            report["fortnite_streams"] = [
                {
                    "user_login": st.user_login,
                    "viewer_count": st.viewer_count,
                    "language": st.language,
                }
                for st in fs.streams
            ]
            report["fortnite_sample_status"] = fs.status
            report["fortnite_sampled_viewers"] = fs.viewer_sum
    report["token"] = client.tokens.state()
    report["rate_limit"] = client.rate.as_dict()
    report["requests_sent"] = client.requests_sent
    report["ok"] = report["error"] is None and bool(report["top_games"])
    return report


# ---------------------------------------------------------------------------- read models


def game_view(row: TwitchGameRow, now: datetime) -> dict[str, Any]:
    a = row.attention or {}
    v = a.get("viewers") or {}
    r = a.get("rank") or {}
    labels = list(a.get("labels") or ["TWITCH_ACTIVITY"])
    return {
        "game_id": row.game_id,
        "name": row.name,
        "igdb_id": row.igdb_id,
        "category_kind": row.category_kind,
        "latest_rank": row.latest_rank,
        "best_rank": row.best_rank,
        "rank_observed_at": _iso(row.latest_rank_at),
        "positions_gained": r.get("positions_gained"),
        "window_positions_gained": r.get("window_positions_gained"),
        "new_in_top": bool(r.get("new_in_top")),
        "observed_viewers": row.latest_viewers,
        "observed_streams": row.latest_streams,
        "sample_status": row.latest_sample_status,
        "viewers_observed_at": _iso(row.latest_viewers_at),
        "viewers_pct_per_hour": v.get("pct_per_hour"),
        "viewer_comparison": v.get("comparison"),
        "attention_state": row.attention_state,
        "attention_score": round(row.attention_score, 2),
        "attention_version": row.attention_version,
        "labels": labels,
        "entity_id": row.entity_id,
        "promoted_at": _iso(row.promoted_at),
        "first_seen_at": _iso(row.first_seen_at),
        "last_seen_at": _iso(row.last_seen_at),
        "freshness_minutes": round((now - row.last_seen_at).total_seconds() / 60, 1),
        "expires_at": _iso(row.expires_at),
        "signal_class": SIGNAL_CLASS,
        "evidence_class": "TWITCH_ACTIVITY",
    }


def stream_view(row: TwitchStreamObservationRow) -> dict[str, Any]:
    return {
        "stream_id": row.stream_id,
        "user_login": row.user_login,
        "user_name": row.user_name,
        "game_id": row.game_id,
        "game_name": row.game_name,
        "title": row.title,
        "language": row.language,
        "started_at": _iso(row.started_at),
        "viewer_count": row.viewer_count,
        "position": row.position,
        "observed_at": _iso(row.observed_at),
        "channel_url": f"https://www.twitch.tv/{row.user_login}",
        "evidence_class": "OBSERVED_VIEWERS",
    }


def format_candidates(keywords: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    templates = {k["template"] for k in keywords if k.get("template")}
    out = []
    for fmt, (tpls, needs) in FORMATS.items():
        hits = sorted(templates & set(tpls))
        out.append(
            {
                "format": fmt,
                "eligible": bool(hits),
                "grounded_by": hits,
                "requires": needs,
                "queries": [k["query"] for k in keywords if k.get("template") in tpls][:5],
            }
        )
    return out


class TwitchReadService:
    def __init__(
        self, factory: SessionFactory, settings: TwitchSettings, *, clock: Clock = utc_now
    ) -> None:
        self._factory = factory
        self._settings = settings
        self._clock = clock

    def summary(self) -> dict[str, Any]:
        now = self._clock()
        with transaction(self._factory) as s:
            stats = TwitchRepository(s).stats()
        fresh = {
            kind: {
                "snapshots": v["snapshots"],
                "last_observed_at": _iso(v["last_observed_at"]),
                "age_minutes": round((now - v["last_observed_at"]).total_seconds() / 60, 1)
                if v["last_observed_at"]
                else None,
            }
            for kind, v in stats.pop("snapshots").items()
        }
        stats["oldest_expiry"] = _iso(stats.get("oldest_expiry"))
        r = self._settings.retention
        return {
            **stats,
            "freshness": fresh,
            "retention": {
                "hours": r.hours,
                "extended_storage_authorized": r.extended_storage_authorized,
                "policy": f"Twitch data is cached for {POLICY_CACHE_HOURS} hours unless Twitch "
                "authorises longer storage in writing (Developer Services Agreement)",
            },
            "cadence": self._settings.cadence.model_dump(),
            "evidence_classes": EVIDENCE_CLASSES,
            "definitions": MEASUREMENT_DEFINITIONS,
            "signal_label": SIGNAL_LABEL,
            "caveat": NOT_SEARCH_DEMAND,
        }

    def top_games(
        self,
        *,
        limit: int = 50,
        max_rank: int | None = None,
        state: str | None = None,
        category: str | None = None,
        hours: int | None = None,
        name: str | None = None,
        order: str = "rank",
    ) -> dict[str, Any]:
        now = self._clock()
        with transaction(self._factory) as s:
            repo = TwitchRepository(s)
            latest = repo.latest_snapshot("top_games")
            prev = repo.previous_snapshot("top_games", latest.observed_at) if latest else None
            prev_ranks = repo.ranks_in(prev.id) if prev else {}
            current = repo.ranks_in(latest.id) if latest else {}
            rows, total = repo.games(
                state=state,
                category=category,
                max_rank=max_rank,
                seen_since=now - timedelta(hours=hours) if hours else None,
                name=name,
                limit=limit,
                order=order,
            )
            items = []
            for row in rows:
                view = game_view(row, now)
                in_latest = row.game_id in current
                view["in_latest_ranking"] = in_latest
                view["rank_change_since_previous_poll"] = (
                    prev_ranks[row.game_id] - current[row.game_id]
                    if in_latest and row.game_id in prev_ranks
                    else None
                )
                items.append(view)
        return {
            "total": total,
            "items": items,
            "snapshot": {
                "observed_at": _iso(latest.observed_at) if latest else None,
                "previous_observed_at": _iso(prev.observed_at) if prev else None,
                "games": latest.items if latest else 0,
                "sample_status": latest.sample_status if latest else None,
            },
            "caveat": NOT_SEARCH_DEMAND,
        }

    def streams(
        self, *, game_id: str | None = None, language: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        with transaction(self._factory) as s:
            repo = TwitchRepository(s)
            snap = repo.latest_snapshot("game_streams", game_id) if game_id else None
            if snap is None:
                snap = repo.latest_snapshot("top_streams")
            rows = (
                repo.streams_in(snap.id, game_id=game_id, language=language, limit=limit)
                if snap
                else []
            )
            items = [stream_view(r) for r in rows]
            meta = (
                {
                    "kind": snap.kind,
                    "observed_at": _iso(snap.observed_at),
                    "sample_status": snap.sample_status,
                    "status_reason": snap.status_reason,
                    "pages_fetched": snap.pages_fetched,
                    "pages_requested": snap.pages_requested,
                    "streams": snap.stream_count,
                    "viewer_sum": snap.viewer_sum,
                    "languages": list(snap.languages or ()),
                }
                if snap
                else None
            )
        shown_sum = sum(i["viewer_count"] for i in items)
        return {
            "items": items,
            "snapshot": meta,
            "shown_viewer_sum": shown_sum,
            "basis": "viewers of the streams listed; a partial sample is not a game total",
            "caveat": NOT_SEARCH_DEMAND,
        }

    def history(self, game_id: str, *, hours: int = 24) -> dict[str, Any] | None:
        now = self._clock()
        since = now - timedelta(hours=hours)
        with transaction(self._factory) as s:
            repo = TwitchRepository(s)
            row = repo.game(game_id)
            if row is None:
                return None
            ranks = repo.rank_history(game_id, since)
            viewers = repo.viewer_history(game_id, since)
            tops = repo.snapshot_times("top_games", since)
            return {
                "game_id": game_id,
                "name": row.name,
                "window_hours": hours,
                "ranks": [_rank_point(r) for r in ranks],
                "top_games_polls": len(tops),
                "polls_outside_top_n": len(tops) - len({r.observed_at for r in ranks}),
                "viewers": [_viewer_point(v) for v in viewers],
                "note": "Polls where the game was outside the polled top-N have no rank "
                "(position unknown, not zero viewers).",
            }

    def game_detail(self, game_id: str, *, hours: int = 24) -> dict[str, Any] | None:
        now = self._clock()
        hist = self.history(game_id, hours=hours)
        if hist is None:
            return None
        with transaction(self._factory) as s:
            repo = TwitchRepository(s)
            row = repo.game(game_id)
            assert row is not None
            view = game_view(row, now)
            entity = repo.entity(row.entity_id)
            keywords = repo.keywords_for(row.entity_id)
            around = row.latest_viewers_at or row.latest_rank_at or now
            explanations = repo.official_explanations(row.entity_id, around=around)
            phase = _phase_links(s, [k["query_id"] for k in keywords])
            monitored = next((m for m in repo.monitored() if m.game_id == game_id), None)
            direct = direct_interest_available(s)
        view.update(
            {
                "attention": row.attention,
                "history": hist,
                "monitoring": {
                    "tier": monitored.tier,
                    "origin": monitored.origin,
                    "next_poll_at": _iso(monitored.next_poll_at),
                    "last_polled_at": _iso(monitored.last_polled_at),
                }
                if monitored
                else None,
                "entity": {
                    "id": entity.id,
                    "name": entity.canonical_name,
                    "type": entity.entity_type.value,
                    "status": entity.status.value,
                    "evergreen": entity.evergreen,
                }
                if entity
                else None,
                "possible_explanations": explanations,
                "keyword_hypotheses": keywords,
                "format_candidates": format_candidates(keywords),
                **phase,
                "direct_youtube_search_interest": {
                    "evidence_class": "DIRECT_YOUTUBE_SEARCH_INTEREST",
                    "available": direct,
                    "detail": "not measured: no live-verified direct YouTube Search provider "
                    "has data"
                    if not direct
                    else "see the trends page for monitored queries; Twitch activity is "
                    "never used as this signal",
                },
                "caveat": NOT_SEARCH_DEMAND,
            }
        )
        return view

    def trending(self, *, limit: int = 20) -> dict[str, Any]:
        now = self._clock()
        with transaction(self._factory) as s:
            repo = TwitchRepository(s)
            rows, _ = repo.games(category="game", limit=500, order="attention")
            measured = [r for r in rows if r.attention_state not in (
                "insufficient_observations", "incomparable_samples"
            )]  # fmt: skip
            rising = [r for r in measured if r.attention_state in RISING][:limit]
            items = [game_view(r, now) for r in rising]
        return {
            "items": items,
            "measured": len(measured),
            "awaiting_comparable_observations": len(rows) - len(measured),
            "basis": "ranking and observed-viewer changes between comparable stored "
            "observations (twitch-attention-v1)",
            "caveat": NOT_SEARCH_DEMAND,
        }

    def opportunities(self, *, limit: int = 20) -> dict[str, Any]:
        now = self._clock()
        out = []
        with transaction(self._factory) as s:
            repo = TwitchRepository(s)
            rows = s.execute(
                sa.select(TwitchGameRow)
                .where(
                    sa.or_(
                        TwitchGameRow.attention_state.in_(RISING),
                        TwitchGameRow.promoted_at.is_not(None),
                    ),
                    TwitchGameRow.category_kind == "game",
                )
                .order_by(TwitchGameRow.attention_score.desc())
                .limit(limit)
            ).scalars()
            for row in rows:
                keywords = repo.keywords_for(row.entity_id)
                entity = repo.entity(row.entity_id)
                out.append(
                    {
                        **game_view(row, now),
                        "entity": {
                            "id": entity.id,
                            "name": entity.canonical_name,
                            "status": entity.status.value,
                            "evergreen": entity.evergreen,
                        }
                        if entity
                        else None,
                        "keyword_hypotheses": keywords[:8],
                        "format_candidates": [
                            f for f in format_candidates(keywords) if f["eligible"]
                        ],
                        **_phase_links(s, [k["query_id"] for k in keywords]),
                        "evidence_class": "PROXY_OPPORTUNITY",
                    }
                )
        return {"items": out, "caveat": NOT_SEARCH_DEMAND}


def _rank_point(r: TwitchRankObservationRow) -> dict[str, Any]:
    return {"observed_at": r.observed_at.isoformat(), "rank": r.rank}


def _viewer_point(v: TwitchViewerObservationRow) -> dict[str, Any]:
    return {
        "observed_at": v.observed_at.isoformat(),
        "viewers_observed": v.viewers_observed,
        "streams_observed": v.streams_observed,
        "top_stream_viewers": v.top_stream_viewers,
        "sample_status": v.sample_status,
        "sample_basis": v.sample_basis,
        "sample_cap": v.sample_cap,
        "languages": list(v.languages or ()),
        "evidence_class": "OBSERVED_VIEWERS",
    }


def _phase_links(session: Session, query_ids: Sequence[int]) -> dict[str, Any]:
    """Phase 5 gaps/opportunities, Phase 7 recommendations and Phase 6 forecasts that exist
    for the game's keyword hypotheses (read-only; each keeps its own classification)."""
    qids = sorted({q for q in query_ids if q is not None})
    empty: dict[str, Any] = {
        "content_gaps": [],
        "content_opportunities": [],
        "recommendations": [],
        "forecasts": [],
    }
    if not qids:
        return empty
    gaps = session.execute(
        sa.select(ContentGapRow)
        .where(ContentGapRow.query_id.in_(qids), ContentGapRow.is_synthetic.is_(False))
        .order_by(ContentGapRow.created_at.desc())
        .limit(10)
    ).scalars()
    opps = list(
        session.execute(
            sa.select(ContentOpportunityRow)
            .where(
                ContentOpportunityRow.query_id.in_(qids),
                ContentOpportunityRow.is_synthetic.is_(False),
            )
            .order_by(ContentOpportunityRow.created_at.desc())
            .limit(10)
        ).scalars()
    )
    recs: Sequence[VideoRecommendationRow] = (
        session.execute(
            sa.select(VideoRecommendationRow)
            .where(VideoRecommendationRow.opportunity_row_id.in_([o.id for o in opps]))
            .order_by(VideoRecommendationRow.composite_score.desc().nulls_last())
            .limit(10)
        )
        .scalars()
        .all()
        if opps
        else []
    )
    fc = session.execute(
        sa.select(ForecastPredictionRow)
        .where(
            ForecastPredictionRow.query_id.in_(qids), ForecastPredictionRow.is_synthetic.is_(False)
        )
        .order_by(ForecastPredictionRow.prediction_timestamp.desc())
        .limit(10)
    ).scalars()
    return {
        "content_gaps": [
            {
                "gap_type": g.gap_type,
                "evidence_strength": g.evidence_strength,
                "explanation": g.explanation,
                "query_id": g.query_id,
                "created_at": _iso(g.created_at),
            }
            for g in gaps
        ],
        "content_opportunities": [
            {
                "query": o.canonical_query,
                "score": o.score,
                "status": o.status,
                "demand_basis": o.demand_basis,
                "evidence_quality": o.evidence_quality,
                "created_at": _iso(o.created_at),
            }
            for o in opps
        ],
        "recommendations": [
            {
                "id": str(r.id),
                "title": r.title,
                "query": r.query,
                "video_format": r.video_format,
                "status": r.status,
                "demand_basis": r.demand_basis,
                "composite_score": r.composite_score,
            }
            for r in recs
        ],
        "forecasts": [
            {
                "query_id": f.query_id,
                "target_family": f.target_family,
                "source_classification": f.source_classification,
                "forecast_status": f.forecast_status,
                "horizon_hours": f.horizon_hours,
                "prediction_timestamp": _iso(f.prediction_timestamp),
                "basis": "Phase 6 forecast of the keyword's own series (its classification "
                "shown); Twitch data is not a forecast input",
            }
            for f in fc
        ],
    }
