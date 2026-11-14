"""Phase 7 persistence: idempotent recommendation writes, user state, alerts, retention.

Recommendation rows are keyed by their deterministic ID: saving the same recommendation
twice inserts nothing (only rank and diversity decisions, which depend on the other
recommendations, are refreshed). Superseded recommendations stay (``is_current = false``)
so user state attached to them is never lost.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session, defer

from trend_agent.db.content_models import ContentAnalysisRunRow, ContentOpportunityRow
from trend_agent.db.detection_models import SurgeEventRow
from trend_agent.db.forecast_models import ForecastPredictionRow
from trend_agent.db.models import SearchQueryRow
from trend_agent.db.monitoring_models import (
    MonitoringTargetRow,
    ObservationRow,
    ProviderHealthRow,
    SeriesRow,
)
from trend_agent.db.recommendation_models import (
    CreatorProfileRow,
    NotificationDeliveryRow,
    NotificationRow,
    ProductionTrackingRow,
    RecommendationFeedbackRow,
    RecommendationHookRow,
    RecommendationOutlineRow,
    RecommendationRunRow,
    RecommendationScoreRow,
    SavedRecommendationRow,
    VideoRecommendationRow,
)
from trend_agent.schemas.dashboard import ProductionView, UserState
from trend_agent.schemas.recommendation import (
    CreatorProfile,
    ProductionStatus,
    RecommendationOutput,
    Suppression,
)

V = VideoRecommendationRow


def _uuid(rid: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(rid)
    except (ValueError, AttributeError, TypeError):
        return None


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class RecommendationRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    # ------------------------------------------------------------------ profiles
    def profile(self, profile_id: str | None = None) -> CreatorProfile | None:
        if profile_id is None:
            row = self._s.execute(
                sa.select(CreatorProfileRow).where(CreatorProfileRow.is_default)
            ).scalar_one_or_none()
        else:
            row = self._s.get(CreatorProfileRow, profile_id)
        return CreatorProfile.model_validate(row.profile) if row is not None else None

    def save_profile(self, profile: CreatorProfile, *, default: bool, now: datetime) -> None:
        if default:
            self._s.execute(
                sa.update(CreatorProfileRow)
                .where(CreatorProfileRow.is_default, CreatorProfileRow.id != profile.profile_id)
                .values(is_default=False)
            )
        row = self._s.get(CreatorProfileRow, profile.profile_id)
        doc = profile.model_dump(mode="json")
        if row is None:
            self._s.add(
                CreatorProfileRow(
                    id=profile.profile_id,
                    profile=doc,
                    is_default=default,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            row.profile, row.updated_at = doc, now
            row.is_default = row.is_default or default
        self._s.flush()

    # ---------------------------------------------------------------------- runs
    def start_run(self, run_id: uuid.UUID, **values: Any) -> None:
        self._s.add(RecommendationRunRow(id=run_id, status="running", **values))
        self._s.flush()

    def finish_run(self, run_id: uuid.UUID, **values: Any) -> None:
        self._s.execute(
            sa.update(RecommendationRunRow)
            .where(RecommendationRunRow.id == run_id)
            .values(**values)
        )

    def tokens_since(self, since: datetime) -> int:
        r = RecommendationRunRow
        return int(
            self._s.execute(
                sa.select(sa.func.coalesce(sa.func.sum(r.input_tokens + r.output_tokens), 0)).where(
                    r.started_at >= since
                )
            ).scalar_one()
        )

    def runs(self, limit: int = 20) -> list[RecommendationRunRow]:
        return list(
            self._s.execute(
                sa.select(RecommendationRunRow)
                .order_by(RecommendationRunRow.started_at.desc())
                .limit(limit)
            ).scalars()
        )

    # ----------------------------------------------------------- recommendations
    def save(
        self,
        rec: RecommendationOutput,
        *,
        run_id: uuid.UUID | None,
        opportunity_row_id: int | None,
        opportunity_score: float | None,
        expires_at: datetime | None,
        now: datetime,
    ) -> bool:
        """Insert one recommendation (+ hooks, outline, score components). An identical ID
        already stored is a no-op apart from refreshing rank/suppression and currency."""
        rid = uuid.UUID(rec.recommendation_id)
        row = self._s.get(V, rid)
        if row is not None:
            self._apply_ranking(row, rec.ranking.rank, rec.suppression, now)
            row.is_current = True
            return False
        self._s.add(
            V(
                id=rid,
                run_id=run_id,
                opportunity_key=rec.opportunity_id,
                opportunity_row_id=opportunity_row_id,
                query=rec.primary_query,
                category=rec.category,
                region=rec.region,
                language=rec.language,
                video_format=rec.video_format.value,
                intent=rec.target_intent,
                sub_intent=rec.target_sub_intent,
                title=rec.title,
                status=rec.verification.status.value,
                research=rec.research_hypothesis,
                eligible=rec.ranking.eligible,
                composite_score=rec.ranking.composite_score,
                rank=rec.ranking.rank,
                ranking_version=rec.ranking.version,
                urgency=rec.production.urgency.value,
                production_profile=rec.production.production_profile.value,
                hours_low=rec.production.estimated_hours_low,
                hours_high=rec.production.estimated_hours_high,
                demand_basis=rec.demand.basis,
                opportunity_score=opportunity_score,
                forecast_available=rec.forecast.available,
                suppressed=rec.suppression.suppressed,
                suppression_reason=rec.suppression.reason,
                duplicate_of=rec.suppression.duplicate_of,
                fingerprint=rec.input_fingerprint,
                generator=rec.generator,
                profile_id=rec.creator_profile_id or "default",
                recommendation_version=rec.versions.get("recommendation", ""),
                evidence_ids=[e.evidence_id for e in rec.evidence][:200],
                concept=rec.model_dump(mode="json"),
                is_current=True,
                is_synthetic=rec.synthetic,
                generated_at=rec.generated_at,
                created_at=now,
                updated_at=now,
                expires_at=expires_at,
            )
        )
        self._s.flush()
        for i, h in enumerate(rec.hooks):
            self._s.add(
                RecommendationHookRow(
                    recommendation_id=rid,
                    position=i,
                    hook_type=h.hook_type.value,
                    text=h.text,
                    estimated_seconds=h.estimated_seconds,
                    labels=[h.verification_status, "accepted" if h.accepted else "rejected"],
                )
            )
        labels: Counter[str] = Counter(
            it.label.value for sec in rec.outline.sections for it in sec.items
        )
        self._s.add(
            RecommendationOutlineRow(
                recommendation_id=rid,
                outline=rec.outline.model_dump(mode="json"),
                section_count=len(rec.outline.sections),
                label_counts=dict(labels),
            )
        )
        for c in rec.ranking.components:
            self._s.add(
                RecommendationScoreRow(
                    recommendation_id=rid,
                    component=c.name,
                    value=c.value,
                    weight=c.weight,
                    contribution=c.contribution,
                    available=c.available,
                    raw=dict(c.raw),
                    note=c.note,
                    ranking_version=rec.ranking.version,
                )
            )
        self._s.flush()
        return True

    @staticmethod
    def _apply_ranking(
        row: VideoRecommendationRow, rank: int | None, sup: Suppression, now: datetime
    ) -> bool:
        if (
            row.rank == rank
            and row.suppressed == sup.suppressed
            and row.suppression_reason == sup.reason
            and row.duplicate_of == sup.duplicate_of
        ):
            return False
        row.rank = rank
        row.suppressed, row.suppression_reason, row.duplicate_of = (
            sup.suppressed,
            sup.reason,
            sup.duplicate_of,
        )
        doc = dict(row.concept)
        doc["ranking"] = {**doc["ranking"], "rank": rank}
        doc["suppression"] = sup.model_dump(mode="json")
        row.concept, row.updated_at = doc, now
        return True

    def set_ranking(
        self, rid: str, rank: int | None, suppression: Suppression, now: datetime
    ) -> bool:
        """Refresh the cross-recommendation decisions (rank, diversity suppression)."""
        key = _uuid(rid)
        row = self._s.get(V, key) if key else None
        return row is not None and self._apply_ranking(row, rank, suppression, now)

    def current_for(
        self, opportunity_key: str, profile_id: str, fingerprint: str | None = None
    ) -> list[VideoRecommendationRow]:
        stmt = sa.select(V).where(
            V.opportunity_key == opportunity_key, V.profile_id == profile_id, V.is_current
        )
        if fingerprint is not None:
            stmt = stmt.where(V.fingerprint == fingerprint)
        return list(self._s.execute(stmt.order_by(V.created_at)).scalars())

    def supersede(self, opportunity_key: str, profile_id: str, keep: Iterable[str]) -> int:
        keep_ids = [k for k in (_uuid(x) for x in keep) if k is not None]
        stmt = (
            sa.update(V)
            .where(V.opportunity_key == opportunity_key, V.profile_id == profile_id, V.is_current)
            .values(is_current=False)
        )
        if keep_ids:
            stmt = stmt.where(V.id.not_in(keep_ids))
        return int(getattr(self._s.execute(stmt), "rowcount", 0) or 0)

    def current(
        self, *, profile_id: str | None, include_synthetic: bool
    ) -> list[VideoRecommendationRow]:
        stmt = sa.select(V).where(V.is_current)
        if profile_id is not None:
            stmt = stmt.where(V.profile_id == profile_id)
        if not include_synthetic:
            stmt = stmt.where(sa.not_(V.is_synthetic))
        return list(self._s.execute(stmt).scalars())

    def get(self, rid: str) -> VideoRecommendationRow | None:
        key = _uuid(rid)
        return self._s.get(V, key) if key else None

    def for_opportunities(
        self, keys: Sequence[str], *, include_synthetic: bool
    ) -> list[VideoRecommendationRow]:
        if not keys:
            return []
        stmt = sa.select(V).where(V.opportunity_key.in_(list(keys)), V.is_current)
        if not include_synthetic:
            stmt = stmt.where(sa.not_(V.is_synthetic))
        return list(self._s.execute(stmt).scalars())

    def search(
        self,
        *,
        user_key: str,
        include_synthetic: bool,
        category: str | None = None,
        status: str | None = None,
        q: str | None = None,
        urgency: str | None = None,
        min_score: float | None = None,
        include_research: bool = False,
        include_suppressed: bool = False,
        include_dismissed: bool = False,
        opportunity_id: str | None = None,
        sort: str = "score",
        order: str = "desc",
        page: int = 1,
        page_size: int = 25,
    ) -> tuple[list[VideoRecommendationRow], int]:
        stmt = sa.select(V).where(V.is_current)
        if not include_synthetic:
            stmt = stmt.where(sa.not_(V.is_synthetic))
        if not include_research:
            stmt = stmt.where(sa.not_(V.research), V.eligible)  # REJECTED never ranked
        if not include_suppressed:
            stmt = stmt.where(sa.not_(V.suppressed))
        if not include_dismissed:
            dismissed = sa.select(SavedRecommendationRow.recommendation_id).where(
                SavedRecommendationRow.user_key == user_key,
                SavedRecommendationRow.state == "DISMISSED",
            )
            stmt = stmt.where(V.id.not_in(dismissed))
        if category:
            stmt = stmt.where(V.category == category)
        if status:
            stmt = stmt.where(V.status == status)
        if urgency:
            stmt = stmt.where(V.urgency == urgency)
        if opportunity_id:
            stmt = stmt.where(V.opportunity_key == opportunity_id)
        if min_score is not None:
            stmt = stmt.where(V.composite_score >= min_score)
        if q:
            pat = _like(q)
            stmt = stmt.where(
                sa.or_(V.title.ilike(pat, escape="\\"), V.query.ilike(pat, escape="\\"))
            )
        total = int(
            self._s.execute(sa.select(sa.func.count()).select_from(stmt.subquery())).scalar_one()
        )
        cols: dict[str, Any] = {
            "score": V.composite_score,
            "rank": V.rank,
            "hours": V.hours_high,
            "generated": V.generated_at,
            "title": sa.func.lower(V.title),
        }
        col = cols.get(sort, V.composite_score)
        if sort == "rank":
            order = "asc" if order == "desc" else order
        direction = col.desc() if order == "desc" else col.asc()
        page = max(1, page)
        rows = list(
            self._s.execute(
                stmt.order_by(direction.nulls_last(), V.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).scalars()
        )
        return rows, total

    def counts(self, *, include_synthetic: bool) -> dict[str, Any]:
        base = sa.select(V.status, V.research, V.suppressed, sa.func.count()).where(V.is_current)
        if not include_synthetic:
            base = base.where(sa.not_(V.is_synthetic))
        out: dict[str, Any] = {"by_status": {}, "research": 0, "suppressed": 0, "ranked": 0}
        for status, research, suppressed, n in self._s.execute(
            base.group_by(V.status, V.research, V.suppressed)
        ):
            out["by_status"][status] = out["by_status"].get(status, 0) + int(n)
            if research:
                out["research"] += int(n)
            elif suppressed:
                out["suppressed"] += int(n)
            else:
                out["ranked"] += int(n)
        cat = sa.select(V.category, sa.func.count()).where(
            V.is_current, sa.not_(V.research), sa.not_(V.suppressed)
        )
        if not include_synthetic:
            cat = cat.where(sa.not_(V.is_synthetic))
        out["by_category"] = {
            (c or "other"): int(n) for c, n in self._s.execute(cat.group_by(V.category))
        }
        fb = sa.select(RecommendationFeedbackRow.feedback_type, sa.func.count()).group_by(
            RecommendationFeedbackRow.feedback_type
        )
        out["feedback"] = {t: int(n) for t, n in self._s.execute(fb)}
        return out

    # ------------------------------------------------------------- user state
    def user_states(self, rids: Sequence[str], user_key: str) -> dict[str, UserState]:
        keys = [k for k in (_uuid(r) for r in rids) if k is not None]
        if not keys:
            return {}
        saved = {
            r.recommendation_id: r
            for r in self._s.execute(
                sa.select(SavedRecommendationRow).where(
                    SavedRecommendationRow.user_key == user_key,
                    SavedRecommendationRow.recommendation_id.in_(keys),
                )
            ).scalars()
        }
        prod = {
            r.recommendation_id: r
            for r in self._s.execute(
                sa.select(ProductionTrackingRow).where(
                    ProductionTrackingRow.user_key == user_key,
                    ProductionTrackingRow.recommendation_id.in_(keys),
                )
            ).scalars()
        }
        fb: dict[uuid.UUID, dict[str, int]] = {}
        for rid, ftype, n in self._s.execute(
            sa.select(
                RecommendationFeedbackRow.recommendation_id,
                RecommendationFeedbackRow.feedback_type,
                sa.func.count(),
            )
            .where(
                RecommendationFeedbackRow.user_key == user_key,
                RecommendationFeedbackRow.recommendation_id.in_(keys),
            )
            .group_by(
                RecommendationFeedbackRow.recommendation_id, RecommendationFeedbackRow.feedback_type
            )
        ):
            fb.setdefault(rid, {})[ftype] = int(n)
        out: dict[str, UserState] = {}
        for k in keys:
            s, p, f = saved.get(k), prod.get(k), fb.get(k)
            if s is None and p is None and f is None:
                continue
            out[str(k)] = UserState(
                saved=s is not None and s.state == "SAVED",
                saved_at=s.saved_at if s is not None and s.state == "SAVED" else None,
                notes=s.notes if s is not None else None,
                dismissed=s is not None and s.state == "DISMISSED",
                production=_production_view(p) if p is not None else None,
                feedback=f or {},
            )
        return out

    def user_state(self, rid: str, user_key: str) -> UserState:
        return self.user_states([rid], user_key).get(rid) or UserState(
            saved=False, saved_at=None, notes=None, dismissed=False, production=None, feedback={}
        )

    def _saved_row(self, key: uuid.UUID, user_key: str) -> SavedRecommendationRow | None:
        return self._s.execute(
            sa.select(SavedRecommendationRow).where(
                SavedRecommendationRow.user_key == user_key,
                SavedRecommendationRow.recommendation_id == key,
            )
        ).scalar_one_or_none()

    def set_saved_state(
        self, rid: str, user_key: str, state: str | None, notes: str | None, now: datetime
    ) -> None:
        """``state`` SAVED / DISMISSED, or None to remove the decision (unsave)."""
        key = uuid.UUID(rid)
        row = self._saved_row(key, user_key)
        if state is None:
            if row is not None and row.state == "SAVED":
                self._s.delete(row)
            return
        if row is None:
            self._s.add(
                SavedRecommendationRow(
                    user_key=user_key,
                    recommendation_id=key,
                    state=state,
                    notes=notes,
                    saved_at=now if state == "SAVED" else None,
                    updated_at=now,
                )
            )
        else:
            if state == "SAVED" and row.state != "SAVED":
                row.saved_at = now
            elif state != "SAVED":
                row.saved_at = None
            row.state = state
            row.notes = notes if notes is not None else row.notes
            row.updated_at = now
        self._s.flush()

    def add_feedback(
        self,
        rid: str,
        user_key: str,
        ftype: str,
        value: str | None,
        notes: str | None,
        now: datetime,
    ) -> None:
        row = self.get(rid)
        self._s.add(
            RecommendationFeedbackRow(
                user_key=user_key,
                recommendation_id=uuid.UUID(rid),
                feedback_type=ftype,
                value=value,
                notes=notes,
                ranking_version=row.ranking_version if row else None,
                composite_score=row.composite_score if row else None,
                created_at=now,
            )
        )
        self._s.flush()

    def production(self, rid: str, user_key: str) -> ProductionTrackingRow | None:
        return self._s.execute(
            sa.select(ProductionTrackingRow).where(
                ProductionTrackingRow.user_key == user_key,
                ProductionTrackingRow.recommendation_id == uuid.UUID(rid),
            )
        ).scalar_one_or_none()

    def set_production(
        self,
        rid: str,
        user_key: str,
        *,
        status: ProductionStatus,
        planned: datetime | None,
        actual: datetime | None,
        video_id: str | None,
        notes: str | None,
        now: datetime,
    ) -> None:
        row = self.production(rid, user_key)
        entry = {"status": status.value, "at": now.isoformat()}
        if row is None:
            self._s.add(
                ProductionTrackingRow(
                    user_key=user_key,
                    recommendation_id=uuid.UUID(rid),
                    status=status.value,
                    planned_publication_time=planned,
                    actual_publication_time=actual,
                    published_video_id=video_id,
                    notes=notes,
                    history=[entry],
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            if row.status != status.value:
                row.history = [*row.history, entry]
            row.status = status.value
            row.planned_publication_time = planned
            row.actual_publication_time = actual
            row.published_video_id = video_id
            row.notes = notes if notes is not None else row.notes
            row.updated_at = now
        self._s.flush()

    def saved(
        self,
        user_key: str,
        *,
        status: str | None = None,
        category: str | None = None,
        include_synthetic: bool,
    ) -> list[tuple[VideoRecommendationRow, SavedRecommendationRow]]:
        stmt = (
            sa.select(V, SavedRecommendationRow)
            .join(SavedRecommendationRow, SavedRecommendationRow.recommendation_id == V.id)
            .where(
                SavedRecommendationRow.user_key == user_key,
                SavedRecommendationRow.state == "SAVED",
            )
        )
        if not include_synthetic:
            stmt = stmt.where(sa.not_(V.is_synthetic))
        if category:
            stmt = stmt.where(V.category == category)
        if status:
            stmt = stmt.join(
                ProductionTrackingRow,
                sa.and_(
                    ProductionTrackingRow.recommendation_id == V.id,
                    ProductionTrackingRow.user_key == user_key,
                ),
            ).where(ProductionTrackingRow.status == status)
        return [
            (v, s)
            for v, s in self._s.execute(stmt.order_by(SavedRecommendationRow.saved_at.desc())).all()
        ]

    # ------------------------------------------------------------------ alerts
    def alert_exists(self, dedup_key: str) -> bool:
        return (
            self._s.execute(
                sa.select(NotificationRow.id).where(NotificationRow.dedup_key == dedup_key)
            ).first()
            is not None
        )

    def last_alert_for_subject(self, subject_key: str) -> datetime | None:
        return self._s.execute(
            sa.select(sa.func.max(NotificationRow.created_at)).where(
                NotificationRow.subject_key == subject_key
            )
        ).scalar_one()

    def add_alert(self, row: NotificationRow) -> None:
        self._s.add(row)
        self._s.flush()

    def update_delivery(
        self, alert_id: str, channel: str, status: dict[str, Any], now: datetime
    ) -> None:
        nid = uuid.UUID(alert_id)
        row = self._s.execute(
            sa.select(NotificationDeliveryRow).where(
                NotificationDeliveryRow.notification_id == nid,
                NotificationDeliveryRow.channel == channel,
            )
        ).scalar_one_or_none()
        if row is None:
            row = NotificationDeliveryRow(notification_id=nid, channel=channel)
            self._s.add(row)
        row.status = str(status.get("status", "unknown"))
        row.attempts = int(status.get("attempts", 0) or 0)
        row.detail = status.get("last_error")
        row.last_attempt_at = now
        self._s.flush()

    def alerts(
        self, *, unread_only: bool, include_synthetic: bool, limit: int = 200
    ) -> list[tuple[NotificationRow, list[NotificationDeliveryRow]]]:
        stmt = sa.select(NotificationRow)
        if unread_only:
            stmt = stmt.where(sa.not_(NotificationRow.read))
        if not include_synthetic:
            stmt = stmt.where(sa.not_(NotificationRow.is_synthetic))
        rows = list(
            self._s.execute(stmt.order_by(NotificationRow.created_at.desc()).limit(limit)).scalars()
        )
        deliveries: dict[uuid.UUID, list[NotificationDeliveryRow]] = {}
        if rows:
            for d in self._s.execute(
                sa.select(NotificationDeliveryRow).where(
                    NotificationDeliveryRow.notification_id.in_([r.id for r in rows])
                )
            ).scalars():
                deliveries.setdefault(d.notification_id, []).append(d)
        return [(r, deliveries.get(r.id, [])) for r in rows]

    def mark_alert_read(self, aid: str) -> NotificationRow | None:
        key = _uuid(aid)
        row = self._s.get(NotificationRow, key) if key else None
        if row is not None:
            row.read = True
            self._s.flush()
        return row

    # --------------------------------------------------------------- retention
    def _kept(self) -> sa.CompoundSelect[Any]:
        """Recommendation IDs the creator saved or is producing (their own data)."""
        return sa.union(
            sa.select(SavedRecommendationRow.recommendation_id).where(
                SavedRecommendationRow.state == "SAVED"
            ),
            sa.select(ProductionTrackingRow.recommendation_id),
        )

    def count_expired(self, now: datetime) -> dict[str, int]:
        expired = sa.and_(V.expires_at.is_not(None), V.expires_at <= now)
        n = self._s.execute(
            sa.select(sa.func.count()).select_from(V).where(expired, V.id.not_in(self._kept()))
        ).scalar_one()
        return {"video_recommendations": int(n)}

    def purge_expired(self, now: datetime) -> dict[str, int]:
        """Delete expired recommendations nobody saved or tracks (hooks, outlines, scores,
        feedback cascade); saved/tracked ones are kept but marked not current."""
        expired = sa.and_(V.expires_at.is_not(None), V.expires_at <= now)
        kept = self._kept()
        res = self._s.execute(sa.delete(V).where(expired, V.id.not_in(kept)))
        self._s.execute(
            sa.update(V)
            .where(expired, V.id.in_(self._kept()), V.is_current)
            .values(is_current=False)
        )
        return {"video_recommendations": int(getattr(res, "rowcount", 0) or 0)}


def _production_view(p: ProductionTrackingRow) -> ProductionView:
    return ProductionView(
        status=ProductionStatus(p.status),
        planned_publication_time=p.planned_publication_time,
        actual_publication_time=p.actual_publication_time,
        published_video_id=p.published_video_id,
        notes=p.notes,
        updated_at=p.updated_at,
        history=list(p.history or []),
    )


# ------------------------------------------------------------------- dashboard reads
class DashboardReadRepository:
    """Read-only queries behind the dashboard (Phase 3-6 tables). Never writes."""

    def __init__(self, session: Session) -> None:
        self._s = session

    # ------------------------------------------------------------- opportunities
    def opportunities(
        self,
        *,
        include_synthetic: bool,
        category: str | None = None,
        region: str | None = None,
        intent: str | None = None,
        surge: str | None = None,
        status: str | None = None,
        verification: str | None = None,
        min_score: float | None = None,
        max_score: float | None = None,
        since: datetime | None = None,
        q: str | None = None,
        sort: str = "score",
        order: str = "desc",
        page: int | None = 1,
        page_size: int = 25,
    ) -> tuple[list[ContentOpportunityRow], int]:
        o = ContentOpportunityRow
        stmt = sa.select(o)
        if not include_synthetic:
            stmt = stmt.where(sa.not_(o.is_synthetic))
        if category:
            stmt = stmt.where(o.category == category)
        if region:
            stmt = stmt.where(sa.func.upper(o.region) == region.upper())
        if intent:
            stmt = stmt.where(
                sa.or_(
                    o.report["intent"]["primary_intent"].astext == intent,
                    o.report["intent"]["sub_intent"].astext == intent,
                )
            )
        if surge:
            stage = sa.func.coalesce(o.report["demand"]["alert_stage"].astext, "none")
            stmt = stmt.where(stage == surge)
        if status:
            stmt = stmt.where(o.status == status)
        if verification:
            stmt = stmt.where(o.verification_state == verification)
        if min_score is not None:
            stmt = stmt.where(o.score >= min_score)
        if max_score is not None:
            stmt = stmt.where(o.score <= max_score)
        if since is not None:
            stmt = stmt.where(o.updated_at >= since)
        if q:
            stmt = stmt.where(o.canonical_query.ilike(_like(q), escape="\\"))
        total = int(
            self._s.execute(sa.select(sa.func.count()).select_from(stmt.subquery())).scalar_one()
        )
        cols: dict[str, Any] = {
            "score": o.score,
            "updated": o.updated_at,
            "query": sa.func.lower(o.canonical_query),
            "severity": o.report["demand"]["severity"].as_float(),
        }
        col = cols.get(sort, o.score)
        direction = col.desc() if order == "desc" else col.asc()
        stmt = stmt.order_by(direction.nulls_last(), o.id)
        if page is not None:
            stmt = stmt.offset((max(1, page) - 1) * page_size).limit(page_size)
        else:
            # bounded in-memory sorts (e.g. by forecast probability): the large report
            # document is loaded later, only for the rows of the requested page
            stmt = stmt.options(defer(o.report)).limit(1000)
        return list(self._s.execute(stmt).scalars()), total

    def opportunity(self, ident: str) -> tuple[ContentOpportunityRow, str | None] | None:
        o = ContentOpportunityRow
        if ident.isdigit():
            row = self._s.get(o, int(ident))
        else:
            key = _uuid(ident)
            row = (
                self._s.execute(sa.select(o).where(o.opportunity_key == key)).scalar_one_or_none()
                if key
                else None
            )
        if row is None:
            return None
        run = (
            self._s.get(ContentAnalysisRunRow, row.analysis_run_id) if row.analysis_run_id else None
        )
        return row, (str(run.bundle_id) if run is not None and run.bundle_id else None)

    def opportunity_counts(self, *, include_synthetic: bool) -> dict[str, int]:
        o = ContentOpportunityRow
        stmt = sa.select(o.status, sa.func.count()).group_by(o.status)
        if not include_synthetic:
            stmt = stmt.where(sa.not_(o.is_synthetic))
        return {s: int(n) for s, n in self._s.execute(stmt)}

    def recent_opportunities(
        self, *, include_synthetic: bool, limit: int = 10
    ) -> list[ContentOpportunityRow]:
        o = ContentOpportunityRow
        stmt = sa.select(o)
        if not include_synthetic:
            stmt = stmt.where(sa.not_(o.is_synthetic))
        return list(self._s.execute(stmt.order_by(o.updated_at.desc()).limit(limit)).scalars())

    def latest_opportunity_for_queries(
        self, query_ids: Sequence[int], *, include_synthetic: bool
    ) -> dict[int, str]:
        if not query_ids:
            return {}
        o = ContentOpportunityRow
        stmt = sa.select(o.query_id, o.opportunity_key).where(o.query_id.in_(list(query_ids)))
        if not include_synthetic:
            stmt = stmt.where(sa.not_(o.is_synthetic))
        out: dict[int, str] = {}
        for qid, key in self._s.execute(stmt.order_by(o.updated_at.asc())):
            out[int(qid)] = str(key)
        return out

    # ------------------------------------------------------------------ forecasts
    def forecasts_for_queries(
        self, queries: Sequence[str], *, include_synthetic: bool, since: datetime | None
    ) -> dict[str, list[ForecastPredictionRow]]:
        if not queries:
            return {}
        p = ForecastPredictionRow
        qexpr = sa.func.lower(p.output["query"].astext)
        stmt = sa.select(p).where(qexpr.in_([q.lower() for q in queries]))
        if not include_synthetic:
            stmt = stmt.where(sa.not_(p.is_synthetic))
        if since is not None:
            stmt = stmt.where(p.prediction_timestamp >= since)
        out: dict[str, list[ForecastPredictionRow]] = {}
        for row in self._s.execute(
            stmt.order_by(p.prediction_timestamp.desc()).limit(5000)
        ).scalars():
            out.setdefault(str(row.output.get("query", "")).lower(), []).append(row)
        return out

    def forecast(self, prediction_id: str) -> ForecastPredictionRow | None:
        key = _uuid(prediction_id)
        return self._s.get(ForecastPredictionRow, key) if key else None

    # -------------------------------------------------------------------- trends
    def targets(
        self,
        *,
        include_synthetic: bool,
        category: str | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> tuple[list[tuple[MonitoringTargetRow, str]], int]:
        t = MonitoringTargetRow
        stmt = sa.select(t, SearchQueryRow.canonical_query).join(
            SearchQueryRow, SearchQueryRow.id == t.query_id
        )
        if not include_synthetic:
            stmt = stmt.where(sa.not_(t.is_synthetic))
        if category:
            stmt = stmt.where(t.category == category)
        if q:
            stmt = stmt.where(SearchQueryRow.canonical_query.ilike(_like(q), escape="\\"))
        total = int(
            self._s.execute(sa.select(sa.func.count()).select_from(stmt.subquery())).scalar_one()
        )
        rows = self._s.execute(
            stmt.order_by(t.priority_score.desc(), t.id)
            .offset((max(1, page) - 1) * page_size)
            .limit(page_size)
        ).all()
        return [(r[0], str(r[1])) for r in rows], total

    def target(self, target_id: int) -> tuple[MonitoringTargetRow, str] | None:
        row = self._s.execute(
            sa.select(MonitoringTargetRow, SearchQueryRow.canonical_query)
            .join(SearchQueryRow, SearchQueryRow.id == MonitoringTargetRow.query_id)
            .where(MonitoringTargetRow.id == target_id)
        ).first()
        return (row[0], str(row[1])) if row is not None else None

    def latest_values(self, series_ids: Sequence[int]) -> dict[int, tuple[float, datetime]]:
        """Latest current observation with a value, per series (DISTINCT ON)."""
        if not series_ids:
            return {}
        t = ObservationRow
        stmt = (
            sa.select(t.series_id, t.value, t.period_end)
            .where(t.series_id.in_(list(series_ids)), t.is_current, t.value.is_not(None))
            .distinct(t.series_id)
            .order_by(t.series_id, t.period_end.desc())
        )
        return {int(s): (float(v), pe) for s, v, pe in self._s.execute(stmt)}

    def latest_episode_stage(
        self, target_ids: Sequence[int], *, include_synthetic: bool
    ) -> dict[int, str]:
        if not target_ids:
            return {}
        e = SurgeEventRow
        stmt = (
            sa.select(e.target_id, e.status)
            .where(e.target_id.in_(list(target_ids)))
            .distinct(e.target_id)
            .order_by(e.target_id, e.last_updated_at.desc())
        )
        if not include_synthetic:
            stmt = stmt.where(sa.not_(e.is_synthetic))
        return {int(t): str(getattr(s, "value", s)) for t, s in self._s.execute(stmt)}

    def counts(self, *, include_synthetic: bool) -> dict[str, int]:
        t, e = MonitoringTargetRow, SurgeEventRow
        targets = sa.select(sa.func.count()).select_from(t).where(t.status == "active")
        surges = (
            sa.select(sa.func.count())
            .select_from(e)
            .where(e.resolved_at.is_(None), e.status.in_(["candidate", "confirmed"]))
        )
        if not include_synthetic:
            targets = targets.where(sa.not_(t.is_synthetic))
            surges = surges.where(sa.not_(e.is_synthetic))
        return {
            "monitored_queries": int(self._s.execute(targets).scalar_one()),
            "active_surges": int(self._s.execute(surges).scalar_one()),
        }

    def freshness(
        self, *, include_synthetic: bool
    ) -> list[tuple[str, str, str, int, datetime | None]]:
        """(provider, measurement, search property, series, latest period end)."""
        s, t = SeriesRow, ObservationRow
        last = (
            sa.select(t.series_id, sa.func.max(t.period_end).label("last"))
            .where(t.is_current, t.value.is_not(None))
            .group_by(t.series_id)
            .subquery()
        )
        stmt = (
            sa.select(
                s.provider_name,
                s.measurement_category,
                s.search_property,
                sa.func.count(s.id),
                sa.func.max(last.c.last),
            )
            .outerjoin(last, last.c.series_id == s.id)
            .group_by(s.provider_name, s.measurement_category, s.search_property)
        )
        if not include_synthetic:
            stmt = stmt.where(sa.not_(s.is_synthetic))
        return [
            (
                str(p),
                str(getattr(m, "value", m)),
                str(getattr(sp, "value", sp)),
                int(n),
                lp,
            )
            for p, m, sp, n, lp in self._s.execute(stmt)
        ]

    def provider_health(self) -> list[ProviderHealthRow]:
        return list(
            self._s.execute(
                sa.select(ProviderHealthRow).order_by(ProviderHealthRow.provider_name)
            ).scalars()
        )

    def schema_version(self) -> str | None:
        try:
            return self._s.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
        except sa.exc.SQLAlchemyError:  # pragma: no cover - schema created without Alembic
            return None
