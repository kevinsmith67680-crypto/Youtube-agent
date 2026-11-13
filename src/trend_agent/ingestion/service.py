"""Ingestion orchestration.

Pipeline for every operation:
  resolve provider -> check capability -> check readiness (no quota spent if not ready)
  -> record run -> fetch (bounded concurrency, per-unit timeout, typed errors)
  -> validate -> apply storage policy -> persist in ONE transaction -> finish run record
  -> return a structured summary.

Accounting invariant (no silent data loss): every record received from a provider ends in
exactly one outcome bucket: stored, already_present, duplicate_in_batch, rejected or
withheld (storage not permitted). ``IngestionSummary.is_balanced`` checks this.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.exc import SQLAlchemyError

from trend_agent.db.repositories import (
    IngestionRunRepository,
    InsertResult,
    ObservationRepository,
    ProviderRepository,
    QueryRepository,
    RejectionRecord,
    VideoRepository,
)
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.ingestion.retention import storage_decision
from trend_agent.ingestion.validation import ValidationConfig, validate_batch
from trend_agent.logging import get_logger, log_event, redact_text
from trend_agent.providers.base import (
    Provider,
    RequestContext,
    SearchInterestProvider,
    VideoDataProvider,
)
from trend_agent.providers.errors import (
    AccessRestrictedError,
    CapabilityNotSupportedError,
    ProviderDisabledError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderTimeoutError,
    QuotaExhaustedError,
    RateLimitedError,
    TemporarilyUnavailableError,
)
from trend_agent.providers.quota import QuotaLedger
from trend_agent.providers.registry import ProviderRegistry
from trend_agent.schemas.enums import (
    Capability,
    IngestionStatus,
    ProviderStatus,
    RecordKind,
    RejectionReason,
    SearchProperty,
    TimeGranularity,
)
from trend_agent.schemas.observation import ValidatedObservation
from trend_agent.schemas.provider import (
    PopularVideosRequest,
    ProviderDescriptor,
    ProviderStatusReport,
    SearchInterestBatch,
    SearchInterestRequest,
    VideoSearchRequest,
)
from trend_agent.schemas.video import ChannelDetailsResponse, VideoDetails, VideoSearchResponse
from trend_agent.utils.codes import normalize_language, normalize_region
from trend_agent.utils.text import canonicalize_query, clean_original_query
from trend_agent.utils.time import Clock, ensure_utc, utc_now

_log = get_logger(__name__)
T = TypeVar("T")


class Outcome(StrEnum):
    STORED = "stored"
    ALREADY_PRESENT = "already_present"
    DUPLICATE_IN_BATCH = "duplicate_in_batch"
    REJECTED = "rejected"
    WITHHELD = "withheld"


class FetchOutcome(BaseModel):
    unit: str
    attempted: bool = True
    succeeded: bool
    records: int = 0
    error_type: str | None = None
    error_message: str | None = None


class IngestionSummary(BaseModel):
    run_id: uuid.UUID | None
    provider: str
    operation: str
    status: IngestionStatus
    started_at: datetime
    completed_at: datetime
    by_kind: dict[str, dict[str, int]] = Field(default_factory=dict)
    rejected_by_reason: dict[str, int] = Field(default_factory=dict)
    fetches: list[FetchOutcome] = Field(default_factory=list)
    quota_used: dict[str, int] = Field(default_factory=dict)
    error_type: str | None = None
    error_message: str | None = None
    warnings: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    def _total(self, outcome: Outcome) -> int:
        return sum(counts.get(outcome.value, 0) for counts in self.by_kind.values())

    @property
    def records_received(self) -> int:
        return sum(counts.get("received", 0) for counts in self.by_kind.values())

    @property
    def records_stored(self) -> int:
        return self._total(Outcome.STORED)

    @property
    def records_already_present(self) -> int:
        return self._total(Outcome.ALREADY_PRESENT)

    @property
    def records_duplicate_in_batch(self) -> int:
        return self._total(Outcome.DUPLICATE_IN_BATCH)

    @property
    def records_rejected(self) -> int:
        return self._total(Outcome.REJECTED)

    @property
    def records_withheld(self) -> int:
        return self._total(Outcome.WITHHELD)

    @property
    def estimated_quota_used(self) -> int:
        return sum(self.quota_used.values())

    def is_balanced(self) -> bool:
        return all(
            counts.get("received", 0) == sum(counts.get(o.value, 0) for o in Outcome)
            for counts in self.by_kind.values()
        )

    def totals(self) -> dict[str, int]:
        return {
            "received": self.records_received,
            "stored": self.records_stored,
            "already_present": self.records_already_present,
            "duplicate_in_batch": self.records_duplicate_in_batch,
            "rejected": self.records_rejected,
            "withheld": self.records_withheld,
        }


class _Tally:
    def __init__(self) -> None:
        self.by_kind: dict[str, dict[str, int]] = {}

    def add(self, kind: RecordKind, outcome: Outcome, n: int = 1) -> None:
        counts = self.by_kind.setdefault(kind.value, {"received": 0})
        counts["received"] += n
        counts[outcome.value] = counts.get(outcome.value, 0) + n


class SearchInterestIngestion(BaseModel):
    """Request to ingest search interest for one or more queries from one provider."""

    model_config = ConfigDict(frozen=True)

    provider: str
    queries: tuple[str, ...] = Field(min_length=1, max_length=100)
    region: str | None = None
    language: str | None = None
    start: datetime
    end: datetime
    granularity: TimeGranularity = TimeGranularity.DAY
    search_property: SearchProperty = SearchProperty.YOUTUBE_SEARCH
    provider_options: dict[str, str] = Field(default_factory=dict)
    request_budget: dict[str, int] | None = None

    @field_validator("region")
    @classmethod
    def _region(cls, value: str | None) -> str | None:
        return normalize_region(value) if value is not None else None

    @field_validator("language")
    @classmethod
    def _language(cls, value: str | None) -> str | None:
        return normalize_language(value) if value is not None else None

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


@dataclass(frozen=True)
class IngestionSettings:
    max_concurrency: int = 4
    fetch_timeout_seconds: float = 120.0


@dataclass
class _RunContext:
    provider_name: str
    operation: str
    started_at: datetime
    provider_id: int | None = None
    run_id: uuid.UUID | None = None


def _status_error(report: ProviderStatusReport, capability: Capability) -> ProviderError:
    status = report.capability_status(capability)
    detail = report.detail
    name = report.provider_name
    match status:
        case ProviderStatus.NOT_CONFIGURED:
            return ProviderNotConfiguredError(
                detail, missing=report.missing_configuration, provider=name
            )
        case ProviderStatus.DISABLED:
            return ProviderDisabledError(detail, provider=name)
        case ProviderStatus.ACCESS_RESTRICTED:
            return AccessRestrictedError(detail, provider=name)
        case ProviderStatus.QUOTA_EXHAUSTED:
            return QuotaExhaustedError(
                f"{detail} ({capability.value})",
                bucket="unknown",
                resets_at=None,
                source="local_estimate",
                provider=name,
            )
        case ProviderStatus.UNSUPPORTED:
            return CapabilityNotSupportedError(
                f"{name} does not support {capability.value}", provider=name
            )
        case ProviderStatus.RATE_LIMITED:
            return RateLimitedError(detail, provider=name)
        case _:
            return TemporarilyUnavailableError(detail, provider=name)


def _fail(unit: str, exc: BaseException, attempted: bool = True) -> FetchOutcome:
    if isinstance(exc, ProviderError):
        return FetchOutcome(
            unit=unit,
            attempted=attempted,
            succeeded=False,
            error_type=exc.error_type,
            error_message=exc.message,
        )
    return FetchOutcome(
        unit=unit,
        attempted=attempted,
        succeeded=False,
        error_type="internal_error",
        error_message=redact_text(f"{type(exc).__name__}: {exc}")[:500],
    )


def _overall_status(fetches: Sequence[FetchOutcome]) -> IngestionStatus:
    failed = [f for f in fetches if not f.succeeded]
    if not failed:
        return IngestionStatus.SUCCEEDED
    if len(failed) == len(fetches):
        return IngestionStatus.FAILED
    return IngestionStatus.PARTIAL


def _first_error(fetches: Sequence[FetchOutcome]) -> tuple[str | None, str | None]:
    failed = [f for f in fetches if not f.succeeded]
    if not failed:
        return None, None
    types = {f.error_type for f in failed}
    error_type = failed[0].error_type if len(types) == 1 else "multiple"
    message = "; ".join(f"{f.unit}: {f.error_message}" for f in failed[:5])
    return error_type, message[:2000]


class IngestionService:
    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        session_factory: SessionFactory,
        settings: IngestionSettings | None = None,
        clock: Clock = utc_now,
        validation: ValidationConfig | None = None,
    ) -> None:
        self._registry = registry
        self._factory = session_factory
        self._settings = settings or IngestionSettings()
        self._clock = clock
        self._validation = validation or ValidationConfig()

    # ================================================================ search interest
    async def ingest_search_interest(self, req: SearchInterestIngestion) -> IngestionSummary:
        run = _RunContext(req.provider, "search_interest", self._clock())
        params = req.model_dump(mode="json")
        try:
            provider = self._registry.search_interest(req.provider)
            self._require_ready(provider, Capability.SEARCH_INTEREST)
        except ProviderError as exc:
            return self._fail_before_fetch(run, params, exc, self._descriptor_or_none(req.provider))
        descriptor = provider.descriptor
        early = self._start_run(run, descriptor, params)
        if early is not None:
            return early

        ctx = RequestContext(ledger=QuotaLedger(budget=req.request_budget))
        try:
            fetches, batches = await self._fetch_search_interest(provider, req, ctx)
        except asyncio.CancelledError:
            self._finish_cancelled(run, ctx.ledger)
            raise

        candidates = [c for batch in batches for c in batch.observations]
        report = validate_batch(candidates, descriptor, now=self._clock(), config=self._validation)
        warnings = sorted({w for batch in batches for w in batch.warnings})
        tally = _Tally()
        kind = RecordKind.SEARCH_INTEREST_OBSERVATION
        tally.add(kind, Outcome.DUPLICATE_IN_BATCH, 0)
        if report.duplicates_in_batch:
            tally.add(kind, Outcome.DUPLICATE_IN_BATCH, report.duplicates_in_batch)
        rejected_by_reason = report.rejected_by_reason()
        payload_policy = storage_decision(descriptor, kind, self._clock())

        def persist(session: Any) -> None:
            assert run.provider_id is not None
            queries = QueryRepository(session)
            observations = ObservationRepository(session)
            query_ids: dict[tuple[str, str | None, str | None], int] = {}
            rejections: list[RejectionRecord] = []
            for obs in report.accepted:
                decision = storage_decision(descriptor, kind, obs.collected_at)
                if not decision.allowed:
                    tally.add(kind, Outcome.WITHHELD)
                    continue
                key = (obs.canonical_query, obs.query_language, obs.query_region)
                if key not in query_ids:
                    query_ids[key] = queries.get_or_create(
                        obs.canonical_query,
                        obs.original_query,
                        obs.query_language,
                        obs.query_region,
                    )
                result, detail = observations.insert(
                    obs,
                    query_id=query_ids[key],
                    provider_id=run.provider_id,
                    run_id=run.run_id,
                    expires_at=decision.expires_at,
                )
                if result is InsertResult.INSERTED:
                    tally.add(kind, Outcome.STORED)
                elif result is InsertResult.ALREADY_PRESENT:
                    tally.add(kind, Outcome.ALREADY_PRESENT)
                else:
                    tally.add(kind, Outcome.REJECTED)
                    reason = RejectionReason.CONFLICTS_WITH_STORED.value
                    rejected_by_reason[reason] = rejected_by_reason.get(reason, 0) + 1
                    rejections.append(
                        RejectionRecord(
                            kind, RejectionReason.CONFLICTS_WITH_STORED, detail, _obs_payload(obs)
                        )
                    )
            for item in report.rejected:
                tally.add(kind, Outcome.REJECTED)
                rejections.append(
                    RejectionRecord(
                        kind,
                        item.primary_reason,
                        item.detail,
                        item.candidate.diagnostic_payload()
                        if payload_policy.allowed
                        else {"payload": "withheld: provider data may not be stored"},
                    )
                )
            IngestionRunRepository(session).add_rejections(
                run.run_id,  # type: ignore[arg-type]
                run.provider_id,
                rejections,
                payload_policy.expires_at,
            )

        return self._persist_and_finish(
            run,
            persist,
            tally,
            fetches=fetches,
            ledger=ctx.ledger,
            rejected_by_reason=rejected_by_reason,
            warnings=warnings,
            extra={"queries": list(req.queries)},
        )

    async def _fetch_search_interest(
        self, provider: SearchInterestProvider, req: SearchInterestIngestion, ctx: RequestContext
    ) -> tuple[list[FetchOutcome], list[SearchInterestBatch]]:
        semaphore = asyncio.Semaphore(self._settings.max_concurrency)
        stop = asyncio.Event()

        async def one(query: str) -> tuple[FetchOutcome, SearchInterestBatch | None]:
            async with semaphore:
                if stop.is_set():
                    return (
                        FetchOutcome(
                            unit=query,
                            attempted=False,
                            succeeded=False,
                            error_type=QuotaExhaustedError.error_type,
                            error_message="not attempted: quota exhausted earlier in this run",
                        ),
                        None,
                    )
                try:
                    request = SearchInterestRequest(
                        query=query,
                        region=req.region,
                        language=req.language,
                        start=req.start,
                        end=req.end,
                        granularity=req.granularity,
                        search_property=req.search_property,
                        provider_options=req.provider_options,
                    )
                    batch = await self._with_timeout(provider.fetch_search_interest(request, ctx))
                except QuotaExhaustedError as exc:
                    stop.set()
                    return _fail(query, exc), None
                except ValidationError as exc:
                    return (
                        FetchOutcome(
                            unit=query,
                            succeeded=False,
                            error_type="invalid_request",
                            error_message=str(exc.errors()[0].get("msg", "invalid request")),
                        ),
                        None,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not isinstance(exc, ProviderError):
                        _log.exception("ingestion.unexpected_provider_error")
                    return _fail(query, exc), None
                return FetchOutcome(
                    unit=query, succeeded=True, records=len(batch.observations)
                ), batch

        results = await asyncio.gather(*(one(q) for q in req.queries))
        return [r[0] for r in results], [r[1] for r in results if r[1] is not None]

    # ================================================================ videos
    async def ingest_video_search(
        self,
        provider_name: str,
        request: VideoSearchRequest,
        *,
        fetch_details: bool = True,
        fetch_channels: bool = True,
        request_budget: dict[str, int] | None = None,
    ) -> IngestionSummary:
        run = _RunContext(provider_name, "video_search", self._clock())
        params = {
            **request.model_dump(mode="json"),
            "fetch_details": fetch_details,
            "fetch_channels": fetch_channels,
        }
        try:
            provider = self._registry.video_data(provider_name)
            self._require_ready(provider, Capability.VIDEO_SEARCH)
        except ProviderError as exc:
            return self._fail_before_fetch(
                run, params, exc, self._descriptor_or_none(provider_name)
            )
        descriptor = provider.descriptor
        early = self._start_run(run, descriptor, params)
        if early is not None:
            return early

        ctx = RequestContext(ledger=QuotaLedger(budget=request_budget))
        fetches: list[FetchOutcome] = []
        search: VideoSearchResponse | None = None
        videos: list[VideoDetails] = []
        channels: ChannelDetailsResponse | None = None
        warnings: list[str] = []
        extra: dict[str, Any] = {}
        try:
            try:
                search = await self._with_timeout(provider.search_videos(request, ctx))
                fetches.append(
                    FetchOutcome(unit="search", succeeded=True, records=len(search.results))
                )
                warnings.extend(search.warnings)
                extra["approx_matching_videos_not_search_volume"] = search.approx_matching_videos
                extra["more_results_available"] = search.more_available
            except ProviderError as exc:
                fetches.append(_fail("search", exc))
            if search is not None and fetch_details and search.results:
                try:
                    details = await self._with_timeout(
                        provider.get_videos([r.video_id for r in search.results], ctx)
                    )
                    videos = list(details.videos)
                    warnings.extend(details.warnings)
                    if details.missing_ids:
                        warnings.append(
                            f"{len(details.missing_ids)} search results unavailable in videos.list "
                            "(deleted or private since search)"
                        )
                    fetches.append(
                        FetchOutcome(unit="video_details", succeeded=True, records=len(videos))
                    )
                except ProviderError as exc:
                    fetches.append(_fail("video_details", exc))
            if search is not None and fetch_channels and search.results:
                channels = await self._fetch_missing_channels(
                    provider, {r.channel_id for r in search.results}, ctx, fetches, extra
                )
                if channels is not None:
                    warnings.extend(channels.warnings)
        except asyncio.CancelledError:
            self._finish_cancelled(run, ctx.ledger)
            raise

        tally = _Tally()
        query_text = request.query

        def persist(session: Any) -> None:
            repo = VideoRepository(session)
            assert run.provider_id is not None
            if search is not None:
                query_id = QueryRepository(session).get_or_create(
                    canonicalize_query(query_text),
                    clean_original_query(query_text),
                    request.language,
                    request.region,
                )
                video_rule = storage_decision(descriptor, RecordKind.VIDEO, search.collected_at)
                result_rule = storage_decision(
                    descriptor, RecordKind.VIDEO_SEARCH_RESULT, search.collected_at
                )
                for result in search.results:
                    if video_rule.allowed and video_rule.expires_at is not None:
                        created = repo.insert_video_from_search(
                            result,
                            provider_id=run.provider_id,
                            collected_at=search.collected_at,
                            expires_at=video_rule.expires_at,
                            persist_description=self._persist_descriptions(provider),
                        )
                        tally.add(
                            RecordKind.VIDEO, Outcome.STORED if created else Outcome.ALREADY_PRESENT
                        )
                    else:
                        tally.add(RecordKind.VIDEO, Outcome.WITHHELD)
                    if (
                        result_rule.allowed
                        and result_rule.expires_at is not None
                        and video_rule.allowed
                    ):
                        stored = repo.insert_search_result(
                            result,
                            query_id=query_id,
                            provider_id=run.provider_id,
                            run_id=run.run_id,
                            region=search.region,
                            language=search.language,
                            order=search.order,
                            published_after=search.published_after,
                            published_before=search.published_before,
                            collected_at=search.collected_at,
                            expires_at=result_rule.expires_at,
                        )
                        tally.add(
                            RecordKind.VIDEO_SEARCH_RESULT,
                            Outcome.STORED if stored else Outcome.ALREADY_PRESENT,
                        )
                    else:
                        tally.add(RecordKind.VIDEO_SEARCH_RESULT, Outcome.WITHHELD)
            self._persist_video_details(session, descriptor, provider, run, videos, tally)
            self._persist_channels(session, descriptor, run, channels, tally)
            self._record_malformed(session, run, warnings, tally)

        return self._persist_and_finish(
            run, persist, tally, fetches=fetches, ledger=ctx.ledger, warnings=warnings, extra=extra
        )

    async def ingest_popular_videos(
        self,
        provider_name: str,
        request: PopularVideosRequest,
        *,
        fetch_channels: bool = True,
        request_budget: dict[str, int] | None = None,
    ) -> IngestionSummary:
        run = _RunContext(provider_name, "popular_videos", self._clock())
        params = {**request.model_dump(mode="json"), "fetch_channels": fetch_channels}
        try:
            provider = self._registry.video_data(provider_name)
            self._require_ready(provider, Capability.TREND_DISCOVERY)
        except ProviderError as exc:
            return self._fail_before_fetch(
                run, params, exc, self._descriptor_or_none(provider_name)
            )
        descriptor = provider.descriptor
        early = self._start_run(run, descriptor, params)
        if early is not None:
            return early

        ctx = RequestContext(ledger=QuotaLedger(budget=request_budget))
        fetches: list[FetchOutcome] = []
        videos: list[VideoDetails] = []
        channels: ChannelDetailsResponse | None = None
        warnings: list[str] = []
        extra: dict[str, Any] = {}
        try:
            try:
                popular = await self._with_timeout(provider.popular_videos(request, ctx))
                videos = list(popular.videos)
                warnings.extend(popular.warnings)
                fetches.append(FetchOutcome(unit="popular", succeeded=True, records=len(videos)))
            except ProviderError as exc:
                fetches.append(_fail("popular", exc))
            if videos and fetch_channels:
                channels = await self._fetch_missing_channels(
                    provider, {v.channel_id for v in videos}, ctx, fetches, extra
                )
                if channels is not None:
                    warnings.extend(channels.warnings)
        except asyncio.CancelledError:
            self._finish_cancelled(run, ctx.ledger)
            raise

        tally = _Tally()

        def persist(session: Any) -> None:
            self._persist_video_details(session, descriptor, provider, run, videos, tally)
            self._persist_channels(session, descriptor, run, channels, tally)
            self._record_malformed(session, run, warnings, tally)

        return self._persist_and_finish(
            run, persist, tally, fetches=fetches, ledger=ctx.ledger, warnings=warnings, extra=extra
        )

    async def refresh_expiring_videos(
        self,
        provider_name: str,
        *,
        horizon: timedelta = timedelta(days=2),
        limit: int = 500,
        request_budget: dict[str, int] | None = None,
    ) -> IngestionSummary:
        """Re-fetch metadata for videos nearing expiry; delete those no longer available."""
        run = _RunContext(provider_name, "refresh_videos", self._clock())
        params = {"horizon_seconds": horizon.total_seconds(), "limit": limit}
        try:
            provider = self._registry.video_data(provider_name)
            self._require_ready(provider, Capability.VIDEO_STATISTICS)
        except ProviderError as exc:
            return self._fail_before_fetch(
                run, params, exc, self._descriptor_or_none(provider_name)
            )
        descriptor = provider.descriptor
        early = self._start_run(run, descriptor, params)
        if early is not None:
            return early
        with transaction(self._factory) as session:
            due = list(
                VideoRepository(session).videos_due_for_refresh(self._clock(), horizon, limit)
            )
        ctx = RequestContext(ledger=QuotaLedger(budget=request_budget))
        fetches: list[FetchOutcome] = []
        videos: list[VideoDetails] = []
        missing: list[str] = []
        warnings: list[str] = []
        if due:
            try:
                details = await self._with_timeout(provider.get_videos(due, ctx))
                videos, missing = list(details.videos), list(details.missing_ids)
                warnings.extend(details.warnings)
                fetches.append(
                    FetchOutcome(unit="video_details", succeeded=True, records=len(videos))
                )
            except asyncio.CancelledError:
                self._finish_cancelled(run, ctx.ledger)
                raise
            except ProviderError as exc:
                fetches.append(_fail("video_details", exc))
        tally = _Tally()
        extra: dict[str, Any] = {"due": len(due), "deleted_unavailable": 0}

        def persist(session: Any) -> None:
            self._persist_video_details(session, descriptor, provider, run, videos, tally)
            if fetches and fetches[0].succeeded:
                # Videos the API no longer returns (deleted/private) must not be retained.
                extra["deleted_unavailable"] = VideoRepository(session).delete_videos(missing)
            self._record_malformed(session, run, warnings, tally)

        return self._persist_and_finish(
            run, persist, tally, fetches=fetches, ledger=ctx.ledger, warnings=warnings, extra=extra
        )

    # ================================================================ helpers
    async def _fetch_missing_channels(
        self,
        provider: VideoDataProvider,
        channel_ids: set[str],
        ctx: RequestContext,
        fetches: list[FetchOutcome],
        extra: dict[str, Any],
    ) -> ChannelDetailsResponse | None:
        """Fetch only channels without fresh stored metadata (avoids needless lookups)."""
        with transaction(self._factory) as session:
            fresh = VideoRepository(session).fresh_channel_ids(channel_ids, self._clock())
        needed = sorted(channel_ids - fresh)
        extra["channels_skipped_fresh"] = len(fresh)
        if not needed:
            return None
        try:
            channels = await self._with_timeout(provider.get_channels(needed, ctx))
        except ProviderError as exc:
            fetches.append(_fail("channels", exc))
            return None
        fetches.append(
            FetchOutcome(unit="channels", succeeded=True, records=len(channels.channels))
        )
        return channels

    def _persist_descriptions(self, provider: Provider) -> bool:
        config = getattr(provider, "retention_config", None)
        return bool(getattr(config, "persist_descriptions", True))

    def _persist_video_details(
        self,
        session: Any,
        descriptor: ProviderDescriptor,
        provider: Provider,
        run: _RunContext,
        videos: Sequence[VideoDetails],
        tally: _Tally,
    ) -> None:
        assert run.provider_id is not None
        repo = VideoRepository(session)
        for video in videos:
            video_rule = storage_decision(descriptor, RecordKind.VIDEO, video.collected_at)
            if video_rule.allowed and video_rule.expires_at is not None:
                repo.upsert_video_details(
                    video,
                    provider_id=run.provider_id,
                    expires_at=video_rule.expires_at,
                    persist_description=self._persist_descriptions(provider),
                )
                tally.add(RecordKind.VIDEO, Outcome.STORED)
            else:
                tally.add(RecordKind.VIDEO, Outcome.WITHHELD)
            if video.statistics is None:
                continue
            stats_rule = storage_decision(
                descriptor, RecordKind.VIDEO_STATISTICS, video.collected_at
            )
            if stats_rule.allowed and stats_rule.expires_at is not None and video_rule.allowed:
                stored = repo.insert_statistics(
                    video.video_id,
                    video.statistics,
                    collected_at=video.collected_at,
                    provider_id=run.provider_id,
                    run_id=run.run_id,
                    expires_at=stats_rule.expires_at,
                )
                tally.add(
                    RecordKind.VIDEO_STATISTICS,
                    Outcome.STORED if stored else Outcome.ALREADY_PRESENT,
                )
            else:
                tally.add(RecordKind.VIDEO_STATISTICS, Outcome.WITHHELD)

    def _persist_channels(
        self,
        session: Any,
        descriptor: ProviderDescriptor,
        run: _RunContext,
        channels: ChannelDetailsResponse | None,
        tally: _Tally,
    ) -> None:
        if channels is None:
            return
        assert run.provider_id is not None
        repo = VideoRepository(session)
        for channel in channels.channels:
            rule = storage_decision(descriptor, RecordKind.CHANNEL, channel.collected_at)
            if rule.allowed and rule.expires_at is not None:
                repo.upsert_channel(
                    channel, provider_id=run.provider_id, expires_at=rule.expires_at
                )
                tally.add(RecordKind.CHANNEL, Outcome.STORED)
            else:
                tally.add(RecordKind.CHANNEL, Outcome.WITHHELD)

    def _record_malformed(
        self, session: Any, run: _RunContext, warnings: Sequence[str], tally: _Tally
    ) -> None:
        malformed = [w for w in warnings if w.startswith(("item ", "page "))]
        if not malformed:
            return
        for _ in malformed:
            tally.add(RecordKind.VIDEO, Outcome.REJECTED)
        IngestionRunRepository(session).add_rejections(
            run.run_id,  # type: ignore[arg-type]
            run.provider_id,
            [
                RejectionRecord(RecordKind.VIDEO, RejectionReason.MALFORMED_PROVIDER_ITEM, w, {})
                for w in malformed
            ],
            None,
        )

    async def _with_timeout(self, awaitable: Awaitable[T]) -> T:
        try:
            async with asyncio.timeout(self._settings.fetch_timeout_seconds):
                return await awaitable
        except TimeoutError:
            raise ProviderTimeoutError(
                f"fetch exceeded {self._settings.fetch_timeout_seconds}s ingestion timeout"
            ) from None

    def _descriptor_or_none(self, name: str) -> ProviderDescriptor | None:
        try:
            return self._registry.get(name).descriptor
        except ProviderError:
            return None

    def _require_ready(self, provider: Provider, capability: Capability) -> None:
        """Fail fast (spending no quota) unless the capability is available."""
        report = provider.status()
        if report.capability_status(capability) is not ProviderStatus.AVAILABLE:
            raise _status_error(report, capability)

    def _start_run(
        self, run: _RunContext, descriptor: ProviderDescriptor, params: dict[str, Any]
    ) -> IngestionSummary | None:
        try:
            with transaction(self._factory) as session:
                run.provider_id = ProviderRepository(session).sync(descriptor)
                run.run_id = IngestionRunRepository(session).start(
                    provider_id=run.provider_id,
                    provider_name=run.provider_name,
                    operation=run.operation,
                    request_params=params,
                    started_at=run.started_at,
                )
        except SQLAlchemyError as exc:
            # Without a database nothing can be stored; do not spend quota fetching.
            return IngestionSummary(
                run_id=None,
                provider=run.provider_name,
                operation=run.operation,
                status=IngestionStatus.FAILED,
                started_at=run.started_at,
                completed_at=self._clock(),
                error_type="database_unavailable",
                error_message=redact_text(f"{type(exc).__name__}: {str(exc).splitlines()[0]}"),
            )
        log_event(
            _log,
            "ingestion.started",
            run_id=str(run.run_id),
            provider=run.provider_name,
            operation=run.operation,
        )
        return None

    def _fail_before_fetch(
        self,
        run: _RunContext,
        params: dict[str, Any],
        exc: ProviderError,
        descriptor: ProviderDescriptor | None,
    ) -> IngestionSummary:
        summary = IngestionSummary(
            run_id=None,
            provider=run.provider_name,
            operation=run.operation,
            status=IngestionStatus.FAILED,
            started_at=run.started_at,
            completed_at=self._clock(),
            error_type=exc.error_type,
            error_message=exc.message,
            extra={"missing_configuration": list(getattr(exc, "missing", ()))},
        )
        try:
            with transaction(self._factory) as session:
                provider_id = ProviderRepository(session).sync(descriptor) if descriptor else None
                runs = IngestionRunRepository(session)
                run_id = runs.start(
                    provider_id=provider_id,
                    provider_name=run.provider_name,
                    operation=run.operation,
                    request_params=params,
                    started_at=run.started_at,
                )
                runs.finish(
                    run_id,
                    status=IngestionStatus.FAILED,
                    completed_at=summary.completed_at,
                    received=0,
                    accepted=0,
                    rejected=0,
                    duplicate=0,
                    withheld=0,
                    quota_used=0,
                    quota_detail={},
                    error_type=exc.error_type,
                    error_message=exc.message,
                    summary=summary.model_dump(mode="json"),
                )
            summary.run_id = run_id
        except SQLAlchemyError as db_exc:
            summary.warnings.append(f"run could not be recorded: {type(db_exc).__name__}")
        log_event(
            _log,
            "ingestion.rejected_before_fetch",
            logging.INFO,
            provider=run.provider_name,
            operation=run.operation,
            error_type=exc.error_type,
        )
        return summary

    def _finish_cancelled(self, run: _RunContext, ledger: QuotaLedger) -> None:
        if run.run_id is None:
            return
        try:
            with transaction(self._factory) as session:
                IngestionRunRepository(session).finish(
                    run.run_id,
                    status=IngestionStatus.CANCELLED,
                    completed_at=self._clock(),
                    received=0,
                    accepted=0,
                    rejected=0,
                    duplicate=0,
                    withheld=0,
                    quota_used=ledger.total_units,
                    quota_detail=dict(ledger.used),
                    error_type="cancelled",
                    error_message="cancelled before persistence; nothing from this run was stored",
                    summary={},
                )
        except SQLAlchemyError:
            _log.exception("ingestion.cancel_record_failed")
        log_event(_log, "ingestion.cancelled", logging.WARNING, run_id=str(run.run_id))

    def _persist_and_finish(
        self,
        run: _RunContext,
        persist: Any,
        tally: _Tally,
        *,
        fetches: list[FetchOutcome],
        ledger: QuotaLedger,
        rejected_by_reason: dict[str, int] | None = None,
        warnings: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> IngestionSummary:
        assert run.run_id is not None
        status = _overall_status(fetches) if fetches else IngestionStatus.SUCCEEDED
        error_type, error_message = _first_error(fetches)
        summary = IngestionSummary(
            run_id=run.run_id,
            provider=run.provider_name,
            operation=run.operation,
            status=status,
            started_at=run.started_at,
            completed_at=run.started_at,
            fetches=fetches,
            quota_used=dict(ledger.used),
            error_type=error_type,
            error_message=error_message,
            warnings=list(warnings or []),
            extra=dict(extra or {}),
        )
        try:
            with transaction(self._factory) as session:
                persist(session)
                summary.by_kind = tally.by_kind
                # persist() may record operation-specific results (e.g. deletions) in extra.
                summary.extra = dict(extra or {})
                summary.rejected_by_reason = dict(rejected_by_reason or {})
                if not summary.rejected_by_reason and summary.records_rejected:
                    summary.rejected_by_reason = {
                        RejectionReason.MALFORMED_PROVIDER_ITEM.value: summary.records_rejected
                    }
                summary.completed_at = self._clock()
                self._finish_row(session, summary)
        except SQLAlchemyError as exc:
            # The transaction was rolled back: nothing from this run was stored.
            summary.by_kind = {}
            summary.status = IngestionStatus.FAILED
            summary.error_type = "database_error"
            summary.error_message = redact_text(
                f"{type(exc).__name__}: {str(exc).splitlines()[0]}; transaction rolled back, "
                "no records stored"
            )
            summary.completed_at = self._clock()
            try:
                with transaction(self._factory) as session:
                    self._finish_row(session, summary)
            except SQLAlchemyError:
                summary.warnings.append("run record could not be finalised")
        if not summary.is_balanced():  # pragma: no cover - invariant guard
            summary.warnings.append("INTERNAL: record accounting does not balance")
        log_event(
            _log,
            "ingestion.completed",
            run_id=str(summary.run_id),
            provider=summary.provider,
            operation=summary.operation,
            status=summary.status.value,
            totals=summary.totals(),
            quota_used=summary.quota_used,
        )
        return summary

    def _finish_row(self, session: Any, summary: IngestionSummary) -> None:
        assert summary.run_id is not None
        IngestionRunRepository(session).finish(
            summary.run_id,
            status=summary.status,
            completed_at=summary.completed_at,
            received=summary.records_received,
            accepted=summary.records_stored,
            rejected=summary.records_rejected,
            duplicate=summary.records_already_present + summary.records_duplicate_in_batch,
            withheld=summary.records_withheld,
            quota_used=summary.estimated_quota_used,
            quota_detail=summary.quota_used,
            error_type=summary.error_type,
            error_message=summary.error_message,
            summary=summary.model_dump(mode="json"),
        )


def _obs_payload(obs: ValidatedObservation) -> dict[str, Any]:
    return obs.model_dump(mode="json")
