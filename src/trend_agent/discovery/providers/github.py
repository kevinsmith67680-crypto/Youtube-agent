"""GitHub discovery provider (REST API): emerging repositories and monitored releases.

Source types:

* ``repository_search`` - a GitHub repository search narrowed at fetch time by a strategy
  (``emerging``: created recently; ``active``: established and pushed recently). Each
  repository that passes the quality gates becomes a ``repository_metadata`` item.
* ``repository`` - a monitored repository: its current metadata (one
  ``repository_metadata`` item, which also records an observation for momentum) and its
  latest published releases (``code_release`` items, release notes quoted, never
  summarised). The releases list uses a conditional request (ETag).

GitHub data is external developer activity: supply-side evidence of what developers are
building and releasing. It is never search interest and never search volume.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from trend_agent.discovery.models import (
    DiscoveryItem,
    DiscoveryRequest,
    DiscoveryResult,
    DiscoverySource,
    EvidenceType,
    SourceType,
)
from trend_agent.discovery.providers.base import afford, discovery_descriptor
from trend_agent.github.analysis import (
    analyse_release,
    assess_repository,
    build_search_query,
    coordinated_cohorts,
)
from trend_agent.github.client import (
    BUCKET_CORE,
    BUCKET_SEARCH,
    REPO_PATTERN,
    GitHubClient,
)
from trend_agent.github.config import GitHubSettings
from trend_agent.github.models import SIGNAL_LABEL, GitHubRepository
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import InvalidRequestError, ProviderDisabledError
from trend_agent.schemas.enums import Capability, ProviderStatus, ProviderType
from trend_agent.schemas.provider import (
    CapabilityStatus,
    ProviderDescriptor,
    ProviderStatusReport,
)
from trend_agent.utils.time import Clock
from trend_agent.verification.untrusted import injection_signals, sanitize

NAME = "github"
CAPABILITIES = frozenset(
    {
        Capability.TREND_DISCOVERY,
        Capability.REPOSITORY_DISCOVERY,
        Capability.REPOSITORY_METADATA,
        Capability.RELEASE_MONITORING,
        Capability.TOPIC_DISCOVERY,
        Capability.DEVELOPER_ACTIVITY,
    }
)
DOCS = (
    "https://docs.github.com/en/rest/search/search#search-repositories",
    "https://docs.github.com/en/rest/repos/repos#get-a-repository",
    "https://docs.github.com/en/rest/releases/releases#list-releases",
    "https://docs.github.com/en/rest/rate-limit/rate-limit",
    "https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api",
)
MAX_SEARCH_QUERY = 200  # base query budget, leaving room for the strategy qualifiers
_SEARCH_TYPES = frozenset({SourceType.REPOSITORY_SEARCH})


class GitHubDiscoveryProvider:
    NAME = NAME

    def __init__(
        self,
        client: GitHubClient,
        *,
        settings: GitHubSettings,
        enabled: bool,
        api_enabled: bool,
        retention_days: int | None,
        clock: Clock,
    ) -> None:
        self.client = client
        self.settings = settings
        self._enabled = enabled and api_enabled
        self._api_enabled = api_enabled
        self._clock = clock
        self._descriptor = discovery_descriptor(
            name=NAME,
            display_name="GitHub REST API (repositories and releases)",
            version="2.0.0",
            provider_type=ProviderType.OFFICIAL_API,
            capabilities=CAPABILITIES,
            requires_authentication=False,
            enabled=self._enabled,
            retention_days=retention_days,
            usage_restrictions=(
                "External developer activity only: never search interest or search volume.",
                "Stars and forks are cumulative account actions, not unique people.",
                "Read-only public data; no write permission is ever requested.",
                "Respect GitHub rate limits (5,000 core requests/hour and 30 searches/minute "
                "with a token; 60/hour and 10/minute without). Never rotate tokens to "
                "evade limits.",
            ),
            documentation_urls=DOCS,
        )

    # ------------------------------------------------------------------ Provider API
    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def status(self) -> ProviderStatusReport:
        now = self._clock()
        mode = self.client.auth_mode()
        if not self._api_enabled:
            status, detail = ProviderStatus.DISABLED, "GITHUB_API_ENABLED is false."
        elif not self._enabled:
            status, detail = ProviderStatus.DISABLED, "Disabled by discovery configuration."
        elif mode == "invalid_credentials":
            status = ProviderStatus.ACCESS_RESTRICTED
            detail = (
                "GitHub rejected the configured token (HTTP 401). Fix GITHUB_TOKEN or remove "
                "it to use unauthenticated public access."
            )
        else:
            limited = [r for r in ("core", "search") if not self.client.rate_limit_ok(r)[0]]
            if limited:
                status = ProviderStatus.RATE_LIMITED
                detail = "; ".join(self.client.rate_limit_ok(r)[1] for r in limited)
            elif mode == "authenticated":
                status = ProviderStatus.AVAILABLE
                detail = (
                    "Token configured (validity is only confirmed by a live request; "
                    "5,000 core requests/hour, 30 searches/minute)."
                )
            else:
                status = ProviderStatus.AVAILABLE
                detail = (
                    "No token: unauthenticated public access (60 core requests/hour, "
                    "10 searches/minute). Set GITHUB_TOKEN for higher limits."
                )
        caps = tuple(
            CapabilityStatus(capability=c, status=status, detail=detail)
            for c in sorted(CAPABILITIES, key=lambda c: c.value)
        )
        return ProviderStatusReport(
            provider_name=NAME,
            status=status,
            detail=detail,
            capabilities=caps,
            missing_configuration=(),
            checked_at=now,
        )

    async def aclose(self) -> None:
        return None  # the shared HTTP client is closed by the container

    def supports(self, source: DiscoverySource) -> bool:
        if source.source_type is SourceType.REPOSITORY:
            return bool(REPO_PATTERN.match(source.reference))
        if source.source_type in _SEARCH_TYPES:
            return 0 < len(source.reference.strip()) <= MAX_SEARCH_QUERY
        return False

    def can_afford(self, source: DiscoverySource) -> tuple[bool, str]:
        if source.source_type in _SEARCH_TYPES:
            ok, why = self.client.rate_limit_ok("search")
            return (afford(self.client.quota, BUCKET_SEARCH, 1)) if ok else (False, why)
        ok, why = self.client.rate_limit_ok("core")
        return afford(self.client.quota, BUCKET_CORE, 2) if ok else (False, why)

    async def discover(self, request: DiscoveryRequest, ctx: RequestContext) -> DiscoveryResult:
        if not self._enabled:
            raise ProviderDisabledError("GitHub discovery is disabled", provider=NAME)
        source = request.source
        if source.source_type in _SEARCH_TYPES:
            return await self._search(request, ctx)
        if source.source_type is SourceType.REPOSITORY:
            return await self._repository(request, ctx)
        raise InvalidRequestError(
            f"unsupported source type {source.source_type.value}", provider=NAME
        )

    # ------------------------------------------------------------------ search
    def query_for(self, source: DiscoverySource, now: datetime) -> tuple[str, str]:
        strategy_name = str(source.params.get("strategy", "emerging"))
        strategy = self.settings.strategies.get(strategy_name)
        if strategy is None:
            raise InvalidRequestError(f"unknown GitHub strategy {strategy_name!r}", provider=NAME)
        language = source.params.get("language")
        return (
            build_search_query(
                source.reference, strategy, now, str(language) if language else None
            ),
            strategy_name,
        )

    async def _search(self, request: DiscoveryRequest, ctx: RequestContext) -> DiscoveryResult:
        source = request.source
        now = self._clock()
        query, strategy_name = self.query_for(source, now)
        strategy = self.settings.strategies[strategy_name]
        api = self.settings.api
        result = await self.client.search_repositories(
            query,
            ctx,
            sort=None if strategy.sort == "best_match" else strategy.sort,
            per_page=min(api.per_page, request.max_items),
            max_pages=api.max_pages,
            max_items=request.max_items,
        )
        items: list[DiscoveryItem] = []
        excluded: Counter[str] = Counter()
        cohort = coordinated_cohorts(result.repositories, self.settings)
        for rank, repo in enumerate(result.repositories, start=1):
            assessment = assess_repository(repo, source.category, self.settings, now)
            if repo.id in cohort:
                assessment.flags = (*assessment.flags, "coordinated_star_pattern")
            if assessment.excluded:
                excluded.update(assessment.exclusion_reasons())
                continue
            items.append(
                self._repository_item(
                    repo,
                    source,
                    assessment=assessment.as_dict(),
                    description=assessment.description,
                    entity_hint=repo.name,
                    entity_type=assessment.entity_type.value,
                    published_at=repo.created_at
                    if strategy.created_within_days
                    else (repo.pushed_at or repo.created_at),
                    published_basis="created_at" if strategy.created_within_days else "pushed_at",
                    extra={
                        "strategy": strategy_name,
                        "query": query,
                        "rank": rank,
                        "search_total_count": result.total_count,
                    },
                    now=now,
                )
            )
        warnings = list(result.warnings[:5])
        if excluded:
            warnings.append(
                "excluded by quality gates: "
                + ", ".join(f"{k} x{v}" for k, v in sorted(excluded.items()))
            )
        return DiscoveryResult(
            provider_name=NAME, source_key=source.key, items=tuple(items), warnings=tuple(warnings)
        )

    # ------------------------------------------------------------------ monitored repo
    async def _repository(self, request: DiscoveryRequest, ctx: RequestContext) -> DiscoveryResult:
        source = request.source
        now = self._clock()
        items: list[DiscoveryItem] = []
        warnings: list[str] = []
        repo, _ = await self.client.get_repository(source.reference, ctx)
        if repo is not None:
            assessment = assess_repository(repo, source.category, self.settings, now)
            hint = source.entity_hint or repo.name
            if not ({"prompt_injection_suspected", "private"} & set(assessment.flags)):
                items.append(
                    self._repository_item(
                        repo,
                        source,
                        assessment=assessment.as_dict(),
                        description=assessment.description,
                        entity_hint=hint,
                        entity_type=source.entity_hint_type or assessment.entity_type.value,
                        published_at=repo.pushed_at or repo.created_at,
                        published_basis="pushed_at",
                        extra={"strategy": "monitored"},
                        now=now,
                    )
                )
        releases = await self.client.list_releases(
            source.reference, ctx, per_page=self.settings.api.release_page_size, etag=request.etag
        )
        warnings.extend(releases.warnings[:5])
        for release in releases.releases:
            published = release.published_at
            if request.since and published and published < request.since:
                continue
            analysis = analyse_release(release)
            title = sanitize(release.name or release.tag_name, 300) or release.tag_name
            if injection_signals(release.name or ""):
                title = release.tag_name
                analysis.flags.append("prompt_injection_suspected")
            # Quoted lines keep the section they came from in the release notes, so later
            # grounding ("breaking changes") reflects what the notes actually say.
            parts = []
            if analysis.breaking_excerpts:
                parts.append(
                    "Breaking changes: "
                    + "; ".join(line.rstrip(".") for line in analysis.breaking_excerpts)
                    + "."
                )
            if analysis.capability_excerpts:
                parts.append(
                    "New features: "
                    + "; ".join(line.rstrip(".") for line in analysis.capability_excerpts)
                    + "."
                )
            text = " ".join(parts)[:2000] or None
            items.append(
                DiscoveryItem(
                    provider_name=NAME,
                    source_key=source.key,
                    source_reference=release.html_url,
                    evidence_type=source.evidence_type or EvidenceType.CODE_RELEASE,
                    title=title[:1000],
                    text=text,
                    url=release.html_url,
                    publisher=f"github.com/{source.reference}",
                    published_at=published,
                    collected_at=now,
                    category_hint=source.category,
                    entity_hint=source.entity_hint,
                    entity_hint_type=source.entity_hint_type,
                    metadata={
                        "tag": release.tag_name,
                        "prerelease": release.prerelease,
                        "signal_class": "OFFICIAL_EVENT_SIGNAL",
                        "github": {
                            "kind": "release",
                            "release_id": release.id,
                            "repository": source.reference,
                            "repository_id": repo.id if repo else None,
                            "tag_name": release.tag_name,
                            "name": release.name,
                            "published_at": published.isoformat() if published else None,
                            "created_at": release.created_at.isoformat()
                            if release.created_at
                            else None,
                            "html_url": release.html_url,
                            "author_login": release.author.login if release.author else None,
                            "analysis": analysis.as_dict(),
                        },
                    },
                )
            )
        not_modified = releases.not_modified and repo is None
        return DiscoveryResult(
            provider_name=NAME,
            source_key=source.key,
            items=tuple(items),
            not_modified=not_modified,
            etag=releases.etag,
            warnings=tuple(warnings),
        )

    # ------------------------------------------------------------------ items
    def _repository_item(
        self,
        repo: GitHubRepository,
        source: DiscoverySource,
        *,
        assessment: dict[str, Any],
        description: str | None,
        entity_hint: str,
        entity_type: str,
        published_at: datetime,
        published_basis: str,
        extra: dict[str, Any],
        now: datetime,
    ) -> DiscoveryItem:
        title = f"{repo.name}: {description}" if description else repo.name
        topics = ", ".join(repo.topics[:15])
        # The text grounds what the item *is* (a public repository) for attribute
        # extraction; claims come only from the owner's own description above.
        # (No owner/path in the text: login names are not entities.)
        text = "Public GitHub repository." + (f" Topics: {topics}." if topics else "")
        snapshot = repo.snapshot()
        snapshot["description"] = description
        return DiscoveryItem(
            provider_name=NAME,
            source_key=source.key,
            source_reference=repo.html_url,
            evidence_type=EvidenceType.REPOSITORY_METADATA,
            title=title[:300],
            text=text,
            url=repo.html_url,
            publisher=f"github.com/{repo.full_name}",
            author=repo.owner.login,
            published_at=published_at,
            collected_at=now,
            category_hint=source.category,
            entity_hint=entity_hint,
            entity_hint_type=entity_type,
            metadata={
                "signal_class": "DEVELOPER_ACTIVITY",
                "signal_label": SIGNAL_LABEL,
                # monitored repositories use the configured (established) entity; search
                # results name a newly discovered project
                "entity_hint_origin": "configured" if source.entity_hint else "discovered",
                "github": {
                    "kind": "repository",
                    "repository": snapshot,
                    "assessment": assessment,
                    "published_at_basis": published_basis,
                    **extra,
                },
            },
        )
