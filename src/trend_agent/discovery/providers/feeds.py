"""RSS / Atom feeds: official announcements, release notes, patch notes, package releases.

Feeds are fetched with conditional requests (ETag / If-Modified-Since) so an unchanged
feed costs no parsing and yields ``not_modified``. XML is parsed with ``defusedxml``
because feed content is untrusted (entity-expansion attacks are rejected).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET

from trend_agent.discovery.models import (
    DiscoveryItem,
    DiscoveryRequest,
    DiscoveryResult,
    DiscoverySource,
    EvidenceType,
    SourceType,
)
from trend_agent.discovery.providers.base import (
    REQUEST_BUCKET,
    HttpFetcher,
    afford,
    discovery_descriptor,
)
from trend_agent.discovery.text import domain_of, strip_html
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import MalformedResponseError
from trend_agent.schemas.enums import Capability, ProviderStatus, ProviderType
from trend_agent.schemas.provider import CapabilityStatus, ProviderDescriptor, ProviderStatusReport
from trend_agent.utils.time import Clock

PROVIDER_NAME = "feeds"
ATOM = "{http://www.w3.org/2005/Atom}"
RSS1 = "{http://purl.org/rss/1.0/}"
DC = "{http://purl.org/dc/elements/1.1/}"
MAX_TEXT = 2000


@dataclass(frozen=True)
class FeedEntry:
    title: str
    link: str | None
    guid: str | None
    published: datetime | None
    summary: str | None
    author: str | None


def _text(element: Element | None) -> str | None:
    if element is None:
        return None
    value = "".join(element.itertext()).strip()
    return value or None


def _rss_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return _iso_date(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _iso_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def parse_feed(content: bytes) -> list[FeedEntry]:
    """Parse RSS 2.0, RSS 1.0 (RDF) or Atom. Unknown/unsafe XML -> MalformedResponseError."""
    try:
        root = SafeET.fromstring(content)
    except (ParseError, DefusedXmlException) as exc:
        raise MalformedResponseError(
            f"feed is not safe/valid XML ({type(exc).__name__})", provider=PROVIDER_NAME
        ) from None
    entries: list[FeedEntry] = []
    if root.tag == f"{ATOM}feed":
        for entry in root.findall(f"{ATOM}entry"):
            link = None
            for candidate in entry.findall(f"{ATOM}link"):
                if candidate.get("rel", "alternate") == "alternate":
                    link = candidate.get("href")
                    break
            entries.append(
                FeedEntry(
                    title=_text(entry.find(f"{ATOM}title")) or "",
                    link=link,
                    guid=_text(entry.find(f"{ATOM}id")),
                    published=_iso_date(
                        _text(entry.find(f"{ATOM}published")) or _text(entry.find(f"{ATOM}updated"))
                    ),
                    summary=_text(entry.find(f"{ATOM}summary"))
                    or _text(entry.find(f"{ATOM}content")),
                    author=_text(entry.find(f"{ATOM}author/{ATOM}name")),
                )
            )
    elif root.tag == "rss" or root.tag.endswith("RDF"):
        items = root.findall("channel/item") or root.findall(f"{RSS1}item")
        for item in items:

            def child(name: str, el: Element = item) -> str | None:
                return _text(el.find(name)) or _text(el.find(f"{RSS1}{name}"))

            entries.append(
                FeedEntry(
                    title=child("title") or "",
                    link=child("link"),
                    guid=child("guid"),
                    published=_rss_date(child("pubDate"))
                    or _iso_date(_text(item.find(f"{DC}date"))),
                    summary=child("description"),
                    author=child("author") or _text(item.find(f"{DC}creator")),
                )
            )
    else:
        raise MalformedResponseError(f"unrecognised feed root <{root.tag}>", provider=PROVIDER_NAME)
    return [e for e in entries if e.title.strip()]


class FeedDiscoveryProvider:
    def __init__(
        self,
        *,
        fetcher: HttpFetcher,
        user_agent: str,
        enabled: bool,
        retention_days: int | None,
        clock: Clock,
    ) -> None:
        self._fetcher = fetcher
        self._user_agent = user_agent
        self._clock = clock
        self._enabled = enabled
        self._descriptor: ProviderDescriptor = discovery_descriptor(
            name=PROVIDER_NAME,
            display_name="RSS/Atom feeds (official announcements and releases)",
            version="1.0.0",
            provider_type=ProviderType.OFFICIAL_API,
            capabilities=frozenset({Capability.TREND_DISCOVERY}),
            requires_authentication=False,
            enabled=enabled,
            retention_days=retention_days,
            usage_restrictions=(
                "Only publicly offered feeds; respect publisher terms and polling intervals.",
                "Stores titles, links and short excerpts only.",
            ),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def status(self) -> ProviderStatusReport:
        status = ProviderStatus.AVAILABLE if self._enabled else ProviderStatus.DISABLED
        detail = (
            "No credentials needed; reachability is checked per source."
            if self._enabled
            else "Disabled."
        )
        return ProviderStatusReport(
            provider_name=PROVIDER_NAME,
            status=status,
            detail=detail,
            capabilities=(CapabilityStatus(capability=Capability.TREND_DISCOVERY, status=status),),
            checked_at=self._clock(),
        )

    async def aclose(self) -> None:
        return None

    def supports(self, source: DiscoverySource) -> bool:
        return source.source_type is SourceType.FEED and source.reference.startswith("https://")

    def can_afford(self, source: DiscoverySource) -> tuple[bool, str]:
        return afford(self._fetcher.quota, REQUEST_BUCKET, 1)

    async def discover(self, request: DiscoveryRequest, ctx: RequestContext) -> DiscoveryResult:
        source = request.source
        headers = {
            "User-Agent": self._user_agent,
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5",
        }
        if request.etag:
            headers["If-None-Match"] = request.etag
        if request.last_modified:
            headers["If-Modified-Since"] = request.last_modified
        response = await self._fetcher.get(
            source.reference, ctx, headers=headers, allow_not_modified=True
        )
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")
        if response.status_code == 304:
            return DiscoveryResult(
                provider_name=PROVIDER_NAME,
                source_key=source.key,
                not_modified=True,
                etag=etag or request.etag,
                last_modified=last_modified or request.last_modified,
            )
        entries = await asyncio.to_thread(parse_feed, response.content)
        return DiscoveryResult(
            provider_name=PROVIDER_NAME,
            source_key=source.key,
            items=tuple(self._items(entries, request)),
            etag=etag,
            last_modified=last_modified,
        )

    def _items(self, entries: list[FeedEntry], request: DiscoveryRequest) -> list[DiscoveryItem]:
        source = request.source
        now = self._clock()
        items: list[DiscoveryItem] = []
        for entry in entries:
            if request.since and entry.published and entry.published < request.since:
                continue
            reference = entry.link or entry.guid
            if not reference:
                continue
            summary = strip_html(entry.summary)[:MAX_TEXT] if entry.summary else None
            items.append(
                DiscoveryItem(
                    provider_name=PROVIDER_NAME,
                    source_key=source.key,
                    source_reference=reference,
                    evidence_type=source.evidence_type or EvidenceType.OFFICIAL_ANNOUNCEMENT,
                    title=strip_html(entry.title)[:1000],
                    text=summary,
                    url=entry.link,
                    publisher=source.publisher or domain_of(source.reference),
                    author=entry.author,
                    published_at=entry.published,
                    collected_at=now,
                    category_hint=source.category,
                    entity_hint=source.entity_hint,
                    entity_hint_type=source.entity_hint_type,
                    metadata={"feed": source.reference},
                )
            )
        items.sort(key=lambda i: i.published_at or now, reverse=True)
        return items[: request.max_items]
