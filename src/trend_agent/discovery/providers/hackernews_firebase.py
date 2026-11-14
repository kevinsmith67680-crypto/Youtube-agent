"""Hacker News discovery from the official Firebase API.

A source is one official story list (``newstories``, ``topstories``, ``beststories``,
``showstories``, ``askstories``). A fetch reads the list, takes a bounded slice and
retrieves those item records. For the chronological lists (new, show, ask) only stories
newer than the last one seen are fetched: the last-seen ID is kept in the source's
stored state (its ``etag`` field), so polling is incremental. Ranked lists (top, best)
are re-read so their stories get repeated observations for momentum.

Each relevant story becomes one ``community_discussion`` item. The discussion page is the
stable reference; the linked article (if any) is the item URL and its domain the
publisher, so several stories linking one announcement count as one origin, never as
independent confirmation. Nothing here is search interest.

(The keyword-search provider ``hackernews`` uses the separate HN Search API by Algolia;
the official API has no search, so this provider never pretends to search.)
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
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
from trend_agent.hackernews.classify import StoryAssist, classify_story
from trend_agent.hackernews.client import BUCKET, HackerNewsClient
from trend_agent.hackernews.config import HackerNewsSettings
from trend_agent.hackernews.models import LIST_LABEL, LISTS, SIGNAL_CLASS, SIGNAL_LABEL
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import (
    AccessRestrictedError,
    InvalidRequestError,
    ProviderDisabledError,
    RateLimitedError,
    TemporarilyUnavailableError,
)
from trend_agent.schemas.enums import Capability, ProviderStatus, ProviderType
from trend_agent.schemas.provider import (
    CapabilityStatus,
    ProviderDescriptor,
    ProviderStatusReport,
)
from trend_agent.utils.time import Clock

NAME = "hackernews_firebase"
CAPABILITIES = frozenset(
    {
        Capability.TREND_DISCOVERY,
        Capability.NEW_STORY_DISCOVERY,
        Capability.TOP_STORY_DISCOVERY,
        Capability.SHOW_HN_DISCOVERY,
        Capability.ASK_HN_DISCOVERY,
        Capability.STORY_METADATA,
        Capability.STORY_UPDATES,
        Capability.DISCUSSION_ACTIVITY,
    }
)
INCREMENTAL_LISTS = frozenset({"newstories", "showstories", "askstories"})
_STATE_PREFIX = "hn:last="


def last_seen(etag: str | None) -> int:
    if etag and etag.startswith(_STATE_PREFIX) and etag[len(_STATE_PREFIX) :].isdigit():
        return int(etag[len(_STATE_PREFIX) :])
    return 0


class HackerNewsDiscoveryProvider:
    NAME = NAME

    def __init__(
        self,
        client: HackerNewsClient,
        *,
        settings: HackerNewsSettings,
        enabled: bool,
        api_enabled: bool,
        retention_days: int | None,
        clock: Clock,
        assist: StoryAssist | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self._enabled = enabled and api_enabled
        self._api_enabled = api_enabled
        self._clock = clock
        self._assist = assist
        self._descriptor = discovery_descriptor(
            name=NAME,
            display_name="Hacker News (official Firebase API)",
            version="1.0.0",
            provider_type=ProviderType.OFFICIAL_API,
            capabilities=CAPABILITIES,
            requires_authentication=False,
            enabled=self._enabled,
            retention_days=retention_days,
            usage_restrictions=(
                "Community discussion activity only: never search interest, views or demand.",
                "No documented rate limit; polled at bounded, configured intervals anyway.",
                "Story text is untrusted user content (sanitised; instructions ignored).",
            ),
            documentation_urls=("https://github.com/HackerNews/API",),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    # How long an error keeps the provider out of planning. Transient failures (timeouts,
    # 5xx, network) never do: each source backs off on its own consecutive failures, and
    # blocking the provider would stop the very requests that show it has recovered.
    ACCESS_COOLDOWN = timedelta(minutes=30)
    RATE_LIMIT_COOLDOWN = timedelta(minutes=15)

    def status(self) -> ProviderStatusReport:
        now = self._clock()
        err = self.client.last_error
        at = self.client.last_error_at or now
        if not self._api_enabled:
            status, detail = ProviderStatus.DISABLED, "TREND_AGENT_HACKERNEWS_API_ENABLED is false."
        elif not self._enabled:
            status, detail = ProviderStatus.DISABLED, "Disabled by discovery configuration."
        elif isinstance(err, AccessRestrictedError) and now - at < self.ACCESS_COOLDOWN:
            status = ProviderStatus.ACCESS_RESTRICTED
            retry_at = at + self.ACCESS_COOLDOWN
            detail = f"last request refused: {err.message} (retried after {retry_at:%H:%M} UTC)"
        elif isinstance(err, RateLimitedError) and now - at < self.RATE_LIMIT_COOLDOWN:
            status = ProviderStatus.RATE_LIMITED
            detail = f"asked to slow down at {at:%H:%M} UTC; paused for {self.RATE_LIMIT_COOLDOWN}"
        elif err is not None:
            status = ProviderStatus.AVAILABLE
            detail = (
                f"last request failed at {at:%H:%M} UTC ({err.error_type}: {err.message}); "
                "sources back off individually"
            )
        else:
            status = ProviderStatus.AVAILABLE
            detail = "No credentials needed (official Firebase API; bounded polling)."
        return ProviderStatusReport(
            provider_name=NAME,
            status=status,
            detail=detail,
            capabilities=tuple(
                CapabilityStatus(capability=c, status=status, detail=detail)
                for c in sorted(CAPABILITIES, key=lambda c: c.value)
            ),
            missing_configuration=(),
            checked_at=now,
        )

    async def aclose(self) -> None:
        return None

    def supports(self, source: DiscoverySource) -> bool:
        return source.source_type is SourceType.COMMUNITY and source.reference in LISTS

    def _limit(self, source: DiscoverySource, request_max: int) -> int:
        configured = int(source.params.get("max_items", self.settings.client.max_items_per_list))
        return max(1, min(configured, self.settings.client.max_items_per_list, request_max))

    def can_afford(self, source: DiscoverySource) -> tuple[bool, str]:
        # one list request plus, at worst, one request per item in the slice
        return afford(self.client.quota, BUCKET, 1 + self._limit(source, 500))

    async def discover(self, request: DiscoveryRequest, ctx: RequestContext) -> DiscoveryResult:
        if not self._enabled:
            raise ProviderDisabledError("Hacker News discovery is disabled", provider=NAME)
        source = request.source
        if not self.supports(source):
            raise InvalidRequestError(f"unsupported source {source.key!r}", provider=NAME)
        list_name = source.reference
        now = self._clock()
        ids = await self.client.story_ids(list_name, ctx)
        incremental = list_name in INCREMENTAL_LISTS
        seen = last_seen(request.etag) if incremental else 0
        candidates = [i for i in ids if i > seen] if incremental else list(ids)
        limit = self._limit(source, request.max_items)
        chosen = candidates[:limit]
        batch = await self.client.items(chosen, ctx)
        if chosen and not batch.items and len(batch.failed) == len(chosen):
            raise TemporarilyUnavailableError(
                f"all {len(chosen)} item requests failed", provider=NAME, reason="items_failed"
            )
        rank = {story_id: pos for pos, story_id in enumerate(ids, start=1)}
        excluded: Counter[str] = Counter(batch.skipped.values())
        items: list[DiscoveryItem] = []
        for story_id in chosen:
            item = batch.items.get(story_id)
            if item is None:
                continue
            if request.since and item.published_at and item.published_at < request.since:
                excluded["older_than_lookback"] += 1
                continue
            c = classify_story(item, self.settings.classifier, self._assist)
            if "prompt_injection_suspected" in c.flags:
                excluded["prompt_injection_suspected"] += 1
                continue
            if not c.relevant:
                excluded["not_ai_technology_or_gaming"] += 1
                continue
            linked = (
                item.url
                if isinstance(item.url, str) and item.url.startswith(("http://", "https://"))
                else None
            )
            meta: dict[str, Any] = {
                "kind": "story",
                "story_id": item.id,
                "type": item.type,
                "list": list_name,
                "list_label": LIST_LABEL[list_name],
                "rank": rank.get(story_id),
                "score": item.score,
                "comments": item.descendants,
                "author": item.by,
                "published_at": item.published_at.isoformat() if item.published_at else None,
                "linked_url": linked,
                "discussion_url": item.discussion_url,
                "classification": c.as_dict(),
                "collected_at": now.isoformat(),
            }
            items.append(
                DiscoveryItem(
                    provider_name=NAME,
                    source_key=source.key,
                    source_reference=item.discussion_url,
                    evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
                    title=c.clean_title[:1000],
                    text=c.clean_text,
                    # The linked article is the origin: several stories about one
                    # announcement share it and are never independent confirmation.
                    url=linked or item.discussion_url,
                    publisher=c.domain,
                    author=item.by,
                    published_at=item.published_at,
                    collected_at=now,
                    category_hint=c.primary,
                    entity_hint=c.project_name,
                    entity_hint_type=c.entity_type.value if c.entity_type else None,
                    metadata={
                        "signal_class": SIGNAL_CLASS,
                        "signal_label": SIGNAL_LABEL,
                        "entity_hint_origin": "discovered",
                        "points": item.score,
                        "comments": item.descendants,
                        "hackernews": meta,
                    },
                )
            )
        warnings: list[str] = []
        if excluded:
            warnings.append(
                "not used: " + ", ".join(f"{k} x{v}" for k, v in sorted(excluded.items()))
            )
        if batch.failed:
            warnings.append(
                "item requests failed: "
                + ", ".join(f"{k} x{v}" for k, v in sorted(Counter(batch.failed.values()).items()))
            )
        etag = request.etag
        if incremental and chosen:
            # Advance the cursor only past stories we actually read: a failed request
            # keeps the cursor just below it, so that story is retried next time.
            cursor = min(batch.failed) - 1 if batch.failed else max(chosen)
            etag = f"{_STATE_PREFIX}{max(cursor, seen)}"
        return DiscoveryResult(
            provider_name=NAME,
            source_key=source.key,
            items=tuple(items),
            etag=etag,
            warnings=tuple(warnings),
        )
