"""Persistence for Hacker News stories and their repeated activity observations.

Every write is idempotent: stories are keyed by the Hacker News item ID, observations by
(story, collection time).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from trend_agent.db.discovery_models import (
    DiscoveredEntityRow,
    DiscoveredKeywordRow,
    DiscoveryEvidenceRow,
    DiscoveryItemRow,
    EntityMentionRow,
)
from trend_agent.db.hackernews_models import HNObservationRow, HNStoryRow
from trend_agent.db.models import SearchQueryRow
from trend_agent.discovery.models import EntityStatus
from trend_agent.discovery.text import canonical_url
from trend_agent.utils.time import parse_rfc3339

HN_PROVIDERS = ("hackernews", "hackernews_firebase")
ORDERS = ("published", "priority", "score", "comments", "momentum", "first_seen")


def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return parse_rfc3339(value)
    return None


class HackerNewsRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    # ------------------------------------------------------------------ writes
    def upsert_story(
        self,
        meta: dict[str, Any],
        *,
        title: str,
        source_key: str,
        run_id: uuid.UUID | None,
        item_id: int | None,
        provider_id: int | None,
        seen_at: datetime,
        expires_at: datetime | None,
        is_synthetic: bool = False,
    ) -> int:
        c = meta.get("classification") or {}
        published = _dt(meta.get("published_at")) or seen_at
        linked = meta.get("linked_url")
        values = {
            "story_id": int(meta["story_id"]),
            "title": title[:1000],
            "linked_url": linked,
            "canonical_link": canonical_url(linked) if linked else None,
            "discussion_url": meta["discussion_url"],
            "domain": c.get("domain"),
            "author": (meta.get("author") or None),
            "kind": c.get("kind") or "story",
            "subject": c.get("subject") or "discussion",
            "primary_category": c.get("primary_category") or "other",
            "secondary_category": c.get("secondary_category"),
            "ambiguous": bool(c.get("ambiguous")),
            "relevance": float(c.get("relevance") or 0.0),
            "classification": c,
            "lists": [meta["list"]] if meta.get("list") else [],
            "published_at": published,
            "latest_score": max(int(meta.get("score") or 0), 0),
            "latest_comments": max(int(meta.get("comments") or 0), 0),
            "last_observed_at": seen_at,
            "item_id": item_id,
            "provider_id": provider_id,
            "first_source_key": source_key,
            "first_run_id": run_id,
            "first_seen_at": seen_at,
            "last_seen_at": seen_at,
            "is_synthetic": is_synthetic,
            "expires_at": expires_at,
        }
        t = HNStoryRow.__table__
        stmt = insert(HNStoryRow).values(**values)
        ex = stmt.excluded
        newer = ex.last_seen_at >= t.c.last_seen_at
        stmt = stmt.on_conflict_do_update(
            index_elements=["story_id"],
            set_={
                # Titles and links can be edited on Hacker News: keep the latest record.
                "title": sa.case((newer, ex.title), else_=t.c.title),
                "linked_url": sa.case((newer, ex.linked_url), else_=t.c.linked_url),
                "canonical_link": sa.case((newer, ex.canonical_link), else_=t.c.canonical_link),
                "domain": sa.case((newer, ex.domain), else_=t.c.domain),
                "kind": ex.kind,
                "subject": ex.subject,
                "primary_category": ex.primary_category,
                "secondary_category": ex.secondary_category,
                "ambiguous": ex.ambiguous,
                "relevance": ex.relevance,
                "classification": ex.classification,
                "lists": sa.case(
                    (t.c.lists.contains(ex.lists), t.c.lists),
                    else_=sa.func.array_cat(t.c.lists, ex.lists),
                ),
                "latest_score": sa.case((newer, ex.latest_score), else_=t.c.latest_score),
                "latest_comments": sa.case((newer, ex.latest_comments), else_=t.c.latest_comments),
                "last_observed_at": sa.func.greatest(t.c.last_observed_at, ex.last_observed_at),
                "item_id": sa.func.coalesce(t.c.item_id, ex.item_id),
                "last_seen_at": sa.func.greatest(t.c.last_seen_at, ex.last_seen_at),
                "expires_at": sa.func.greatest(t.c.expires_at, ex.expires_at),
                "updated_at": sa.func.now(),
            },
        )
        self._s.execute(stmt)
        return int(meta["story_id"])

    def add_observation(
        self,
        story_id: int,
        *,
        observed_at: datetime,
        score: int | None,
        comments: int | None,
        origin: str,
        list_name: str | None = None,
        rank: int | None = None,
        source_key: str | None = None,
        run_id: uuid.UUID | None = None,
        item_id: int | None = None,
        expires_at: datetime | None = None,
        is_synthetic: bool = False,
    ) -> bool:
        stmt = (
            insert(HNObservationRow)
            .values(
                story_id=story_id,
                observed_at=observed_at,
                score=max(score, 0) if score is not None else None,
                comments=max(comments, 0) if comments is not None else None,
                list_name=list_name,
                rank=rank,
                origin=origin,
                source_key=source_key,
                run_id=run_id,
                item_id=item_id,
                is_synthetic=is_synthetic,
                expires_at=expires_at,
            )
            .on_conflict_do_nothing(index_elements=["story_id", "observed_at"])
            .returning(HNObservationRow.id)
        )
        return self._s.execute(stmt).scalar_one_or_none() is not None

    def record_refresh(
        self,
        story_id: int,
        *,
        observed_at: datetime,
        score: int | None,
        comments: int | None,
        title: str | None,
    ) -> None:
        """Latest activity from a refresh (only if newer than what is stored)."""
        row = self._s.get(HNStoryRow, story_id)
        if row is None or (row.last_observed_at and row.last_observed_at > observed_at):
            return
        if score is not None:
            row.latest_score = max(score, 0)
        if comments is not None:
            row.latest_comments = max(comments, 0)
        if title:
            row.title = title[:1000]
        row.last_observed_at = observed_at

    def set_assessment(
        self,
        story_id: int,
        *,
        priority: float,
        components: dict[str, Any],
        momentum: dict[str, Any],
        momentum_state: str,
        tier: str,
        monitoring_state: str,
        next_refresh_at: datetime | None,
    ) -> None:
        self._s.execute(
            sa.update(HNStoryRow)
            .where(HNStoryRow.story_id == story_id)
            .values(
                priority_score=priority,
                priority_components=components,
                momentum=momentum,
                momentum_state=momentum_state,
                refresh_tier=tier,
                monitoring_state=monitoring_state,
                next_refresh_at=next_refresh_at,
                updated_at=sa.func.now(),
            )
        )

    def deactivate(self, story_id: int, reason: str) -> None:
        row = self._s.get(HNStoryRow, story_id)
        if row is None:
            return
        row.monitoring_state = "inactive"
        row.next_refresh_at = None
        row.classification = {**(row.classification or {}), "monitoring_stopped": reason}

    def defer(self, story_id: int, until: datetime) -> None:
        self._s.execute(
            sa.update(HNStoryRow)
            .where(HNStoryRow.story_id == story_id)
            .values(next_refresh_at=until)
        )

    def deactivate_older_than(self, cutoff: datetime) -> int:
        res = self._s.execute(
            sa.update(HNStoryRow)
            .where(HNStoryRow.monitoring_state == "active", HNStoryRow.published_at < cutoff)
            .values(monitoring_state="inactive", next_refresh_at=None)
        )
        return int(getattr(res, "rowcount", 0) or 0)

    # Extractors trusted to name what a story is *about*: the Show/Launch HN project
    # (the title's own text) and curated gazetteer matches, the versioned form first
    # ("Rust 1.100" over "Rust"). Other generic matches need this much confidence.
    _ABOUT_EXTRACTORS = ("source_hint", "gazetteer_version", "gazetteer")
    _MIN_OTHER_CONFIDENCE = 0.6

    def link_entities(self, item_ids: Iterable[int]) -> int:
        """Attach the entity each story is *about*: named in its title by a trusted
        extractor (see ``_ABOUT_EXTRACTORS``). A story whose mentions are all weak generic
        matches, or only in its body text, is left without an entity."""
        ids = sorted(set(item_ids))
        if not ids:
            return 0
        m = EntityMentionRow
        rank = sa.case(
            *((m.extractor == name, pos) for pos, name in enumerate(self._ABOUT_EXTRACTORS)),
            else_=len(self._ABOUT_EXTRACTORS),
        )
        item = DiscoveryItemRow
        best = (
            sa.select(m.item_id, m.entity_id)
            .join(item, item.id == m.item_id)
            .where(
                m.item_id.in_(ids),
                m.status != EntityStatus.REJECTED,
                sa.or_(
                    m.extractor.in_(self._ABOUT_EXTRACTORS),
                    m.confidence >= self._MIN_OTHER_CONFIDENCE,
                ),
                # named in the title: a name that only occurs in the body (e.g. the tool
                # used to build the thing) is not what the story is about
                sa.func.strpos(sa.func.lower(item.title), sa.func.lower(m.matched_text)) > 0,
            )
            .order_by(m.item_id, rank, m.confidence.desc(), m.id)
            .distinct(m.item_id)
            .subquery()
        )
        res = self._s.execute(
            sa.update(HNStoryRow)
            .where(
                HNStoryRow.item_id == best.c.item_id,
                HNStoryRow.entity_id.is_distinct_from(best.c.entity_id),
            )
            .values(entity_id=best.c.entity_id)
        )
        self._s.execute(
            sa.update(HNStoryRow)
            .where(
                HNStoryRow.item_id.in_(ids),
                HNStoryRow.entity_id.is_not(None),
                HNStoryRow.item_id.not_in(sa.select(best.c.item_id)),
            )
            .values(entity_id=None)
        )
        return int(getattr(res, "rowcount", 0) or 0)

    # ------------------------------------------------------------------ reads
    def story(self, story_id: int) -> HNStoryRow | None:
        return self._s.get(HNStoryRow, story_id)

    def stories(
        self,
        *,
        since: datetime | None = None,
        seen_since: datetime | None = None,
        category: str | None = None,
        list_name: str | None = None,
        kind: str | None = None,
        momentum_state: str | None = None,
        active_only: bool = False,
        include_synthetic: bool = False,
        limit: int = 50,
        offset: int = 0,
        order: str = "published",
    ) -> tuple[list[HNStoryRow], int]:
        q = sa.select(HNStoryRow)
        if since is not None:
            q = q.where(HNStoryRow.published_at >= since)
        if seen_since is not None:
            q = q.where(HNStoryRow.last_seen_at >= seen_since)
        if category is not None:
            q = q.where(
                sa.or_(
                    HNStoryRow.primary_category == category,
                    HNStoryRow.secondary_category == category,
                )
            )
        if list_name is not None:
            q = q.where(HNStoryRow.lists.contains([list_name]))
        if kind is not None:
            q = q.where(HNStoryRow.kind == kind)
        if momentum_state is not None:
            q = q.where(HNStoryRow.momentum_state == momentum_state)
        if active_only:
            q = q.where(HNStoryRow.monitoring_state == "active")
        if not include_synthetic:
            q = q.where(HNStoryRow.is_synthetic.is_(False))
        total = int(
            self._s.execute(sa.select(sa.func.count()).select_from(q.subquery())).scalar_one()
        )
        rate = HNStoryRow.momentum["score_per_hour"].astext.cast(sa.Double)
        key: Any = {
            "published": HNStoryRow.published_at.desc(),
            "priority": HNStoryRow.priority_score.desc(),
            "score": HNStoryRow.latest_score.desc(),
            "comments": HNStoryRow.latest_comments.desc(),
            "momentum": rate.desc().nulls_last(),
            "first_seen": HNStoryRow.first_seen_at.desc(),
        }[order]
        rows = (
            self._s.execute(q.order_by(key, HNStoryRow.story_id.desc()).limit(limit).offset(offset))
            .scalars()
            .all()
        )
        return list(rows), total

    def observations(self, story_id: int, limit: int = 200) -> list[HNObservationRow]:
        rows = (
            self._s.execute(
                sa.select(HNObservationRow)
                .where(HNObservationRow.story_id == story_id)
                .order_by(HNObservationRow.observed_at.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return list(reversed(rows))

    def refresh_candidates(
        self,
        now: datetime,
        *,
        changed: set[int],
        min_gap: timedelta,
        limit: int,
    ) -> list[HNStoryRow]:
        """Active stories due for a refresh, plus active stories ``/updates.json`` reported
        as changed (pulled forward, but never re-read sooner than ``min_gap``).

        Due stories come first (highest priority first), then changed-but-not-due ones.
        """
        due = (
            self._s.execute(
                sa.select(HNStoryRow)
                .where(
                    HNStoryRow.monitoring_state == "active",
                    HNStoryRow.next_refresh_at <= now,
                    HNStoryRow.is_synthetic.is_(False),
                )
                .order_by(HNStoryRow.priority_score.desc(), HNStoryRow.story_id)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        out = list(due)
        if changed and len(out) < limit:
            taken = {r.story_id for r in out}
            pulled = (
                self._s.execute(
                    sa.select(HNStoryRow)
                    .where(
                        HNStoryRow.monitoring_state == "active",
                        HNStoryRow.story_id.in_(sorted(changed - taken)),
                        HNStoryRow.is_synthetic.is_(False),
                        sa.or_(
                            HNStoryRow.last_observed_at.is_(None),
                            HNStoryRow.last_observed_at <= now - min_gap,
                        ),
                    )
                    .order_by(HNStoryRow.priority_score.desc(), HNStoryRow.story_id)
                    .limit(limit - len(out))
                )
                .scalars()
                .all()
            )
            out.extend(pulled)
        return out

    def repost_of(self, row: HNStoryRow) -> int | None:
        """The earliest other story linking the same article (None if this is the first)."""
        if not row.canonical_link:
            return None
        return self._s.execute(
            sa.select(HNStoryRow.story_id)
            .where(
                HNStoryRow.canonical_link == row.canonical_link,
                HNStoryRow.story_id != row.story_id,
                sa.or_(
                    HNStoryRow.published_at < row.published_at,
                    sa.and_(
                        HNStoryRow.published_at == row.published_at,
                        HNStoryRow.story_id < row.story_id,
                    ),
                ),
            )
            .order_by(HNStoryRow.published_at, HNStoryRow.story_id)
            .limit(1)
        ).scalar_one_or_none()

    def reposts(self, row: HNStoryRow) -> list[int]:
        if not row.canonical_link:
            return []
        return list(
            self._s.execute(
                sa.select(HNStoryRow.story_id)
                .where(
                    HNStoryRow.canonical_link == row.canonical_link,
                    HNStoryRow.story_id != row.story_id,
                )
                .order_by(HNStoryRow.published_at)
                .limit(20)
            ).scalars()
        )

    def entity(self, entity_id: int | None) -> DiscoveredEntityRow | None:
        return self._s.get(DiscoveredEntityRow, entity_id) if entity_id else None

    def corroboration(self, entity_id: int | None) -> dict[str, Any]:
        """Independent publishers outside Hacker News mentioning the entity (a publisher or
        article URL counts once, however many times it was reposted)."""
        if entity_id is None:
            return {
                "independent_publishers": 0,
                "providers": [],
                "hn_stories_about_entity": 0,
                "basis": "no entity linked",
            }
        rows = self._s.execute(
            sa.select(
                DiscoveryItemRow.provider_name,
                sa.func.coalesce(DiscoveryItemRow.publisher, DiscoveryItemRow.canonical_url).label(
                    "origin"
                ),
            )
            .join(EntityMentionRow, EntityMentionRow.item_id == DiscoveryItemRow.id)
            .where(
                EntityMentionRow.entity_id == entity_id,
                EntityMentionRow.status != EntityStatus.REJECTED,
                DiscoveryItemRow.provider_name.not_in(HN_PROVIDERS),
                DiscoveryItemRow.is_synthetic.is_(False),
            )
            .distinct()
        ).all()
        hn = int(
            self._s.execute(
                sa.select(sa.func.count())
                .select_from(HNStoryRow)
                .where(HNStoryRow.entity_id == entity_id)
            ).scalar_one()
        )
        origins = {str(o).lower() for _p, o in rows if o}
        return {
            "independent_publishers": len(origins),
            "providers": sorted({p for p, _o in rows}),
            "hn_stories_about_entity": hn,
            "basis": "distinct publishers outside Hacker News mentioning the entity; several "
            "Hacker News stories about one announcement are one community, not confirmation",
        }

    def keywords_for(self, item_id: int | None, entity_id: int | None) -> list[dict[str, Any]]:
        """Keyword hypotheses grounded in this story's item or its entity."""
        if item_id is None and entity_id is None:
            return []
        conds = []
        if entity_id is not None:
            conds.append(DiscoveredKeywordRow.entity_id == entity_id)
        if item_id is not None:
            conds.append(
                DiscoveredKeywordRow.id.in_(
                    sa.select(DiscoveryEvidenceRow.keyword_id).where(
                        DiscoveryEvidenceRow.item_id == item_id
                    )
                )
            )
        rows = self._s.execute(
            sa.select(DiscoveredKeywordRow, SearchQueryRow.canonical_query)
            .join(SearchQueryRow, SearchQueryRow.id == DiscoveredKeywordRow.query_id)
            .where(sa.or_(*conds))
            .order_by(DiscoveredKeywordRow.priority_score.desc())
            .limit(25)
        ).all()
        return [
            {
                "keyword_id": k.id,
                "query": q,
                "origin": k.origin.value,
                "method": k.discovery_type.value,
                "template": k.template_id,
                "intent": k.primary_intent.value,
                "status": k.candidate_status.value,
                "priority": round(k.priority_score, 2),
                "first_discovered_at": k.first_discovered_at.isoformat(),
                "evidence_class": "KEYWORD_HYPOTHESIS",
            }
            for k, q in rows
        ]

    def stats(self) -> dict[str, Any]:
        def one(q: Any) -> Any:
            return self._s.execute(q).scalar_one()

        real = HNStoryRow.is_synthetic.is_(False)
        by_category: dict[str, int] = {
            str(k): int(v)
            for k, v in self._s.execute(
                sa.select(HNStoryRow.primary_category, sa.func.count())
                .where(real)
                .group_by(HNStoryRow.primary_category)
            ).all()
        }
        lst = sa.func.unnest(HNStoryRow.lists).label("list")
        by_list: dict[str, int] = {
            str(k): int(v)
            for k, v in self._s.execute(
                sa.select(sa.literal_column("list"), sa.func.count())
                .select_from(sa.select(lst).where(real).subquery())
                .group_by(sa.literal_column("list"))
            ).all()
        }
        by_kind: dict[str, int] = {
            str(k): int(v)
            for k, v in self._s.execute(
                sa.select(HNStoryRow.kind, sa.func.count()).where(real).group_by(HNStoryRow.kind)
            ).all()
        }
        return {
            "stories": int(one(sa.select(sa.func.count()).select_from(HNStoryRow).where(real))),
            "active_stories": int(
                one(
                    sa.select(sa.func.count())
                    .select_from(HNStoryRow)
                    .where(real, HNStoryRow.monitoring_state == "active")
                )
            ),
            "rising_stories": int(
                one(
                    sa.select(sa.func.count())
                    .select_from(HNStoryRow)
                    .where(real, HNStoryRow.momentum_state == "rising")
                )
            ),
            "observations": int(
                one(
                    sa.select(sa.func.count())
                    .select_from(HNObservationRow)
                    .where(HNObservationRow.is_synthetic.is_(False))
                )
            ),
            "refresh_observations": int(
                one(
                    sa.select(sa.func.count())
                    .select_from(HNObservationRow)
                    .where(
                        HNObservationRow.is_synthetic.is_(False),
                        HNObservationRow.origin == "refresh",
                    )
                )
            ),
            "entities_linked": int(
                one(
                    sa.select(sa.func.count(sa.distinct(HNStoryRow.entity_id))).where(
                        real, HNStoryRow.entity_id.is_not(None)
                    )
                )
            ),
            "by_category": by_category,
            "by_list": by_list,
            "by_kind": by_kind,
            "last_observed_at": one(sa.select(sa.func.max(HNObservationRow.observed_at))),
            "last_refresh_at": one(
                sa.select(sa.func.max(HNObservationRow.observed_at)).where(
                    HNObservationRow.origin == "refresh"
                )
            ),
            "next_refresh_at": one(
                sa.select(sa.func.min(HNStoryRow.next_refresh_at)).where(
                    HNStoryRow.monitoring_state == "active"
                )
            ),
        }

    # ------------------------------------------------------------------ retention
    _EXPIRING: Sequence[tuple[str, Any]] = (
        ("hn_story_observations", HNObservationRow),
        ("hn_stories", HNStoryRow),
    )

    def count_expired(self, now: datetime) -> dict[str, int]:
        return {
            name: int(
                self._s.execute(
                    sa.select(sa.func.count()).select_from(model).where(model.expires_at <= now)
                ).scalar_one()
            )
            for name, model in self._EXPIRING
        }

    def purge_expired(self, now: datetime) -> dict[str, int]:
        out: dict[str, int] = {}
        for name, model in self._EXPIRING:
            res = self._s.execute(sa.delete(model).where(model.expires_at <= now))
            out[name] = int(getattr(res, "rowcount", 0) or 0)
        return out
