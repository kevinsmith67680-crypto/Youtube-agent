"""``DashboardBackend`` over PostgreSQL (production mode).

Reads Phase 3-7 tables through repositories; writes only user state (saved / dismissed /
feedback / production tracking / profile / alert read flags) and generation runs.
Everything shown is what is stored: a missing series, forecast or score is returned as
missing with its reason, never as a substitute number. Synthetic records are excluded
unless the backend was created for tests with ``include_synthetic=True``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from trend_agent import __version__
from trend_agent.config import Settings, TrendsAccessMode
from trend_agent.db.monitoring_models import SeriesRow
from trend_agent.db.recommendation_models import VideoRecommendationRow
from trend_agent.db.repositories.forecasts import ForecastRepository
from trend_agent.db.repositories.recommendations import (
    DashboardReadRepository,
    RecommendationRepository,
)
from trend_agent.db.repositories.surge_events import SurgeEventRepository
from trend_agent.db.repositories.time_series import SeriesSummary, TimeSeriesRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.recommendations import backend as be
from trend_agent.recommendations import views
from trend_agent.recommendations.config import RECOMMENDATION_VERSION
from trend_agent.recommendations.service import RecommendationService
from trend_agent.schemas.content_opportunity import OpportunityReport
from trend_agent.schemas.dashboard import (
    AlertView,
    DataClass,
    DataQualityView,
    ForecastView,
    FreshnessView,
    GenerateIn,
    GenerateResult,
    OpportunityDetail,
    OpportunitySummary,
    OverviewView,
    Page,
    ProductionIn,
    ProviderView,
    RecommendationDetail,
    RecommendationSummary,
    SavedIdea,
    SeriesPoint,
    SurgeEpisodeView,
    SystemStatusView,
    TrendDetail,
    TrendSeriesView,
    TrendSummary,
    UserState,
)
from trend_agent.schemas.forecast import ForecastOutput
from trend_agent.schemas.recommendation import (
    CreatorProfile,
    EvidenceRef,
    FeedbackType,
    ProductionStatus,
)
from trend_agent.utils.time import Clock, utc_now

_URGENCY_ORDER = ["PUBLISH_SOON", "WITHIN_DAYS", "FLEXIBLE", "UNKNOWN", "LIKELY_TOO_LATE"]
_MEASUREMENT_PREFERENCE = {
    "direct_youtube_search_interest": 0,
    "external_search_interest": 1,
    "search_suggestion_presence": 2,
    "video_activity": 3,
    "community_activity": 4,
    "official_event_signal": 5,
    "synthetic_test_observation": 6,
}
_OK = ("APPROVED", "APPROVED_WITH_UNCERTAINTY")


def row_summary(row: VideoRecommendationRow, user: UserState | None) -> RecommendationSummary:
    """List projection built from the indexed columns (no full-document validation)."""
    prod = row.concept.get("production", {}) if isinstance(row.concept, dict) else {}
    return RecommendationSummary(
        id=str(row.id),
        opportunity_id=row.opportunity_key,
        title=row.title,
        query=row.query,
        category=row.category,
        video_format=row.video_format,
        format_label=str(row.concept.get("format_label") or row.video_format),
        intent=row.intent,
        sub_intent=row.sub_intent,
        score=row.composite_score,
        rank=row.rank,
        status=row.status,
        research=row.research,
        suppressed=row.suppressed,
        suppression_reason=row.suppression_reason,
        urgency=row.urgency,
        hours_low=row.hours_low,
        hours_high=row.hours_high,
        production_profile=row.production_profile,
        complexity=str(prod.get("complexity", "UNKNOWN")),
        demand_basis=row.demand_basis,
        forecast_available=row.forecast_available,
        synthetic=row.is_synthetic,
        saved=bool(user and user.saved),
        dismissed=bool(user and user.dismissed),
        production_status=user.production.status.value if user and user.production else None,
        generated_at=row.generated_at,
    )


def _latest_per_horizon(rows: Sequence[Any]) -> list[ForecastOutput]:
    seen: set[int] = set()
    out = []
    for r in rows:  # newest first
        if r.horizon_hours in seen:
            continue
        seen.add(r.horizon_hours)
        out.append(ForecastOutput.model_validate(r.output))
    return out


class DatabaseBackend:
    mode = "database"

    def __init__(
        self,
        service: RecommendationService,
        factory: SessionFactory,
        settings: Settings,
        *,
        clock: Clock = utc_now,
        trend_days: int = 30,
    ) -> None:
        self.service = service
        self._factory = factory
        self.settings = settings
        self._clock = clock
        self.loaded = service.loaded
        self.include_synthetic = service.include_synthetic
        self.synthetic = service.include_synthetic
        self.user = service.user_key
        self.trend_days = trend_days

    # ---------------------------------------------------------------- helpers
    def _since_forecast(self) -> datetime:
        return self._clock() - timedelta(hours=self.loaded.config.generation.max_forecast_age_hours)

    def _opp_summary(
        self,
        rep: OpportunityReport,
        forecasts: Sequence[ForecastOutput],
        recs: Sequence[VideoRecommendationRow],
    ) -> OpportunitySummary:
        base = views.opportunity_summary(rep, forecasts, [])
        ranked = [r for r in recs if r.eligible and not r.suppressed]
        urgency = (
            min((r.urgency for r in ranked), key=lambda u: _URGENCY_ORDER.index(u))
            if ranked
            else None
        )
        return base.model_copy(
            update={
                "recommendation_count": len(recs),
                "approved_count": sum(1 for r in recs if r.status in _OK),
                "urgency": urgency,
            }
        )

    def _visible(self, synthetic: bool) -> bool:
        return self.include_synthetic or not synthetic

    def _require(self, rid: str) -> VideoRecommendationRow:
        with transaction(self._factory) as s:
            row = RecommendationRepository(s).get(rid)
        if row is None or not self._visible(row.is_synthetic):
            raise be.NotFoundError(rid)
        return row

    # ------------------------------------------------------------------ reads
    def overview(self) -> OverviewView:
        with transaction(self._factory) as s:
            read, repo = DashboardReadRepository(s), RecommendationRepository(s)
            c = read.counts(include_synthetic=self.include_synthetic)
            oc = read.opportunity_counts(include_synthetic=self.include_synthetic)
            rc = repo.counts(include_synthetic=self.include_synthetic)
            recent = read.recent_opportunities(include_synthetic=self.include_synthetic, limit=8)
            top, _ = repo.search(
                user_key=self.user,
                include_synthetic=self.include_synthetic,
                sort="rank",
                page_size=5,
            )
            users = repo.user_states([str(r.id) for r in top], self.user)
            recent_topics = [
                {
                    "query": o.canonical_query,
                    "category": o.category,
                    "analysed_at": o.updated_at.isoformat(),
                    "status": o.status,
                    "opportunity_id": str(o.opportunity_key),
                }
                for o in recent
            ]
            top_summaries = [row_summary(r, users.get(str(r.id))) for r in top]
        notes: list[str] = []
        if not oc:
            notes.append(
                "No content opportunities are stored yet. Run monitoring, detection, analysis "
                "and forecasting first; nothing is estimated in their place. "
                "(`trend-agent dashboard serve --demo` shows the synthetic demonstration.)"
            )
        llm = self.service.llm_status()
        if "template" in llm["mode"]:
            notes.append(f"Concept generation: {llm['mode']}.")
        notes.append(
            "Scores rank research priority under versioned, experimental weights; they are "
            "not probabilities that a video will succeed."
        )
        return OverviewView(
            mode=self.mode,
            synthetic=self.synthetic,
            monitored_queries=c["monitored_queries"],
            active_surges=c["active_surges"],
            opportunities=oc.get("candidate", 0),
            approved_recommendations=rc["ranked"],
            research_candidates=rc["research"],
            recent_topics=recent_topics,
            providers=self.providers(),
            freshness=self.data_quality().items,
            category_distribution=rc["by_category"],
            top_recommendations=top_summaries,
            notes=notes,
        )

    def opportunities(
        self,
        *,
        category: str | None = None,
        region: str | None = None,
        intent: str | None = None,
        surge: str | None = None,
        status: str | None = None,
        verification: str | None = None,
        min_score: float | None = None,
        max_score: float | None = None,
        max_age_hours: float | None = None,
        q: str | None = None,
        sort: str = "score",
        order: str = "desc",
        page: int = 1,
        page_size: int = 25,
        **_: Any,
    ) -> Page[OpportunitySummary]:
        now = self._clock()
        in_memory = sort == "breakout"
        with transaction(self._factory) as s:
            read, repo = DashboardReadRepository(s), RecommendationRepository(s)
            rows, total = read.opportunities(
                include_synthetic=self.include_synthetic,
                category=category,
                region=region,
                intent=intent,
                surge=surge,
                status=status,
                verification=verification,
                min_score=min_score,
                max_score=max_score,
                since=now - timedelta(hours=max_age_hours) if max_age_hours else None,
                q=q,
                sort=sort,
                order=order,
                page=None if in_memory else page,
                page_size=page_size,
            )
            reports = [OpportunityReport.model_validate(r.report) for r in rows]
            fc = read.forecasts_for_queries(
                [r.canonical_query for r in reports],
                include_synthetic=self.include_synthetic,
                since=self._since_forecast(),
            )
            recs: dict[str, list[VideoRecommendationRow]] = {}
            for r in repo.for_opportunities(
                [x.opportunity_id for x in reports], include_synthetic=self.include_synthetic
            ):
                recs.setdefault(r.opportunity_key, []).append(r)
            items = [
                self._opp_summary(
                    rep,
                    _latest_per_horizon(fc.get(rep.canonical_query.lower(), [])),
                    recs.get(rep.opportunity_id, []),
                )
                for rep in reports
            ]
        if in_memory:
            return be.query_opportunities(
                items, sort=sort, order=order, page=page, page_size=page_size
            )
        return Page[OpportunitySummary](
            items=items, total=total, page=max(1, page), page_size=page_size
        )

    def opportunity(self, oid: str) -> OpportunityDetail | None:
        with transaction(self._factory) as s:
            read, repo = DashboardReadRepository(s), RecommendationRepository(s)
            found = read.opportunity(oid)
            if found is None or not self._visible(found[0].is_synthetic):
                return None
            rep = OpportunityReport.model_validate(found[0].report)
            fc = _latest_per_horizon(
                read.forecasts_for_queries(
                    [rep.canonical_query],
                    include_synthetic=self.include_synthetic,
                    since=self._since_forecast(),
                ).get(rep.canonical_query.lower(), [])
            )
            rows = repo.for_opportunities(
                [rep.opportunity_id], include_synthetic=self.include_synthetic
            )
            users = repo.user_states([str(r.id) for r in rows], self.user)
            detail = views.opportunity_detail(rep, fc, [], users)
            summaries = [row_summary(r, users.get(str(r.id))) for r in rows]
        return detail.model_copy(
            update={
                "summary": self._opp_summary(rep, fc, rows),
                "recommendations": sorted(
                    summaries, key=lambda r: (r.rank is None, r.rank or 0, -(r.score or 0))
                ),
            }
        )

    # --------------------------------------------------------------- trends
    @staticmethod
    def _main(series: Sequence[SeriesSummary]) -> SeriesSummary | None:
        if not series:
            return None
        return sorted(
            series,
            key=lambda x: (
                x.is_proxy,
                _MEASUREMENT_PREFERENCE.get(x.effective_category.value, 9),
                -(x.with_value or 0),
            ),
        )[0]

    def trends(
        self,
        *,
        category: str | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = 25,
        **_: Any,
    ) -> Page[TrendSummary]:
        now = self._clock()
        with transaction(self._factory) as s:
            read = DashboardReadRepository(s)
            rows, total = read.targets(
                include_synthetic=self.include_synthetic,
                category=category,
                q=q,
                page=page,
                page_size=page_size,
            )
            tids = [t.id for t, _ in rows]
            sums = TimeSeriesRepository(s).series_summaries(target_ids=tids) if tids else []
            by_target: dict[int, list[SeriesSummary]] = {}
            for x in sums:
                if x.target_id is not None:
                    by_target.setdefault(x.target_id, []).append(x)
            latest = read.latest_values([x.series_id for x in sums])
            stages = read.latest_episode_stage(tids, include_synthetic=self.include_synthetic)
            opps = read.latest_opportunity_for_queries(
                [t.query_id for t, _ in rows], include_synthetic=self.include_synthetic
            )
            items = [
                self._trend_summary(
                    t, query, self._main(by_target.get(t.id, [])), latest, stages, opps, now
                )
                for t, query in rows
            ]
        return Page[TrendSummary](items=items, total=total, page=max(1, page), page_size=page_size)

    @staticmethod
    def _trend_summary(
        t: Any,
        query: str,
        main: SeriesSummary | None,
        latest: dict[int, tuple[float, datetime]],
        stages: dict[int, str],
        opps: dict[int, str],
        now: datetime,
    ) -> TrendSummary:
        lv = latest.get(main.series_id) if main else None
        meas = main.effective_category.value if main else t.measurement_category.value
        return TrendSummary(
            id=str(t.id),
            query=query,
            category=getattr(t.category, "value", t.category),
            provider=main.provider_name if main else (t.provider_in_use or "none"),
            measurement=meas,
            data_class=(
                views.data_class(meas, main.is_synthetic) if main and lv else DataClass.MISSING
            ),
            granularity=main.granularity if main else "none",
            latest_value=lv[0] if lv else None,
            latest_at=lv[1] if lv else None,
            freshness_hours=round((now - lv[1]).total_seconds() / 3600, 1) if lv else None,
            surge_stage=stages.get(t.id),
            opportunity_id=opps.get(t.query_id),
            synthetic=t.is_synthetic,
        )

    def trend(self, tid: str) -> TrendDetail | None:
        if not tid.isdigit():
            return None
        now = self._clock()
        with transaction(self._factory) as s:
            read, ts = DashboardReadRepository(s), TimeSeriesRepository(s)
            found = read.target(int(tid))
            if found is None or not self._visible(found[0].is_synthetic):
                return None
            t, query = found
            sums = ts.series_summaries(target_ids=[t.id])
            series: list[TrendSeriesView] = []
            for x in sorted(
                sums, key=lambda x: _MEASUREMENT_PREFERENCE.get(x.effective_category.value, 9)
            ):
                srow = s.get(SeriesRow, x.series_id)
                prop = getattr(srow.search_property, "value", "unknown") if srow else "unknown"
                pts = ts.points(
                    series_ids=[x.series_id],
                    start=now - timedelta(days=self.trend_days),
                    current_only=True,
                    limit=5000,
                )
                meas = x.effective_category.value
                series.append(
                    views.series_view(
                        series_id=x.series_id,
                        query=query,
                        provider=x.provider_name,
                        measurement=meas,
                        granularity=x.granularity,
                        unit=x.unit,
                        region=x.region,
                        scale_type=x.scale_type,
                        synthetic=x.is_synthetic,
                        points=[
                            SeriesPoint(
                                period_start=p.period_start,
                                period_end=p.period_end,
                                available_at=p.first_available_at or p.collected_at,
                                value=p.value,
                                quality=p.sample_quality.value,
                                revision=p.revision,
                            )
                            for p in pts
                        ],
                        now=now,
                        label=f"{meas.replace('_', ' ')} - {x.provider_name} ({prop})",
                    )
                )
            episodes = [
                SurgeEpisodeView(
                    id=e.id,
                    query=query,
                    start=e.episode_start,
                    end=e.resolved_at,
                    detected_at=e.first_detected_at,
                    stage=getattr(e.status, "value", str(e.status)),
                    classification=getattr(
                        e.evidence_classification, "value", str(e.evidence_classification)
                    ),
                    severity=e.severity,
                    measurement=getattr(e.measurement_category, "value", None),
                    statement=e.explanation,
                    synthetic=e.is_synthetic,
                )
                for e in SurgeEventRepository(s).events(
                    include_synthetic=self.include_synthetic, target_ids=[t.id], limit=20
                )
            ]
            fc = _latest_per_horizon(
                ForecastRepository(s).latest(
                    query=query, include_synthetic=self.include_synthetic, limit=24
                )
            )
            opp = read.latest_opportunity_for_queries(
                [t.query_id], include_synthetic=self.include_synthetic
            ).get(t.query_id)
            evidence: dict[str, EvidenceRef] = {}
            if opp:
                for r in RecommendationRepository(s).for_opportunities(
                    [opp], include_synthetic=self.include_synthetic
                ):
                    for e in r.concept.get("evidence", []):
                        ref = EvidenceRef.model_validate(e)
                        evidence.setdefault(ref.evidence_id, ref)
            latest = read.latest_values([x.series_id for x in sums])
            stages = read.latest_episode_stage([t.id], include_synthetic=self.include_synthetic)
            summary = self._trend_summary(
                t, query, self._main(sums), latest, stages, {t.query_id: opp} if opp else {}, now
            )
        limitations = [
            "Each series is a relative index of its own source and search property; series "
            "are drawn on separate charts and never combined on one scale.",
            "Google Web Search interest is not YouTube Search interest.",
        ]
        if not series:
            limitations.append("No observations are stored for this query yet.")
        if not fc:
            limitations.append(
                "No valid numerical forecast is stored for this query: the forecast is shown "
                "as unavailable (not as zero growth)."
            )
        return TrendDetail(
            summary=summary,
            series=series,
            episodes=episodes,
            forecasts=[views.forecast_view(f) for f in fc],
            evidence=list(evidence.values())[:40],
            announcements=[],
            limitations=limitations,
        )

    def forecast(self, fid: str) -> ForecastView | None:
        with transaction(self._factory) as s:
            row = DashboardReadRepository(s).forecast(fid)
            if row is None or not self._visible(row.is_synthetic):
                return None
            return views.forecast_view(ForecastOutput.model_validate(row.output))

    # ------------------------------------------------------- recommendations
    def recommendations(self, **f: Any) -> Page[RecommendationSummary]:
        page, size = int(f.get("page", 1)), int(f.get("page_size", 25))
        with transaction(self._factory) as s:
            repo = RecommendationRepository(s)
            rows, total = repo.search(
                user_key=self.user, include_synthetic=self.include_synthetic, **f
            )
            users = repo.user_states([str(r.id) for r in rows], self.user)
            items = [row_summary(r, users.get(str(r.id))) for r in rows]
        return Page[RecommendationSummary](
            items=items, total=total, page=max(1, page), page_size=size
        )

    def recommendation(self, rid: str, *, viewed: bool = False) -> RecommendationDetail | None:
        from trend_agent.schemas.recommendation import RecommendationOutput

        now = self._clock()
        with transaction(self._factory) as s:
            repo, read = RecommendationRepository(s), DashboardReadRepository(s)
            row = repo.get(rid)
            if row is None or not self._visible(row.is_synthetic):
                return None
            if viewed:
                repo.add_feedback(rid, self.user, FeedbackType.VIEWED.value, None, None, now)
            rec = RecommendationOutput.model_validate(row.concept)
            state = repo.user_state(rid, self.user)
            found = read.opportunity(row.opportunity_key)
            opp = None
            if found is not None:
                rep = OpportunityReport.model_validate(found[0].report)
                fc = _latest_per_horizon(
                    read.forecasts_for_queries(
                        [rep.canonical_query],
                        include_synthetic=self.include_synthetic,
                        since=self._since_forecast(),
                    ).get(rep.canonical_query.lower(), [])
                )
                opp = self._opp_summary(
                    rep,
                    fc,
                    repo.for_opportunities(
                        [rep.opportunity_id], include_synthetic=self.include_synthetic
                    ),
                )
        return RecommendationDetail(recommendation=rec, user=state, opportunity=opp)

    def generate(self, req: GenerateIn) -> GenerateResult:
        cap = self.loaded.config.api.max_generate_opportunities
        return self.service.generate(
            category=req.category,
            all_categories=req.all,
            max_opportunities=min(req.max_opportunities, cap),
            dry_run=req.dry_run,
            trigger="api",
        )

    # ------------------------------------------------------------ user writes
    def _state(self, rid: str) -> UserState:
        with transaction(self._factory) as s:
            return RecommendationRepository(s).user_state(rid, self.user)

    def save(self, rid: str, notes: str | None) -> UserState:
        self._require(rid)
        now = self._clock()
        with transaction(self._factory) as s:
            repo = RecommendationRepository(s)
            repo.set_saved_state(rid, self.user, "SAVED", notes, now)
            repo.add_feedback(rid, self.user, FeedbackType.SAVED.value, None, None, now)
            if repo.production(rid, self.user) is None:
                repo.set_production(
                    rid,
                    self.user,
                    status=ProductionStatus.IDEA,
                    planned=None,
                    actual=None,
                    video_id=None,
                    notes=None,
                    now=now,
                )
        return self._state(rid)

    def unsave(self, rid: str) -> UserState:
        self._require(rid)
        with transaction(self._factory) as s:
            RecommendationRepository(s).set_saved_state(rid, self.user, None, None, self._clock())
        return self._state(rid)

    def dismiss(self, rid: str) -> UserState:
        self._require(rid)
        now = self._clock()
        with transaction(self._factory) as s:
            repo = RecommendationRepository(s)
            repo.set_saved_state(rid, self.user, "DISMISSED", None, now)
            repo.add_feedback(rid, self.user, FeedbackType.DISMISSED.value, None, None, now)
        return self._state(rid)

    def feedback(
        self, rid: str, ftype: FeedbackType, value: str | None, notes: str | None
    ) -> UserState:
        self._require(rid)
        with transaction(self._factory) as s:
            RecommendationRepository(s).add_feedback(
                rid, self.user, ftype.value, value, notes, self._clock()
            )
        return self._state(rid)

    def production(self, rid: str, req: ProductionIn) -> UserState:
        self._require(rid)
        now = self._clock()
        with transaction(self._factory) as s:
            repo = RecommendationRepository(s)
            cur = repo.production(rid, self.user)
            before = ProductionStatus(cur.status) if cur else None
            be.check_transition(before, req)
            repo.set_production(
                rid,
                self.user,
                status=req.status,
                planned=req.planned_publication_time,
                actual=req.actual_publication_time,
                video_id=req.published_video_id,
                notes=req.notes,
                now=now,
            )
            # production milestones are recorded as feedback events (outcomes are never
            # assumed: PUBLISHED records that a video exists, not that it succeeded)
            event = {
                ProductionStatus.PUBLISHED: FeedbackType.PUBLISHED,
                ProductionStatus.ABANDONED: FeedbackType.ABANDONED,
                ProductionStatus.RECORDING: FeedbackType.PRODUCED,
            }.get(req.status)
            if event is not None and before is not req.status:
                repo.add_feedback(rid, self.user, event.value, req.published_video_id, None, now)
        return self._state(rid)

    def saved(
        self, *, status: str | None = None, category: str | None = None, **_: Any
    ) -> list[SavedIdea]:
        with transaction(self._factory) as s:
            repo = RecommendationRepository(s)
            pairs = repo.saved(
                self.user,
                status=status,
                category=category,
                include_synthetic=self.include_synthetic,
            )
            users = repo.user_states([str(v.id) for v, _ in pairs], self.user)
            return [
                SavedIdea(
                    recommendation=row_summary(v, users.get(str(v.id))),
                    saved_at=sv.saved_at or sv.updated_at,
                    notes=sv.notes,
                    production=(u.production if (u := users.get(str(v.id))) else None),
                )
                for v, sv in pairs
            ]

    def profile(self) -> CreatorProfile:
        return self.service.profile()

    def update_profile(self, profile: CreatorProfile) -> CreatorProfile:
        return self.service.update_profile(profile)

    def alerts(self, *, unread_only: bool = False) -> list[AlertView]:
        with transaction(self._factory) as s:
            rows = RecommendationRepository(s).alerts(
                unread_only=unread_only, include_synthetic=self.include_synthetic
            )
            return [
                AlertView(
                    id=str(n.id),
                    event_type=n.event_type,
                    severity=n.severity,
                    title=n.title,
                    body=n.body,
                    created_at=n.created_at,
                    read=n.read,
                    recommendation_id=n.recommendation_id,
                    opportunity_id=n.opportunity_key,
                    synthetic=n.is_synthetic,
                    deliveries=[
                        {
                            "channel": d.channel,
                            "status": d.status,
                            "attempts": d.attempts,
                            "last_error": d.detail,
                            "at": d.last_attempt_at.isoformat() if d.last_attempt_at else None,
                        }
                        for d in deliveries
                    ],
                )
                for n, deliveries in rows
            ]

    def mark_alert_read(self, aid: str) -> AlertView | None:
        with transaction(self._factory) as s:
            row = RecommendationRepository(s).mark_alert_read(aid)
            if row is None or not self._visible(row.is_synthetic):
                return None
            rid = str(row.id)
        return next((a for a in self.alerts() if a.id == rid), None)

    # ---------------------------------------------------------------- system
    def providers(self) -> list[ProviderView]:
        st = self.settings
        out = [
            ProviderView(
                name="youtube_data_api",
                kind="metadata_and_search_results",
                status="ok" if st.youtube_enabled and st.youtube_configured else "unconfigured",
                configured=st.youtube_enabled and st.youtube_configured,
                detail=(
                    "API key configured (server side only)"
                    if st.youtube_configured
                    else "TREND_AGENT_YOUTUBE_API_KEY is not set: stored results only"
                ),
            ),
            ProviderView(
                name="google_trends",
                kind="search_interest",
                status=(
                    "unconfigured"
                    if st.google_trends_access_mode is TrendsAccessMode.DISABLED
                    else "unknown"
                ),
                configured=st.google_trends_access_mode is not TrendsAccessMode.DISABLED,
                detail=(
                    "disabled (no access mode configured)"
                    if st.google_trends_access_mode is TrendsAccessMode.DISABLED
                    else f"access mode {st.google_trends_access_mode.value}"
                ),
            ),
            ProviderView(
                name="anthropic_llm",
                kind="concept_generation",
                status=(
                    "ok"
                    if self.loaded.config.llm.enabled and st.anthropic_api_key
                    else "unconfigured"
                ),
                configured=bool(self.loaded.config.llm.enabled and st.anthropic_api_key),
                detail=self.service.llm_status()["mode"],
            ),
        ]
        health_map = {"healthy": "ok", "degraded": "degraded", "unhealthy": "unavailable"}
        with transaction(self._factory) as s:
            for h in DashboardReadRepository(s).provider_health():
                status = getattr(h.status, "value", str(h.status))
                out.append(
                    ProviderView(
                        name=h.provider_name,
                        kind="collector",
                        status=health_map.get(status, "unknown"),
                        configured=True,
                        detail=(
                            f"{h.consecutive_failures} consecutive failure(s): "
                            f"{h.last_error_category or 'unknown error'}"
                            if h.consecutive_failures
                            else "collecting"
                        ),
                        last_success=h.last_success_at,
                    )
                )
        return out

    def data_quality(self) -> DataQualityView:
        now = self._clock()
        with transaction(self._factory) as s:
            rows = DashboardReadRepository(s).freshness(include_synthetic=self.include_synthetic)
        items = []
        for provider, meas, prop, n, last in rows:
            age = (now - last).total_seconds() / 3600 if last else None
            status = (
                "synthetic"
                if meas == "synthetic_test_observation"
                else "missing"
                if age is None
                else "ok"
                if age <= 24
                else "stale"
            )
            items.append(
                FreshnessView(
                    provider=provider,
                    measurement=f"{meas} ({prop})",
                    series=n,
                    latest_observation=last,
                    age_hours=round(age, 1) if age is not None else None,
                    status=status,
                )
            )
        notes = [
            "Age of the latest stored observation with a value, per provider, measurement "
            "and search property. Relative indices are not comparable across providers."
        ]
        if not items:
            notes.append("No time series are stored yet.")
        return DataQualityView(items=items, notes=notes)

    def system_status(self) -> SystemStatusView:
        with transaction(self._factory) as s:
            schema = DashboardReadRepository(s).schema_version()
            rc = RecommendationRepository(s).counts(include_synthetic=self.include_synthetic)
            oc = DashboardReadRepository(s).opportunity_counts(
                include_synthetic=self.include_synthetic
            )
            unread = len(
                RecommendationRepository(s).alerts(
                    unread_only=True, include_synthetic=self.include_synthetic
                )
            )
        return SystemStatusView(
            mode=self.mode,
            synthetic=self.synthetic,
            version=__version__,
            database=f"ok (schema {schema or 'unknown'})",
            components={
                "recommendation_engine": "ok",
                "verification_gate": "ok (application code approves; no LLM self-approval)",
                "content_analysis": "stored reports",
                "forecasting": "stored forecasts",
            },
            llm=self.service.llm_status(),
            versions={
                "recommendation": RECOMMENDATION_VERSION,
                "configuration": self.loaded.version_tag,
                "ranking": self.loaded.config.ranking.version,
            },
            counts={
                "opportunities": sum(oc.values()),
                "candidate_opportunities": oc.get("candidate", 0),
                "ranked_recommendations": rc["ranked"],
                "research_candidates": rc["research"],
                "suppressed": rc["suppressed"],
                "unread_alerts": unread,
            },
            notifications=self.service.notifier.status(),
            generated_at=self._clock(),
        )
