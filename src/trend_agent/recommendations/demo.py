"""Isolated SYNTHETIC demonstration mode (fictional world; never touches the database).

The demo runs the real pipeline end to end on fictional fixtures:

* Phase 5 content analyses of the fictional world (``config/content_analysis``): a new AI
  coding model (CodeForge 2, installation intent), a limited-time game season (Skyforge Arena
  Season 8, item-location intent), a framework major version (Pyxis 5, migration intent),
  plus negative cases (a rumoured game item, proxy-only demand, no evidence);
* Phase 6 forecasts from synthetic series shaped like each story (launch, limited event,
  gradual adoption) with synthetic-trained models;
* the Phase 7 engine, verification gate, ranking and diversity selection.

Everything is labelled synthetic. The snapshot is cached as JSON (keyed by configuration
and version) so the dashboard starts quickly; user actions in demo mode live in memory only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from trend_agent import __version__
from trend_agent.content_analysis import synthetic as ca_syn
from trend_agent.content_analysis.config import load_content_config
from trend_agent.detection.config import load_detection_config
from trend_agent.forecasting import synthetic as fsyn
from trend_agent.forecasting.config import FORECAST_VERSION, load_forecasting_config
from trend_agent.forecasting.prediction import ForecastEngine
from trend_agent.recommendations import backend as be
from trend_agent.recommendations import views
from trend_agent.recommendations.config import (
    RECOMMENDATION_VERSION,
    LoadedRecommendationConfig,
    load_recommendation_config,
)
from trend_agent.recommendations.context_builder import EvidenceContext
from trend_agent.recommendations.diversity import select
from trend_agent.recommendations.notifications import (
    EmailChannel,
    InAppChannel,
    MemoryAlertStore,
    NotificationCenter,
    TelegramChannel,
    detect_events,
)
from trend_agent.recommendations.orchestrator import RecommendationEngine
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
from trend_agent.schemas.enums import ScaleType
from trend_agent.schemas.forecast import ForecastOutput
from trend_agent.schemas.monitoring import MeasurementCategory
from trend_agent.schemas.recommendation import (
    CreatorProfile,
    EvidenceRef,
    FeedbackType,
    RecommendationOutput,
    RecommendationStatus,
)
from trend_agent.schemas.verification import CREDIBLE
from trend_agent.verification.config import load_verification_config
from trend_agent.verification.world import load_world

M = MeasurementCategory
DEMO_SCENARIOS = ("A", "B", "C", "G", "I", "J")
_SHAPES: dict[str, dict[str, Any]] = {
    # story -> synthetic series shape (all fictional)
    "A": {
        "events": (fsyn.Event("launch", 24 * 27, 2.4, rise_h=96, decay_h=120),),
        "base": 16,
        "measurement": M.DIRECT_YOUTUBE_SEARCH_INTEREST,
        "proxy": True,
    },
    "B": {
        "events": (fsyn.Event("limited", 24 * 28, 1.8, rise_h=12, length_h=60),),
        "base": 20,
        "measurement": M.DIRECT_YOUTUBE_SEARCH_INTEREST,
        "proxy": False,
    },
    "C": {
        "events": (fsyn.Event("adoption", 24 * 18, 1.1, length_h=24 * 16),),
        "base": 24,
        "measurement": M.EXTERNAL_SEARCH_INTEREST,
        "proxy": False,
    },
    "G": {
        "events": (fsyn.Event("spike", 24 * 28, 0.8, length_h=30),),
        "base": 9,
        "measurement": M.DIRECT_YOUTUBE_SEARCH_INTEREST,
        "proxy": False,
    },
}


class _Snap(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TrendBundle(_Snap):
    key: str
    opportunity_id: str | None
    query: str
    category: str | None
    series: list[TrendSeriesView]
    episodes: list[SurgeEpisodeView]
    announcements: list[dict[str, Any]]
    evidence: list[EvidenceRef]
    limitations: list[str]


class DemoSnapshot(_Snap):
    key: str
    built_at: datetime
    now: datetime
    reports: list[OpportunityReport]
    forecasts: list[ForecastOutput]
    recommendations: list[RecommendationOutput]
    trends: list[TrendBundle]
    skipped: dict[str, list[str]]
    decisions: list[dict[str, Any]]
    usage: dict[str, int]
    seconds: float
    generator: str


def snapshot_key(root: Path) -> str:
    h = hashlib.sha256()
    for name in (
        "recommendations.yaml",
        "forecasting.yaml",
        "content_analysis.yaml",
        "verification.yaml",
        "content_analysis/world.yaml",
        "content_analysis/scenarios.yaml",
    ):
        p = root / name
        if p.is_file():
            h.update(p.read_bytes())
    h.update(f"{__version__}|{RECOMMENDATION_VERSION}|{FORECAST_VERSION}".encode())
    return h.hexdigest()[:16]


def _points(pts: Sequence[Any]) -> list[SeriesPoint]:
    current = {}
    for p in sorted(pts, key=lambda p: (p.period_start, p.revision)):
        current[p.period_start] = p  # latest revision per period
    return [
        SeriesPoint(
            period_start=p.period_start,
            period_end=p.period_end,
            available_at=p.provider_published_at or p.first_available_at,
            value=p.value,
            quality=p.sample_quality.value,
            revision=p.revision,
        )
        for p in current.values()
    ]


def build_snapshot(
    root: Path,
    *,
    forecast_engine: ForecastEngine | None = None,
    scale: float = 0.3,
    workers: int = 2,
) -> DemoSnapshot:
    t0 = time.perf_counter()
    lc, vc = load_content_config(root), load_verification_config(root)
    rc = load_recommendation_config(root)
    world = load_world(root / "content_analysis" / "world.yaml", vc)
    scen = {s["id"]: s for s in ca_syn.load_scenarios(root)}
    if forecast_engine is None:
        from trend_agent.forecasting import scenarios as fscen

        forecast_engine = fscen.prepare(
            load_forecasting_config(root), load_detection_config(root), scale=scale, workers=workers
        ).engine
    engine = RecommendationEngine(rc)
    reports: list[OpportunityReport] = []
    forecasts: list[ForecastOutput] = []
    trends: list[TrendBundle] = []
    recs: list[RecommendationOutput] = []
    skipped: dict[str, list[str]] = {}
    usage: Counter[str] = Counter()
    profile = CreatorProfile()
    for i, key in enumerate(DEMO_SCENARIOS):
        s = scen[key]
        target = world.targets[s["target"]]
        analyzer, _ = ca_syn.analyzer_for(
            world, lc, vc, [ca_syn._video(v) for v in s.get("videos", ())], target.query
        )
        inp = ca_syn.inputs_for(world, s["target"], category=s.get("category"))
        out = asyncio.run(analyzer.analyse(inp))
        report = out.report
        reports.append(report)
        fc: list[ForecastOutput] = []
        series: list[TrendSeriesView] = []
        shape = _SHAPES.get(key)
        if shape is not None:
            spec = fsyn.Spec(
                key=f"demo_{key}",
                seed=7000 + i,
                periods=24 * 30,
                base=shape["base"],
                events=shape["events"],
                measurement=shape["measurement"],
                category=report.category or "ai",
                query=report.canonical_query,
                start=world.now - timedelta(hours=24 * 30),
                provider="synthetic_demo_trends",
            )
            support = {}
            if shape["proxy"]:
                support[M.VIDEO_ACTIVITY] = fsyn.Spec(
                    key=f"demo_{key}_video",
                    seed=7100 + i,
                    periods=24 * 30,
                    base=12,
                    noise=0.15,
                    events=tuple(
                        fsyn.Event(
                            e.kind, e.onset_h - 12, e.gain * 0.9, e.rise_h, e.length_h, e.decay_h
                        )
                        for e in shape["events"]
                    ),
                    measurement=M.VIDEO_ACTIVITY,
                    scale=ScaleType.ABSOLUTE_COUNT,
                    category=report.category or "ai",
                    query=report.canonical_query,
                    start=world.now - timedelta(hours=24 * 30),
                    provider="synthetic_demo_video",
                )
            sb, _ = fsyn.Bundle(spec, support).build(9000 + i, 900_000 + i * 10_000)
            fc = forecast_engine.forecast(sb, world.now)
            forecasts.extend(fc)
            series.append(
                views.series_view(
                    series_id=sb.meta.series_id,
                    query=report.canonical_query,
                    provider=sb.meta.provider,
                    measurement=sb.meta.measurement.value,
                    granularity=sb.meta.granularity.value,
                    unit=sb.meta.unit,
                    region=sb.meta.region,
                    scale_type=sb.meta.scale_type.value,
                    synthetic=True,
                    points=_points(sb.points),
                    now=world.now,
                    label=f"{sb.meta.measurement.value.replace('_', ' ')} (synthetic)",
                )
            )
            for cat, pts in sb.supporting.items():
                series.append(
                    views.series_view(
                        series_id=None,
                        query=report.canonical_query,
                        provider="synthetic_demo_video",
                        measurement=cat.value,
                        granularity="hour",
                        unit="count",
                        region="US",
                        scale_type="absolute_count",
                        synthetic=True,
                        points=_points(pts),
                        now=world.now,
                        label="video activity (proxy signal, synthetic)",
                    )
                )
        records = world.registry.store.get_many(out.bundle.evidence_ids) if out.bundle else {}
        if out.bundle is not None:
            ev = EvidenceContext(
                world.registry,
                vc,
                out.bundle,
                tuple(records.values()),
                inp.catalog,
                inp.stats,
                world.now,
            )
            oc = engine.recommend(report, fc, ev, profile)
            recs.extend(oc.recommendations)
            usage["llm_calls"] += oc.usage.calls
            usage["input_tokens"] += oc.usage.input_tokens
            usage["output_tokens"] += oc.usage.output_tokens
            if oc.skipped:
                skipped[report.opportunity_id] = list(oc.skipped)
        d = report.demand
        trends.append(
            TrendBundle(
                key=f"demo-{key.lower()}",
                opportunity_id=report.opportunity_id,
                query=report.canonical_query,
                category=report.category,
                series=series,
                episodes=[
                    SurgeEpisodeView(
                        id=d.surge_event_id,
                        query=report.canonical_query,
                        start=d.window_start,
                        end=d.window_end,
                        detected_at=d.detected_at,
                        stage=d.alert_stage,
                        classification=d.classification,
                        severity=d.severity,
                        measurement=d.measurement_type,
                        statement=d.statement,
                        synthetic=True,
                    )
                ]
                if d.alert_stage
                else [],
                announcements=[
                    {
                        "evidence_id": r.evidence_id,
                        "publisher": r.publisher,
                        "kind": r.evidence_kind.value,
                        "url": r.canonical_url,
                        "published_at": r.published_at.isoformat() if r.published_at else None,
                        "text": (r.excerpt or "")[:400],
                    }
                    for r in records.values()
                    if r.trust_level in CREDIBLE
                    and r.evidence_kind.value in ("official_announcement", "code_release")
                ],
                evidence=[
                    EvidenceRef(
                        evidence_id=r.evidence_id,
                        kind=r.evidence_kind.value,
                        trust=r.trust_level.value,
                        publisher=r.publisher,
                        url=r.canonical_url,
                        published_at=r.published_at,
                    )
                    for r in records.values()
                ][:30],
                limitations=["SYNTHETIC demonstration data (fictional).", *list(d.limitations)],
            )
        )
    sel = select(recs, rc, limit=None)
    return DemoSnapshot(
        key=snapshot_key(root),
        built_at=datetime.now(world.now.tzinfo),
        now=world.now,
        reports=reports,
        forecasts=forecasts,
        recommendations=[*sel.ranked, *sel.suppressed, *sel.research],
        trends=trends,
        skipped=skipped,
        decisions=sel.decisions,
        usage=dict(usage),
        seconds=round(time.perf_counter() - t0, 2),
        generator=engine.generator.name,
    )


def load_or_build(root: Path, cache: Path | None, **kw: Any) -> DemoSnapshot:
    key = snapshot_key(root)
    if cache is not None and cache.is_file():
        try:
            data = json.loads(cache.read_text("utf-8"))
            if data.get("key") == key:
                return DemoSnapshot.model_validate(data)
        except (ValueError, OSError):
            pass
    snap = build_snapshot(root, **kw)
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(snap.model_dump_json(), "utf-8")
        tmp.replace(cache)
    return snap


# ------------------------------------------------------------------ backend
class DemoBackend:
    """Serves a demo snapshot. Writes go to memory only (never to the database)."""

    mode = "demo"
    synthetic = True

    def __init__(self, snap: DemoSnapshot, loaded: LoadedRecommendationConfig) -> None:
        self.snap = snap
        self.loaded = loaded
        self.now = snap.now
        self.users = be.MemoryUserState()
        self._profile = CreatorProfile()
        self.alert_store = MemoryAlertStore()
        self.notifier = NotificationCenter(
            [
                InAppChannel(),
                EmailChannel(
                    host=None, port=587, username=None, password=None, sender=None, recipient=None
                ),
                TelegramChannel(token=None, chat_id=None),
            ],
            self.alert_store,
            loaded,
            demo_mode=True,
        )
        self.recs = {r.recommendation_id: r for r in snap.recommendations}
        self.reports = {r.opportunity_id: r for r in snap.reports}
        self.forecasts = {f.prediction_id: f for f in snap.forecasts}
        self.notifier.dispatch(
            detect_events(snap.recommendations, snap.reports, loaded),
            profile=self._profile,
            now=self.now,
        )

    # ------------------------------------------------------------ helpers
    def _fc(self, query: str) -> list[ForecastOutput]:
        return [f for f in self.snap.forecasts if f.query == query]

    def _recs_for(self, oid: str) -> list[RecommendationOutput]:
        return [r for r in self.snap.recommendations if r.opportunity_id == oid]

    def _user(self, rid: str) -> UserState | None:
        rec = self.users.records.get(rid)
        return rec.state() if rec else None

    def _summaries(self) -> list[RecommendationSummary]:
        return [
            views.rec_summary(r, self._user(r.recommendation_id)) for r in self.snap.recommendations
        ]

    def _opps(self) -> list[OpportunitySummary]:
        return [
            views.opportunity_summary(
                r, self._fc(r.canonical_query), self._recs_for(r.opportunity_id)
            )
            for r in self.snap.reports
        ]

    def _require(self, rid: str) -> RecommendationOutput:
        r = self.recs.get(rid)
        if r is None:
            raise be.NotFoundError(rid)
        return r

    # ------------------------------------------------------------- reads
    def overview(self) -> OverviewView:
        recs = self._summaries()
        ranked = [r for r in recs if not r.research and not r.suppressed]
        return OverviewView(
            mode=self.mode,
            synthetic=True,
            monitored_queries=len(self.snap.trends),
            active_surges=sum(
                1
                for t in self.snap.trends
                for e in t.episodes
                if e.stage in ("candidate", "confirmed")
            ),
            opportunities=sum(1 for r in self.snap.reports if r.status.value == "candidate"),
            approved_recommendations=len(ranked),
            research_candidates=sum(1 for r in recs if r.research),
            recent_topics=[
                {
                    "query": r.canonical_query,
                    "category": r.category,
                    "analysed_at": r.analysed_at.isoformat(),
                    "status": r.status.value,
                    "opportunity_id": r.opportunity_id,
                }
                for r in sorted(self.snap.reports, key=lambda r: r.analysed_at, reverse=True)
            ],
            providers=self.providers(),
            freshness=self.data_quality().items,
            category_distribution=dict(Counter(r.category or "other" for r in ranked)),
            top_recommendations=sorted(ranked, key=lambda r: r.rank or 999)[:5],
            notes=[
                "SYNTHETIC DEMONSTRATION: fictional products, games and data. Nothing here "
                "is a real recommendation or real search demand."
            ],
        )

    def opportunities(self, **f: Any) -> Page[OpportunitySummary]:
        return be.query_opportunities(self._opps(), now=self.now, **f)

    def opportunity(self, oid: str) -> OpportunityDetail | None:
        rep = self.reports.get(oid)
        if rep is None:
            return None
        users = {
            r.recommendation_id: s
            for r in self._recs_for(oid)
            if (s := self._user(r.recommendation_id)) is not None
        }
        return views.opportunity_detail(
            rep, self._fc(rep.canonical_query), self._recs_for(oid), users
        )

    def _trend_summary(self, t: TrendBundle) -> TrendSummary:
        main = t.series[0] if t.series else None
        obs = [p for p in main.points if p.value is not None] if main else []
        return TrendSummary(
            id=t.key,
            query=t.query,
            category=t.category,
            provider=main.provider if main else "none",
            measurement=main.measurement if main else "none",
            data_class=main.data_class if main else DataClass.MISSING,
            granularity=main.granularity if main else "none",
            latest_value=obs[-1].value if obs else None,
            latest_at=obs[-1].period_end if obs else None,
            freshness_hours=main.freshness_hours if main else None,
            surge_stage=t.episodes[0].stage if t.episodes else None,
            opportunity_id=t.opportunity_id,
            synthetic=True,
        )

    def trends(
        self,
        *,
        category: str | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = 25,
        **_: Any,
    ) -> Page[TrendSummary]:
        items = [self._trend_summary(t) for t in self.snap.trends]
        if category:
            items = [t for t in items if t.category == category]
        if q:
            items = [t for t in items if q.lower() in t.query.lower()]
        return be.paginate(items, page, page_size)

    def trend(self, tid: str) -> TrendDetail | None:
        t = next((t for t in self.snap.trends if t.key == tid), None)
        if t is None:
            return None
        return TrendDetail(
            summary=self._trend_summary(t),
            series=t.series,
            episodes=t.episodes,
            forecasts=[views.forecast_view(f) for f in self._fc(t.query)],
            evidence=t.evidence,
            announcements=t.announcements,
            limitations=t.limitations,
        )

    def forecast(self, fid: str) -> ForecastView | None:
        f = self.forecasts.get(fid)
        return views.forecast_view(f) if f else None

    def recommendations(self, **f: Any) -> Page[RecommendationSummary]:
        return be.query_recommendations(self._summaries(), **f)

    def recommendation(self, rid: str, *, viewed: bool = False) -> RecommendationDetail | None:
        r = self.recs.get(rid)
        if r is None:
            return None
        if viewed:
            self.users.get(rid).feedback["VIEWED"] += 1
        rep = self.reports.get(r.opportunity_id)
        return RecommendationDetail(
            recommendation=r,
            user=self.users.get(rid).state(),
            opportunity=views.opportunity_summary(
                rep, self._fc(rep.canonical_query), self._recs_for(rep.opportunity_id)
            )
            if rep
            else None,
        )

    # ------------------------------------------------------------ writes (memory only)
    def generate(self, req: GenerateIn) -> GenerateResult:
        recs = [
            r
            for r in self.snap.recommendations
            if req.all or req.category is None or r.category == req.category
        ]
        counts = Counter(r.verification.status.value for r in recs)
        return GenerateResult(
            run_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"demo:{self.snap.key}")),
            considered=len({r.opportunity_id for r in recs}),
            generated=len(recs),
            approved=counts[RecommendationStatus.APPROVED.value]
            + counts[RecommendationStatus.APPROVED_WITH_UNCERTAINTY.value],
            research=counts[RecommendationStatus.RESEARCH_REQUIRED.value],
            rejected=counts[RecommendationStatus.REJECTED.value],
            suppressed=sum(r.suppression.suppressed for r in recs),
            skipped=[f"{k}: {'; '.join(v)}" for k, v in self.snap.skipped.items()],
            llm_calls=0,
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.0,
            seconds=0.0,
            synthetic=True,
            generator=self.snap.generator,
        )

    def save(self, rid: str, notes: str | None) -> UserState:
        self._require(rid)
        return self.users.save(rid, notes, self.now)

    def unsave(self, rid: str) -> UserState:
        self._require(rid)
        return self.users.unsave(rid)

    def dismiss(self, rid: str) -> UserState:
        self._require(rid)
        return self.users.dismiss(rid)

    def feedback(
        self, rid: str, ftype: FeedbackType, value: str | None, notes: str | None
    ) -> UserState:
        self._require(rid)
        return self.users.feedback(rid, ftype, value, notes, self.now)

    def production(self, rid: str, req: ProductionIn) -> UserState:
        self._require(rid)
        return self.users.production(rid, req, self.now)

    def saved(
        self, *, status: str | None = None, category: str | None = None, **_: Any
    ) -> list[SavedIdea]:
        out = []
        for rid, rec in self.users.records.items():
            if rec.saved_at is None or rid not in self.recs:
                continue
            r = self.recs[rid]
            if category and r.category != category:
                continue
            if status and (rec.production is None or rec.production.status.value != status):
                continue
            out.append(
                SavedIdea(
                    recommendation=views.rec_summary(r, rec.state()),
                    saved_at=rec.saved_at,
                    notes=rec.notes,
                    production=rec.production,
                )
            )
        return sorted(out, key=lambda s: s.saved_at, reverse=True)

    def profile(self) -> CreatorProfile:
        return self._profile

    def update_profile(self, profile: CreatorProfile) -> CreatorProfile:
        self._profile = profile
        return profile

    def alerts(self, *, unread_only: bool = False) -> list[AlertView]:
        return [
            a.view()
            for a in sorted(self.alert_store.alerts, key=lambda a: a.created_at, reverse=True)
            if not (unread_only and a.read)
        ]

    def mark_alert_read(self, aid: str) -> AlertView | None:
        for a in self.alert_store.alerts:
            if a.id == aid:
                a.read = True
                return a.view()
        return None

    # -------------------------------------------------------------- system
    def providers(self) -> list[ProviderView]:
        return [
            ProviderView(
                name="synthetic_demo_trends",
                kind="search_interest",
                status="synthetic",
                configured=True,
                detail="fictional series generated for the demo",
            ),
            ProviderView(
                name="synthetic_demo_video",
                kind="video_activity",
                status="synthetic",
                configured=True,
                detail="fictional proxy series",
            ),
            ProviderView(
                name="youtube_data_api",
                kind="metadata",
                status="unconfigured",
                configured=False,
                detail="not used in demo mode",
            ),
            ProviderView(
                name="google_trends",
                kind="search_interest",
                status="unconfigured",
                configured=False,
                detail="not used in demo mode",
            ),
        ]

    def data_quality(self) -> DataQualityView:
        items = []
        for t in self.snap.trends:
            for s in t.series:
                obs = [p for p in s.points if p.value is not None]
                items.append(
                    FreshnessView(
                        provider=s.provider,
                        measurement=s.measurement,
                        series=1,
                        latest_observation=obs[-1].period_end if obs else None,
                        age_hours=s.freshness_hours,
                        status="synthetic" if obs else "missing",
                    )
                )
        return DataQualityView(items=items, notes=["Demo mode: synthetic series only."])

    def system_status(self) -> SystemStatusView:
        return SystemStatusView(
            mode=self.mode,
            synthetic=True,
            version=__version__,
            database="not used (demo mode)",
            components={
                "recommendation_engine": "ok",
                "verification_gate": "ok",
                "forecasting": "synthetic models",
                "content_analysis": "fixtures",
            },
            llm={
                "mode": "template (LLM disabled in demo)",
                "calls": self.snap.usage.get("llm_calls", 0),
                "daily_token_budget": self.loaded.config.llm.daily_token_budget,
            },
            versions={
                "recommendation": RECOMMENDATION_VERSION,
                "configuration": self.loaded.version_tag,
                "snapshot": self.snap.key,
            },
            counts={
                "opportunities": len(self.snap.reports),
                "recommendations": len(self.snap.recommendations),
                "forecasts": len(self.snap.forecasts),
            },
            notifications=self.notifier.status(),
            generated_at=self.snap.built_at,
        )


def demo_backend(root: Path, cache: Path | None = None, **kw: Any) -> DemoBackend:
    loaded = load_recommendation_config(root)
    return DemoBackend(load_or_build(root, cache, **kw), loaded)
