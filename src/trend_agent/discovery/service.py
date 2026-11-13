"""Keyword-discovery orchestration.

    plan (budgets, intervals, provider readiness, quota)
      -> fetch sources (bounded concurrency, timeouts, typed errors; a provider that runs
         out of quota is not asked again in the run)
      -> store items (idempotent; flagged; retention expiry)
      -> extract + resolve entities (grounded; versions/events kept distinct)
      -> candidates: observed queries, title phrases, seed terms, grounded templates
      -> classify (category, intent) -> validate -> cluster (topic, intent)
      -> novelty + priority (versioned, explainable) -> lifecycle transitions (logged)
      -> dormancy sweep -> run records

Each category is persisted in its own transaction. Nothing here measures search demand.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from trend_agent.db.discovery_models import (
    DiscoveredEntityRow,
    DiscoveredKeywordRow,
    DiscoveryItemRow,
)
from trend_agent.db.repositories import ProviderRepository, QueryRepository
from trend_agent.db.repositories.discovery import DiscoveryRepository, SourceRecord
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.discovery.budget import DiscoveryPlan, build_plan
from trend_agent.discovery.clustering import Clusterer, LexicalSimilarity, SimilarityBackend
from trend_agent.discovery.config import GazetteerEntity, LoadedDiscoveryConfig, slug
from trend_agent.discovery.evidence import (
    EvidenceView,
    claimed_release_dates,
    corroboration_score,
    evidence_quality,
    independent_origins,
    quality_flags,
    resolve_release_date,
)
from trend_agent.discovery.expansion import (
    EntityView,
    KeywordCandidate,
    cap_candidates,
    expand_entity,
    make_candidate,
)
from trend_agent.discovery.extraction import (
    TRUSTED_EVIDENCE,
    DeterministicExtractor,
    EntityExtractor,
    Gazetteer,
    ground,
)
from trend_agent.discovery.intent import (
    CategoryClassifier,
    IntentClassifier,
    RuleBasedIntentClassifier,
)
from trend_agent.discovery.lifecycle import (
    DISCOVERY_ACTOR,
    check_origin_change,
    status_after_scoring,
    transition,
    validate_keyword,
)
from trend_agent.discovery.models import (
    QUERY_LEVEL_EVIDENCE,
    TARGET_CATEGORIES,
    CandidateStatus,
    Category,
    DiscoveryItem,
    DiscoveryMethod,
    DiscoveryRequest,
    DiscoveryResult,
    DiscoverySource,
    EntityOrigin,
    EntityStatus,
    EntityType,
    EvidenceRelation,
    EvidenceType,
    ExtractedEntity,
    KeywordOrigin,
    SearchIntent,
    SourceHealth,
    SourcePool,
    SourceType,
)
from trend_agent.discovery.providers.base import DiscoveryProvider
from trend_agent.discovery.scoring import (
    NoveltyInputs,
    PriorityComponents,
    actionability,
    freshness,
    novelty,
    priority,
    relevance,
)
from trend_agent.discovery.text import (
    canonical_url,
    content_tokens,
    entity_key,
    fingerprint,
    jaccard,
    normalize_name,
)
from trend_agent.logging import get_logger, log_event, redact_text
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import ProviderError, ProviderNotFoundError, QuotaExhaustedError
from trend_agent.providers.quota import QuotaLedger
from trend_agent.providers.registry import ProviderRegistry
from trend_agent.schemas.enums import Capability, IngestionStatus, ProviderStatus, RecordKind
from trend_agent.utils.time import Clock, utc_now

_log = get_logger(__name__)

AUTHORITY = [
    EvidenceType.OFFICIAL_ANNOUNCEMENT,
    EvidenceType.CODE_RELEASE,
    EvidenceType.RELATED_QUERY,
    EvidenceType.SEARCH_SUGGESTION,
    EvidenceType.VIDEO_TITLE,
    EvidenceType.VIDEO_DESCRIPTION,
    EvidenceType.COMMUNITY_DISCUSSION,
    EvidenceType.CURATED_SEED,
]
TITLE_PHRASE_TYPES = frozenset(
    {EvidenceType.VIDEO_TITLE, EvidenceType.VIDEO_DESCRIPTION, EvidenceType.COMMUNITY_DISCUSSION}
)
_SEGMENT = re.compile(r"\s+[|\-\u2013\u2014:]\s+|[!?()\[\]]")
EXPLORATION_WINDOW = timedelta(days=7)
QUERY_LIKE_RULES = frozenset(
    {
        "how_to",
        "tutorial",
        "guide_tech",
        "explain",
        "versus",
        "review",
        "demo",
        "gameplay",
        "troubleshoot",
        "game_guide",
        "item_where",
        "item_get",
        "challenge",
        "run_local",
    }
)


# ---------------------------------------------------------------------------- summaries


class SourceOutcome(BaseModel):
    key: str
    provider: str
    category: Category
    pool: SourcePool
    status: str  # succeeded | not_modified | failed | skipped
    items_received: int = 0
    items_new: int = 0
    error_type: str | None = None
    error_message: str | None = None
    quota_used: dict[str, int] = Field(default_factory=dict)


class CategorySummary(BaseModel):
    run_id: uuid.UUID | None = None
    status: IngestionStatus = IngestionStatus.SUCCEEDED
    slots: int = 0
    exploration_slots: int = 0
    unused_slots: int = 0
    sources_checked: int = 0
    sources_failed: int = 0
    items_received: int = 0
    items_new: int = 0
    items_duplicate: int = 0
    items_outdated: int = 0
    entities_new: int = 0
    entities_rejected: int = 0
    candidates_total: int = 0
    candidates_new: int = 0
    candidates_merged: int = 0
    candidates_rejected: int = 0
    candidates_capped: int = 0
    prioritized: int = 0
    quota_used: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    rejected_reasons: dict[str, int] = Field(default_factory=dict)


class DiscoveryRunSummary(BaseModel):
    started_at: datetime
    completed_at: datetime
    status: IngestionStatus
    dry_run: bool
    is_synthetic: bool
    categories: dict[str, CategorySummary]
    sources: list[SourceOutcome]
    skipped: list[dict[str, str]]
    dormant: int = 0
    warnings: list[str] = Field(default_factory=list)


@dataclass
class _Fetch:
    source: DiscoverySource
    pool: SourcePool
    record: SourceRecord
    result: DiscoveryResult | None = None
    error: ProviderError | None = None
    ledger: QuotaLedger = field(default_factory=QuotaLedger)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    new_items: int = 0


@dataclass
class _EntityState:
    row: DiscoveredEntityRow
    new_this_run: bool


# ---------------------------------------------------------------------------- service


class DiscoveryService:
    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        session_factory: SessionFactory,
        loaded: LoadedDiscoveryConfig,
        clock: Clock = utc_now,
        extra_extractors: Sequence[EntityExtractor] = (),
        intent_classifier: IntentClassifier | None = None,
        similarity: SimilarityBackend | None = None,
    ) -> None:
        self._registry = registry
        self._factory = session_factory
        self._loaded = loaded
        self._config = loaded.config
        self._clock = clock
        self._extra_extractors = list(extra_extractors)
        self._intents: IntentClassifier = intent_classifier or RuleBasedIntentClassifier()
        self._similarity = similarity or LexicalSimilarity()
        self._gazetteer = Gazetteer(loaded.gazetteer())
        self._gazetteer_meta: dict[str, GazetteerEntity] = {
            entity_key(cat.value, ent.entity_type.value, ent.name): ent
            for cat, ent in loaded.gazetteer()
        }
        vocab: dict[Category, set[str]] = {}
        for cat, file in loaded.seeds.items():
            words = vocab.setdefault(cat, set())
            for seed in file.seeds:
                words.update(content_tokens(seed.term))
            for entity in file.entities:
                words.update(content_tokens(entity.name))
                for alias in (*entity.aliases, *entity.case_sensitive_aliases):
                    words.update(content_tokens(alias))
        # Generic words shared across categories would bias classification.
        common = set.intersection(*vocab.values()) if len(vocab) > 1 else set()
        self._categories = CategoryClassifier({c: w - common for c, w in vocab.items()})

    # ------------------------------------------------------------------ sources
    def configured_sources(self, *, now: datetime | None = None) -> list[DiscoverySource]:
        """Static sources + seed-derived query sources + exploration sources."""
        sources = [s for s in self._loaded.sources if s.source_type is not SourceType.SYNTHETIC]
        hn_categories = set(self._config.provider("hackernews").params.get("categories", []))
        region = self._config.regions[0] if self._config.regions else None
        for category in TARGET_CATEGORIES:
            for seed in self._loaded.enabled_seeds(category):
                key = f"{category.value}:{slug(seed.term)}"
                if seed.search:
                    sources.append(
                        DiscoverySource(
                            key=f"seed:youtube:{key}",
                            provider="youtube_discovery",
                            name=f"YouTube search: {seed.term}",
                            source_type=SourceType.API_QUERY,
                            category=category,
                            reference=seed.term,
                            reliability=0.6,
                            min_poll_interval_minutes=720,
                            evidence_type=EvidenceType.VIDEO_TITLE,
                            region=(seed.regions[0] if seed.regions else region),
                            params={"seed_priority": seed.priority},
                        )
                    )
                    if category.value in hn_categories:
                        sources.append(
                            DiscoverySource(
                                key=f"seed:hn:{key}",
                                provider="hackernews",
                                name=f"HN search: {seed.term}",
                                source_type=SourceType.API_QUERY,
                                category=category,
                                reference=seed.term,
                                reliability=0.5,
                                min_poll_interval_minutes=360,
                                evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
                            )
                        )
                if seed.track_as_keyword:
                    sources.append(
                        DiscoverySource(
                            key=f"seed:curated:{key}",
                            provider="curated_seeds",
                            name=f"Seed: {seed.term}",
                            source_type=SourceType.SEED,
                            category=category,
                            reference=seed.term,
                            reliability=0.5,
                            min_poll_interval_minutes=1440,
                            evidence_type=EvidenceType.CURATED_SEED,
                            params={"track_as_keyword": True, "seed_priority": seed.priority},
                        )
                    )
        sources.extend(self._exploration_sources(now or self._clock()))
        return sources

    def _exploration_sources(self, now: datetime) -> list[DiscoverySource]:
        """Query sources built from recently discovered (verified, non-curated) entities."""
        out: list[DiscoverySource] = []
        try:
            with transaction(self._factory) as session:
                repo = DiscoveryRepository(session)
                recent = repo.recent_entities(now - EXPLORATION_WINDOW, None, False, 30)
                for entity in recent:
                    if (
                        entity.status is not EntityStatus.VERIFIED
                        or entity.evergreen
                        or entity.is_family
                    ):
                        continue
                    if entity.category not in TARGET_CATEGORIES:
                        continue
                    out.append(
                        DiscoverySource(
                            key=f"explore:youtube:{slug(entity.entity_key)}",
                            provider="youtube_discovery",
                            name=f"Explore: {entity.canonical_name}",
                            source_type=SourceType.EXPLORATION_QUERY,
                            category=entity.category,
                            reference=entity.canonical_name,
                            pool=SourcePool.EXPLORATION,
                            reliability=0.6,
                            min_poll_interval_minutes=1440,
                            evidence_type=EvidenceType.VIDEO_TITLE,
                        )
                    )
        except Exception as exc:  # database unavailable: plan without exploration
            log_event(_log, "discovery.exploration_unavailable", error=type(exc).__name__)
        return out

    def _provider(self, name: str) -> DiscoveryProvider | None:
        try:
            provider = self._registry.get(name)
        except ProviderNotFoundError:
            return None
        return provider if isinstance(provider, DiscoveryProvider) else None

    def readiness(self, source: DiscoverySource) -> tuple[bool, str, SourceHealth | None]:
        provider = self._provider(source.provider)
        if provider is None:
            return (
                False,
                f"unsupported: no registered discovery provider {source.provider!r}",
                SourceHealth.UNSUPPORTED,
            )
        if not self._config.provider(source.provider).enabled:
            return False, "provider disabled in discovery.yaml", None
        if not provider.supports(source):
            return (
                False,
                f"provider {source.provider} cannot handle this source",
                SourceHealth.UNSUPPORTED,
            )
        report = provider.status()
        caps = [
            c
            for c in (Capability.TREND_DISCOVERY, Capability.SEARCH_SUGGESTIONS)
            if provider.descriptor.supports(c)
        ]
        status = report.capability_status(caps[0]) if caps else ProviderStatus.UNSUPPORTED
        if status is not ProviderStatus.AVAILABLE:
            health = {
                ProviderStatus.NOT_CONFIGURED: SourceHealth.NOT_CONFIGURED,
                ProviderStatus.UNSUPPORTED: SourceHealth.UNSUPPORTED,
                ProviderStatus.ACCESS_RESTRICTED: SourceHealth.NOT_CONFIGURED,
            }.get(status)
            return False, f"{status.value}: {report.detail}", health
        return True, "", None

    # ------------------------------------------------------------------ run
    async def run(
        self,
        categories: Iterable[Category],
        *,
        max_fetches: int | None = None,
        dry_run: bool = False,
        sources: Sequence[DiscoverySource] | None = None,
        synthetic: bool = False,
        force: bool = False,
    ) -> DiscoveryRunSummary:
        """Run discovery. ``force`` ignores minimum polling intervals (not quotas)."""
        started = self._clock()
        selected = [c for c in TARGET_CATEGORIES if c in set(categories)]
        all_sources = list(sources) if sources is not None else self.configured_sources(now=started)
        all_sources = [s for s in all_sources if s.category in selected]

        with transaction(self._factory) as session:
            records = self._sync_sources(session, all_sources, synthetic)
        health_updates: dict[str, tuple[SourceHealth, str]] = {}

        def provider_ready(source: DiscoverySource) -> tuple[bool, str]:
            ok, why, health = self.readiness(source)
            if not ok and health is not None:
                health_updates[source.key] = (health, why)
            return ok, why

        def quota_ok(source: DiscoverySource) -> tuple[bool, str]:
            provider = self._provider(source.provider)
            return provider.can_afford(source) if provider else (False, "unsupported")

        plan = build_plan(
            all_sources,
            {k: r.state for k, r in records.items()},
            categories=selected,
            config=self._config,
            now=started,
            provider_ready=provider_ready,
            quota_ok=quota_ok,
            max_fetches=max_fetches,
            respect_intervals=not force,
        )
        if health_updates and not dry_run:
            with transaction(self._factory) as session:
                repo = DiscoveryRepository(session)
                for key, (health, why) in health_updates.items():
                    repo.mark_source_health(records[key].id, health, why)

        summaries = {
            c.value: CategorySummary(
                slots=plan.slots.get(c, 0),
                exploration_slots=plan.exploration_slots.get(c, 0),
                unused_slots=plan.unused_slots.get(c, 0),
            )
            for c in selected
        }
        skipped = [
            {"source": s.source_key, "category": s.category.value, "reason": s.reason}
            for s in plan.skipped
        ]
        if dry_run:
            return DiscoveryRunSummary(
                started_at=started,
                completed_at=self._clock(),
                status=IngestionStatus.SUCCEEDED,
                dry_run=True,
                is_synthetic=synthetic,
                categories=summaries,
                sources=[
                    SourceOutcome(
                        key=f.source.key,
                        provider=f.source.provider,
                        category=f.source.category,
                        pool=f.pool,
                        status="planned",
                    )
                    for f in plan.fetches
                ],
                skipped=skipped,
            )

        fetches = await self._fetch_all(plan, records)
        outcomes: list[SourceOutcome] = []
        for category in selected:
            cat_fetches = [f for f in fetches if f.source.category is category]
            summary = summaries[category.value]
            try:
                self._process_category(category, cat_fetches, summary, plan, started, synthetic)
            except Exception as exc:  # keep other categories going; record the failure
                summary.status = IngestionStatus.FAILED
                summary.errors.append(redact_text(f"{type(exc).__name__}: {exc}")[:500])
                _log.exception("discovery.category_failed")
            for f in cat_fetches:
                outcomes.append(self._outcome(f))

        dormant = self._dormancy_sweep()
        statuses = {s.status for s in summaries.values()}
        overall = (
            IngestionStatus.FAILED
            if statuses == {IngestionStatus.FAILED}
            else IngestionStatus.PARTIAL
            if statuses - {IngestionStatus.SUCCEEDED}
            else IngestionStatus.SUCCEEDED
        )
        result = DiscoveryRunSummary(
            started_at=started,
            completed_at=self._clock(),
            status=overall,
            dry_run=False,
            is_synthetic=synthetic,
            categories=summaries,
            sources=outcomes,
            skipped=skipped,
            dormant=dormant,
        )
        log_event(
            _log,
            "discovery.completed",
            status=overall.value,
            categories={k: v.candidates_total for k, v in summaries.items()},
        )
        return result

    def _sync_sources(
        self, session: Session, sources: Sequence[DiscoverySource], synthetic: bool
    ) -> dict[str, SourceRecord]:
        repo = DiscoveryRepository(session)
        providers = ProviderRepository(session)
        provider_ids: dict[str, int | None] = {}
        for source in sources:
            if source.provider not in provider_ids:
                provider = self._provider(source.provider)
                provider_ids[source.provider] = (
                    providers.sync(provider.descriptor) if provider else None
                )
            repo.sync_source(source, provider_ids[source.provider], is_synthetic=synthetic)
        return repo.source_records([s.key for s in sources])

    async def _fetch_all(
        self, plan: DiscoveryPlan, records: dict[str, SourceRecord]
    ) -> list[_Fetch]:
        semaphore = asyncio.Semaphore(self._config.run.fetch_concurrency)
        since = self._clock() - timedelta(days=self._config.run.lookback_days)

        async def one(fetch: _Fetch) -> _Fetch:
            async with semaphore:
                fetch.started_at = self._clock()
                provider = self._provider(fetch.source.provider)
                if provider is None:  # pragma: no cover - filtered by the planner
                    return fetch
                # Re-check quota right before the call: an earlier fetch in this run may have
                # exhausted the bucket (locally, or as reported by the server).
                affordable, why = provider.can_afford(fetch.source)
                if not affordable:
                    fetch.error = QuotaExhaustedError(
                        f"not attempted: {why}",
                        bucket="*",
                        resets_at=None,
                        source="local_estimate",
                        provider=fetch.source.provider,
                    )
                    fetch.completed_at = self._clock()
                    return fetch
                request = DiscoveryRequest(
                    source=fetch.source,
                    since=since,
                    max_items=self._config.run.max_items_per_source,
                    etag=fetch.record.etag,
                    last_modified=fetch.record.last_modified,
                )
                ctx = RequestContext(ledger=fetch.ledger)
                try:
                    async with asyncio.timeout(self._config.run.fetch_timeout_seconds):
                        fetch.result = await provider.discover(request, ctx)
                except ProviderError as exc:
                    fetch.error = exc
                except TimeoutError:
                    from trend_agent.providers.errors import ProviderTimeoutError

                    fetch.error = ProviderTimeoutError(
                        f"fetch exceeded {self._config.run.fetch_timeout_seconds}s",
                        provider=fetch.source.provider,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _log.exception("discovery.unexpected_provider_error")
                    from trend_agent.providers.errors import TemporarilyUnavailableError

                    fetch.error = TemporarilyUnavailableError(
                        redact_text(f"internal error: {type(exc).__name__}: {exc}")[:300],
                        provider=fetch.source.provider,
                    )
                fetch.completed_at = self._clock()
                return fetch

        fetches = [_Fetch(f.source, f.pool, records[f.source.key]) for f in plan.fetches]
        return list(await asyncio.gather(*(one(f) for f in fetches)))

    def _outcome(self, f: _Fetch) -> SourceOutcome:
        if f.error is not None:
            status = "failed"
        elif f.result is not None and f.result.not_modified:
            status = "not_modified"
        else:
            status = "succeeded"
        return SourceOutcome(
            key=f.source.key,
            provider=f.source.provider,
            category=f.source.category,
            pool=f.pool,
            status=status,
            items_received=len(f.result.items) if f.result else 0,
            items_new=f.new_items,
            error_type=f.error.error_type if f.error else None,
            error_message=f.error.message if f.error else None,
            quota_used=dict(f.ledger.used),
        )

    # ------------------------------------------------------------------ processing
    def _process_category(
        self,
        category: Category,
        fetches: list[_Fetch],
        summary: CategorySummary,
        plan: DiscoveryPlan,
        now: datetime,
        synthetic: bool,
    ) -> None:
        with transaction(self._factory) as session:
            repo = DiscoveryRepository(session)
            run_id = repo.start_run(
                category=category,
                started_at=now,
                scoring_version=self._config.scoring.version,
                is_synthetic=synthetic,
                plan={
                    "fetches": [f.source.key for f in plan.for_category(category)],
                    "slots": plan.slots.get(category, 0),
                    "exploration_slots": plan.exploration_slots.get(category, 0),
                },
            )
        summary.run_id = run_id
        with transaction(self._factory) as session:
            repo = DiscoveryRepository(session)
            ctx = _RunContext(self, session, repo, category, run_id, now, summary)
            ctx.process(fetches)
            failed = sum(1 for f in fetches if f.error is not None)
            summary.sources_checked = len(fetches)
            summary.sources_failed = failed
            if fetches and failed == len(fetches):
                summary.status = IngestionStatus.FAILED
            elif failed:
                summary.status = IngestionStatus.PARTIAL
            for f in fetches:
                for bucket, units in f.ledger.used.items():
                    summary.quota_used[bucket] = summary.quota_used.get(bucket, 0) + units
            repo.finish_run(
                run_id,
                completed_at=self._clock(),
                status=summary.status,
                total_candidates=summary.candidates_total,
                new_candidates=summary.candidates_new,
                merged_candidates=summary.candidates_merged,
                rejected_candidates=summary.candidates_rejected,
                items_received=summary.items_received,
                items_new=summary.items_new,
                entities_new=summary.entities_new,
                sources_checked=summary.sources_checked,
                sources_failed=summary.sources_failed,
                quota_estimate=sum(summary.quota_used.values()),
                quota_detail=summary.quota_used,
                errors=[
                    {
                        "source": f.source.key,
                        "error_type": f.error.error_type,
                        "message": f.error.message,
                    }
                    for f in fetches
                    if f.error is not None
                ]
                + [{"error": e} for e in summary.errors],
                summary=summary.model_dump(mode="json"),
            )

    def _dormancy_sweep(self) -> int:
        now = self._clock()
        cutoff = now - timedelta(days=self._config.lifecycle.dormant_after_days)
        count = 0
        with transaction(self._factory) as session:
            repo = DiscoveryRepository(session)
            for keyword in repo.dormancy_candidates(cutoff):
                t = transition(
                    keyword.candidate_status,
                    CandidateStatus.DORMANT,
                    reason=(
                        "no new discovery evidence for "
                        f"{self._config.lifecycle.dormant_after_days} days"
                    ),
                    actor=DISCOVERY_ACTOR,
                    at=now,
                )
                keyword.candidate_status = CandidateStatus.DORMANT
                keyword.status_reason = t.reason
                repo.add_history(keyword.id, t, None)
                count += 1
        return count

    # ------------------------------------------------------------------ helpers for CLI
    def expand_preview(
        self, name: str
    ) -> list[tuple[DiscoveredEntityRow, list[KeywordCandidate], list[tuple[str, str]]]]:
        """Grounded hypotheses for stored entities matching ``name`` (no persistence)."""
        out = []
        with transaction(self._factory) as session:
            repo = DiscoveryRepository(session)
            for row in repo.find_entity_any_category(name):
                view = _entity_view(repo, row, self._config.expansion.max_comparisons_per_entity)
                report = expand_entity(view, self._config.expansion)
                out.append((row, report.candidates, report.skipped))
        return out


def effective_type(evidence_type: EvidenceType, simulated: EvidenceType | None) -> EvidenceType:
    return (
        simulated
        if evidence_type is EvidenceType.SYNTHETIC_TEST_DATA and simulated
        else evidence_type
    )


def _item_effective_type(item: DiscoveryItem) -> EvidenceType:
    simulated = item.metadata.get("simulated_evidence_type") if item.is_synthetic else None
    return EvidenceType(simulated) if simulated else item.evidence_type


def _row_effective_type(row: DiscoveryItemRow) -> EvidenceType:
    return effective_type(row.evidence_type, row.simulated_evidence_type)


def _version_tuple(version: str | None) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version or ""))


_LEVEL_RANK = {"major": 3, "minor": 2}


def _entity_view(
    repo: DiscoveryRepository, row: DiscoveredEntityRow, max_comparisons: int
) -> EntityView:
    parent = (
        repo._s.get(DiscoveredEntityRow, row.parent_entity_id) if row.parent_entity_id else None
    )
    parent_game: str | None = None
    if row.entity_type in (EntityType.GAME_EVENT, EntityType.GAME_UPDATE) and parent is not None:
        parent_game = parent.canonical_name
    elif row.entity_type is EntityType.GAME_ITEM and parent is not None:
        if parent.entity_type is EntityType.GAME_EVENT and parent.parent_entity_id:
            grand = repo._s.get(DiscoveredEntityRow, parent.parent_entity_id)
            parent_game = grand.canonical_name if grand else None
        else:
            parent_game = parent.canonical_name
    competitors: list[str] = []
    superseded_by: str | None = None
    # Nearest earlier version of the same family (e.g. "X 2" vs "X 1.5").
    if parent is not None and row.version:
        siblings = [
            s
            for s in repo.children(parent.id)
            if s.id != row.id
            and s.entity_type is row.entity_type
            and s.version
            and s.status is EntityStatus.VERIFIED
        ]
        older = [s for s in siblings if _version_tuple(s.version) < _version_tuple(row.version)]
        if older:
            competitors.append(max(older, key=lambda s: _version_tuple(s.version)).canonical_name)
        # A newer release of the same or a higher level supersedes this one (a newer minor
        # supersedes a minor; only a newer major supersedes a major; patches never do).
        own_rank = _LEVEL_RANK.get(row.release_level or "", 0)
        newer = [
            s
            for s in siblings
            if _version_tuple(s.version) > _version_tuple(row.version)
            and own_rank > 0
            and _LEVEL_RANK.get(s.release_level or "", 0) >= own_rank
        ]
        if newer:
            superseded_by = max(newer, key=lambda s: _version_tuple(s.version)).canonical_name
    # Co-mentioned with a comparison cue in the evidence. Never the entity's own family
    # (parent/children) and never a *newer* sibling: comparisons are generated from the
    # newer release ("X 2 vs X 1.5"), so the reverse pair is not produced again.
    for mention, _item in repo.mentions_for_entity(row.id):
        for name in mention.related:
            for candidate in repo.entities_by_name(row.category, name):
                same_family = (
                    candidate.is_family
                    or candidate.id == row.parent_entity_id
                    or candidate.parent_entity_id == row.id
                )
                newer_sibling = (
                    candidate.parent_entity_id is not None
                    and candidate.parent_entity_id == row.parent_entity_id
                    and _version_tuple(candidate.version) > _version_tuple(row.version)
                )
                if (
                    candidate.status is EntityStatus.VERIFIED
                    and candidate.canonical_name not in competitors
                    and normalize_name(candidate.canonical_name)
                    != normalize_name(row.canonical_name)
                    and not same_family
                    and not newer_sibling
                ):
                    competitors.append(candidate.canonical_name)
    return EntityView(
        key=row.entity_key,
        canonical_name=row.canonical_name,
        entity_type=row.entity_type,
        category=row.category,
        status=row.status,
        confidence=row.confidence,
        attributes=frozenset(row.attributes or []),
        release_level=row.release_level,
        parent_game=parent_game,
        competitors=tuple(competitors[: max(max_comparisons, 0) + 2]),
        evergreen=row.evergreen or row.is_family,
        superseded_by=superseded_by,
    )


class _RunContext:
    """State for processing one category's fetched material in one transaction."""

    def __init__(
        self,
        service: DiscoveryService,
        session: Session,
        repo: DiscoveryRepository,
        category: Category,
        run_id: uuid.UUID,
        now: datetime,
        summary: CategorySummary,
    ) -> None:
        self.svc = service
        self.session = session
        self.repo = repo
        self.category = category
        self.run_id = run_id
        self.now = now
        self.summary = summary
        self.config = service._config
        self.entities: dict[str, _EntityState] = {}
        self.item_rows: dict[int, DiscoveryItemRow] = {}
        self.item_entities: dict[int, list[str]] = {}
        known = self._known_gazetteer()
        self.extractor = DeterministicExtractor(service._gazetteer, known)

    # -- known entities from the database
    def _known_gazetteer(self) -> Gazetteer:
        rows = self.session.execute(
            sa.select(DiscoveredEntityRow).where(
                DiscoveredEntityRow.status != EntityStatus.REJECTED,
                DiscoveredEntityRow.origin != EntityOrigin.GAZETTEER,
            )
        ).scalars()
        return Gazetteer([(r.category, _as_gazetteer(r)) for r in rows])

    def _add_known(self, row: DiscoveredEntityRow) -> None:
        entries = [*self.extractor.known.entries, (row.category, _as_gazetteer(row))]
        self.extractor.known = Gazetteer(entries)

    def process(self, fetches: list[_Fetch]) -> None:
        items: list[tuple[DiscoveryItem, DiscoveryItemRow, frozenset[str]]] = []
        for fetch in fetches:
            self._record_fetch(fetch)
            if fetch.result is None:
                continue
            new = 0
            seen: set[str] = set()
            for item in fetch.result.items:
                self.summary.items_received += 1
                if item.source_reference in seen:
                    self.summary.items_duplicate += 1
                    continue
                seen.add(item.source_reference)
                stored = self._store_item(item, fetch)
                if stored is None:
                    continue
                row, created, flags = stored
                if created:
                    new += 1
                    self.summary.items_new += 1
                else:
                    self.summary.items_duplicate += 1
                items.append((item, row, flags))
            fetch.new_items = new
            self.repo.add_run_source(
                run_id=self.run_id,
                source_id=fetch.record.id,
                status="not_modified" if fetch.result.not_modified else "succeeded",
                items_received=len(fetch.result.items),
                items_new=new,
                quota_used=dict(fetch.ledger.used),
                started_at=fetch.started_at or self.now,
                completed_at=fetch.completed_at or self.now,
            )
            self.repo.record_fetch(
                fetch.record.id,
                now=self.now,
                success=True,
                new_items=new,
                health=SourceHealth.HEALTHY,
                etag=fetch.result.etag,
                last_modified=fetch.result.last_modified,
            )

        # Most authoritative evidence first, so later mentions resolve to its entities.
        items.sort(
            key=lambda t: (
                AUTHORITY.index(_item_effective_type(t[0]))
                if _item_effective_type(t[0]) in AUTHORITY
                else 99,
                t[0].published_at or t[0].collected_at,
                t[0].source_reference,
            )
        )
        touched: set[str] = set()
        for item, row, flags in items:
            if "outdated" in flags:
                self.summary.items_outdated += 1
                continue
            touched |= self._extract(item, row)
        for key in sorted(touched):
            self._aggregate_entity(self.entities[key].row)

        candidates = self._candidates(items, touched)
        capped, dropped = cap_candidates(candidates, self.config.expansion.max_keywords_per_run)
        self.summary.candidates_capped = dropped
        self._persist_candidates(capped)

    def _count_rejection(self, reason: str) -> None:
        self.summary.rejected_reasons[reason] = self.summary.rejected_reasons.get(reason, 0) + 1

    # -- fetch bookkeeping
    def _record_fetch(self, fetch: _Fetch) -> None:
        if fetch.error is None:
            return
        health = {
            "not_configured": SourceHealth.NOT_CONFIGURED,
            "unsupported_capability": SourceHealth.UNSUPPORTED,
            "access_restricted": SourceHealth.FAILING,
        }.get(fetch.error.error_type, SourceHealth.DEGRADED)
        if fetch.record.state.consecutive_failures + 1 >= 3:
            health = SourceHealth.FAILING
        self.repo.record_fetch(
            fetch.record.id,
            now=self.now,
            success=False,
            new_items=0,
            health=health,
            error=f"[{fetch.error.error_type}] {fetch.error.message}",
        )
        self.repo.add_run_source(
            run_id=self.run_id,
            source_id=fetch.record.id,
            status="failed",
            error_type=fetch.error.error_type,
            error_message=fetch.error.message,
            quota_used=dict(fetch.ledger.used),
            started_at=fetch.started_at or self.now,
            completed_at=fetch.completed_at or self.now,
        )

    def _store_item(
        self, item: DiscoveryItem, fetch: _Fetch
    ) -> tuple[DiscoveryItemRow, bool, frozenset[str]] | None:
        etype = _item_effective_type(item)
        if item.url and not item.url.startswith(("http://", "https://")):
            self._count_rejection("item_invalid_url")
            return None
        if item.is_synthetic != (item.evidence_type is EvidenceType.SYNTHETIC_TEST_DATA):
            self._count_rejection("item_synthetic_label")
            return None
        flags = quality_flags(
            evidence_type=etype,
            title=item.title,
            text=item.text,
            url=item.url,
            published_at=item.published_at,
            collected_at=item.collected_at,
            max_age_days=self.config.run.max_item_age_days,
        )
        view = EvidenceView(
            item_id=item.source_reference,
            provider=item.provider_name,
            evidence_type=etype,
            source_reference=item.source_reference,
            title=item.title,
            collected_at=item.collected_at,
            url=item.url,
            publisher=item.publisher,
            author=item.author,
            excerpt=item.text,
            published_at=item.published_at,
            reliability=fetch.source.reliability,
            flags=flags,
        )
        provider = self.svc._provider(item.provider_name)
        expires_at = None
        if provider is not None:
            rule = provider.descriptor.retention_for(RecordKind.DISCOVERY_ITEM)
            if not rule.persist_allowed:
                self._count_rejection("item_storage_not_permitted")
                return None
            if rule.max_retention is not None:
                expires_at = item.collected_at + rule.max_retention
        item_id, created = self.repo.insert_item(
            {
                "source_id": fetch.record.id,
                "provider_name": item.provider_name,
                "run_id": self.run_id,
                "source_reference": item.source_reference,
                "evidence_type": item.evidence_type,
                "simulated_evidence_type": etype if item.is_synthetic else None,
                "is_synthetic": item.is_synthetic,
                "title": item.title,
                "excerpt": (item.text or "")[:500] or None,
                "url": item.url,
                "canonical_url": canonical_url(item.url),
                "publisher": item.publisher,
                "author": item.author,
                "published_at": item.published_at,
                "collected_at": item.collected_at,
                "category_hint": item.category_hint,
                "content_fingerprint": fingerprint(f"{item.title} {item.text or ''}"),
                "quality": evidence_quality(view),
                "quality_flags": sorted(flags),
                "observed_queries": list(item.observed_queries),
                "extra": {**item.metadata, "source_key": item.source_key},
                "expires_at": expires_at,
            }
        )
        row = self.session.get(DiscoveryItemRow, item_id)
        assert row is not None
        self.item_rows[item_id] = row
        return row, created, flags

    # -- entities
    def _extract(self, item: DiscoveryItem, row: DiscoveryItemRow) -> set[str]:
        if item.observed_queries:
            # Observed queries are part of what the source says: extract from them too.
            body = ". ".join([item.text or "", *item.observed_queries]).strip(". ")
            item = item.model_copy(update={"text": body})
        extracted = self.extractor.extract(item)
        for extra in self.svc._extra_extractors:
            for proposed in extra.extract(item):
                extracted.append(
                    ground(proposed.model_copy(update={"origin": EntityOrigin.LLM_PROPOSED}), item)
                )
        touched: set[str] = set()
        for ex in extracted:
            if ex.status is EntityStatus.REJECTED:
                self.summary.entities_rejected += 1
                continue
            # Very weak mentions are dropped; weak-but-plausible ones (e.g. a single
            # speculative community claim) are kept as NEEDS_VERIFICATION for audit.
            if ex.confidence < 0.2:
                continue
            state = self._resolve(ex, row)
            if state is None:
                continue
            touched.add(state.row.entity_key)
            self.item_entities.setdefault(row.id, []).append(state.row.entity_key)
            self.repo.add_mention(
                {
                    "entity_id": state.row.id,
                    "item_id": row.id,
                    "matched_text": ex.matched_text[:500],
                    "confidence": ex.confidence,
                    "extractor": ex.extractor,
                    "status": ex.status,
                    "attributes": list(ex.attributes),
                    "related": list(ex.related),
                }
            )
        return touched

    def _resolve(self, ex: ExtractedEntity, item: DiscoveryItemRow) -> _EntityState | None:
        entity_type = ex.entity_type
        key = entity_key(ex.category.value, entity_type.value, ex.canonical_name)
        existing = self.repo.entity_by_key(key)
        if existing is None and "type_inferred" in ex.flags:
            same = [
                e
                for e in self.repo.entities_by_name(ex.category, ex.canonical_name)
                if e.status is not EntityStatus.REJECTED
            ]
            if len(same) == 1:
                existing = same[0]
            elif ex.parent_name:
                parents = self.repo.entities_by_name(ex.category, ex.parent_name)
                if len(parents) == 1:
                    entity_type = parents[0].entity_type
                    key = entity_key(ex.category.value, entity_type.value, ex.canonical_name)
                    existing = self.repo.entity_by_key(key)
        parent_row = self._parent(ex, entity_type, item) if ex.parent_name else None
        if existing is not None:
            key = existing.entity_key
            state = self.entities.get(key)
            if state is None:
                state = _EntityState(existing, False)
                self.entities[key] = state
            row = existing
            row.last_seen_at = max(row.last_seen_at, self.now)
            row.confidence = max(row.confidence, ex.confidence)
            if (
                ex.status is EntityStatus.VERIFIED
                and row.status is EntityStatus.NEEDS_VERIFICATION
                and ex.entity_type is not EntityType.GAME_ITEM
            ):
                row.status = EntityStatus.VERIFIED
            row.is_synthetic = row.is_synthetic and item.is_synthetic
            if row.parent_entity_id is None and parent_row is not None and parent_row.id != row.id:
                row.parent_entity_id = parent_row.id
        else:
            gaz = self.svc._gazetteer_meta.get(key)
            level = next(
                (f.split(":", 1)[1] for f in ex.flags if f.startswith("release_level:")), None
            )
            row = self.repo.create_entity(
                {
                    "entity_key": key,
                    "canonical_name": ex.canonical_name,
                    "entity_type": entity_type,
                    "category": ex.category,
                    "parent_entity_id": parent_row.id if parent_row else None,
                    "version": ex.version,
                    "release_level": level,
                    "status": ex.status,
                    "origin": ex.origin,
                    "is_family": False,
                    "evergreen": bool(gaz.evergreen) if gaz else ex.extractor == "source_hint",
                    "confidence": ex.confidence,
                    "first_seen_at": self.now,
                    "last_seen_at": self.now,
                    "is_synthetic": item.is_synthetic,
                    "extra": {"flags": list(ex.flags), "extractor": ex.extractor},
                }
            )
            state = _EntityState(row, True)
            self.entities[key] = state
            self.summary.entities_new += 1
            if ex.origin is not EntityOrigin.GAZETTEER:
                self._add_known(row)
        for alias in {ex.canonical_name, *ex.aliases}:
            self.repo.add_alias(row.id, alias, ex.extractor)
        return state

    def _parent(
        self, ex: ExtractedEntity, child_type: EntityType, item: DiscoveryItemRow
    ) -> DiscoveredEntityRow:
        assert ex.parent_name is not None
        parent_type = ex.parent_type or child_type
        candidates = [
            e
            for e in self.repo.entities_by_name(ex.category, ex.parent_name)
            if e.status is not EntityStatus.REJECTED
        ]
        exact = [e for e in candidates if e.entity_type is parent_type]
        chosen = exact[0] if exact else (candidates[0] if len(candidates) == 1 else None)
        if chosen is not None:
            return chosen
        key = entity_key(ex.category.value, parent_type.value, ex.parent_name)
        row = self.repo.create_entity(
            {
                "entity_key": key,
                "canonical_name": ex.parent_name,
                "entity_type": parent_type,
                "category": ex.category,
                "status": ex.status,
                "origin": EntityOrigin.EXTRACTED,
                "is_family": True,
                "evergreen": False,
                "confidence": ex.confidence,
                "first_seen_at": self.now,
                "last_seen_at": self.now,
                "is_synthetic": item.is_synthetic,
                "extra": {"family_of": ex.canonical_name},
            }
        )
        self.repo.add_alias(row.id, ex.parent_name, "family")
        self.entities[key] = _EntityState(row, True)
        self.summary.entities_new += 1
        self._add_known(row)
        return row

    def _aggregate_entity(self, row: DiscoveredEntityRow) -> None:
        mentions = self.repo.mentions_for_entity(row.id)
        attributes: set[str] = set(row.attributes or [])
        claims = []
        trusted_verified = False
        views: list[EvidenceView] = []
        published: list[datetime] = []
        announced: list[datetime] = []  # publication times of trusted, non-speculative items
        for mention, item in mentions:
            etype = _row_effective_type(item)
            if mention.status is not EntityStatus.REJECTED:
                attributes.update(mention.attributes or [])
            if mention.status is EntityStatus.VERIFIED and etype in TRUSTED_EVIDENCE:
                trusted_verified = True
            names = (row.canonical_name, mention.matched_text)
            for claimed in claimed_release_dates(f"{item.title}. {item.excerpt or ''}", names):
                claims.append((claimed, etype, item.id))
            if item.published_at:
                published.append(item.published_at)
                if (
                    etype in (EvidenceType.OFFICIAL_ANNOUNCEMENT, EvidenceType.CODE_RELEASE)
                    and mention.status is not EntityStatus.REJECTED
                    and "speculative" not in (item.quality_flags or [])
                ):
                    announced.append(item.published_at)
            if "speculative" not in (item.quality_flags or []):
                views.append(_evidence_view(item, 0.7))
        origins = len(independent_origins(views))
        # Upgrade to VERIFIED on trusted evidence or >= 2 independent origins. In-game items
        # need trusted (official) evidence: repeated community claims are not enough.
        corroborated = origins >= 2 and row.entity_type is not EntityType.GAME_ITEM
        if row.status is EntityStatus.NEEDS_VERIFICATION and (trusted_verified or corroborated):
            row.status = EntityStatus.VERIFIED
        row.attributes = sorted(attributes)
        if published:
            row.first_published_at = min(published)
        resolved, conflict = resolve_release_date([(d, t) for d, t, _ in claims])
        extra = dict(row.extra or {})
        extra.pop("conflicting_release_dates", None)
        extra.pop("conflicting_item_ids", None)
        # A release date is either stated in the evidence or, for a new verified entity, the
        # first official announcement / release item. It is never guessed from arbitrary
        # mentions: a curated product, a family or a rumour has no release date here.
        if row.evergreen or row.is_family:
            row.release_date = None  # a product line / curated entity has no single release
            extra["release_date_basis"] = None
        elif resolved is not None:
            row.release_date = datetime(resolved.year, resolved.month, resolved.day, tzinfo=UTC)
            extra["release_date_basis"] = "claimed_in_evidence"
            if conflict:
                extra["conflicting_release_dates"] = sorted({d.isoformat() for d, _, _ in claims})
                extra["conflicting_item_ids"] = sorted({i for d, _, i in claims if d != resolved})
        elif announced and row.status is EntityStatus.VERIFIED:
            row.release_date = min(announced)
            extra["release_date_basis"] = "first_official_publication"
        else:
            row.release_date = None
            extra["release_date_basis"] = None
        extra["independent_origins"] = origins
        row.extra = extra

    # -- candidates
    def _candidates(
        self, items: list[tuple[DiscoveryItem, DiscoveryItemRow, frozenset[str]]], touched: set[str]
    ) -> list[KeywordCandidate]:
        out: list[KeywordCandidate] = []
        for item, row, flags in items:
            if "outdated" in flags:
                continue
            etype = _item_effective_type(item)
            entities = [
                self.entities[k].row
                for k in self.item_entities.get(row.id, [])
                if k in self.entities
            ]
            if etype in QUERY_LEVEL_EVIDENCE:
                for query in item.observed_queries:
                    entity = _entity_in_text(query, entities)
                    candidate = make_candidate(
                        query,
                        origin=KeywordOrigin.OBSERVED_QUERY,
                        method=DiscoveryMethod.OBSERVED_QUERY,
                        category=entity.category if entity else self.category,
                        entity=self._view(entity) if entity else None,
                        evidence_refs=(item.source_reference,),
                    )
                    if candidate:
                        out.append(candidate)
            if etype in TITLE_PHRASE_TYPES:
                out.extend(self._title_phrases(item, entities))
            if etype is EvidenceType.CURATED_SEED and item.metadata.get("track_as_keyword"):
                candidate = make_candidate(
                    item.title,
                    origin=KeywordOrigin.GENERATED_HYPOTHESIS,
                    method=DiscoveryMethod.SEED_TERM,
                    category=self.category,
                    evidence_refs=(item.source_reference,),
                )
                if candidate:
                    out.append(candidate)
        for key in sorted(touched):
            state = self.entities[key]
            report = expand_entity(self._view(state.row), self.config.expansion)
            out.extend(report.candidates)
        # De-duplicate by canonical query, keeping the strongest origin.
        best: dict[str, KeywordCandidate] = {}
        for c in out:
            current = best.get(c.canonical)
            if current is None or (
                c.origin is KeywordOrigin.OBSERVED_QUERY
                and current.origin is not KeywordOrigin.OBSERVED_QUERY
            ):
                best[c.canonical] = c
        return list(best.values())

    def _entity_context(self, entity: DiscoveredEntityRow | None) -> str | None:
        """Entity name plus parent names: subject words, not part of the search need."""
        if entity is None:
            return None
        names = [entity.canonical_name]
        node = entity
        for _ in range(2):
            if node.parent_entity_id is None:
                break
            parent = self.session.get(DiscoveredEntityRow, node.parent_entity_id)
            if parent is None:
                break
            names.append(parent.canonical_name)
            node = parent
        return " ".join(names)

    def _view(self, row: DiscoveredEntityRow) -> EntityView:
        return _entity_view(self.repo, row, self.config.expansion.max_comparisons_per_entity)

    def _title_phrases(
        self, item: DiscoveryItem, entities: list[DiscoveredEntityRow]
    ) -> list[KeywordCandidate]:
        out = []
        for segment in _SEGMENT.split(item.title):
            phrase = segment.strip()
            words = phrase.split()
            if not 3 <= len(words) <= self.config.expansion.max_query_words:
                continue
            entity = _entity_in_text(
                phrase, [e for e in entities if e.status is EntityStatus.VERIFIED]
            )
            if entity is None:
                continue
            result = self.svc._intents.classify(phrase, entity.category)
            # Only phrases worded like searches ("how to ...", "X vs Y", "... guide").
            if not set(result.matched_rules) & QUERY_LIKE_RULES:
                continue
            intent = result.primary
            candidate = make_candidate(
                phrase,
                origin=KeywordOrigin.GENERATED_HYPOTHESIS,
                method=DiscoveryMethod.TITLE_PHRASE,
                category=entity.category,
                entity=self._view(entity),
                intent_hint=intent,
                evidence_refs=(item.source_reference,),
            )
            if candidate:
                out.append(candidate)
        return out

    def _seed_priority(
        self, canonical: str, category: Category, entity: DiscoveredEntityRow | None
    ) -> float | None:
        words = set(content_tokens(canonical))
        best: float | None = None
        for seed in self.svc._loaded.enabled_seeds(category):
            seed_words = set(content_tokens(seed.term))
            if seed_words and (seed_words <= words or jaccard(seed_words, words) >= 0.5):
                best = max(best or 0.0, float(seed.priority))
        node = entity
        for _ in range(3):
            if node is None:
                break
            gaz = self.svc._gazetteer_meta.get(node.entity_key)
            if gaz is not None:
                best = max(best or 0.0, float(gaz.priority))
            node = (
                self.session.get(DiscoveredEntityRow, node.parent_entity_id)
                if node.parent_entity_id
                else None
            )
        return best

    def _persist_candidates(self, candidates: list[KeywordCandidate]) -> None:
        queries = QueryRepository(self.session)
        clusterer = Clusterer(
            intent_threshold=self.config.clustering.intent_similarity_threshold,
            topic_threshold=self.config.clustering.topic_similarity_threshold,
            backend=self.svc._similarity,
        )
        for topic_key, canonical in self.repo.topic_keys():
            clusterer.add_topic(topic_key, canonical)
        for (
            topic_key,
            _canon,
            intent_key,
            query,
            entity_name,
            intent,
        ) in self.repo.cluster_members():
            clusterer.add_intent_member(topic_key, intent_key, query, intent, entity_name)
        language = self.config.languages[0] if self.config.languages else None

        for c in sorted(candidates, key=lambda c: (c.entity_key or "", c.canonical)):
            self.summary.candidates_total += 1
            entity = (
                self.entities[c.entity_key].row
                if c.entity_key in self.entities
                else (self.repo.entity_by_key(c.entity_key) if c.entity_key else None)
            )
            cat = self.svc._categories.classify(c.query, entity.category if entity else c.category)
            intent = self.svc._intents.classify(c.query, cat.primary)
            primary_intent = intent.primary
            if primary_intent is SearchIntent.OTHER and c.intent_hint is not None:
                primary_intent = c.intent_hint
            outcome = validate_keyword(
                canonical=c.canonical,
                origin=c.origin,
                category=cat.primary,
                entity_status=entity.status if entity else None,
                max_words=self.config.expansion.max_query_words,
            )
            if (
                c.origin is KeywordOrigin.GENERATED_HYPOTHESIS
                and entity is None
                and c.method in (DiscoveryMethod.SEED_TERM,)
            ):
                outcome = validate_keyword(
                    canonical=c.canonical,
                    origin=KeywordOrigin.OBSERVED_QUERY,
                    category=cat.primary,
                    entity_status=None,
                    max_words=self.config.expansion.max_query_words,
                )
                outcome = type(outcome)(
                    outcome.valid, "curated seed term" if outcome.valid else outcome.reason
                )
            query_id = queries.get_or_create(c.canonical, c.query, language, None)
            keyword = self.repo.keyword_by_query(query_id)
            items = self._evidence_items(c, entity)

            if keyword is not None:
                self.summary.candidates_merged += 1
                check_origin_change(keyword.origin, c.origin, DISCOVERY_ACTOR)
                if (
                    c.origin is KeywordOrigin.OBSERVED_QUERY
                    and keyword.origin is KeywordOrigin.GENERATED_HYPOTHESIS
                ):
                    keyword.origin = KeywordOrigin.OBSERVED_QUERY  # upgraded by direct observation
                keyword.last_discovered_at = max(keyword.last_discovered_at, self.now)
                keyword.is_synthetic = keyword.is_synthetic and all(
                    i.is_synthetic for i, _ in items
                )
                self._link_evidence(keyword.id, items)
                if keyword.candidate_status in (
                    CandidateStatus.REJECTED,
                    CandidateStatus.MONITORING,
                ):
                    continue
                self._score_and_transition(keyword, entity, cat.scores, cat.primary, primary_intent)
                continue

            if not outcome.valid:
                self.summary.candidates_rejected += 1
                self._count_rejection(outcome.reason)
            topic_key, canonical_topic, topic_decision = clusterer.assign_topic(
                c.query,
                entity.entity_key if entity else None,
                entity.canonical_name if entity else None,
            )
            topic_id, _ = self.repo.topic_cluster(
                topic_key, canonical_topic, cat.primary, entity.id if entity else None
            )
            intent_key, intent_decision = clusterer.assign_intent(
                topic_key, c.query, primary_intent, self._entity_context(entity)
            )
            intent_id, _ = self.repo.intent_cluster(topic_id, intent_key, primary_intent, c.query)
            keyword = self.repo.create_keyword(
                {
                    "query_id": query_id,
                    "entity_id": entity.id if entity else None,
                    "topic_cluster_id": topic_id,
                    "intent_cluster_id": intent_id,
                    "origin": c.origin,
                    "discovery_type": c.method,
                    "template_id": c.template_id,
                    "primary_category": cat.primary,
                    "category_scores": {k.value: v for k, v in cat.scores.items()},
                    "primary_intent": primary_intent,
                    "intent_scores": {k.value: v for k, v in intent.scores.items()},
                    "candidate_status": CandidateStatus.DISCOVERED,
                    "status_reason": "discovered",
                    "first_discovered_at": self.now,
                    "last_discovered_at": self.now,
                    "novelty_score": 0.0,
                    "novelty_kind": "recurring",
                    "novelty_components": {},
                    "priority_score": 0.0,
                    "scoring_version": self.config.scoring.version,
                    "score_components": {},
                    "topic_assignment": topic_decision.as_dict(),
                    "intent_assignment": {
                        **intent_decision.as_dict(),
                        "matched_rules": list(intent.matched_rules),
                    },
                    "grounding": list(c.grounding),
                    "is_synthetic": bool(items) and all(i.is_synthetic for i, _ in items),
                }
            )
            self.repo.add_history(
                keyword.id,
                transition(
                    None,
                    CandidateStatus.DISCOVERED,
                    reason=f"{c.method.value} via {c.origin.value}",
                    actor=DISCOVERY_ACTOR,
                    at=self.now,
                ),
                self.run_id,
            )
            self._link_evidence(keyword.id, items)
            target = CandidateStatus.VALIDATED if outcome.valid else CandidateStatus.REJECTED
            t = transition(
                CandidateStatus.DISCOVERED,
                target,
                reason=outcome.reason,
                actor=DISCOVERY_ACTOR,
                at=self.now,
            )
            keyword.candidate_status = target
            keyword.status_reason = outcome.reason
            self.repo.add_history(keyword.id, t, self.run_id)
            if not outcome.valid:
                continue
            self.summary.candidates_new += 1
            self._score_and_transition(keyword, entity, cat.scores, cat.primary, primary_intent)

    def _evidence_items(
        self, c: KeywordCandidate, entity: DiscoveredEntityRow | None
    ) -> list[tuple[DiscoveryItemRow, EvidenceRelation]]:
        out: list[tuple[DiscoveryItemRow, EvidenceRelation]] = []
        refs = set(c.evidence_refs)
        for row in self.item_rows.values():
            if row.source_reference in refs:
                relation = (
                    EvidenceRelation.QUERY_OBSERVED
                    if c.origin is KeywordOrigin.OBSERVED_QUERY
                    else EvidenceRelation.TEMPLATE_GROUNDING
                )
                out.append((row, relation))
        if entity is not None:
            for _mention, item in self.repo.mentions_for_entity(entity.id):
                out.append((item, EvidenceRelation.ENTITY_MENTION))
        return out

    def _link_evidence(
        self, keyword_id: int, items: list[tuple[DiscoveryItemRow, EvidenceRelation]]
    ) -> None:
        for item, relation in items:
            self.repo.add_evidence(keyword_id, item, relation, item.quality)

    def _score_and_transition(
        self,
        keyword: DiscoveredKeywordRow,
        entity: DiscoveredEntityRow | None,
        category_scores: dict[Category, float],
        primary: Category,
        intent: SearchIntent,
    ) -> None:
        items = self.repo.evidence_items(keyword.id)
        conflicting = set((entity.extra or {}).get("conflicting_item_ids", [])) if entity else set()
        reliability = self.repo.source_reliability(i.source_id for i in items)
        views = [
            _evidence_view(
                i,
                reliability.get(i.source_id, 0.7),
                {"conflicting_dates"} if i.id in conflicting else set(),
            )
            for i in items
        ]
        groups = independent_origins([v for v in views if "speculative" not in v.flags])
        quality = (
            sum(max(evidence_quality(v) for v in g) for g in groups) / len(groups)
            if groups
            else 0.0
        )
        newest = max((v.observed_at for v in views), default=None)
        intent_first, topic_first = self.repo.cluster_first_seen(
            keyword.intent_cluster_id, keyword.topic_cluster_id
        )
        nov = novelty(
            NoveltyInputs(
                now=self.now,
                has_entity=entity is not None,
                entity_first_seen=entity.first_seen_at if entity else None,
                entity_type=entity.entity_type if entity else None,
                ancestor_known_before=bool(entity and self.repo.ancestor_known_before(entity)),
                evergreen=bool(entity and (entity.evergreen or entity.is_family)),
                query_first_seen=keyword.first_discovered_at,
                intent_cluster_first_seen=intent_first,
                topic_first_seen=topic_first,
                release_date=entity.release_date if entity else None,
            ),
            self.config.scoring.recency_half_life_days,
        )
        score = priority(
            PriorityComponents(
                novelty=nov.score,
                freshness=freshness(newest, self.now, self.config.scoring.freshness_half_life_days),
                corroboration=corroboration_score(len(groups)),
                relevance=relevance(
                    category_scores,
                    primary,
                    self._seed_priority(_canonical_of(self.session, keyword), primary, entity),
                ),
                intent_actionability=actionability(intent),
                evidence_quality=round(quality, 2),
            ),
            self.config.scoring,
        )
        keyword.novelty_score = nov.score
        keyword.novelty_kind = nov.kind
        keyword.novelty_components = nov.components
        keyword.priority_score = score.score
        keyword.scoring_version = score.version
        keyword.score_components = {
            **score.as_dict(),
            "independent_origins": len(groups),
            "evidence_items": len(items),
        }
        target = status_after_scoring(
            keyword.candidate_status, score.score, self.config.scoring.prioritize_threshold
        )
        if target is not keyword.candidate_status:
            reason = (
                f"priority {score.score} >= {self.config.scoring.prioritize_threshold}"
                if target is CandidateStatus.PRIORITIZED
                else f"priority {score.score} < {self.config.scoring.prioritize_threshold}"
            )
            t = transition(
                keyword.candidate_status, target, reason=reason, actor=DISCOVERY_ACTOR, at=self.now
            )
            keyword.candidate_status = target
            keyword.status_reason = reason
            self.repo.add_history(keyword.id, t, self.run_id)
        if keyword.candidate_status is CandidateStatus.PRIORITIZED:
            self.summary.prioritized += 1


def _canonical_of(session: Session, keyword: DiscoveredKeywordRow) -> str:
    from trend_agent.db.models import SearchQueryRow

    row = session.get(SearchQueryRow, keyword.query_id)
    return row.canonical_query if row else ""


def _evidence_view(
    item: DiscoveryItemRow, reliability: float, extra_flags: set[str] | None = None
) -> EvidenceView:
    return EvidenceView(
        item_id=item.id,
        provider=item.provider_name,
        evidence_type=_row_effective_type(item),
        source_reference=item.source_reference,
        title=item.title,
        collected_at=item.collected_at,
        url=item.url,
        publisher=item.publisher,
        author=item.author,
        excerpt=item.excerpt,
        published_at=item.published_at,
        reliability=reliability,
        flags=frozenset(item.quality_flags or []) | frozenset(extra_flags or set()),
    )


def _entity_in_text(
    text: str, entities: Sequence[DiscoveredEntityRow]
) -> DiscoveredEntityRow | None:
    """The query's subject: earliest-occurring, most specific (non-family, longest) entity."""
    key = normalize_name(text)
    found = [(key.find(normalize_name(e.canonical_name)), e) for e in entities]
    matches = [(pos, e) for pos, e in found if pos >= 0]
    if not matches:
        return None
    return min(matches, key=lambda pe: (pe[1].is_family, pe[0], -len(pe[1].canonical_name)))[1]


def _as_gazetteer(row: DiscoveredEntityRow) -> GazetteerEntity:
    return GazetteerEntity(
        name=row.canonical_name, entity_type=row.entity_type, evergreen=row.evergreen
    )
