"""Collectors: turn a (target, window) into validated observation drafts.

* ``SearchInterestCollector`` wraps any Phase 1 ``SearchInterestProvider`` (Google Trends,
  the Phase 1 mock, the Phase 3 synthetic provider). Output passes Phase 1 validation and
  the provider's data policy before it can be stored.
* ``EventSignalCollector`` derives daily *proxy* series (official events, community
  activity, suggestion presence) from stored Phase 2 discovery evidence. It makes no
  network requests. Days without items produce no observation: unknown is not zero.

A collector never fabricates a value: an unavailable provider raises a typed error.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Protocol

from trend_agent.db.repositories.monitoring import MonitoringRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.discovery.models import EvidenceRelation, EvidenceType
from trend_agent.ingestion.validation import ValidationConfig, validate_batch
from trend_agent.monitoring.config import Compliance
from trend_agent.monitoring.policies import PolicyRegistry
from trend_agent.providers.base import Provider, RequestContext, SearchInterestProvider
from trend_agent.providers.errors import ProviderNotConfiguredError
from trend_agent.providers.quota import (
    EndpointCost,
    QuotaBucket,
    QuotaManager,
    QuotaPolicy,
    QuotaStore,
)
from trend_agent.schemas.enums import (
    Capability,
    DataNature,
    ProviderStatus,
    SampleQuality,
    ScaleType,
    SearchProperty,
    TimeGranularity,
)
from trend_agent.schemas.monitoring import (
    MeasurementCategory,
    category_for_measurement,
    search_property_for,
)
from trend_agent.schemas.observation import NormalizationContext
from trend_agent.schemas.provider import (
    CapabilityStatus,
    ProviderStatusReport,
    SearchInterestRequest,
)
from trend_agent.schemas.scheduling import ProviderProfile
from trend_agent.schemas.time_series import ObservationDraft
from trend_agent.utils.codes import WORLD
from trend_agent.utils.time import Clock

REQUEST_BUCKET = "requests"
REQUEST_ENDPOINT = "collect"

UNITS: dict[ScaleType, str] = {
    ScaleType.RELATIVE_PER_REQUEST: "relative_0_100",
    ScaleType.CONSISTENT_INDEX: "index",
    ScaleType.ABSOLUTE_COUNT: "count",
    ScaleType.ESTIMATED_COUNT: "estimated_count",
    ScaleType.RANK: "rank",
    ScaleType.PRESENCE: "presence",
}


@dataclass(frozen=True)
class TargetSnapshot:
    target_id: int
    keyword_id: int | None
    query_id: int
    canonical_query: str
    original_query: str
    region: str
    language: str | None
    measurement_category: MeasurementCategory
    is_proxy: bool
    is_synthetic: bool


@dataclass
class CollectionResult:
    drafts: list[ObservationDraft] = field(default_factory=list)
    received: int = 0
    rejected: int = 0
    rejected_by_reason: dict[str, int] = field(default_factory=dict)
    in_batch_duplicates: int = 0
    withheld: int = 0
    warnings: list[str] = field(default_factory=list)


class Collector(Protocol):
    name: str
    profile: ProviderProfile

    def status(self) -> ProviderStatusReport: ...

    def quota_manager(self) -> QuotaManager | None: ...

    async def collect(
        self, target: TargetSnapshot, start: datetime, end: datetime, ctx: RequestContext
    ) -> CollectionResult: ...


def local_quota(profile: ProviderProfile, store: QuotaStore, clock: Clock) -> QuotaManager | None:
    """Local daily request cap for a provider without its own quota manager."""
    spec = profile.quota
    if spec.daily_requests is None:
        return None
    policy = QuotaPolicy(
        provider_name=f"monitoring:{profile.name}",
        reset_timezone=spec.reset_timezone,
        buckets={
            REQUEST_BUCKET: QuotaBucket(REQUEST_BUCKET, spec.daily_requests, spec.safety_margin_pct)
        },
        endpoint_costs={REQUEST_ENDPOINT: EndpointCost(REQUEST_BUCKET, spec.request_cost)},
    )
    return QuotaManager(policy, store, clock)


class SearchInterestCollector:
    def __init__(
        self,
        provider: SearchInterestProvider,
        profile: ProviderProfile,
        *,
        policies: PolicyRegistry,
        quota: QuotaManager | None,
        clock: Clock,
    ) -> None:
        self.name = profile.name
        self.profile = profile
        self._provider = provider
        self._policies = policies
        self._quota = quota
        self._clock = clock

    def status(self) -> ProviderStatusReport:
        return self._provider.status()

    def quota_manager(self) -> QuotaManager | None:
        return self._quota

    async def collect(
        self, target: TargetSnapshot, start: datetime, end: datetime, ctx: RequestContext
    ) -> CollectionResult:
        if self._quota is not None:
            self._quota.reserve(REQUEST_ENDPOINT, ctx.ledger)  # raises QuotaExhaustedError
        request = SearchInterestRequest(
            query=target.original_query,
            region=target.region,
            language=target.language,
            start=start,
            end=end,
            granularity=self.profile.collection_granularity,
            search_property=search_property_for(target.measurement_category),
        )
        batch = await self._provider.fetch_search_interest(request, ctx)
        now = self._clock()
        report = validate_batch(
            batch.observations, self._provider.descriptor, now=now, config=ValidationConfig()
        )
        result = CollectionResult(
            received=report.received,
            rejected=len(report.rejected),
            rejected_by_reason=report.rejected_by_reason(),
            in_batch_duplicates=report.duplicates_in_batch,
            warnings=list(batch.warnings),
        )
        for obs in report.accepted:
            category = category_for_measurement(obs.measurement_type, obs.search_property)
            if category is None or category is not target.measurement_category:
                result.warnings.append(
                    f"dropped {obs.measurement_type.value}: target measures "
                    f"{target.measurement_category.value}"
                )
                result.rejected += 1
                continue
            decision = self._policies.decide(self.profile.retention_policy, obs.collected_at)
            if not decision.allowed:
                result.withheld += 1
                continue
            synthetic = obs.data_nature is DataNature.SYNTHETIC
            result.drafts.append(
                ObservationDraft(
                    query_id=target.query_id,
                    target_id=target.target_id,
                    provider_name=obs.provider_name,
                    provider_version=obs.provider_version,
                    measurement_category=(
                        MeasurementCategory.SYNTHETIC_TEST_OBSERVATION if synthetic else category
                    ),
                    simulated_category=category if synthetic else None,
                    unit=UNITS[obs.scale_type],
                    period_start=obs.observation_start,
                    period_end=obs.observation_end,
                    granularity=obs.granularity,
                    provider_published_at=obs.available_at,
                    first_available_at=obs.available_at,
                    collected_at=obs.collected_at,
                    value=obs.value,
                    scale_type=obs.scale_type,
                    normalization_context=obs.normalization_context,
                    region=obs.geography,
                    language=obs.language,
                    search_property=obs.search_property,
                    data_nature=obs.data_nature,
                    sample_quality=obs.sample_quality,
                    source_reference=obs.source_reference,
                    data_policy_id=decision.policy_id,
                    expires_at=decision.expires_at,
                )
            )
        if result.withheld:
            result.warnings.append(
                f"{result.withheld} observation(s) withheld: "
                f"{self._policies.resolved(self.profile.retention_policy).reason}"
            )
        return result


# ---------------------------------------------------------------------------- event signals

_EVENT_CATEGORY: dict[EvidenceType, MeasurementCategory] = {
    EvidenceType.OFFICIAL_ANNOUNCEMENT: MeasurementCategory.OFFICIAL_EVENT_SIGNAL,
    EvidenceType.CODE_RELEASE: MeasurementCategory.OFFICIAL_EVENT_SIGNAL,
    EvidenceType.COMMUNITY_DISCUSSION: MeasurementCategory.COMMUNITY_ACTIVITY,
    EvidenceType.SEARCH_SUGGESTION: MeasurementCategory.SEARCH_SUGGESTION_PRESENCE,
    EvidenceType.RELATED_QUERY: MeasurementCategory.SEARCH_SUGGESTION_PRESENCE,
    EvidenceType.VIDEO_TITLE: MeasurementCategory.VIDEO_ACTIVITY,
    EvidenceType.VIDEO_DESCRIPTION: MeasurementCategory.VIDEO_ACTIVITY,
}
YOUTUBE_SOURCED = frozenset({"youtube_discovery", "youtube_data_api"})


class EventSignalCollector:
    PROVIDER_VERSION = "1.0.0"

    def __init__(
        self,
        session_factory: SessionFactory,
        profile: ProviderProfile,
        *,
        policies: PolicyRegistry,
        compliance: Compliance,
        clock: Clock,
    ) -> None:
        self.name = profile.name
        self.profile = profile
        self._factory = session_factory
        self._policies = policies
        self._compliance = compliance
        self._clock = clock

    def status(self) -> ProviderStatusReport:
        return ProviderStatusReport(
            provider_name=self.name,
            status=ProviderStatus.AVAILABLE,
            detail="Derived from stored discovery evidence; no external requests.",
            capabilities=(
                CapabilityStatus(
                    capability=Capability.TREND_DISCOVERY, status=ProviderStatus.AVAILABLE
                ),
            ),
            checked_at=self._clock(),
        )

    def quota_manager(self) -> QuotaManager | None:
        return None

    async def collect(
        self, target: TargetSnapshot, start: datetime, end: datetime, ctx: RequestContext
    ) -> CollectionResult:
        result = CollectionResult()
        if target.keyword_id is None:
            result.warnings.append("target has no discovery keyword; no evidence to derive from")
            return result
        include_youtube = self._compliance.youtube_derived_metrics_permitted
        with transaction(self._factory) as session:
            rows = MonitoringRepository(session).evidence_items(target.keyword_id, start, end)
        groups: dict[
            tuple[MeasurementCategory, datetime, bool],
            list[tuple[int, datetime, datetime, datetime | None]],
        ] = defaultdict(list)
        skipped_youtube = 0
        for row in rows:
            result.received += 1
            effective = row.simulated_evidence_type or row.evidence_type
            category = _EVENT_CATEGORY.get(effective)
            if category is None:
                continue
            youtube = (
                category is MeasurementCategory.VIDEO_ACTIVITY
                or row.provider_name in YOUTUBE_SOURCED
            )
            if youtube and not include_youtube:
                skipped_youtube += 1
                continue
            if category is MeasurementCategory.SEARCH_SUGGESTION_PRESENCE and (
                row.relation is not EvidenceRelation.QUERY_OBSERVED
            ):
                continue  # presence means this exact query was observed, not a mention
            if "future_dated" in (row.quality_flags or []) or row.published_at > row.collected_at:
                result.rejected += 1
                result.rejected_by_reason["invalid_timestamp"] = (
                    result.rejected_by_reason.get("invalid_timestamp", 0) + 1
                )
                continue
            day = datetime.combine(row.published_at.astimezone(UTC).date(), time.min, tzinfo=UTC)
            groups[(category, day, bool(row.is_synthetic))].append(
                (row.id, row.published_at, row.collected_at, row.expires_at)
            )
        if skipped_youtube:
            result.withheld += skipped_youtube
            result.warnings.append(
                f"{skipped_youtube} YouTube-sourced item(s) excluded: derived YouTube metrics "
                "need explicit permission (compliance.youtube_derived_metrics_permitted)"
            )
        for (category, day, synthetic), items in sorted(
            groups.items(), key=lambda kv: (kv[0][0].value, kv[0][1])
        ):
            unique = {item_id: (pub, col, exp) for item_id, pub, col, exp in items}
            presence = category is MeasurementCategory.SEARCH_SUGGESTION_PRESENCE
            scale = ScaleType.PRESENCE if presence else ScaleType.ABSOLUTE_COUNT
            norm = NormalizationContext(
                scale_type=scale,
                method="observed_in_suggestions"
                if presence
                else "distinct_discovery_items_per_utc_day",
                scale_version="1",
                granularity=TimeGranularity.DAY,
                search_property=SearchProperty.NOT_APPLICABLE,
            )
            expiries = [exp for _, _, exp in unique.values() if exp is not None]
            collected = max(col for _, col, _ in unique.values())
            decision = self._policies.decide(
                self.profile.retention_policy,
                collected,
                source_expiry=min(expiries) if expiries else None,
            )
            if not decision.allowed:
                result.withheld += 1
                continue
            ids = sorted(unique)
            ref = (
                f"discovery-evidence://keyword/{target.keyword_id}/{category.value}/"
                f"{day.date()}?items={','.join(map(str, ids[:50]))}"
            )
            result.drafts.append(
                ObservationDraft(
                    query_id=target.query_id,
                    target_id=target.target_id,
                    provider_name=self.name,
                    provider_version=self.PROVIDER_VERSION,
                    measurement_category=(
                        MeasurementCategory.SYNTHETIC_TEST_OBSERVATION if synthetic else category
                    ),
                    simulated_category=category if synthetic else None,
                    is_proxy=True,
                    unit="presence" if presence else "items",
                    provider_observation_id=hashlib.sha256(ref.encode()).hexdigest()[:32],
                    period_start=day,
                    period_end=day + timedelta(days=1),
                    granularity=TimeGranularity.DAY,
                    event_time=min(pub for pub, _, _ in unique.values()),
                    provider_published_at=max(pub for pub, _, _ in unique.values()),
                    first_available_at=max(pub for pub, _, _ in unique.values()),
                    collected_at=collected,
                    value=1.0 if presence else float(len(unique)),
                    scale_type=scale,
                    normalization_context=norm,
                    region=WORLD,
                    language=None,
                    search_property=SearchProperty.NOT_APPLICABLE,
                    data_nature=DataNature.SYNTHETIC if synthetic else DataNature.ACTUAL,
                    sample_quality=SampleQuality.COMPLETE,
                    source_reference=ref,
                    data_policy_id=decision.policy_id,
                    expires_at=decision.expires_at,
                )
            )
        return result


# ---------------------------------------------------------------------------- registry


@dataclass(frozen=True)
class CollectorAvailability:
    provider: str
    available: bool
    status: str
    detail: str
    missing: tuple[str, ...] = ()


class CollectorRegistry:
    def __init__(self, profiles: Mapping[str, ProviderProfile]) -> None:
        self._profiles = dict(profiles)
        self._collectors: dict[str, Collector] = {}

    def add(self, collector: Collector) -> None:
        self._collectors[collector.name] = collector

    def get(self, name: str) -> Collector:
        try:
            return self._collectors[name]
        except KeyError:
            raise ProviderNotConfiguredError(
                f"no collector registered for provider {name!r}", provider=name
            ) from None

    def names(self) -> list[str]:
        return sorted(self._collectors)

    def profile(self, name: str) -> ProviderProfile | None:
        return self._profiles.get(name)

    def profiles(self) -> dict[str, ProviderProfile]:
        return dict(self._profiles)

    def availability(self, name: str) -> CollectorAvailability:
        profile = self._profiles.get(name)
        if profile is None:
            return CollectorAvailability(name, False, "unknown", "no provider profile configured")
        if not profile.enabled:
            return CollectorAvailability(name, False, "disabled", "disabled in monitoring.yaml")
        if profile.collection_disabled_reason:
            return CollectorAvailability(
                name, False, "not_permitted", " ".join(profile.collection_disabled_reason.split())
            )
        collector = self._collectors.get(name)
        if collector is None:
            return CollectorAvailability(
                name,
                False,
                ProviderStatus.NOT_CONFIGURED.value,
                "provider is not registered in this process",
            )
        report = collector.status()
        return CollectorAvailability(
            name,
            report.status is ProviderStatus.AVAILABLE,
            report.status.value,
            report.detail,
            tuple(report.missing_configuration),
        )


def as_search_interest(provider: Provider) -> SearchInterestProvider | None:
    return provider if isinstance(provider, SearchInterestProvider) else None
