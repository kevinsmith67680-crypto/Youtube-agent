"""Bounded, quota-aware, cached retrieval of YouTube search results.

* ``YouTubeResultSource`` uses the existing Phase 1 ingestion path (``search.list`` +
  ``videos.list`` through the quota manager), so results are stored with the YouTube
  30-day retention and counted against the daily search budget. Stored results younger
  than ``retrieval.cache_ttl`` are reused instead of repeating requests.
* ``FixtureResultSource`` serves deterministic fixtures (tests, demos, evaluation).

API order is recorded as ``api_position`` for that one request; it is not a YouTube
search ranking. ``approx_total_reported`` is the API's own estimate, never a count of
competing videos.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol

import sqlalchemy as sa

from trend_agent.content_analysis.entity_verification import QueryEntities
from trend_agent.content_analysis.result_normalization import dedupe, normalise
from trend_agent.db.models import SearchQueryRow, VideoRow, VideoSearchResultRow
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.schemas.search_intent import IntentClassification, SubIntent
from trend_agent.schemas.search_result import (
    CandidateVideo,
    RetrievalRequest,
    RetrievalStatus,
    RetrievedResultSet,
)
from trend_agent.utils.text import canonicalize_query
from trend_agent.verification.permissions import read_only

_VARIANT_WORD: dict[SubIntent, str] = {
    SubIntent.MODEL_INSTALLATION: "install",
    SubIntent.SOFTWARE_INSTALLATION: "install",
    SubIntent.LOCAL_MODEL_SETUP: "run locally",
    SubIntent.AI_AGENT_TUTORIAL: "tutorial",
    SubIntent.API_INTEGRATION: "api tutorial",
    SubIntent.VERSION_MIGRATION: "migration guide",
    SubIntent.ITEM_LOCATION: "location",
    SubIntent.EVENT_GUIDE: "event guide",
    SubIntent.CHALLENGE_COMPLETION: "challenges guide",
    SubIntent.MODEL_COMPARISON: "comparison",
    SubIntent.PRODUCT_COMPARISON: "comparison",
    SubIntent.PRODUCT_REVIEW: "review",
    SubIntent.TROUBLESHOOTING: "fix",
    SubIntent.GAME_TROUBLESHOOTING: "fix",
}


def make_variants(
    query: str, intent: IntentClassification, entities: QueryEntities, limit: int
) -> tuple[str, ...]:
    """At most ``limit - 1`` deterministic variants (entity name + intent word)."""
    if limit <= 1:
        return ()
    primary = entities.primary
    word = _VARIANT_WORD.get(intent.sub_intent) if intent.sub_intent else None
    out = []
    if primary is not None and word:
        out.append(f"{primary.ref.name.lower()} {word}")
    canon = canonicalize_query(query)
    return tuple(v for v in dict.fromkeys(out) if canonicalize_query(v) != canon)[: limit - 1]


class ResultSource(Protocol):
    name: str

    async def retrieve(self, request: RetrievalRequest, *, now: datetime) -> RetrievedResultSet: ...


class FixtureResultSource:
    """Deterministic results keyed by query text (each variant costs one simulated call)."""

    name = "fixture"

    def __init__(
        self,
        results: Mapping[str, Sequence[dict[str, Any]]],
        *,
        collected_at: datetime | None = None,
        retention: timedelta = timedelta(days=30),
        synthetic: bool = True,
        fail: bool = False,
    ) -> None:
        self._results = {canonicalize_query(k): list(v) for k, v in results.items()}
        self._collected_at = collected_at
        self._retention = retention
        self._synthetic = synthetic
        self._fail = fail
        self.calls: list[str] = []

    async def retrieve(self, request: RetrievalRequest, *, now: datetime) -> RetrievedResultSet:
        if self._fail:
            return RetrievedResultSet(
                request=request,
                status=RetrievalStatus.UNAVAILABLE,
                videos=(),
                provider=self.name,
                collected_at=None,
                warnings=("result source unavailable",),
            )
        collected = self._collected_at or now
        videos: list[CandidateVideo] = []
        requests = 0
        for variant in (request.query, *request.variants):
            self.calls.append(variant)
            requests += 1
            rows = self._results.get(canonicalize_query(variant), [])
            for pos, raw in enumerate(rows[: request.max_results_per_variant], start=1):
                published = raw.get("published_at")
                if (
                    request.published_after is not None
                    and isinstance(published, str)
                    and datetime.fromisoformat(published.replace("Z", "+00:00"))
                    < request.published_after
                ):
                    continue
                videos.append(
                    normalise(
                        raw,
                        variant=variant,
                        position=pos,
                        collected_at=collected,
                        expires_at=collected + self._retention,
                        synthetic=self._synthetic,
                        transcript_authorised=bool(raw.get("transcript_authorised")),
                    )
                )
        return RetrievedResultSet(
            request=request,
            status=RetrievalStatus.FETCHED,
            videos=tuple(dedupe(videos)),
            provider=self.name,
            collected_at=collected,
            requests_made=requests,
            quota_units=requests,
        )


class YouTubeResultSource:
    """YouTube Data API through the Phase 1 ingestion service (quota + retention + cache)."""

    name = "youtube_data_api"

    def __init__(
        self,
        *,
        ingestion: Any,
        session_factory: SessionFactory,
        cache_ttl: timedelta,
        fetch_details: bool = True,
        provider_name: str = "youtube_data_api",
        offline_reason: str = "",
    ) -> None:
        self._ingestion = ingestion  # None: stored (cached) results only, no API calls
        self._offline_reason = offline_reason or "YouTube Data API not configured"
        self._factory = session_factory
        self._ttl = cache_ttl
        self._details = fetch_details
        self._provider = provider_name

    def _stored(
        self, variant: str, request: RetrievalRequest, now: datetime, run_id: Any = None
    ) -> tuple[list[dict[str, Any]], datetime | None]:
        r, v = VideoSearchResultRow, VideoRow
        with transaction(self._factory) as s:
            read_only(s)
            # Several query rows can share a canonical text (one per provider/region).
            qids = list(
                s.execute(
                    sa.select(SearchQueryRow.id).where(
                        SearchQueryRow.canonical_query == canonicalize_query(variant)
                    )
                ).scalars()
            )
            if not qids:
                return [], None
            stmt = (
                sa.select(r, v)
                .join(v, v.video_id == r.video_id)
                .where(r.query_id.in_(qids), r.expires_at > now, v.metadata_expires_at > now)
            )
            if request.region:
                stmt = stmt.where(r.region == request.region)
            if request.language:
                stmt = stmt.where(r.language == request.language)
            if run_id is not None:
                stmt = stmt.where(r.ingestion_run_id == run_id)
            else:
                stmt = stmt.where(r.collected_at >= now - self._ttl)
            rows = s.execute(stmt.order_by(r.collected_at.desc(), r.rank)).all()
            if not rows:
                return [], None
            latest = rows[0][0].collected_at
            out = []
            for res, vid in rows:
                if res.collected_at != latest:
                    continue
                out.append(
                    {
                        "video_id": vid.video_id,
                        "title": vid.title,
                        "description": vid.description,
                        "channel_id": vid.channel_id,
                        "channel_title": vid.channel_title,
                        "published_at": vid.published_at,
                        "duration_seconds": vid.duration_seconds,
                        "default_language": vid.default_language,
                        "default_audio_language": vid.default_audio_language,
                        "_position": res.rank,
                        "_expires_at": min(res.expires_at, vid.metadata_expires_at),
                    }
                )
            return out, latest

    async def retrieve(self, request: RetrievalRequest, *, now: datetime) -> RetrievedResultSet:
        from trend_agent.schemas.provider import VideoSearchRequest

        videos: list[CandidateVideo] = []
        warnings: list[str] = []
        requests = cache_hits = units = 0
        failed = 0
        collected: datetime | None = None
        approx: int | None = None
        for variant in (request.query, *request.variants):
            rows, at = self._stored(variant, request, now)
            if rows:
                cache_hits += 1
            elif self._ingestion is None:
                failed += 1
                warnings.append(
                    f"{variant!r}: no stored results within the cache window and live retrieval "
                    f"is unavailable ({self._offline_reason})"
                )
                continue
            else:
                summary = await self._ingestion.ingest_video_search(
                    self._provider,
                    VideoSearchRequest(
                        query=variant,
                        region=request.region,
                        language=request.language,
                        published_after=request.published_after,
                        max_results=request.max_results_per_variant,
                    ),
                    fetch_details=self._details,
                    fetch_channels=False,
                )
                requests += 1
                units += sum(summary.quota_used.values())
                approx = approx or summary.extra.get("approx_matching_videos_not_search_volume")
                if summary.error_message:
                    warnings.append(f"{variant!r}: {summary.error_type}: {summary.error_message}")
                rows, at = (
                    self._stored(variant, request, now, run_id=summary.run_id)
                    if summary.run_id is not None
                    else ([], None)
                )
                if not rows:
                    failed += 1
                    continue
            collected = max(filter(None, (collected, at)), default=None)
            for raw in rows:
                videos.append(
                    normalise(
                        raw,
                        variant=variant,
                        position=int(raw["_position"]),
                        collected_at=at or now,
                        expires_at=raw["_expires_at"],
                    )
                )
        total = 1 + len(request.variants)
        status = (
            RetrievalStatus.UNAVAILABLE
            if failed == total
            else RetrievalStatus.PARTIAL
            if failed
            else RetrievalStatus.CACHED
            if cache_hits == total
            else RetrievalStatus.FETCHED
        )
        return RetrievedResultSet(
            request=request,
            status=status,
            videos=tuple(dedupe(videos)),
            provider=self.name,
            collected_at=collected,
            requests_made=requests,
            quota_units=units,
            cache_hits=cache_hits,
            approx_total_reported=approx if isinstance(approx, int) else None,
            warnings=tuple(warnings),
        )
