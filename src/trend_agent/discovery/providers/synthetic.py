"""Deterministic synthetic discovery provider (test and demo data only).

Every item is labelled: ``is_synthetic=True``, persisted evidence type
``SYNTHETIC_TEST_DATA`` (the simulated type is kept in metadata for scoring realism) and a
``synthetic://`` source reference. Timestamps are offsets from the provider clock, so a
fixed clock reproduces identical output.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from trend_agent.discovery.models import (
    Category,
    DiscoveryItem,
    DiscoveryRequest,
    DiscoveryResult,
    DiscoverySource,
    EvidenceType,
    SourceType,
)
from trend_agent.discovery.providers.base import discovery_descriptor, simple_status
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import (
    InvalidRequestError,
    QuotaExhaustedError,
    RateLimitedError,
    TemporarilyUnavailableError,
)
from trend_agent.schemas.enums import Capability, DataNature, ProviderType
from trend_agent.schemas.provider import ProviderDescriptor, ProviderStatusReport
from trend_agent.utils.time import Clock, parse_rfc3339

PROVIDER_NAME = "synthetic_discovery"


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SyntheticItemSpec(_Spec):
    id: str
    title: str
    text: str | None = None
    url: str | None = None
    publisher: str | None = None
    author: str | None = None
    published_offset_hours: float | None = None
    evidence_type: EvidenceType | None = None
    observed_queries: tuple[str, ...] = ()


class SyntheticSourceSpec(_Spec):
    key: str
    stage: str = "current"
    category: Category
    name: str
    provider: str = PROVIDER_NAME
    behaviour: str = Field(default="ok", pattern=r"^(ok|outage|quota_exhausted|rate_limited)$")
    evidence_type: EvidenceType = EvidenceType.OFFICIAL_ANNOUNCEMENT
    publisher: str | None = None
    reliability: float = 0.7
    entity_hint: str | None = None
    entity_hint_type: str | None = None
    items: tuple[SyntheticItemSpec, ...] = ()


class SyntheticStage(_Spec):
    name: str
    offset_days: float


class SyntheticScenario(_Spec):
    anchor: str
    stages: tuple[SyntheticStage, ...]
    sources: tuple[SyntheticSourceSpec, ...]

    @property
    def anchor_time(self) -> datetime:
        return parse_rfc3339(self.anchor)

    def stage_time(self, stage: str) -> datetime:
        for s in self.stages:
            if s.name == stage:
                return self.anchor_time + timedelta(days=s.offset_days)
        raise KeyError(stage)

    def sources_for(self, stage: str) -> list[DiscoverySource]:
        return [
            DiscoverySource(
                key=s.key,
                provider=s.provider,
                name=s.name,
                source_type=SourceType.SYNTHETIC,
                category=s.category,
                reference=f"synthetic://{s.key}",
                evidence_type=EvidenceType.SYNTHETIC_TEST_DATA,
                publisher=s.publisher,
                reliability=s.reliability,
                min_poll_interval_minutes=1,
                entity_hint=s.entity_hint,
                entity_hint_type=s.entity_hint_type,
                verified=True,
            )
            for s in self.sources
            if s.stage == stage
        ]


def load_scenario(path: str | Path | None = None) -> SyntheticScenario:
    if path is None:
        text = (
            resources.files("trend_agent.discovery.fixtures")
            .joinpath("synthetic_discovery.yaml")
            .read_text(encoding="utf-8")
        )
    else:
        text = Path(path).read_text(encoding="utf-8")
    data: Any = yaml.safe_load(text)
    return SyntheticScenario.model_validate(data)


class SyntheticDiscoveryProvider:
    def __init__(self, scenario: SyntheticScenario, *, clock: Clock) -> None:
        self._scenario = scenario
        self._specs = {s.key: s for s in scenario.sources}
        self._clock = clock
        self._descriptor = discovery_descriptor(
            name=PROVIDER_NAME,
            display_name="Synthetic discovery fixtures (test data only)",
            version="1.0.0",
            provider_type=ProviderType.SYNTHETIC,
            capabilities=frozenset({Capability.TREND_DISCOVERY, Capability.SEARCH_SUGGESTIONS}),
            requires_authentication=False,
            enabled=True,
            retention_days=None,
            data_nature=DataNature.SYNTHETIC,
            usage_restrictions=("SYNTHETIC DATA. Never evidence of real topics or demand.",),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def status(self) -> ProviderStatusReport:
        return simple_status(PROVIDER_NAME, True, "Synthetic fixtures loaded.", self._clock())

    async def aclose(self) -> None:
        return None

    def supports(self, source: DiscoverySource) -> bool:
        return source.source_type is SourceType.SYNTHETIC and source.key in self._specs

    def can_afford(self, source: DiscoverySource) -> tuple[bool, str]:
        return True, ""

    async def discover(self, request: DiscoveryRequest, ctx: RequestContext) -> DiscoveryResult:
        spec = self._specs.get(request.source.key)
        if spec is None:
            raise InvalidRequestError(
                f"no synthetic source {request.source.key!r}", provider=PROVIDER_NAME
            )
        now = self._clock()
        match spec.behaviour:
            case "outage":
                raise TemporarilyUnavailableError(
                    "synthetic provider outage", provider=PROVIDER_NAME
                )
            case "quota_exhausted":
                raise QuotaExhaustedError(
                    "synthetic quota exhausted",
                    bucket="synthetic",
                    resets_at=now + timedelta(hours=8),
                    source="server",
                    provider=PROVIDER_NAME,
                )
            case "rate_limited":
                raise RateLimitedError("synthetic rate limit", provider=PROVIDER_NAME)
        items = []
        for item in spec.items:
            published = (
                now + timedelta(hours=item.published_offset_hours)
                if item.published_offset_hours is not None
                else None
            )
            simulated = item.evidence_type or spec.evidence_type
            items.append(
                DiscoveryItem(
                    provider_name=PROVIDER_NAME,
                    source_key=spec.key,
                    source_reference=f"synthetic://{spec.key}/{item.id}",
                    evidence_type=EvidenceType.SYNTHETIC_TEST_DATA,
                    title=item.title,
                    text=item.text,
                    url=item.url,
                    publisher=item.publisher or spec.publisher,
                    author=item.author,
                    published_at=published,
                    collected_at=now,
                    category_hint=spec.category,
                    observed_queries=item.observed_queries,
                    entity_hint=spec.entity_hint,
                    entity_hint_type=spec.entity_hint_type,
                    is_synthetic=True,
                    metadata={"simulated_evidence_type": simulated.value, "fixture_item": item.id},
                )
            )
        return DiscoveryResult(provider_name=PROVIDER_NAME, source_key=spec.key, items=tuple(items))
