"""Persistence for Twitch snapshots, rankings, observed viewers, streams and game state.

Writes are idempotent: game rows are keyed by Twitch's game ID, observation rows by
(snapshot, game) or (snapshot, stream). Every row holding Twitch data expires (default 24
hours) and :meth:`TwitchRepository.purge_expired` deletes it, Twitch-sourced discovery items
included.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from trend_agent.db.discovery_models import (
    DiscoveredEntityRow,
    DiscoveredKeywordRow,
    DiscoveryItemRow,
    EntityMentionRow,
)
from trend_agent.db.models import SearchQueryRow
from trend_agent.db.twitch_models import (
    TwitchGameRow,
    TwitchMonitoredGameRow,
    TwitchRankObservationRow,
    TwitchSnapshotRow,
    TwitchStreamObservationRow,
    TwitchViewerObservationRow,
)
from trend_agent.discovery.models import EntityStatus, EntityType, EvidenceType
from trend_agent.twitch.client import Collection
from trend_agent.twitch.models import PROVIDER_NAME, TwitchGame, TwitchStream, category_kind
from trend_agent.verification.untrusted import injection_signals, sanitize


class TwitchRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    # ------------------------------------------------------------------ snapshots
    def add_snapshot(
        self,
        *,
        kind: str,
        game_id: str | None,
        languages: Sequence[str],
        observed_at: datetime,
        completed_at: datetime,
        collection: Collection,
        items: int,
        viewer_sum: int | None,
        stream_count: int | None,
        requests: int,
        rate_limit_remaining: int | None,
        trigger: str,
        expires_at: datetime | None,
        is_synthetic: bool = False,
    ) -> int:
        row = TwitchSnapshotRow(
            kind=kind,
            game_id=game_id,
            languages=list(languages),
            observed_at=observed_at,
            completed_at=completed_at,
            pages_requested=collection.pages_requested,
            pages_fetched=collection.pages_fetched,
            page_size=collection.page_size,
            items=items,
            duplicates=collection.duplicates,
            skipped=dict(collection.skipped),
            sample_status=collection.status,
            status_reason=collection.reason[:120],
            viewer_sum=viewer_sum,
            stream_count=stream_count,
            requests=requests,
            rate_limit_remaining=rate_limit_remaining,
            trigger=trigger,
            is_synthetic=is_synthetic,
            expires_at=expires_at,
        )
        self._s.add(row)
        self._s.flush()
        return int(row.id)

    # ------------------------------------------------------------------ games
    def upsert_game(
        self,
        game_id: str,
        name: str,
        *,
        igdb_id: str | None,
        box_art_url: str | None,
        seen_at: datetime,
        expires_at: datetime | None,
        non_games: Iterable[str],
        is_synthetic: bool = False,
    ) -> None:
        kind = category_kind(name, igdb_id, non_games)
        t = TwitchGameRow.__table__
        stmt = insert(TwitchGameRow).values(
            game_id=game_id,
            name=name[:300],
            igdb_id=igdb_id,
            box_art_url=box_art_url,
            category_kind=kind,
            first_seen_at=seen_at,
            last_seen_at=seen_at,
            is_synthetic=is_synthetic,
            expires_at=expires_at,
        )
        ex = stmt.excluded
        self._s.execute(
            stmt.on_conflict_do_update(
                index_elements=["game_id"],
                set_={
                    # games can be renamed; the newest record wins
                    "name": sa.case((ex.last_seen_at >= t.c.last_seen_at, ex.name), else_=t.c.name),
                    "igdb_id": sa.func.coalesce(ex.igdb_id, t.c.igdb_id),
                    "box_art_url": sa.func.coalesce(ex.box_art_url, t.c.box_art_url),
                    "category_kind": sa.case(
                        (ex.category_kind == "unknown", t.c.category_kind),
                        else_=ex.category_kind,
                    ),
                    "last_seen_at": sa.func.greatest(t.c.last_seen_at, ex.last_seen_at),
                    "expires_at": sa.func.greatest(t.c.expires_at, ex.expires_at),
                    "updated_at": sa.func.now(),
                },
            )
        )

    def upsert_games(
        self,
        games: Iterable[TwitchGame],
        *,
        seen_at: datetime,
        expires_at: datetime | None,
        non_games: Iterable[str],
        is_synthetic: bool = False,
    ) -> None:
        excluded = tuple(non_games)
        for g in games:
            self.upsert_game(
                g.id,
                g.name,
                igdb_id=g.igdb_id,
                box_art_url=g.box_art_url,
                seen_at=seen_at,
                expires_at=expires_at,
                non_games=excluded,
                is_synthetic=is_synthetic,
            )

    def game(self, game_id: str) -> TwitchGameRow | None:
        return self._s.get(TwitchGameRow, game_id)

    def game_by_name(self, name: str) -> TwitchGameRow | None:
        return (
            self._s.execute(
                sa.select(TwitchGameRow)
                .where(sa.func.lower(TwitchGameRow.name) == name.strip().lower())
                .order_by(TwitchGameRow.last_seen_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )

    # ------------------------------------------------------------------ observations
    def add_ranks(
        self,
        snapshot_id: int,
        ranked: Sequence[tuple[int, TwitchGame]],
        *,
        observed_at: datetime,
        expires_at: datetime | None,
        is_synthetic: bool = False,
    ) -> int:
        if not ranked:
            return 0
        rows = [
            {
                "snapshot_id": snapshot_id,
                "game_id": g.id,
                "rank": rank,
                "observed_at": observed_at,
                "is_synthetic": is_synthetic,
                "expires_at": expires_at,
            }
            for rank, g in ranked
        ]
        res = self._s.execute(
            insert(TwitchRankObservationRow)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["snapshot_id", "game_id"])
            .returning(TwitchRankObservationRow.id)
        )
        inserted = len(res.all())
        for rank, g in ranked:
            self._s.execute(
                sa.update(TwitchGameRow)
                .where(TwitchGameRow.game_id == g.id)
                .where(
                    sa.or_(
                        TwitchGameRow.latest_rank_at.is_(None),
                        TwitchGameRow.latest_rank_at <= observed_at,
                    )
                )
                .values(
                    latest_rank=rank,
                    latest_rank_at=observed_at,
                    best_rank=sa.func.least(sa.func.coalesce(TwitchGameRow.best_rank, rank), rank),
                )
            )
        return inserted

    def add_streams(
        self,
        snapshot_id: int,
        streams: Sequence[TwitchStream],
        *,
        observed_at: datetime,
        expires_at: datetime | None,
        title_chars: int,
        is_synthetic: bool = False,
    ) -> int:
        if not streams:
            return 0
        rows = []
        for pos, st in enumerate(streams, start=1):
            title = st.title or ""
            # stream titles are untrusted user text: sanitised, bounded, and withheld when
            # they look like instructions; they are displayed, never acted on
            clean = None if injection_signals(title) else sanitize(title, title_chars) or None
            rows.append(
                {
                    "snapshot_id": snapshot_id,
                    "stream_id": st.id,
                    "user_id": st.user_id,
                    "user_login": st.user_login[:64],
                    "user_name": (st.user_name or "")[:100] or None,
                    "game_id": st.game_id,
                    "game_name": (st.game_name or "")[:300] or None,
                    "title": clean,
                    "language": (st.language or "")[:8] or None,
                    "started_at": st.started_at,
                    "viewer_count": st.viewer_count,
                    "position": pos,
                    "observed_at": observed_at,
                    "is_synthetic": is_synthetic,
                    "expires_at": expires_at,
                }
            )
        res = self._s.execute(
            insert(TwitchStreamObservationRow)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["snapshot_id", "stream_id"])
            .returning(TwitchStreamObservationRow.id)
        )
        return len(res.all())

    def add_viewers(
        self,
        snapshot_id: int,
        per_game: Sequence[dict[str, Any]],
        *,
        observed_at: datetime,
        expires_at: datetime | None,
        is_synthetic: bool = False,
    ) -> int:
        """One observed-viewer row per game *covered by this snapshot* (never zeros for
        games the snapshot did not reach)."""
        if not per_game:
            return 0
        rows = [
            {
                "snapshot_id": snapshot_id,
                "game_id": g["game_id"],
                "observed_at": observed_at,
                "streams_observed": g["streams"],
                "viewers_observed": g["viewers"],
                "top_stream_viewers": g.get("top_stream_viewers"),
                "sample_status": g["status"],
                "sample_basis": g["basis"],
                "languages": list(g.get("languages") or ()),
                "sample_cap": g["cap"],
                "is_synthetic": is_synthetic,
                "expires_at": expires_at,
            }
            for g in per_game
        ]
        res = self._s.execute(
            insert(TwitchViewerObservationRow)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["snapshot_id", "game_id"])
            .returning(TwitchViewerObservationRow.id)
        )
        for g in per_game:
            # the game page shows its own-stream sample when it has one
            if g["basis"] != "game_streams":
                continue
            self._s.execute(
                sa.update(TwitchGameRow)
                .where(TwitchGameRow.game_id == g["game_id"])
                .where(
                    sa.or_(
                        TwitchGameRow.latest_viewers_at.is_(None),
                        TwitchGameRow.latest_viewers_at <= observed_at,
                    )
                )
                .values(
                    latest_viewers=g["viewers"],
                    latest_streams=g["streams"],
                    latest_sample_status=g["status"],
                    latest_viewers_at=observed_at,
                )
            )
        return len(res.all())

    # ------------------------------------------------------------------ history
    def rank_history(self, game_id: str, since: datetime) -> list[TwitchRankObservationRow]:
        return list(
            self._s.execute(
                sa.select(TwitchRankObservationRow)
                .where(
                    TwitchRankObservationRow.game_id == game_id,
                    TwitchRankObservationRow.observed_at >= since,
                )
                .order_by(TwitchRankObservationRow.observed_at)
            ).scalars()
        )

    def viewer_history(self, game_id: str, since: datetime) -> list[TwitchViewerObservationRow]:
        return list(
            self._s.execute(
                sa.select(TwitchViewerObservationRow)
                .where(
                    TwitchViewerObservationRow.game_id == game_id,
                    TwitchViewerObservationRow.observed_at >= since,
                )
                .order_by(TwitchViewerObservationRow.observed_at)
            ).scalars()
        )

    def snapshot_times(
        self, kind: str, since: datetime, game_id: str | None = None
    ) -> list[datetime]:
        q = sa.select(TwitchSnapshotRow.observed_at).where(
            TwitchSnapshotRow.kind == kind, TwitchSnapshotRow.observed_at >= since
        )
        if game_id is not None:
            q = q.where(TwitchSnapshotRow.game_id == game_id)
        return list(self._s.execute(q.order_by(TwitchSnapshotRow.observed_at)).scalars())

    def latest_snapshot(self, kind: str, game_id: str | None = None) -> TwitchSnapshotRow | None:
        q = sa.select(TwitchSnapshotRow).where(TwitchSnapshotRow.kind == kind)
        if game_id is not None:
            q = q.where(TwitchSnapshotRow.game_id == game_id)
        return (
            self._s.execute(q.order_by(TwitchSnapshotRow.observed_at.desc()).limit(1))
            .scalars()
            .first()
        )

    def ranks_in(self, snapshot_id: int) -> dict[str, int]:
        rows = self._s.execute(
            sa.select(TwitchRankObservationRow.game_id, TwitchRankObservationRow.rank).where(
                TwitchRankObservationRow.snapshot_id == snapshot_id
            )
        ).all()
        return {str(gid): int(rank) for gid, rank in rows}

    def previous_snapshot(self, kind: str, before: datetime) -> TwitchSnapshotRow | None:
        return (
            self._s.execute(
                sa.select(TwitchSnapshotRow)
                .where(TwitchSnapshotRow.kind == kind, TwitchSnapshotRow.observed_at < before)
                .order_by(TwitchSnapshotRow.observed_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )

    def streams_in(
        self,
        snapshot_id: int,
        *,
        game_id: str | None = None,
        language: str | None = None,
        limit: int = 100,
    ) -> list[TwitchStreamObservationRow]:
        q = sa.select(TwitchStreamObservationRow).where(
            TwitchStreamObservationRow.snapshot_id == snapshot_id
        )
        if game_id is not None:
            q = q.where(TwitchStreamObservationRow.game_id == game_id)
        if language is not None:
            q = q.where(TwitchStreamObservationRow.language == language)
        return list(
            self._s.execute(q.order_by(TwitchStreamObservationRow.position).limit(limit)).scalars()
        )

    def games(
        self,
        *,
        state: str | None = None,
        category: str | None = None,
        max_rank: int | None = None,
        seen_since: datetime | None = None,
        name: str | None = None,
        limit: int = 100,
        offset: int = 0,
        order: str = "rank",
    ) -> tuple[list[TwitchGameRow], int]:
        q = sa.select(TwitchGameRow).where(TwitchGameRow.is_synthetic.is_(False))
        if state is not None:
            q = q.where(TwitchGameRow.attention_state == state)
        if category is not None:
            q = q.where(TwitchGameRow.category_kind == category)
        if max_rank is not None:
            q = q.where(TwitchGameRow.latest_rank <= max_rank)
        if seen_since is not None:
            q = q.where(TwitchGameRow.last_seen_at >= seen_since)
        if name:
            q = q.where(TwitchGameRow.name.ilike(f"%{name.strip()[:80]}%"))
        total = int(
            self._s.execute(sa.select(sa.func.count()).select_from(q.subquery())).scalar_one()
        )
        key: Any = {
            "rank": TwitchGameRow.latest_rank.asc().nulls_last(),
            "attention": TwitchGameRow.attention_score.desc(),
            "viewers": TwitchGameRow.latest_viewers.desc().nulls_last(),
            "name": TwitchGameRow.name.asc(),
        }[order]
        rows = (
            self._s.execute(q.order_by(key, TwitchGameRow.game_id).limit(limit).offset(offset))
            .scalars()
            .all()
        )
        return list(rows), total

    def set_attention(
        self, game_id: str, *, result: dict[str, Any], state: str, score: float, version: str
    ) -> None:
        self._s.execute(
            sa.update(TwitchGameRow)
            .where(TwitchGameRow.game_id == game_id)
            .values(
                attention=result,
                attention_state=state,
                attention_score=score,
                attention_version=version,
                updated_at=sa.func.now(),
            )
        )

    # ------------------------------------------------------------------ monitoring list
    def monitored(self) -> list[TwitchMonitoredGameRow]:
        return list(
            self._s.execute(
                sa.select(TwitchMonitoredGameRow).order_by(
                    TwitchMonitoredGameRow.tier, TwitchMonitoredGameRow.game_id
                )
            ).scalars()
        )

    def set_monitored(
        self,
        game_id: str,
        *,
        tier: str,
        origin: str,
        requested_name: str | None,
        reason: str | None,
        now: datetime,
        expires_at: datetime | None,
    ) -> None:
        t = TwitchMonitoredGameRow.__table__
        stmt = insert(TwitchMonitoredGameRow).values(
            game_id=game_id,
            requested_name=requested_name,
            tier=tier,
            origin=origin,
            reason=reason,
            next_poll_at=now,
            expires_at=expires_at,
        )
        ex = stmt.excluded
        self._s.execute(
            stmt.on_conflict_do_update(
                index_elements=["game_id"],
                set_={
                    # an operator's or configured selection is never downgraded by automation
                    "tier": sa.case((t.c.origin == "auto", ex.tier), else_=t.c.tier),
                    "origin": sa.case((t.c.origin == "auto", ex.origin), else_=t.c.origin),
                    "reason": sa.case((t.c.origin == "auto", ex.reason), else_=t.c.reason),
                    "requested_name": sa.func.coalesce(ex.requested_name, t.c.requested_name),
                    "expires_at": sa.case(
                        (t.c.origin == "auto", ex.expires_at), else_=t.c.expires_at
                    ),
                },
            )
        )

    def remove_monitored(self, game_id: str) -> bool:
        res = self._s.execute(
            sa.delete(TwitchMonitoredGameRow).where(TwitchMonitoredGameRow.game_id == game_id)
        )
        return bool(getattr(res, "rowcount", 0))

    def due_monitored(self, now: datetime, limit: int) -> list[TwitchMonitoredGameRow]:
        return list(
            self._s.execute(
                sa.select(TwitchMonitoredGameRow)
                .where(
                    sa.or_(
                        TwitchMonitoredGameRow.next_poll_at.is_(None),
                        TwitchMonitoredGameRow.next_poll_at <= now,
                    ),
                    sa.or_(
                        TwitchMonitoredGameRow.expires_at.is_(None),
                        TwitchMonitoredGameRow.expires_at > now,
                    ),
                )
                .order_by(
                    (TwitchMonitoredGameRow.tier == "high").desc(),
                    TwitchMonitoredGameRow.next_poll_at.asc().nulls_first(),
                )
                .limit(limit)
            ).scalars()
        )

    def mark_polled(self, game_id: str, *, now: datetime, next_at: datetime) -> None:
        self._s.execute(
            sa.update(TwitchMonitoredGameRow)
            .where(TwitchMonitoredGameRow.game_id == game_id)
            .values(last_polled_at=now, next_poll_at=next_at)
        )

    # ------------------------------------------------------------------ discovery links
    def link_promoted(self, game_id: str, *, now: datetime) -> int | None:
        """After promotion: attach the discovery item and the entity its catalog name
        verified (the source-hint mention)."""
        item = (
            self._s.execute(
                sa.select(DiscoveryItemRow)
                .where(
                    DiscoveryItemRow.provider_name == PROVIDER_NAME,
                    DiscoveryItemRow.source_reference == f"twitch:game:{game_id}",
                )
                .order_by(DiscoveryItemRow.collected_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if item is None:
            return None
        entity_id = self._s.execute(
            sa.select(EntityMentionRow.entity_id)
            .where(
                EntityMentionRow.item_id == item.id,
                EntityMentionRow.status != EntityStatus.REJECTED,
            )
            .order_by((EntityMentionRow.extractor == "source_hint").desc(), EntityMentionRow.id)
            .limit(1)
        ).scalar_one_or_none()
        self._s.execute(
            sa.update(TwitchGameRow)
            .where(TwitchGameRow.game_id == game_id)
            .values(item_id=item.id, entity_id=entity_id, promoted_at=now)
        )
        return entity_id

    def link_existing_entities(self) -> int:
        """Resolve Twitch game titles against existing *game* entities by exact
        (case-insensitive) name. Conservative: no fuzzy matching, games only, never
        rejected entities; promotion's catalog evidence links the rest."""
        e = DiscoveredEntityRow
        match = (
            sa.select(e.id)
            .where(
                sa.func.lower(e.canonical_name) == sa.func.lower(TwitchGameRow.name),
                e.entity_type == EntityType.GAME,
                e.status != EntityStatus.REJECTED,
                e.is_synthetic.is_(False),
            )
            .order_by(e.id)
            .limit(1)
            .scalar_subquery()
        )
        res = self._s.execute(
            sa.update(TwitchGameRow)
            .where(
                TwitchGameRow.entity_id.is_(None),
                TwitchGameRow.category_kind == "game",
                match.is_not(None),
            )
            .values(entity_id=match)
            .execution_options(synchronize_session=False)
        )
        return int(getattr(res, "rowcount", 0) or 0)

    def entity(self, entity_id: int | None) -> DiscoveredEntityRow | None:
        return self._s.get(DiscoveredEntityRow, entity_id) if entity_id else None

    def keywords_for(self, entity_id: int | None) -> list[dict[str, Any]]:
        if entity_id is None:
            return []
        rows = self._s.execute(
            sa.select(DiscoveredKeywordRow, SearchQueryRow.canonical_query)
            .join(SearchQueryRow, SearchQueryRow.id == DiscoveredKeywordRow.query_id)
            .where(DiscoveredKeywordRow.entity_id == entity_id)
            .order_by(DiscoveredKeywordRow.priority_score.desc())
            .limit(25)
        ).all()
        return [
            {
                "keyword_id": k.id,
                "query_id": k.query_id,
                "query": q,
                "origin": k.origin.value,
                "method": k.discovery_type.value,
                "template": k.template_id,
                "intent": k.primary_intent.value,
                "status": k.candidate_status.value,
                "priority": round(k.priority_score, 2),
                "evidence_class": "PROXY_OPPORTUNITY",
            }
            for k, q in rows
        ]

    def official_explanations(
        self, entity_id: int | None, *, around: datetime, days_before: int = 7
    ) -> list[dict[str, Any]]:
        """Official announcements / releases mentioning the game near the attention change.
        A temporal co-occurrence only: never presented as the cause."""
        if entity_id is None:
            return []
        rows = self._s.execute(
            sa.select(DiscoveryItemRow)
            .join(EntityMentionRow, EntityMentionRow.item_id == DiscoveryItemRow.id)
            .where(
                EntityMentionRow.entity_id == entity_id,
                EntityMentionRow.status != EntityStatus.REJECTED,
                DiscoveryItemRow.evidence_type.in_(
                    (EvidenceType.OFFICIAL_ANNOUNCEMENT, EvidenceType.CODE_RELEASE)
                ),
                DiscoveryItemRow.is_synthetic.is_(False),
                sa.func.coalesce(DiscoveryItemRow.published_at, DiscoveryItemRow.collected_at)
                >= around - timedelta(days=days_before),
                sa.func.coalesce(DiscoveryItemRow.published_at, DiscoveryItemRow.collected_at)
                <= around + timedelta(days=1),
            )
            .order_by(DiscoveryItemRow.published_at.desc().nulls_last())
            .limit(10)
        ).scalars()
        return [
            {
                "title": r.title,
                "url": r.url,
                "publisher": r.publisher,
                "published_at": r.published_at.isoformat() if r.published_at else None,
                "evidence_type": r.evidence_type.value,
                "relationship": "temporal co-occurrence: a possible explanation; causation "
                "is not established",
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ stats / retention
    def stats(self) -> dict[str, Any]:
        def one(q: Any) -> Any:
            return self._s.execute(q).scalar_one()

        by_kind: dict[str, Any] = defaultdict(dict)
        for kind, n, last in self._s.execute(
            sa.select(
                TwitchSnapshotRow.kind,
                sa.func.count(),
                sa.func.max(TwitchSnapshotRow.observed_at),
            ).group_by(TwitchSnapshotRow.kind)
        ).all():
            by_kind[kind] = {"snapshots": int(n), "last_observed_at": last}
        status_counts: dict[str, int] = {
            str(k): int(v)
            for k, v in self._s.execute(
                sa.select(TwitchSnapshotRow.sample_status, sa.func.count()).group_by(
                    TwitchSnapshotRow.sample_status
                )
            ).all()
        }
        return {
            "games": int(one(sa.select(sa.func.count()).select_from(TwitchGameRow))),
            "games_real": int(
                one(
                    sa.select(sa.func.count())
                    .select_from(TwitchGameRow)
                    .where(TwitchGameRow.category_kind == "game")
                )
            ),
            "rising_games": int(
                one(
                    sa.select(sa.func.count())
                    .select_from(TwitchGameRow)
                    .where(TwitchGameRow.attention_state.in_(("rising", "sustained_rise")))
                )
            ),
            "monitored_games": int(
                one(sa.select(sa.func.count()).select_from(TwitchMonitoredGameRow))
            ),
            "rank_observations": int(
                one(sa.select(sa.func.count()).select_from(TwitchRankObservationRow))
            ),
            "viewer_observations": int(
                one(sa.select(sa.func.count()).select_from(TwitchViewerObservationRow))
            ),
            "stream_observations": int(
                one(sa.select(sa.func.count()).select_from(TwitchStreamObservationRow))
            ),
            "snapshots": {k: dict(v) for k, v in by_kind.items()},
            "sample_status": {str(k): int(v) for k, v in status_counts.items()},
            "oldest_expiry": one(sa.select(sa.func.min(TwitchSnapshotRow.expires_at))),
        }

    _EXPIRING: Sequence[tuple[str, Any]] = (
        ("twitch_stream_observations", TwitchStreamObservationRow),
        ("twitch_viewer_observations", TwitchViewerObservationRow),
        ("twitch_rank_observations", TwitchRankObservationRow),
        ("twitch_snapshots", TwitchSnapshotRow),
        ("twitch_games", TwitchGameRow),
        ("twitch_monitored_games", TwitchMonitoredGameRow),  # auto-selections only
    )

    def count_expired(self, now: datetime) -> dict[str, int]:
        out = {
            name: int(
                self._s.execute(
                    sa.select(sa.func.count()).select_from(model).where(model.expires_at <= now)
                ).scalar_one()
            )
            for name, model in self._EXPIRING
        }
        out["twitch_discovery_items"] = int(
            self._s.execute(
                sa.select(sa.func.count())
                .select_from(DiscoveryItemRow)
                .where(
                    DiscoveryItemRow.provider_name == PROVIDER_NAME,
                    DiscoveryItemRow.expires_at <= now,
                )
            ).scalar_one()
        )
        return out

    def purge_expired(self, now: datetime) -> dict[str, int]:
        out: dict[str, int] = {}
        for name, model in self._EXPIRING:
            res = self._s.execute(sa.delete(model).where(model.expires_at <= now))
            out[name] = int(getattr(res, "rowcount", 0) or 0)
        # Twitch-sourced discovery items (game catalog records) follow the same cache limit;
        # their entity mentions and evidence links cascade. Entities and keyword hypotheses
        # (a game title and generated phrases) are not Twitch data and stay.
        res = self._s.execute(
            sa.delete(DiscoveryItemRow).where(
                DiscoveryItemRow.provider_name == PROVIDER_NAME,
                DiscoveryItemRow.expires_at <= now,
            )
        )
        out["twitch_discovery_items"] = int(getattr(res, "rowcount", 0) or 0)
        return out
