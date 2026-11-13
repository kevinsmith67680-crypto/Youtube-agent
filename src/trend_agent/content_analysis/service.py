"""Phase 5 application service: database context, persistence and the analysis queue.

Responsibilities kept out of the deterministic core (``orchestrator``):

* assembling inputs from the database - a monitoring target's evidence through the
  verification layer (``VerificationService.build_context``), or, for a manual query,
  the catalog entities it names; demand evidence is copied from Phase 4 surge alerts;
* registering evidence and issuing bundles as the application principal
  (``SqlRegistrar``) - the analysis code, the LLM and source content never write;
* idempotent persistence (an identical completed analysis is never written twice);
* the cost-aware queue (``scheduling``) with bounded concurrency and retries, daily search
  and LLM budgets, and cache reuse of unchanged inputs;
* re-verification of stored opportunity reports against the current evidence registry.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any

import sqlalchemy as sa

from trend_agent.config import Settings
from trend_agent.content_analysis import scheduling
from trend_agent.content_analysis.config import ANALYSIS_VERSION, LoadedContentConfig
from trend_agent.content_analysis.demand import from_alerts, manual
from trend_agent.content_analysis.llm_client import (
    AnalysisLLM,
    AnthropicAnalysisLLM,
    NullAnalysisLLM,
)
from trend_agent.content_analysis.orchestrator import (
    AnalysisInputs,
    AnalysisOutcome,
    ContentAnalyzer,
    inputs_fingerprint,
)
from trend_agent.content_analysis.result_retrieval import ResultSource, YouTubeResultSource
from trend_agent.db.content_models import AnalysisRunStatus, ContentAnalysisRunRow
from trend_agent.db.discovery_models import (
    DiscoveredEntityRow,
    DiscoveryItemRow,
    DiscoverySourceRow,
    EntityAliasRow,
    EntityMentionRow,
)
from trend_agent.db.models import SearchQueryRow
from trend_agent.db.monitoring_models import MonitoringTargetRow
from trend_agent.db.repositories.content_analyses import (
    ContentAnalysisRepository,
    OpportunityRepository,
)
from trend_agent.db.repositories.surge_events import SurgeEventRepository
from trend_agent.db.repositories.verification import SqlEvidenceStore
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.discovery.models import EntityStatus
from trend_agent.providers.errors import (
    AccessRestrictedError,
    ProviderError,
    ProviderNotConfiguredError,
    QuotaExhaustedError,
    RequestBudgetExceededError,
)
from trend_agent.schemas.content_opportunity import (
    AnalysisTrigger,
    DemandBasis,
    OpportunityReport,
)
from trend_agent.schemas.surge import SurgeAlert
from trend_agent.schemas.verification import (
    AuthoritativeStatistics,
    EvidenceBundle,
    EvidenceDraft,
    EvidenceRecord,
)
from trend_agent.utils.text import canonicalize_query
from trend_agent.utils.time import Clock, utc_now
from trend_agent.verification.config import LoadedVerificationConfig
from trend_agent.verification.entities import CatalogEntity, EntityCatalog
from trend_agent.verification.permissions import (
    Capability,
    PermissionPolicy,
    Principal,
    read_only,
)
from trend_agent.verification.registry import EvidenceRegistry, EvidenceSigner
from trend_agent.verification.service import FactoryEvidenceStore, VerificationService
from trend_agent.verification.sources import from_discovery_item, from_entity

#: Failures worth one more attempt (transient); anything else fails the analysis at once.
_TRANSIENT: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError, ProviderError)
_NOT_RETRIED: tuple[type[BaseException], ...] = (
    QuotaExhaustedError,
    RequestBudgetExceededError,
    AccessRestrictedError,
    ProviderNotConfiguredError,
)


# --------------------------------------------------------------------------- registrar
class _AnalysisStore:
    """Registry store for one analysis: reads committed records (cached by immutable id),
    keeps the ephemeral bundles issued only to resolve context, and refuses writes."""

    def __init__(self, factory: SessionFactory) -> None:
        self._db = FactoryEvidenceStore(factory)
        self._records: dict[str, EvidenceRecord] = {}
        self._bundles: dict[str, EvidenceBundle] = {}

    def lock(self, evidence_key: str) -> None:
        raise PermissionError("the analysis' registry view is read-only")

    def current(self, evidence_key: str) -> EvidenceRecord | None:
        return self._db.current(evidence_key)

    def insert(self, record: EvidenceRecord, supersedes: EvidenceRecord | None) -> None:
        raise PermissionError("the analysis' registry view is read-only")

    def get_many(self, evidence_ids: Iterable[str]) -> dict[str, EvidenceRecord]:
        wanted = list(evidence_ids)
        missing = [e for e in wanted if e not in self._records]
        if missing:
            self._records.update(self._db.get_many(missing))
        return {e: self._records[e] for e in wanted if e in self._records}

    def save_bundle(self, bundle: EvidenceBundle) -> None:
        self._bundles[bundle.bundle_id] = bundle  # context-resolution bundle, never stored

    def get_bundle(self, bundle_id: str) -> EvidenceBundle | None:
        return self._bundles.get(bundle_id) or self._db.get_bundle(bundle_id)

    def mark_source_removed(self, evidence_key: str) -> int:
        raise PermissionError("the analysis' registry view is read-only")


class SqlRegistrar:
    """Evidence registration and bundle issue as the application principal (one short
    transaction per batch). Resolution goes through a read-only registry view."""

    def __init__(
        self,
        factory: SessionFactory,
        signer: EvidenceSigner,
        loaded: LoadedVerificationConfig,
        *,
        clock: Clock = utc_now,
        policy: PermissionPolicy | None = None,
    ) -> None:
        self._factory = factory
        self._signer = signer
        self._config = loaded.config
        self._clock = clock
        self._policy = policy or PermissionPolicy.default()
        self.registry = EvidenceRegistry(_AnalysisStore(factory), signer, loaded.config, clock)

    def register(self, drafts: Sequence[EvidenceDraft]) -> list[EvidenceRecord]:
        self._policy.require(Principal.APPLICATION, Capability.REGISTER_EVIDENCE)
        if not drafts:
            return []
        with transaction(self._factory) as s:
            reg = EvidenceRegistry(SqlEvidenceStore(s), self._signer, self._config, self._clock)
            return [reg.register(d) for d in drafts]

    def bundle(
        self, records: Sequence[EvidenceRecord], *, target_id: int | None, synthetic: bool
    ) -> EvidenceBundle:
        self._policy.require(Principal.APPLICATION, Capability.REGISTER_EVIDENCE)
        with transaction(self._factory) as s:
            if target_id is not None and s.get(MonitoringTargetRow, target_id) is None:
                target_id = None
            reg = EvidenceRegistry(SqlEvidenceStore(s), self._signer, self._config, self._clock)
            return reg.issue_bundle(records, target_id=target_id, synthetic=synthetic)


# ------------------------------------------------------------------------------ results
@dataclass
class AnalysisResult:
    status: str  # completed | cached | failed | skipped
    run_id: str | None
    report: OpportunityReport | None = None
    outcome: AnalysisOutcome | None = None
    created: bool = False
    attempts: int = 0
    error: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class QueueRunReport:
    plan: scheduling.QueuePlan
    results: list[tuple[scheduling.Planned, AnalysisResult]] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for _, r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out


@dataclass(frozen=True)
class ClaimCheck:
    text: str
    kind: str
    was_published: bool
    still_valid: bool
    problems: tuple[str, ...]


# ------------------------------------------------------------------------------ helpers
def make_llm(settings: Settings, loaded: LoadedContentConfig) -> AnalysisLLM:
    """Anthropic adapter when enabled and a key is configured; otherwise reduced-capability
    mode (deterministic analysis only - nothing is silently replaced)."""
    llm = loaded.config.llm
    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    if not llm.enabled or not key or llm.provider != "anthropic":
        return NullAnalysisLLM()
    return AnthropicAnalysisLLM(
        api_key=key,
        model=llm.model,
        max_tokens=llm.max_tokens,
        timeout_seconds=llm.timeout_seconds,
        server_side_fallbacks=llm.server_side_fallbacks,
    )


def youtube_available(settings: Settings) -> tuple[bool, str]:
    if not settings.youtube_enabled:
        return False, "the YouTube provider is disabled (TREND_AGENT_YOUTUBE_ENABLED=false)"
    if settings.youtube_api_key is None:
        return False, "TREND_AGENT_YOUTUBE_API_KEY is not set; only stored results can be used"
    return True, "YouTube Data API configured"


def _start_of_day(now: datetime) -> datetime:
    return datetime.combine(now.date(), time.min, now.tzinfo)


def _alert(payload: dict[str, Any] | None) -> SurgeAlert | None:
    if not payload or "schema_version" not in payload or "contract_error" in payload:
        return None
    try:
        return SurgeAlert.model_validate(payload)
    except ValueError:
        return None


def _catalog(
    entities: Sequence[tuple[DiscoveredEntityRow, list[str]]], records: Sequence[EvidenceRecord]
) -> EntityCatalog:
    by_key = {r.evidence_key: r for r in records}
    return EntityCatalog(
        CatalogEntity(
            key=e.entity_key,
            canonical_name=e.canonical_name,
            entity_type=e.entity_type.value,
            category=e.category.value,
            status=e.status,
            origin=e.origin,
            aliases=tuple(al),
            release_date=e.release_date.date() if e.release_date else None,
            release_level=e.release_level,
            version=e.version,
            synthetic=e.is_synthetic,
            evidence_id=r.evidence_id if (r := by_key.get(f"entity:{e.entity_key}")) else None,
        )
        for e, al in entities
    )


# ------------------------------------------------------------------------------ service
class ContentAnalysisService:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        loaded: LoadedContentConfig,
        verification: VerificationService,
        source: ResultSource | None = None,
        llm: AnalysisLLM | None = None,
        clock: Clock = utc_now,
        ingestion: Any = None,
        settings: Settings | None = None,
        policy: PermissionPolicy | None = None,
    ) -> None:
        self._factory = session_factory
        self.loaded = loaded
        self.config = loaded.config
        self.verification = verification
        self.llm = llm or NullAnalysisLLM()
        self._clock = clock
        self.policy = policy or PermissionPolicy.default()
        if source is not None:
            self.source: ResultSource = source
            self.source_note = f"result source: {type(source).__name__}"
        else:
            ok, why = youtube_available(settings) if settings else (False, "no settings")
            self.source = YouTubeResultSource(
                ingestion=ingestion if ok else None,
                session_factory=session_factory,
                cache_ttl=self.config.retrieval.cache_ttl,
                fetch_details=self.config.retrieval.fetch_details,
                offline_reason=why,
            )
            self.source_note = why if ok else f"cache-only: {why}"

    # ------------------------------------------------------------------ properties
    @property
    def llm_mode(self) -> str:
        if isinstance(self.llm, NullAnalysisLLM) or not self.config.llm.enabled:
            return "reduced-capability (deterministic only; no LLM configured or enabled)"
        return f"assist enabled ({getattr(self.llm, 'model', None) or 'unknown model'})"

    def _registrar(self) -> SqlRegistrar:
        return SqlRegistrar(
            self._factory,
            self.verification.signer,
            self.verification.loaded,
            clock=self._clock,
            policy=self.policy,
        )

    # ------------------------------------------------------------------ inputs
    def inputs_for_target(
        self,
        target_id: int,
        *,
        event_id: int | None = None,
        region: str | None = None,
        language: str | None = None,
        include_synthetic: bool = False,
        trigger: AnalysisTrigger = AnalysisTrigger.SURGE_EVENT,
    ) -> AnalysisInputs:
        ctx = self.verification.build_context(target_id, include_synthetic=include_synthetic)
        with transaction(self._factory) as s:
            read_only(s)
            target = s.get(MonitoringTargetRow, target_id)
            assert target is not None  # build_context raised otherwise
            repo = SurgeEventRepository(s)
            rows = repo.events(
                active=True, include_synthetic=include_synthetic, target_ids=[target_id], limit=50
            )
            if event_id is not None and all(r.id != event_id for r in rows):
                extra = repo.get(event_id)
                if extra is None or extra.target_id != target_id:
                    raise LookupError(f"surge event {event_id} not found for target {target_id}")
                if extra.is_synthetic and not include_synthetic:
                    raise PermissionError(f"surge event {event_id} is synthetic")
                rows.append(extra)
            alerts = [a for r in rows if (a := _alert(r.payload)) is not None]
            t_region, t_language = target.region, target.language
        ids = {
            a.event_id: rec.evidence_id
            for a in alerts
            for rec in ctx.records
            if rec.evidence_key == f"surge_event:{a.event_id}"
        }
        if alerts:
            demand, stats = from_alerts(
                alerts, query=ctx.query, event_id=event_id, evidence_ids=ids
            )
            if trigger is not AnalysisTrigger.SURGE_EVENT:
                demand = demand.model_copy(update={"trigger": trigger})
        else:
            demand, stats = manual(ctx.query, ctx.query_id, synthetic=ctx.synthetic), ctx.stats
            if trigger is AnalysisTrigger.SURGE_EVENT:
                demand = demand.model_copy(
                    update={"limitations": (*demand.limitations, "no open surge event")}
                )
        r = self.config.retrieval
        return AnalysisInputs(
            query=ctx.query,
            query_id=ctx.query_id,
            category=ctx.category,
            region=region or t_region or r.default_region,
            language=language or t_language or r.default_language,
            demand=demand,
            stats=stats,
            context_records=ctx.records,
            catalog=ctx.catalog,
            now=self._clock(),
            topic_cluster_id=ctx.topic_id,
            target_id=target_id,
            synthetic=ctx.synthetic or demand.synthetic,
        )

    def inputs_for_query(
        self,
        query: str,
        *,
        category: str | None = None,
        region: str | None = None,
        language: str | None = None,
        include_synthetic: bool = False,
    ) -> AnalysisInputs:
        """Manual analysis. A query that is already monitored reuses the target's evidence
        (its Phase 4 alerts are attached, labelled as a manual trigger); otherwise only the
        catalog entities the query names are used and demand is MANUAL_NO_DEMAND_EVIDENCE."""
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        canonical = canonicalize_query(query)
        with transaction(self._factory) as s:
            read_only(s)
            qrow = s.execute(
                sa.select(SearchQueryRow).where(SearchQueryRow.canonical_query == canonical)
            ).scalar_one_or_none()
            target_id = None
            if qrow is not None:
                stmt = sa.select(MonitoringTargetRow.id).where(
                    MonitoringTargetRow.query_id == qrow.id
                )
                if not include_synthetic:
                    stmt = stmt.where(sa.not_(MonitoringTargetRow.is_synthetic))
                target_id = s.execute(stmt.order_by(MonitoringTargetRow.id)).scalars().first()
            query_id = qrow.id if qrow is not None else None
            entities, drafts = self._entities_named(s, query, include_synthetic)
        if target_id is not None:
            inp = self.inputs_for_target(
                target_id,
                region=region,
                language=language,
                include_synthetic=include_synthetic,
                trigger=AnalysisTrigger.MANUAL,
            )
            return inp if category is None else _replace(inp, category=category)
        records = self._registrar().register(drafts)
        synthetic = any(r.is_synthetic for r in records)
        r = self.config.retrieval
        return AnalysisInputs(
            query=query,
            query_id=query_id,
            category=category,
            region=region or r.default_region,
            language=language or r.default_language,
            demand=manual(query, query_id, synthetic=synthetic),
            stats=AuthoritativeStatistics(target_id=None, available=False),
            context_records=records,
            catalog=_catalog(entities, records),
            now=self._clock(),
            synthetic=synthetic,
        )

    def _entities_named(
        self, s: Any, query: str, include_synthetic: bool
    ) -> tuple[list[tuple[DiscoveredEntityRow, list[str]]], list[EvidenceDraft]]:
        """Catalog entities whose name or alias occurs in the query, plus up to the bundle
        limit of source items that mention them (read-only)."""
        q = query.casefold()
        e, a = DiscoveredEntityRow, EntityAliasRow
        lit = sa.literal(q)
        hits = set(
            s.execute(
                sa.select(e.id).where(lit.contains(sa.func.lower(e.canonical_name)))
            ).scalars()
        ) | set(
            s.execute(sa.select(a.entity_id).where(lit.contains(sa.func.lower(a.alias)))).scalars()
        )
        rows = [
            row
            for row in s.execute(sa.select(e).where(e.id.in_(hits))).scalars()
            if (include_synthetic or not row.is_synthetic) and row.status != EntityStatus.REJECTED
        ]
        aliases: dict[int, list[str]] = {}
        for al in s.execute(sa.select(a).where(a.entity_id.in_([r.id for r in rows]))).scalars():
            aliases.setdefault(al.entity_id, []).append(al.alias)

        def named(row: DiscoveredEntityRow) -> bool:
            names = [row.canonical_name, *aliases.get(row.id, [])]
            return any(re.search(rf"(?<!\w){re.escape(n.casefold())}(?!\w)", q) for n in names if n)

        entities = [(row, aliases.get(row.id, [])) for row in rows if named(row)][:20]
        drafts: list[EvidenceDraft] = [from_entity(row, al) for row, al in entities]
        if not entities:
            return entities, drafts
        cfg = self.verification.config
        now = self._clock()
        m = EntityMentionRow
        item_ids = list(
            dict.fromkeys(
                s.execute(
                    sa.select(m.item_id)
                    .where(m.entity_id.in_([row.id for row, _ in entities]))
                    .order_by(m.item_id.desc())
                    .limit(cfg.max_bundle_records)
                ).scalars()
            )
        )
        items = [
            i
            for i in s.execute(
                sa.select(DiscoveryItemRow).where(DiscoveryItemRow.id.in_(item_ids))
            ).scalars()
            if (include_synthetic or not i.is_synthetic)
            and (i.expires_at is None or i.expires_at > now)
        ]
        mentions: dict[int, list[tuple[str, str]]] = {}
        for mrow, ent in s.execute(
            sa.select(m, e)
            .join(e, e.id == m.entity_id)
            .where(m.item_id.in_([i.id for i in items]), m.status != EntityStatus.REJECTED)
        ).all():
            mentions.setdefault(mrow.item_id, []).append((ent.canonical_name, mrow.matched_text))
        verified = {
            src.id: bool((src.config or {}).get("verified", False))
            for src in s.execute(
                sa.select(DiscoverySourceRow).where(
                    DiscoverySourceRow.id.in_({i.source_id for i in items})
                )
            ).scalars()
        }
        drafts += [
            from_discovery_item(
                i,
                mentions=mentions.get(i.id, ()),
                source_verified=verified.get(i.source_id, False),
                max_chars=cfg.matching.max_excerpt_chars,
            )
            for i in items
        ]
        return entities, drafts

    # ------------------------------------------------------------------ analysis
    def _analyzer(self, use_llm: bool) -> ContentAnalyzer:
        loaded = self.loaded
        if not use_llm and loaded.config.llm.enabled:
            loaded = loaded.model_copy(
                update={"config": loaded.config.model_copy(
                    update={"llm": loaded.config.llm.model_copy(update={"enabled": False})})}
            )  # fmt: skip
        return ContentAnalyzer(
            loaded,
            self._registrar(),
            verification_config=self.verification.config,
            source=self.source,
            llm=self.llm if use_llm else NullAnalysisLLM(),
            clock=self._clock,
        )

    def _cached(self, fp: str, now: datetime) -> ContentAnalysisRunRow | None:
        with transaction(self._factory) as s:
            read_only(s)
            row = ContentAnalysisRepository(s).latest_completed(inputs_fingerprint=fp)
            if row is None or row.completed_at is None:
                return None
            if now - row.completed_at >= self.config.scheduling.reanalyse_after:
                return None
            if row.expires_at is not None and row.expires_at <= now:
                return None
            s.expunge(row)
            return row

    def _stored_report(self, run_id: uuid.UUID) -> OpportunityReport | None:
        from trend_agent.db.content_models import ContentOpportunityRow

        with transaction(self._factory) as s:
            read_only(s)
            row = s.execute(
                sa.select(ContentOpportunityRow).where(
                    ContentOpportunityRow.analysis_run_id == run_id
                )
            ).scalar_one_or_none()
            return OpportunityReport.model_validate(row.report) if row is not None else None

    async def analyse(
        self,
        inp: AnalysisInputs,
        *,
        use_llm: bool = True,
        force: bool = False,
        persist: bool = True,
        priority: float = 0.0,
    ) -> AnalysisResult:
        self.policy.require(Principal.APPLICATION, Capability.READ_EVIDENCE)
        fp = inputs_fingerprint(inp, self.loaded.version_tag)
        if persist and not force:
            hit = self._cached(fp, inp.now)
            if hit is not None:
                return AnalysisResult(
                    status="cached",
                    run_id=str(hit.id),
                    report=self._stored_report(hit.id),
                    notes=["identical inputs were analysed recently; the stored result is reused"],
                )
        analyzer = self._analyzer(use_llm)
        attempts = 0
        started = self._clock()
        outcome: AnalysisOutcome | None = None
        error: str | None = None
        while attempts < self.config.scheduling.max_attempts:
            attempts += 1
            try:
                outcome = await analyzer.analyse(inp)
                break
            except _NOT_RETRIED as exc:
                error = f"{type(exc).__name__}: {exc}"
                break
            except _TRANSIENT as exc:
                error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(min(2.0 * attempts, 5.0))
        if outcome is None:
            run_id = None
            if persist:
                with transaction(self._factory) as s:
                    run_id = str(
                        ContentAnalysisRepository(s).record_failure(
                            inp,
                            inputs_fingerprint=fp,
                            error=error or "unknown error",
                            started_at=started,
                            model_version="none",
                            analysis_version=ANALYSIS_VERSION,
                            configuration_version=self.loaded.version_tag,
                            attempts=attempts,
                        )
                    )
            return AnalysisResult("failed", run_id, attempts=attempts, error=error)
        notes = [self.source_note, f"LLM: {self.llm_mode if use_llm else 'not used for this run'}"]
        if not persist:
            return AnalysisResult(
                "completed", outcome.run_id, outcome.report, outcome, attempts=attempts, notes=notes
            )
        model = outcome.llm.model if outcome.llm and outcome.llm.output else None
        with transaction(self._factory) as s:
            saved_id, created = ContentAnalysisRepository(s).save(
                outcome,
                inp,
                inputs_fingerprint=fp,
                started_at=started,
                completed_at=self._clock(),
                model_version=model or "none (deterministic)",
                priority=priority,
                attempts=attempts,
            )
        if not created:
            notes.append("an identical completed analysis already existed; nothing was written")
        return AnalysisResult(
            "completed", str(saved_id), outcome.report, outcome, created, attempts, notes=notes
        )

    async def analyse_query(self, query: str, **kw: Any) -> AnalysisResult:
        opts = {k: kw.pop(k) for k in ("category", "region", "language") if k in kw}
        inp = self.inputs_for_query(
            query, include_synthetic=kw.pop("include_synthetic", False), **opts
        )
        return await self.analyse(inp, **kw)

    async def analyse_event(
        self, event_id: int, *, include_synthetic: bool = False, **kw: Any
    ) -> AnalysisResult:
        with transaction(self._factory) as s:
            read_only(s)
            row = SurgeEventRepository(s).get(event_id)
            if row is None:
                raise LookupError(f"surge event {event_id} not found")
            target_id = row.target_id
        inp = self.inputs_for_target(
            target_id, event_id=event_id, include_synthetic=include_synthetic
        )
        return await self.analyse(inp, **kw)

    # ------------------------------------------------------------------ queue
    def candidates(
        self, *, include_synthetic: bool = False, limit: int = 200
    ) -> list[scheduling.QueueItem]:
        """Open Phase 4 surge events, one item per monitoring target (strongest event)."""
        from trend_agent.content_analysis.demand import basis_for

        with transaction(self._factory) as s:
            read_only(s)
            rows = SurgeEventRepository(s).events(
                active=True, include_synthetic=include_synthetic, limit=limit
            )
            queries = {
                q.id: q.original_query
                for q in s.execute(
                    sa.select(SearchQueryRow).where(
                        SearchQueryRow.id.in_({r.query_id for r in rows})
                    )
                ).scalars()
            }
            last = ContentAnalysisRepository(s).last_completed_by_target(
                list({r.target_id for r in rows})
            )
            items = []
            for r in rows:
                alert = _alert(r.payload)
                prev = last.get(r.target_id)
                items.append(
                    scheduling.QueueItem(
                        key=f"target:{r.target_id}",
                        query=queries.get(r.query_id, f"query {r.query_id}"),
                        category=r.category.value,
                        basis=basis_for(alert) if alert else DemandBasis.INSUFFICIENT_EVIDENCE,
                        event_id=r.id,
                        target_id=r.target_id,
                        query_id=r.query_id,
                        severity=r.maximum_severity,
                        detected_at=r.first_detected_at,
                        updated_at=r.last_updated_at,
                        last_analysed_at=prev.completed_at if prev else None,
                        last_versions=(prev.analysis_version, prev.configuration_version)
                        if prev
                        else None,
                        last_expires_at=prev.expires_at if prev else None,
                        synthetic=r.is_synthetic,
                    )
                )
        return items

    def usage_today(self) -> tuple[int, int]:
        now = self._clock()
        with transaction(self._factory) as s:
            read_only(s)
            return ContentAnalysisRepository(s).usage_since(_start_of_day(now))

    def pending(
        self, *, include_synthetic: bool = False, limit: int | None = None, force: bool = False
    ) -> scheduling.QueuePlan:
        searches, tokens = self.usage_today()
        return scheduling.plan(
            self.candidates(include_synthetic=include_synthetic),
            self.config,
            now=self._clock(),
            analysis_version=ANALYSIS_VERSION,
            configuration_version=self.loaded.version_tag,
            search_requests_used=searches,
            llm_tokens_used=tokens,
            llm_enabled=self.config.llm.enabled and not isinstance(self.llm, NullAnalysisLLM),
            limit=limit,
            force=force,
        )

    async def run(
        self, *, include_synthetic: bool = False, limit: int | None = None, force: bool = False
    ) -> QueueRunReport:
        plan = self.pending(include_synthetic=include_synthetic, limit=limit, force=force)
        report = QueueRunReport(plan)
        sem = asyncio.Semaphore(self.config.scheduling.max_concurrency)

        async def one(p: scheduling.Planned) -> tuple[scheduling.Planned, AnalysisResult]:
            async with sem:
                it = p.item
                try:
                    assert it.target_id is not None
                    inp = await asyncio.to_thread(
                        self.inputs_for_target,
                        it.target_id,
                        event_id=it.event_id,
                        include_synthetic=include_synthetic,
                    )
                    res = await self.analyse(
                        inp, use_llm=p.use_llm, force=force, priority=p.priority
                    )
                except (LookupError, PermissionError, ValueError) as exc:
                    res = AnalysisResult("failed", None, error=f"{type(exc).__name__}: {exc}")
                if p.urgency:
                    res.notes.append(f"urgency: {p.urgency}")
                return p, res

        report.results = list(await asyncio.gather(*(one(p) for p in plan.selected)))
        return report

    # ------------------------------------------------------------------ reads
    def runs(self, **kw: Any) -> list[ContentAnalysisRunRow]:
        with transaction(self._factory) as s:
            read_only(s)
            rows = ContentAnalysisRepository(s).runs(**kw)
            s.expunge_all()
            return rows

    def run_details(self, run_id: str) -> dict[str, Any] | None:
        try:
            rid = uuid.UUID(run_id)
        except ValueError:
            return None
        with transaction(self._factory) as s:
            read_only(s)
            repo = ContentAnalysisRepository(s)
            run = repo.get_run(rid)
            if run is None:
                return None
            intent = repo.intent(rid)
            return {
                "run": {
                    "id": str(run.id),
                    "query": run.canonical_query,
                    "status": run.status.value
                    if isinstance(run.status, AnalysisRunStatus)
                    else str(run.status),
                    "trigger": run.trigger,
                    "demand_basis": run.demand_basis,
                    "opportunity_status": run.opportunity_status,
                    "verification_state": run.verification_state,
                    "results_inspected": run.results_inspected,
                    "analysis_version": run.analysis_version,
                    "configuration_version": run.configuration_version,
                    "model_version": run.model_version,
                    "cost": run.cost,
                    "error": run.error_details,
                    "synthetic": run.is_synthetic,
                    "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                    "expires_at": run.expires_at.isoformat() if run.expires_at else None,
                },
                "intent": intent.payload if intent else None,
                "requirements": [
                    {
                        "key": r.key,
                        "requirement": r.requirement,
                        "priority": r.priority,
                        "blocking": r.blocking,
                        "source": r.source,
                    }
                    for r in repo.requirements(rid)
                ],
                "results": [
                    {
                        "video_id": v.video_id,
                        "api_position": v.api_position,
                        "relevance_score": v.relevance_score,
                        "relevant": v.relevant,
                        "entity_match": v.entity_match,
                        "format": v.video_format,
                        "freshness": v.freshness_status,
                        "evidence_level": v.evidence_level,
                        "explanation": v.relevance_explanation,
                    }
                    for v in repo.results(rid)
                ],
            }

    def gaps(self, **kw: Any) -> list[dict[str, Any]]:
        with transaction(self._factory) as s:
            read_only(s)
            return [
                {
                    "query": q,
                    "gap_type": g.gap_type,
                    "strength": g.evidence_strength,
                    "verification": g.verification_status,
                    "category": g.category,
                    "explanation": g.explanation,
                    "limitations": list(g.limitations),
                    "run_id": str(g.analysis_run_id),
                    "synthetic": g.is_synthetic,
                }
                for g, q in ContentAnalysisRepository(s).gaps(**kw)
            ]

    def opportunities(self, **kw: Any) -> list[OpportunityReport]:
        with transaction(self._factory) as s:
            read_only(s)
            return [
                OpportunityReport.model_validate(o.report)
                for o in OpportunityRepository(s).ranked(**kw)
            ]

    def opportunity(self, ident: str) -> tuple[OpportunityReport, str | None] | None:
        with transaction(self._factory) as s:
            read_only(s)
            row = OpportunityRepository(s).get(ident)
            if row is None:
                return None
            run = s.get(ContentAnalysisRunRow, row.analysis_run_id) if row.analysis_run_id else None
            bundle = str(run.bundle_id) if run is not None and run.bundle_id else None
            return OpportunityReport.model_validate(row.report), bundle

    def reverify(self, ident: str) -> tuple[OpportunityReport, list[ClaimCheck]] | None:
        """Re-check every claim of a stored report against the evidence registry *now*:
        integrity, bundle membership, expiry and source removal (read-only)."""
        found = self.opportunity(ident)
        if found is None:
            return None
        report, bundle_id = found
        store = _AnalysisStore(self._factory)
        registry = EvidenceRegistry(
            store, self.verification.signer, self.verification.config, self._clock
        )
        bundle = store.get_bundle(bundle_id) if bundle_id else None
        checks = []
        for c in report.claims:
            if bundle is None:
                problems: tuple[str, ...] = ("the analysis' evidence bundle is no longer stored",)
            elif not c.evidence_ids:
                problems = ("no evidence cited",)
            else:
                res = registry.resolve(
                    c.evidence_ids, bundle, allow_synthetic=report.synthetic, now=self._clock()
                )
                problems = tuple(f"{p.evidence_id}: {p.code.value}" for p in res.problems)
            checks.append(
                ClaimCheck(
                    text=c.text,
                    kind=c.kind.value,
                    was_published=c.published,
                    still_valid=not problems,
                    problems=problems,
                )
            )
        return report, checks

    def status(self) -> dict[str, Any]:
        now = self._clock()
        with transaction(self._factory) as s:
            read_only(s)
            repo = ContentAnalysisRepository(s)
            st = repo.status(_start_of_day(now))
            st["expired_rows_awaiting_purge"] = repo.count_expired(now)
        sch = self.config.scheduling
        st["budgets"] = {
            "daily_search_requests": sch.daily_search_requests,
            "daily_llm_tokens": sch.daily_llm_tokens,
            "search_requests_left": max(
                0, sch.daily_search_requests - st["today"]["search_requests"]
            ),
            "llm_tokens_left": max(0, sch.daily_llm_tokens - st["today"]["llm_tokens"]),
        }
        st["retrieval"] = self.source_note
        st["llm"] = self.llm_mode
        st["analysis_version"] = ANALYSIS_VERSION
        st["configuration_version"] = self.loaded.version_tag
        st["disabled_metrics"] = [
            "view counts and other YouTube statistics as demand (not demand; storage off)",
            "total competing videos (search totalResults is an approximation, not supply)",
            "caption/transcript analysis (no authorised caption access configured)",
        ]
        return st


def _replace(inp: AnalysisInputs, **changes: Any) -> AnalysisInputs:
    from dataclasses import replace

    return replace(inp, **changes)


__all__ = [
    "AnalysisResult",
    "ClaimCheck",
    "ContentAnalysisService",
    "QueueRunReport",
    "SqlRegistrar",
    "make_llm",
    "youtube_available",
]
