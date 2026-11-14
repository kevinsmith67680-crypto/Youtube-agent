"""The dashboard backend interface and shared query helpers.

Two implementations exist: ``demo.DemoBackend`` (isolated, in-memory, synthetic fixtures;
never touches the database) and ``service.DatabaseBackend`` (PostgreSQL). The API depends
only on this protocol, so the demo can never write into production tables.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from trend_agent.schemas.content_opportunity import OpportunityReport
from trend_agent.schemas.dashboard import (
    AlertView,
    DataQualityView,
    ForecastView,
    GapRow,
    GenerateIn,
    GenerateResult,
    OpportunityDetail,
    OpportunitySummary,
    OverviewView,
    Page,
    ProductionIn,
    ProductionView,
    ProviderView,
    RecommendationDetail,
    RecommendationSummary,
    SavedIdea,
    SystemStatusView,
    TrendDetail,
    TrendSummary,
    UserState,
)
from trend_agent.schemas.recommendation import (
    CreatorProfile,
    FeedbackType,
    ProductionStatus,
)

PS = ProductionStatus
#: allowed production transitions (moving back is allowed; PUBLISHED needs a video id)
TRANSITIONS: dict[ProductionStatus, set[ProductionStatus]] = {
    PS.IDEA: {PS.RESEARCHING, PS.SCRIPTING, PS.ABANDONED},
    PS.RESEARCHING: {PS.IDEA, PS.SCRIPTING, PS.ABANDONED},
    PS.SCRIPTING: {PS.RESEARCHING, PS.RECORDING, PS.ABANDONED},
    PS.RECORDING: {PS.SCRIPTING, PS.EDITING, PS.ABANDONED},
    PS.EDITING: {PS.RECORDING, PS.READY_TO_PUBLISH, PS.ABANDONED},
    PS.READY_TO_PUBLISH: {PS.EDITING, PS.PUBLISHED, PS.ABANDONED},
    PS.PUBLISHED: set(),
    PS.ABANDONED: {PS.IDEA},
}


class TransitionError(ValueError):
    pass


def check_transition(current: ProductionStatus | None, req: ProductionIn) -> None:
    if current is not None and req.status != current and req.status not in TRANSITIONS[current]:
        raise TransitionError(f"cannot move from {current.value} to {req.status.value}")
    if req.status is PS.PUBLISHED and not req.published_video_id:
        raise TransitionError("PUBLISHED needs the published video ID")


class NotFoundError(LookupError):
    pass


class DashboardBackend(Protocol):
    mode: str
    synthetic: bool

    def overview(self) -> OverviewView: ...
    def opportunities(self, **filters: Any) -> Page[OpportunitySummary]: ...
    def opportunity(self, oid: str) -> OpportunityDetail | None: ...
    def gaps(self, **filters: Any) -> Page[GapRow]: ...
    def trends(self, **filters: Any) -> Page[TrendSummary]: ...
    def trend(self, tid: str) -> TrendDetail | None: ...
    def forecast(self, fid: str) -> ForecastView | None: ...
    def recommendations(self, **filters: Any) -> Page[RecommendationSummary]: ...
    def recommendation(self, rid: str, *, viewed: bool = False) -> RecommendationDetail | None: ...
    def generate(self, req: GenerateIn) -> GenerateResult: ...
    def save(self, rid: str, notes: str | None) -> UserState: ...
    def unsave(self, rid: str) -> UserState: ...
    def dismiss(self, rid: str) -> UserState: ...
    def feedback(
        self, rid: str, ftype: FeedbackType, value: str | None, notes: str | None
    ) -> UserState: ...
    def production(self, rid: str, req: ProductionIn) -> UserState: ...
    def saved(self, **filters: Any) -> list[SavedIdea]: ...
    def profile(self) -> CreatorProfile: ...
    def update_profile(self, profile: CreatorProfile) -> CreatorProfile: ...
    def alerts(self, *, unread_only: bool = False) -> list[AlertView]: ...
    def mark_alert_read(self, aid: str) -> AlertView | None: ...
    def system_status(self) -> SystemStatusView: ...
    def providers(self) -> list[ProviderView]: ...
    def data_quality(self) -> DataQualityView: ...


# -------------------------------------------------------------------- helpers
def paginate[T](items: Sequence[T], page: int, page_size: int) -> Page[T]:
    page = max(1, page)
    start = (page - 1) * page_size
    return Page[Any](
        items=list(items[start : start + page_size]),
        total=len(items),
        page=page,
        page_size=page_size,
    )


def _sorted[T](items: list[T], key: Callable[[T], Any], order: str) -> list[T]:
    present = [i for i in items if key(i) is not None]
    missing = [i for i in items if key(i) is None]  # missing values always last
    present.sort(key=key, reverse=order == "desc")
    return present + missing


_OPP_SORT: dict[str, Callable[[OpportunitySummary], Any]] = {
    "score": lambda o: o.opportunity_score,
    "updated": lambda o: o.last_updated,
    "severity": lambda o: o.surge_severity,
    "query": lambda o: o.query.lower(),
    "breakout": lambda o: o.breakout_probability,
}


def query_opportunities(
    items: Sequence[OpportunitySummary],
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
    now: datetime | None = None,
    sort: str = "score",
    order: str = "desc",
    page: int = 1,
    page_size: int = 25,
) -> Page[OpportunitySummary]:
    out = list(items)
    if category:
        out = [o for o in out if o.category == category]
    if region:
        out = [o for o in out if (o.region or "").upper() == region.upper()]
    if intent:
        out = [o for o in out if intent in (o.intent, o.sub_intent)]
    if surge:
        out = [o for o in out if (o.surge_stage or "none") == surge]
    if status:
        out = [o for o in out if o.status == status]
    if verification:
        out = [o for o in out if o.verification_state == verification]
    if min_score is not None:
        out = [
            o for o in out if o.opportunity_score is not None and o.opportunity_score >= min_score
        ]
    if max_score is not None:
        out = [
            o for o in out if o.opportunity_score is not None and o.opportunity_score <= max_score
        ]
    if max_age_hours is not None and now is not None:
        out = [o for o in out if (now - o.last_updated).total_seconds() / 3600 <= max_age_hours]
    if q:
        out = [o for o in out if q.lower() in o.query.lower()]
    return paginate(_sorted(out, _OPP_SORT.get(sort, _OPP_SORT["score"]), order), page, page_size)


_STRENGTH = {"strong": 3, "moderate": 2, "weak": 1}
_GAP_SORT: dict[str, Callable[[GapRow], Any]] = {
    "score": lambda g: g.opportunity_score,
    "strength": lambda g: _STRENGTH.get(g.gap.evidence_strength, 0),
    "query": lambda g: g.query.lower(),
    "updated": lambda g: g.analysed_at,
}


def gap_rows(
    reports: Sequence[OpportunityReport], rec_counts: dict[str, int] | None = None
) -> list[GapRow]:
    from trend_agent.recommendations.views import DEMAND_LABELS, gap_view

    out = []
    for rep in reports:
        primary = rep.primary_gap
        for g in rep.gaps:
            out.append(
                GapRow(
                    opportunity_id=rep.opportunity_id,
                    query=rep.canonical_query,
                    category=rep.category,
                    region=rep.region,
                    is_primary=primary is not None
                    and g.gap_type == primary.gap_type
                    and g.statement == primary.statement,
                    gap=gap_view(g),
                    opportunity_score=rep.score.score,
                    opportunity_status=rep.status.value,
                    demand_label=DEMAND_LABELS.get(rep.demand.basis.value, rep.demand.basis.value),
                    recommendation_count=(rec_counts or {}).get(rep.opportunity_id, 0),
                    analysed_at=rep.analysed_at,
                    synthetic=rep.synthetic,
                )
            )
    return out


def query_gaps(
    rows: Sequence[GapRow],
    *,
    category: str | None = None,
    gap_type: str | None = None,
    strength: str | None = None,
    primary_only: bool = False,
    include_unverifiable: bool = False,
    q: str | None = None,
    sort: str = "score",
    order: str = "desc",
    page: int = 1,
    page_size: int = 25,
) -> Page[GapRow]:
    out = list(rows)
    if not include_unverifiable:  # "no verifiable gap" / "insufficient evidence" are not gaps
        out = [
            g for g in out if g.gap.gap_type not in ("no_verifiable_gap", "insufficient_evidence")
        ]
    if category:
        out = [g for g in out if g.category == category]
    if gap_type:
        out = [g for g in out if g.gap.gap_type == gap_type]
    if strength:
        out = [g for g in out if g.gap.evidence_strength == strength]
    if primary_only:
        out = [g for g in out if g.is_primary]
    if q:
        ql = q.lower()
        out = [g for g in out if ql in g.query.lower() or ql in g.gap.statement.lower()]
    return paginate(_sorted(out, _GAP_SORT.get(sort, _GAP_SORT["score"]), order), page, page_size)


_REC_SORT: dict[str, Callable[[RecommendationSummary], Any]] = {
    "score": lambda r: r.score,
    "rank": lambda r: r.rank,
    "hours": lambda r: r.hours_high,
    "generated": lambda r: r.generated_at,
    "title": lambda r: r.title.lower(),
}


def query_recommendations(
    items: Sequence[RecommendationSummary],
    *,
    category: str | None = None,
    status: str | None = None,
    q: str | None = None,
    urgency: str | None = None,
    min_score: float | None = None,
    include_research: bool = False,
    include_suppressed: bool = False,
    include_dismissed: bool = False,
    sort: str = "score",
    order: str = "desc",
    page: int = 1,
    page_size: int = 25,
) -> Page[RecommendationSummary]:
    out = list(items)
    if not include_research:
        ok = ("APPROVED", "APPROVED_WITH_UNCERTAINTY")
        out = [r for r in out if not r.research and r.status in ok]  # REJECTED never ranked
    if not include_suppressed:
        out = [r for r in out if not r.suppressed]
    if not include_dismissed:
        out = [r for r in out if not r.dismissed]
    if category:
        out = [r for r in out if r.category == category]
    if status:
        out = [r for r in out if r.status == status]
    if urgency:
        out = [r for r in out if r.urgency == urgency]
    if min_score is not None:
        out = [r for r in out if r.score is not None and r.score >= min_score]
    if q:
        ql = q.lower()
        out = [r for r in out if ql in r.title.lower() or ql in r.query.lower()]
    if sort == "rank":
        order = "asc" if order == "desc" else order
    return paginate(_sorted(out, _REC_SORT.get(sort, _REC_SORT["score"]), order), page, page_size)


@dataclass
class UserRecord:
    saved_at: datetime | None = None
    notes: str | None = None
    dismissed: bool = False
    production: ProductionView | None = None
    feedback: Counter[str] = field(default_factory=Counter)
    events: list[dict[str, Any]] = field(default_factory=list)

    def state(self) -> UserState:
        return UserState(
            saved=self.saved_at is not None,
            saved_at=self.saved_at,
            notes=self.notes,
            dismissed=self.dismissed,
            production=self.production,
            feedback=dict(self.feedback),
        )


class MemoryUserState:
    """User actions for the demo (never persisted; lost on restart)."""

    def __init__(self) -> None:
        self.records: dict[str, UserRecord] = {}

    def get(self, rid: str) -> UserRecord:
        return self.records.setdefault(rid, UserRecord())

    def save(self, rid: str, notes: str | None, now: datetime) -> UserState:
        r = self.get(rid)
        r.saved_at = r.saved_at or now
        r.notes = notes if notes is not None else r.notes
        r.dismissed = False
        r.feedback["SAVED"] += 1
        if r.production is None:
            r.production = ProductionView(
                status=PS.IDEA,
                planned_publication_time=None,
                actual_publication_time=None,
                published_video_id=None,
                notes=None,
                updated_at=now,
                history=[{"status": "IDEA", "at": now.isoformat()}],
            )
        return r.state()

    def unsave(self, rid: str) -> UserState:
        r = self.get(rid)
        r.saved_at = None
        return r.state()

    def dismiss(self, rid: str) -> UserState:
        r = self.get(rid)
        r.dismissed, r.saved_at = True, None
        r.feedback["DISMISSED"] += 1
        return r.state()

    def feedback(
        self, rid: str, ftype: FeedbackType, value: str | None, notes: str | None, now: datetime
    ) -> UserState:
        r = self.get(rid)
        r.feedback[ftype.value] += 1
        r.events.append(
            {"type": ftype.value, "value": value, "notes": notes, "at": now.isoformat()}
        )
        return r.state()

    def production(self, rid: str, req: ProductionIn, now: datetime) -> UserState:
        r = self.get(rid)
        check_transition(r.production.status if r.production else None, req)
        hist = list(r.production.history) if r.production else []
        hist.append({"status": req.status.value, "at": now.isoformat()})
        r.production = ProductionView(
            status=req.status,
            planned_publication_time=req.planned_publication_time,
            actual_publication_time=req.actual_publication_time,
            published_video_id=req.published_video_id,
            notes=req.notes,
            updated_at=now,
            history=hist,
        )
        if req.status is PS.PUBLISHED:
            r.feedback["PUBLISHED"] += 1
        elif req.status is PS.ABANDONED:
            r.feedback["ABANDONED"] += 1
        elif req.status in (PS.RECORDING, PS.EDITING):
            r.feedback["PRODUCED"] = max(r.feedback["PRODUCED"], 1)
        return r.state()
