"""Deterministic synthetic monitoring world (Phase 3 scenarios A-J).

``SyntheticTrendsProvider`` implements the Phase 1 ``SearchInterestProvider`` protocol, so
its output passes through exactly the same validation as real provider data. Everything it
emits is labelled ``DataNature.SYNTHETIC`` with a ``synthetic://`` reference. Values are a
pure function of (seed, query, day) plus the scenario's revisions, so runs are reproducible.

What it simulates, per query:
* publication lag (a day's value appears only ``publication_lag`` after the day ends),
* consistent-index scaling (comparable across requests) or per-request 0-100 scaling,
* revisions of past values at a stated time, outages, duplicate points in a response,
* limited history (nothing before ``history_days``).
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from trend_agent.discovery.lifecycle import DISCOVERY_ACTOR, transition
from trend_agent.discovery.models import (
    CandidateStatus,
    Category,
    DiscoveryMethod,
    EntityOrigin,
    EntityStatus,
    EntityType,
    KeywordOrigin,
    NoveltyKind,
    SearchIntent,
)
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import (
    CapabilityNotSupportedError,
    TemporarilyUnavailableError,
)
from trend_agent.schemas.enums import (
    Capability,
    DataNature,
    MeasurementType,
    ProviderStatus,
    ProviderType,
    RecordKind,
    SampleQuality,
    ScaleType,
    SearchProperty,
    TimeGranularity,
)
from trend_agent.schemas.observation import NormalizationContext, ObservationCandidate
from trend_agent.schemas.provider import (
    CapabilityStatus,
    ProviderDescriptor,
    ProviderStatusReport,
    RetentionRule,
    SearchInterestBatch,
    SearchInterestRequest,
)
from trend_agent.utils.text import canonicalize_query
from trend_agent.utils.time import Clock

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_monitoring.yaml"
PROVIDER_VERSION = "1.0.0"
DAY = timedelta(days=1)


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Window(_Spec):
    start: datetime
    end: datetime


class SyntheticProviderSpec(_Spec):
    scale: str = Field(default="consistent", pattern="^(consistent|per_request)$")
    publication_lag: timedelta = timedelta(hours=36)
    outages: tuple[Window, ...] = ()
    measurements: tuple[MeasurementType, ...] = (
        MeasurementType.DIRECT_SEARCH_INTEREST,
        MeasurementType.EXTERNAL_SEARCH_INTEREST,
    )


class Shape(_Spec):
    kind: str = Field(pattern="^(emerging|event|evergreen|steady)$")
    base: float = 30.0
    peak: float = 80.0
    onset_day: int = 0  # relative to the anchor (negative = before)
    growth_days: int = 7
    weekly_amplitude: float = 0.0


class Revision(_Spec):
    day: date
    delta: float
    revised_at: datetime


class SyntheticKeyword(_Spec):
    query: str
    category: Category
    priority: float = Field(ge=0, le=100)
    novelty_kind: NoveltyKind = NoveltyKind.RECURRING
    intent: SearchIntent = SearchIntent.OTHER
    entity_name: str | None = None
    entity_type: EntityType | None = None
    entity_age_days: float = 1.0
    time_sensitive_until: datetime | None = None
    shape: Shape
    history_days: int = Field(default=60, ge=0)
    publication_lag: timedelta | None = None
    revisions: tuple[Revision, ...] = ()
    duplicate_points: bool = False
    scenario: str = ""


class SyntheticMonitoringScenario(_Spec):
    anchor: datetime
    demo_days: int = 10
    seed: int = 20260302
    quota_daily_requests: int = 24
    providers: dict[str, SyntheticProviderSpec]
    keywords: tuple[SyntheticKeyword, ...]
    backfills: tuple[dict[str, Any], ...] = ()
    worker_crash_at_hour: int | None = None

    def keyword(self, canonical: str) -> SyntheticKeyword | None:
        for k in self.keywords:
            if canonicalize_query(k.query) == canonical:
                return k
        return None


def load_monitoring_scenario(path: str | Path | None = None) -> SyntheticMonitoringScenario:
    with Path(path or FIXTURE).open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return SyntheticMonitoringScenario.model_validate(data)


def _noise(seed: int, query: str, day: date, scale: float = 2.0) -> float:
    digest = hashlib.sha256(f"{seed}|{query}|{day.isoformat()}".encode()).digest()
    unit = int.from_bytes(digest[:8], "big") / 2**64
    return (unit * 2.0 - 1.0) * scale


def true_value(scenario: SyntheticMonitoringScenario, kw: SyntheticKeyword, day: date) -> float:
    """The synthetic 'world' value for a day (before any revision)."""
    anchor = scenario.anchor.date()
    i = (day - anchor).days
    s = kw.shape
    if s.kind == "emerging":
        x = (i - s.onset_day) / max(s.growth_days, 1)
        level = s.base + (s.peak - s.base) / (1.0 + math.exp(-6.0 * (x - 0.5)))
    elif s.kind == "event":
        until = kw.time_sensitive_until
        start = anchor + timedelta(days=s.onset_day)
        end = until.date() if until else start + timedelta(days=2)
        if start <= day <= end:
            level = s.peak
        elif day > end:
            level = s.base + (s.peak - s.base) * math.exp(-(day - end).days / 1.5)
        else:
            level = s.base
    elif s.kind == "steady":
        level = s.base + (s.peak - s.base) * max(0.0, min(1.0, (i + 60) / 120.0))
    else:  # evergreen with weekly seasonality
        level = s.base + s.weekly_amplitude * math.sin(2 * math.pi * day.weekday() / 7.0)
    return round(max(1.0, level + _noise(scenario.seed, kw.query, day)), 2)


class SyntheticTrendsProvider:
    """Phase 1-compatible synthetic search-interest provider for monitoring scenarios."""

    def __init__(
        self,
        name: str,
        scenario: SyntheticMonitoringScenario,
        *,
        clock: Clock,
    ) -> None:
        self.name = name
        self._spec = scenario.providers[name]
        self._scenario = scenario
        self._clock = clock
        self.calls = 0
        properties = {
            SearchProperty.YOUTUBE_SEARCH
            if m is MeasurementType.DIRECT_SEARCH_INTEREST
            else SearchProperty.GOOGLE_WEB_SEARCH
            for m in self._spec.measurements
        }
        self._descriptor = ProviderDescriptor(
            name=name,
            display_name=f"Synthetic monitoring provider ({self._spec.scale} scale)",
            version=PROVIDER_VERSION,
            provider_type=ProviderType.SYNTHETIC,
            capabilities=frozenset({Capability.SEARCH_INTEREST}),
            measurement_types=frozenset(self._spec.measurements),
            search_properties=frozenset(properties),
            supported_regions=None,
            supported_granularities=frozenset({TimeGranularity.DAY}),
            requires_authentication=False,
            historical_data_available=True,
            data_nature=DataNature.SYNTHETIC,
            enabled=True,
            retention=(
                RetentionRule(
                    record_kind=RecordKind.SEARCH_INTEREST_OBSERVATION,
                    persist_allowed=True,
                    max_retention=None,
                    notes="Synthetic fixture data.",
                ),
            ),
            usage_restrictions=("SYNTHETIC DATA. Never evidence of real demand.",),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def _in_outage(self, now: datetime) -> bool:
        return any(w.start <= now < w.end for w in self._spec.outages)

    def status(self) -> ProviderStatusReport:
        now = self._clock()
        status = (
            ProviderStatus.TEMPORARILY_UNAVAILABLE
            if self._in_outage(now)
            else ProviderStatus.AVAILABLE
        )
        detail = "synthetic outage window" if status is not ProviderStatus.AVAILABLE else "ready"
        return ProviderStatusReport(
            provider_name=self.name,
            status=status,
            detail=detail,
            capabilities=(CapabilityStatus(capability=Capability.SEARCH_INTEREST, status=status),),
            checked_at=now,
        )

    async def aclose(self) -> None:
        return None

    async def fetch_search_interest(
        self, request: SearchInterestRequest, ctx: RequestContext
    ) -> SearchInterestBatch:
        now = self._clock()
        self.calls += 1
        if self._in_outage(now):
            raise TemporarilyUnavailableError(
                "synthetic provider outage (scenario D)", provider=self.name
            )
        if request.granularity is not TimeGranularity.DAY:
            raise CapabilityNotSupportedError("daily granularity only", provider=self.name)
        measurement = (
            MeasurementType.DIRECT_SEARCH_INTEREST
            if request.search_property is SearchProperty.YOUTUBE_SEARCH
            else MeasurementType.EXTERNAL_SEARCH_INTEREST
        )
        if measurement not in self._spec.measurements:
            raise CapabilityNotSupportedError(
                f"{request.search_property.value} not offered", provider=self.name
            )
        canonical = canonicalize_query(request.query)
        kw = self._scenario.keyword(canonical)
        geography = request.region or "US"
        ref = f"synthetic://{self.name}?q={canonical}&geo={geography}"
        if kw is None:
            return SearchInterestBatch(
                provider_name=self.name,
                provider_version=PROVIDER_VERSION,
                request=request,
                observations=(),
                warnings=("SYNTHETIC: unknown query",),
                source_reference=ref,
            )
        lag = kw.publication_lag or self._spec.publication_lag
        first_day = self._scenario.anchor.date() - timedelta(days=kw.history_days)
        day = max(request.start.date(), first_day)
        points: list[tuple[datetime, datetime, float, datetime]] = []
        while True:
            start = datetime.combine(day, time.min, tzinfo=UTC)
            end = start + DAY
            if start >= request.end or end > now:
                break
            published = end + lag
            if published > now:
                break
            value = true_value(self._scenario, kw, day)
            available = published
            for rev in kw.revisions:
                if rev.day == day and rev.revised_at <= now:
                    value = round(value + rev.delta, 2)
                    available = rev.revised_at
            points.append((start, end, value, available))
            day += DAY

        per_request = self._spec.scale == "per_request"
        peak = max((p[2] for p in points), default=0.0)
        window_tag = f"{request.start.date()}_{request.end.date()}"
        if per_request:
            ctx_norm = NormalizationContext(
                scale_type=ScaleType.RELATIVE_PER_REQUEST,
                method="synthetic_max_in_request_0_100",
                batch_id=hashlib.sha256(
                    f"{self.name}|{canonical}|{window_tag}|{now.date()}".encode()
                ).hexdigest()[:16],
                co_normalized_terms=(canonical,),
                reference_geography=geography,
                reference_start=request.start,
                reference_end=request.end,
                granularity=TimeGranularity.DAY,
                search_property=request.search_property,
                notes="SYNTHETIC",
            )
            scale = ScaleType.RELATIVE_PER_REQUEST
        else:
            ctx_norm = NormalizationContext(
                scale_type=ScaleType.CONSISTENT_INDEX,
                method="synthetic_consistent_index",
                scale_version="1",
                reference_geography=geography,
                granularity=TimeGranularity.DAY,
                search_property=request.search_property,
                notes="SYNTHETIC",
            )
            scale = ScaleType.CONSISTENT_INDEX
        candidates: list[ObservationCandidate] = []
        for start, end, value, available in points:
            shown = round(100.0 * value / peak) if per_request and peak > 0 else value
            candidate = ObservationCandidate(
                query=request.query,
                query_language=request.language,
                query_region=request.region,
                provider_name=self.name,
                provider_version=PROVIDER_VERSION,
                measurement_type=measurement.value,
                search_property=request.search_property.value,
                observation_start=start,
                observation_end=end,
                granularity=TimeGranularity.DAY.value,
                collected_at=now,
                available_at=available,
                value=float(shown),
                scale_type=scale.value,
                normalization_context=ctx_norm,
                geography=geography,
                language=request.language,
                sample_quality=SampleQuality.COMPLETE.value,
                data_nature=DataNature.SYNTHETIC.value,
                source_reference=f"{ref}&day={start.date()}",
            )
            candidates.append(candidate)
            if kw.duplicate_points:
                candidates.append(candidate.model_copy())
        return SearchInterestBatch(
            provider_name=self.name,
            provider_version=PROVIDER_VERSION,
            request=request,
            observations=tuple(candidates),
            warnings=("SYNTHETIC DATA - not a measurement of real search demand",),
            source_reference=ref,
        )


# ---------------------------------------------------------------------------- fixture keywords


def seed_keywords(
    session: Session, scenario: SyntheticMonitoringScenario, now: datetime, language: str
) -> dict[str, int]:
    """Insert the fixture's keywords as synthetic Phase 2 keywords (idempotent).

    Returns canonical query -> keyword id. Only used by the demo and tests.
    """
    from trend_agent.db.discovery_models import DiscoveredKeywordRow
    from trend_agent.db.repositories.core import QueryRepository
    from trend_agent.db.repositories.discovery import DiscoveryRepository

    queries = QueryRepository(session)
    repo = DiscoveryRepository(session)
    out: dict[str, int] = {}
    for kw in scenario.keywords:
        canonical = canonicalize_query(kw.query)
        query_id = queries.get_or_create(canonical, kw.query, language, None)
        existing = repo.keyword_by_query(query_id)
        if existing is not None:
            out[canonical] = existing.id
            continue
        entity_id = None
        if kw.entity_type is not None and kw.entity_name:
            key = f"synthetic|{kw.category.value}|{kw.entity_type.value}|{kw.entity_name.lower()}"
            entity = repo.entity_by_key(key)
            if entity is None:
                seen = now - timedelta(days=kw.entity_age_days)
                entity = repo.create_entity(
                    {
                        "entity_key": key,
                        "canonical_name": kw.entity_name,
                        "entity_type": kw.entity_type,
                        "category": kw.category,
                        "status": EntityStatus.VERIFIED,
                        "origin": EntityOrigin.EXTRACTED,
                        "is_family": False,
                        "evergreen": False,
                        "confidence": 0.95,
                        "first_seen_at": seen,
                        "last_seen_at": seen,
                        "is_synthetic": True,
                        "extra": {"fixture": "synthetic_monitoring"},
                    }
                )
            entity_id = entity.id
        topic_id, _ = repo.topic_cluster(
            f"synthetic:{canonical}", canonical, kw.category, entity_id
        )
        intent_id, _ = repo.intent_cluster(topic_id, f"{kw.intent.value}:_", kw.intent, kw.query)
        row = DiscoveredKeywordRow(
            query_id=query_id,
            entity_id=entity_id,
            topic_cluster_id=topic_id,
            intent_cluster_id=intent_id,
            origin=KeywordOrigin.GENERATED_HYPOTHESIS,
            discovery_type=DiscoveryMethod.SEED_TERM,
            template_id=None,
            primary_category=kw.category,
            category_scores={kw.category.value: 1.0},
            primary_intent=kw.intent,
            intent_scores={kw.intent.value: 1.0},
            candidate_status=CandidateStatus.PRIORITIZED,
            status_reason="synthetic monitoring fixture",
            first_discovered_at=now,
            last_discovered_at=now,
            novelty_score=min(100.0, kw.priority),
            novelty_kind=kw.novelty_kind,
            novelty_components={},
            priority_score=kw.priority,
            scoring_version="fixture",
            score_components={"independent_origins": 2, "note": "SYNTHETIC fixture value"},
            topic_assignment={},
            intent_assignment={},
            grounding=["synthetic fixture"],
            is_synthetic=True,
        )
        session.add(row)
        session.flush()
        for current, target in (
            (None, CandidateStatus.DISCOVERED),
            (CandidateStatus.DISCOVERED, CandidateStatus.VALIDATED),
            (CandidateStatus.VALIDATED, CandidateStatus.PRIORITIZED),
        ):
            repo.add_history(
                row.id,
                transition(
                    current,
                    target,
                    reason="synthetic monitoring fixture",
                    actor=DISCOVERY_ACTOR,
                    at=now,
                ),
                None,
            )
        out[canonical] = row.id
    return out
