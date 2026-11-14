"""Converters from Phase 4-7 objects to dashboard read models (shared by both backends)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from trend_agent.schemas.content_gap import ContentGap
from trend_agent.schemas.content_opportunity import OpportunityReport
from trend_agent.schemas.dashboard import (
    CompetingVideoView,
    DataClass,
    ForecastPointView,
    ForecastView,
    GapView,
    OpportunityDetail,
    OpportunitySummary,
    RecommendationSummary,
    RequirementView,
    SeriesPoint,
    TrendSeriesView,
    UserState,
)
from trend_agent.schemas.forecast import ForecastOutput
from trend_agent.schemas.recommendation import RecommendationOutput, RecommendationStatus

DEMAND_LABELS = {
    "confirmed_direct_youtube_search_surge": "Confirmed YouTube Search surge",
    "direct_search_candidate": "YouTube Search surge candidate",
    "external_search_surge": "External web-search surge (not YouTube Search)",
    "potential_youtube_demand_opportunity": "Potential demand: proxy signals only",
    "cold_start_growth_candidate": "Cold-start growth candidate (limited history)",
    "watch": "Watch: weak or early signal",
    "insufficient_evidence": "Insufficient demand evidence",
    "manual_no_demand_evidence": "Manual query: no demand evidence",
}

MEASUREMENT_DEFINITIONS = {
    "direct_youtube_search_interest": "Relative YouTube Search interest index from a provider "
    "that measures YouTube Search specifically (not a number of searches)",
    "external_search_interest": "Relative web-search interest outside YouTube (e.g. web search); "
    "never YouTube Search demand",
    "video_activity": "Video publication or attention activity: a proxy signal, not search demand",
    "community_activity": "Community discussion activity: a proxy signal, not search demand",
    "official_event_signal": "Official announcement or event marker",
    "synthetic_test_observation": "Synthetic test data",
}

_PROXY = {"video_activity", "community_activity", "official_event_signal"}


def data_class(measurement: str, synthetic: bool) -> DataClass:
    if synthetic:
        return DataClass.SYNTHETIC
    if measurement in _PROXY:
        return DataClass.PROXY_SIGNAL
    return DataClass.OBSERVED


def forecast_view(f: ForecastOutput) -> ForecastView:
    pe = f.peak_estimate
    return ForecastView(
        prediction_id=f.prediction_id,
        query=f.query,
        horizon_hours=f.forecast_horizon_hours,
        status=f.status.value,
        status_reasons=list(f.status_reasons),
        issued_at=f.prediction_timestamp,
        data_cutoff=f.data_cutoff,
        breakout_probability=f.breakout.probability if f.breakout else None,
        breakout_calibration=f.breakout.calibration_status.value if f.breakout else None,
        continuation_probability=f.continuation.probability if f.continuation else None,
        baseline_index=f.breakout.baseline_index if f.breakout else None,
        threshold_index=f.breakout.threshold_index if f.breakout else None,
        trajectory=[
            ForecastPointView(
                lead_hours=p.lead_hours,
                forecast_time=p.forecast_time,
                value=p.value,
                lower=p.lower,
                upper=p.upper,
                interval_level=p.interval_level,
                method=p.interval_method,
                model=p.model,
            )
            for p in f.trajectory
        ],
        peak=pe.model_dump(mode="json") if pe else None,
        windows=[w.model_dump(mode="json") for w in f.opportunity_windows],
        target_family=f.target_family,
        source_classification=f.source_classification.value,
        is_proxy=f.is_proxy,
        synthetic=f.synthetic,
        model=(f.breakout.model.name if f.breakout and f.breakout.model else None),
        notice=f.notice,
    )


def _best_forecast(forecasts: Sequence[ForecastOutput]) -> ForecastOutput | None:
    order = {24: 0, 72: 1, 6: 2}
    usable = [
        f for f in forecasts if f.status.value in ("READY", "LIMITED_CONFIDENCE", "BASELINE_ONLY")
    ]
    pool = usable or list(forecasts)
    return sorted(pool, key=lambda f: order.get(f.forecast_horizon_hours, 9))[0] if pool else None


def opportunity_summary(
    report: OpportunityReport,
    forecasts: Sequence[ForecastOutput],
    recs: Sequence[RecommendationOutput],
) -> OpportunitySummary:
    f = _best_forecast(forecasts)
    ranked = [r for r in recs if r.ranking.eligible and not r.suppression.suppressed]
    urgency = None
    if ranked:
        order = ["PUBLISH_SOON", "WITHIN_DAYS", "FLEXIBLE", "UNKNOWN", "LIKELY_TOO_LATE"]
        urgency = min((r.production.urgency.value for r in ranked), key=order.index)
    return OpportunitySummary(
        id=report.opportunity_id,
        query=report.canonical_query,
        category=report.category,
        region=report.region,
        language=report.language,
        intent=report.intent.primary_intent.value,
        sub_intent=report.intent.sub_intent.value if report.intent.sub_intent else None,
        demand_basis=report.demand.basis.value,
        demand_label=DEMAND_LABELS.get(report.demand.basis.value, report.demand.basis.value),
        direct_youtube_search=report.demand.basis.value
        in ("confirmed_direct_youtube_search_surge", "direct_search_candidate"),
        surge_stage=report.demand.alert_stage,
        surge_severity=report.demand.severity,
        gap_type=report.primary_gap.gap_type.value if report.primary_gap else None,
        gap_strength=report.primary_gap.evidence_strength.value if report.primary_gap else None,
        forecast_status=f.status.value if f else None,
        breakout_probability=f.breakout.probability if f and f.breakout else None,
        breakout_calibration=f.breakout.calibration_status.value if f and f.breakout else None,
        opportunity_score=report.score.score,
        time_sensitive=report.intent.time_sensitive,
        urgency=urgency,
        evidence_quality=report.evidence_quality.value if report.evidence_quality else None,
        verification_state=report.verification_state,
        status=report.status.value,
        recommendation_count=len(recs),
        approved_count=sum(
            1
            for r in recs
            if r.verification.status
            in (RecommendationStatus.APPROVED, RecommendationStatus.APPROVED_WITH_UNCERTAINTY)
        ),
        last_updated=report.analysed_at,
        synthetic=report.synthetic,
    )


def rec_summary(r: RecommendationOutput, user: UserState | None = None) -> RecommendationSummary:
    return RecommendationSummary(
        id=r.recommendation_id,
        opportunity_id=r.opportunity_id,
        title=r.title,
        query=r.primary_query,
        category=r.category,
        video_format=r.video_format.value,
        format_label=r.format_label,
        intent=r.target_intent,
        sub_intent=r.target_sub_intent,
        score=r.ranking.composite_score,
        rank=r.ranking.rank,
        status=r.verification.status.value,
        research=r.research_hypothesis,
        suppressed=r.suppression.suppressed,
        suppression_reason=r.suppression.reason,
        urgency=r.production.urgency.value,
        hours_low=r.production.estimated_hours_low,
        hours_high=r.production.estimated_hours_high,
        production_profile=r.production.production_profile.value,
        complexity=r.production.complexity.value,
        demand_basis=r.demand.basis,
        forecast_available=r.forecast.available,
        synthetic=r.synthetic,
        saved=bool(user and user.saved),
        dismissed=bool(user and user.dismissed),
        production_status=user.production.status.value if user and user.production else None,
        generated_at=r.generated_at,
    )


def gap_view(g: ContentGap) -> GapView:
    return GapView(
        gap_type=g.gap_type.value,
        statement=g.statement,
        evidence_strength=g.evidence_strength.value,
        verification_status=g.verification_status.value,
        requirement_keys=list(g.requirement_keys),
        limitations=list(g.limitations),
        evidence_ids=list(g.supporting_evidence),
    )


def opportunity_detail(
    report: OpportunityReport,
    forecasts: Sequence[ForecastOutput],
    recs: Sequence[RecommendationOutput],
    users: dict[str, UserState],
) -> OpportunityDetail:
    gap = gap_view

    transcript = {
        v.video_id
        for v in report.relevant_videos
        if v.evidence_tier in ("b_captions", "c_full_content")
    }
    return OpportunityDetail(
        summary=opportunity_summary(report, forecasts, recs),
        demand_statement=report.demand.statement,
        demand_limitations=list(report.demand.limitations),
        intent_entities=[e.model_dump(mode="json") for e in report.intent.entities],
        requirements=[
            RequirementView(
                key=q.requirement_key,
                requirement=q.requirement,
                priority=q.priority,
                coverage=q.best_status.value,
                statement=q.statement,
                covered_by=list(q.covered_by),
                evidence_tier=q.max_tier.value,
            )
            for q in report.requirement_coverage
        ],
        gaps=[gap(g) for g in report.gaps],
        primary_gap=gap(report.primary_gap) if report.primary_gap else None,
        competing_videos=[
            CompetingVideoView(
                video_id=v.video_id,
                url=f"https://www.youtube.com/watch?v={v.video_id}",
                published_at=v.published_at,
                relevance_score=v.relevance_score,
                entity_match=v.entity_match,
                video_format=v.video_format,
                freshness=v.freshness,
                evidence_tier=v.evidence_tier,
                finding_basis="full content (authorised transcript)"
                if v.video_id in transcript
                else "metadata only (title and description)",
            )
            for v in report.relevant_videos
        ],
        supply_scope=report.supply.scope,
        explanation=dict(report.explanation),
        main_uncertainty=report.main_uncertainty,
        coverage_limitations=list(report.coverage_limitations),
        score_components=[c.model_dump(mode="json") for c in report.score.components],
        claims=[c.model_dump(mode="json") for c in report.claims if c.published],
        recommendations=[rec_summary(r, users.get(r.recommendation_id)) for r in recs],
        forecasts=[forecast_view(f) for f in forecasts],
        notice=report.notice,
    )


def series_view(
    *,
    series_id: int | None,
    query: str,
    provider: str,
    measurement: str,
    granularity: str,
    unit: str,
    region: str | None,
    scale_type: str,
    synthetic: bool,
    points: Sequence[SeriesPoint],
    now: datetime,
    label: str | None = None,
) -> TrendSeriesView:
    observed = [p for p in points if p.value is not None]
    fresh = ((now - observed[-1].period_end).total_seconds() / 3600.0) if observed else None
    dc = data_class(measurement, synthetic)
    note = {
        DataClass.PROXY_SIGNAL: "Proxy signal on its own axis; never combined with search "
        "interest.",
        DataClass.SYNTHETIC: "Synthetic test data.",
        DataClass.OBSERVED: "Observed relative index of this source only.",
    }.get(dc, "")
    return TrendSeriesView(
        series_id=series_id,
        label=label or f"{measurement.replace('_', ' ')} ({provider})",
        query=query,
        provider=provider,
        measurement=measurement,
        measurement_definition=MEASUREMENT_DEFINITIONS.get(measurement, measurement),
        data_class=dc,
        granularity=granularity,
        unit=unit,
        region=region,
        scale_type=scale_type,
        synthetic=synthetic,
        points=list(points),
        freshness_hours=round(fresh, 1) if fresh is not None else None,
        note=note,
    )
