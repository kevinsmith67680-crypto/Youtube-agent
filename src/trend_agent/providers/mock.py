"""Deterministic synthetic search-interest provider.

Every record it emits is labelled ``DataNature.SYNTHETIC`` and carries a
``synthetic://`` source reference. Output is a pure function of (seed, scenario, query,
region, granularity, requested window), so fixtures are reproducible across runs and
machines. Values are normalised per request to 0-100 (Google-Trends-website style) so
that downstream code must respect normalisation batches.

These scenarios are evaluation datasets for later phases; no detection logic lives here.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Mapping
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum

from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import (
    CapabilityNotSupportedError,
    InvalidRequestError,
    ProviderDisabledError,
    RateLimitedError,
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
from trend_agent.utils.codes import WORLD
from trend_agent.utils.text import canonicalize_query
from trend_agent.utils.time import Clock, utc_now

PROVIDER_NAME = "mock"
PROVIDER_VERSION = "1.0.0"
SCALE_METHOD = "synthetic_max_in_request_0_100"
MAX_WINDOWS = 3660
AVAILABILITY_LAG = timedelta(days=1)


class MockScenario(StrEnum):
    STABLE = "stable"
    GRADUAL_GROWTH = "gradual_growth"
    SUDDEN_SURGE = "sudden_surge"
    DECLINING = "declining"
    SPARSE = "sparse"
    MISSING_DATA = "missing_data"
    OUT_OF_ORDER = "out_of_order"
    DUPLICATES = "duplicates"
    MIXED_NORMALIZATION = "mixed_normalization"
    INVALID_RECORDS = "invalid_records"
    PROVIDER_FAILURE = "provider_failure"
    RATE_LIMITED = "rate_limited"


_STEP = {TimeGranularity.DAY: timedelta(days=1), TimeGranularity.WEEK: timedelta(days=7)}
_Series = list[tuple[float | None, SampleQuality]]


class MockSearchInterestProvider:
    def __init__(
        self,
        *,
        seed: int = 20_260_101,
        enabled: bool = True,
        clock: Clock = utc_now,
        scenario_overrides: Mapping[str, MockScenario] | None = None,
    ) -> None:
        """``scenario_overrides`` maps canonical queries to scenarios (for tests)."""
        self._seed = seed
        self._enabled = enabled
        self._clock = clock
        self._overrides = {canonicalize_query(k): v for k, v in (scenario_overrides or {}).items()}
        self._descriptor = ProviderDescriptor(
            name=PROVIDER_NAME,
            display_name="Synthetic search-interest provider (test data only)",
            version=PROVIDER_VERSION,
            provider_type=ProviderType.SYNTHETIC,
            capabilities=frozenset({Capability.SEARCH_INTEREST}),
            measurement_types=frozenset(
                {MeasurementType.DIRECT_SEARCH_INTEREST, MeasurementType.EXTERNAL_SEARCH_INTEREST}
            ),
            search_properties=frozenset(
                {SearchProperty.YOUTUBE_SEARCH, SearchProperty.GOOGLE_WEB_SEARCH}
            ),
            supported_regions=None,
            supported_granularities=frozenset(_STEP),
            requires_authentication=False,
            historical_data_available=True,
            data_nature=DataNature.SYNTHETIC,
            enabled=enabled,
            retention=(
                RetentionRule(
                    record_kind=RecordKind.SEARCH_INTEREST_OBSERVATION,
                    persist_allowed=True,
                    max_retention=None,
                    notes="Synthetic data; no third-party retention limits apply.",
                ),
            ),
            usage_restrictions=("SYNTHETIC DATA. Never use as evidence of real demand.",),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def status(self) -> ProviderStatusReport:
        status = ProviderStatus.AVAILABLE if self._enabled else ProviderStatus.DISABLED
        detail = "Synthetic provider ready." if self._enabled else "Disabled by configuration."
        return ProviderStatusReport(
            provider_name=PROVIDER_NAME,
            status=status,
            detail=detail,
            capabilities=(CapabilityStatus(capability=Capability.SEARCH_INTEREST, status=status),),
            checked_at=self._clock(),
        )

    async def aclose(self) -> None:
        return None

    def scenario_for(self, request: SearchInterestRequest) -> MockScenario:
        override = self._overrides.get(canonicalize_query(request.query))
        if override is not None:
            return override
        raw = request.provider_options.get("scenario", MockScenario.STABLE.value)
        try:
            return MockScenario(raw)
        except ValueError:
            valid = ", ".join(s.value for s in MockScenario)
            raise InvalidRequestError(
                f"unknown mock scenario {raw!r}; valid: {valid}", provider=PROVIDER_NAME
            ) from None

    async def fetch_search_interest(
        self, request: SearchInterestRequest, ctx: RequestContext
    ) -> SearchInterestBatch:
        if not self._enabled:
            raise ProviderDisabledError("mock provider disabled", provider=PROVIDER_NAME)
        if request.granularity not in _STEP:
            raise CapabilityNotSupportedError(
                f"granularity {request.granularity.value!r} not supported by mock provider",
                provider=PROVIDER_NAME,
            )
        if request.search_property not in self._descriptor.search_properties:
            raise CapabilityNotSupportedError(
                f"search property {request.search_property.value!r} not supported",
                provider=PROVIDER_NAME,
            )
        scenario = self.scenario_for(request)
        if scenario is MockScenario.PROVIDER_FAILURE:
            raise TemporarilyUnavailableError(
                "synthetic provider failure (scenario=provider_failure)", provider=PROVIDER_NAME
            )
        if scenario is MockScenario.RATE_LIMITED:
            raise RateLimitedError(
                "synthetic rate limit (scenario=rate_limited)", provider=PROVIDER_NAME
            )

        collected_at = self._clock()
        windows = self._windows(request, collected_at)
        rng = random.Random(self._seed_for(request, scenario))  # noqa: S311 - synthetic data
        series = _generate(scenario, len(windows), rng)

        canonical = canonicalize_query(request.query)
        geography = request.region or WORLD
        ref = (
            f"synthetic://mock/{scenario.value}?q={canonical}&geo={geography}"
            f"&start={request.start.date()}&end={request.end.date()}"
        )
        measurement = (
            MeasurementType.DIRECT_SEARCH_INTEREST
            if request.search_property is SearchProperty.YOUTUBE_SEARCH
            else MeasurementType.EXTERNAL_SEARCH_INTEREST
        )

        def context(batch_suffix: str) -> NormalizationContext:
            return NormalizationContext(
                scale_type=ScaleType.RELATIVE_PER_REQUEST,
                method=SCALE_METHOD,
                scale_version="1",
                batch_id=f"mock-{self._seed_for(request, scenario):x}{batch_suffix}",
                co_normalized_terms=(canonical,),
                reference_geography=geography,
                reference_start=request.start,
                reference_end=request.end,
                granularity=request.granularity,
                search_property=request.search_property,
                notes="SYNTHETIC",
            )

        # Normalise per batch. MIXED_NORMALIZATION splits the request into two batches
        # normalised independently, which makes values across the halves incomparable.
        if scenario is MockScenario.MIXED_NORMALIZATION and len(series) >= 2:
            half = len(series) // 2
            batches = [(0, half, context("-a")), (half, len(series), context("-b"))]
        else:
            batches = [(0, len(series), context(""))]

        candidates: list[ObservationCandidate] = []
        for lo, hi, ctx_norm in batches:
            normalised = _normalise(series[lo:hi])
            for (start, end), (value, quality) in zip(windows[lo:hi], normalised, strict=True):
                if end > collected_at and value is not None:
                    quality = SampleQuality.PARTIAL_PERIOD
                candidates.append(
                    ObservationCandidate(
                        query=request.query,
                        query_language=request.language,
                        query_region=request.region,
                        provider_name=PROVIDER_NAME,
                        provider_version=PROVIDER_VERSION,
                        measurement_type=measurement.value,
                        search_property=request.search_property.value,
                        observation_start=start,
                        observation_end=end,
                        granularity=request.granularity.value,
                        collected_at=collected_at,
                        available_at=min(end + AVAILABILITY_LAG, collected_at),
                        value=value,
                        scale_type=ScaleType.RELATIVE_PER_REQUEST.value,
                        normalization_context=ctx_norm,
                        geography=geography,
                        language=request.language,
                        sample_quality=quality.value,
                        data_nature=DataNature.SYNTHETIC.value,
                        source_reference=ref,
                    )
                )

        candidates = _post_process(scenario, candidates, rng)
        return SearchInterestBatch(
            provider_name=PROVIDER_NAME,
            provider_version=PROVIDER_VERSION,
            request=request,
            observations=tuple(candidates),
            warnings=("SYNTHETIC DATA - not a measurement of real search demand",),
            source_reference=ref,
        )

    # ------------------------------------------------------------------ internals
    def _seed_for(self, request: SearchInterestRequest, scenario: MockScenario) -> int:
        key = "|".join(
            [
                str(self._seed),
                scenario.value,
                canonicalize_query(request.query),
                request.region or WORLD,
                request.granularity.value,
                request.search_property.value,
                request.start.isoformat(),
                request.end.isoformat(),
            ]
        )
        return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")

    def _windows(
        self, request: SearchInterestRequest, now: datetime
    ) -> list[tuple[datetime, datetime]]:
        step = _STEP[request.granularity]
        cursor = datetime.combine(request.start.date(), time.min, tzinfo=UTC)
        windows: list[tuple[datetime, datetime]] = []
        while cursor < request.end and cursor < now and len(windows) < MAX_WINDOWS:
            windows.append((cursor, cursor + step))
            cursor += step
        return windows


def _generate(scenario: MockScenario, n: int, rng: random.Random) -> _Series:
    complete = SampleQuality.COMPLETE

    def noisy(base: float, sd: float = 3.0) -> float:
        return max(0.0, base + rng.gauss(0.0, sd))

    def ramp(i: int, start: float, stop: float) -> float:
        return start + (stop - start) * (i / (n - 1) if n > 1 else 0.0)

    series: _Series = []
    match scenario:
        case (
            MockScenario.GRADUAL_GROWTH
            | MockScenario.OUT_OF_ORDER
            | (MockScenario.MIXED_NORMALIZATION)
        ):
            series = [(noisy(ramp(i, 30, 70)), complete) for i in range(n)]
        case MockScenario.DECLINING:
            series = [(noisy(ramp(i, 80, 25)), complete) for i in range(n)]
        case MockScenario.SUDDEN_SURGE:
            onset = max(0, n - 7)
            for i in range(n):
                level = 20.0 + (75.0 * math.exp(-(i - onset) / 3.0) if i >= onset else 0.0)
                series.append((noisy(level, 2.0), complete))
        case MockScenario.SPARSE:
            for _ in range(n):
                roll = rng.random()
                if roll < 0.4:
                    series.append((None, SampleQuality.SUPPRESSED))
                elif roll < 0.7:
                    series.append((rng.uniform(1, 5), SampleQuality.LOW_VOLUME))
                else:
                    series.append((rng.uniform(3, 12), complete))
        case MockScenario.MISSING_DATA:
            for i in range(n):
                missing = i % 5 == 2  # deterministic gaps: ~20% of windows
                series.append((None, SampleQuality.MISSING) if missing else (noisy(50), complete))
        case _:  # STABLE, DUPLICATES, INVALID_RECORDS
            series = [(noisy(50.0), complete) for _ in range(n)]
    return series


def _normalise(series: _Series) -> _Series:
    """Scale to 0-100 relative to the batch maximum, rounded like Trends integers."""
    peak = max((v for v, _ in series if v is not None), default=0.0)
    out: _Series = []
    for value, quality in series:
        if value is None:
            out.append((None, quality))
        elif peak <= 0:
            out.append((0.0, SampleQuality.LOW_VOLUME))
        else:
            out.append((float(round(100.0 * value / peak)), quality))
    return out


def _post_process(
    scenario: MockScenario, candidates: list[ObservationCandidate], rng: random.Random
) -> list[ObservationCandidate]:
    if not candidates:
        return candidates
    if scenario is MockScenario.OUT_OF_ORDER:
        shuffled = list(candidates)
        rng.shuffle(shuffled)
        return shuffled
    if scenario is MockScenario.DUPLICATES:
        extra = [c.model_copy() for c in candidates[: min(3, len(candidates))]]
        result = candidates + extra
        if len(candidates) > 3:
            conflicting = candidates[3]
            new_value = 100.0 - (conflicting.value or 0.0) if conflicting.value != 50 else 49.0
            result.append(conflicting.model_copy(update={"value": new_value}))
        return result
    if scenario is MockScenario.INVALID_RECORDS:
        bad = list(candidates)
        mutations: list[dict[str, object]] = [
            {"value": math.nan},
            {"value": 150.0},
            {"observation_end": bad[0].observation_start - timedelta(days=1)},  # type: ignore[operator]
            {"geography": "ZZ"},
            {"source_reference": None},
            {"observation_start": datetime(2025, 1, 1), "observation_end": datetime(2025, 1, 2)},  # noqa: DTZ001 - deliberately naive
        ]
        for index, mutation in enumerate(mutations):
            if index < len(bad):
                bad[index] = bad[index].model_copy(update=mutation)
        return bad
    return candidates
