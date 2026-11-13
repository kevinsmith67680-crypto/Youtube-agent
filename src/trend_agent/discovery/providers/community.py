"""Code-release and community adapters: GitHub releases, Hacker News, Reddit.

All three use documented public APIs:
* GitHub REST ``GET /repos/{owner}/{repo}/releases`` (60 req/h unauthenticated, 5,000 with a
  token). Rate-limit headers are honoured.
* Hacker News via the Algolia HN Search API (no authentication).
* Reddit via OAuth2 application-only credentials (``client_credentials``) on
  ``oauth.reddit.com``; without credentials the provider reports NOT_CONFIGURED and never
  falls back to unauthenticated scraping.

Community material is a discovery signal only: low evidence quality, never demand.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import SecretStr

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
    simple_status,
)
from trend_agent.discovery.text import domain_of, strip_html
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import (
    AccessRestrictedError,
    InvalidCredentialsError,
    InvalidRequestError,
    MalformedResponseError,
    ProviderNotConfiguredError,
    TemporarilyUnavailableError,
)
from trend_agent.schemas.enums import Capability, ProviderStatus, ProviderType
from trend_agent.schemas.provider import ProviderDescriptor, ProviderStatusReport
from trend_agent.utils.time import Clock, parse_rfc3339

MAX_TEXT = 2000
# owner: GitHub login rules; repo: no "." / ".." segments (no path traversal).
_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/(?!\.{1,2}$)[A-Za-z0-9_.-]{1,100}$")
_SUBREDDIT = re.compile(r"^[A-Za-z0-9_]{2,21}$")


def _dt(value: Any) -> datetime | None:
    if isinstance(value, str) and value:
        try:
            return parse_rfc3339(value)
        except ValueError:
            return None
    if isinstance(value, int | float) and value > 0:
        return datetime.fromtimestamp(value, tz=UTC)
    return None


# ---------------------------------------------------------------------------- GitHub


class GitHubReleasesProvider:
    NAME = "github_releases"
    API = "https://api.github.com"

    def __init__(
        self,
        *,
        fetcher: HttpFetcher,
        token: SecretStr | None,
        user_agent: str,
        enabled: bool,
        retention_days: int | None,
        clock: Clock,
    ) -> None:
        self._fetcher = fetcher
        self._token = token
        self._ua = user_agent
        self._enabled = enabled
        self._clock = clock
        self._descriptor = discovery_descriptor(
            name=self.NAME,
            display_name="GitHub releases (REST API)",
            version="1.0.0",
            provider_type=ProviderType.OFFICIAL_API,
            capabilities=frozenset({Capability.TREND_DISCOVERY}),
            requires_authentication=False,
            enabled=enabled,
            retention_days=retention_days,
            usage_restrictions=(
                "GitHub API terms; 60 requests/hour unauthenticated, 5,000 with a token.",
            ),
            documentation_urls=("https://docs.github.com/en/rest/releases/releases",),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def status(self) -> ProviderStatusReport:
        detail = (
            "Token configured (5,000 requests/hour)."
            if self._token
            else "No token: unauthenticated limit of 60 requests/hour applies."
        )
        return simple_status(self.NAME, self._enabled, detail, self._clock())

    async def aclose(self) -> None:
        return None

    def supports(self, source: DiscoverySource) -> bool:
        return source.source_type is SourceType.REPOSITORY and bool(_REPO.match(source.reference))

    def can_afford(self, source: DiscoverySource) -> tuple[bool, str]:
        return afford(self._fetcher.quota, REQUEST_BUCKET, 1)

    async def discover(self, request: DiscoveryRequest, ctx: RequestContext) -> DiscoveryResult:
        source = request.source
        if not _REPO.match(source.reference):
            raise InvalidRequestError(
                f"invalid repository {source.reference!r}", provider=self.NAME
            )
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": self._ua,
        }
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        data = await self._fetcher.get_json(
            f"{self.API}/repos/{source.reference}/releases",
            ctx,
            params={"per_page": str(min(request.max_items, 100))},
            headers=headers,
        )
        if not isinstance(data, list):
            raise MalformedResponseError("releases response is not a list", provider=self.NAME)
        now = self._clock()
        items: list[DiscoveryItem] = []
        for release in data:
            if not isinstance(release, dict) or release.get("draft"):
                continue
            published = _dt(release.get("published_at"))
            if request.since and published and published < request.since:
                continue
            title = str(release.get("name") or release.get("tag_name") or "").strip()
            url = release.get("html_url")
            if not title or not isinstance(url, str):
                continue
            items.append(
                DiscoveryItem(
                    provider_name=self.NAME,
                    source_key=source.key,
                    source_reference=url,
                    evidence_type=source.evidence_type or EvidenceType.CODE_RELEASE,
                    title=title[:1000],
                    text=strip_html(str(release.get("body") or ""))[:MAX_TEXT] or None,
                    url=url,
                    publisher=f"github.com/{source.reference}",
                    published_at=published,
                    collected_at=now,
                    category_hint=source.category,
                    entity_hint=source.entity_hint,
                    entity_hint_type=source.entity_hint_type,
                    metadata={
                        "tag": release.get("tag_name"),
                        "prerelease": bool(release.get("prerelease")),
                    },
                )
            )
        return DiscoveryResult(provider_name=self.NAME, source_key=source.key, items=tuple(items))


# ---------------------------------------------------------------------------- Hacker News


class HackerNewsProvider:
    NAME = "hackernews"
    API = "https://hn.algolia.com/api/v1"

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
        self._ua = user_agent
        self._enabled = enabled
        self._clock = clock
        self._descriptor = discovery_descriptor(
            name=self.NAME,
            display_name="Hacker News (Algolia HN Search API)",
            version="1.0.0",
            provider_type=ProviderType.OFFICIAL_API,
            capabilities=frozenset({Capability.TREND_DISCOVERY}),
            requires_authentication=False,
            enabled=enabled,
            retention_days=retention_days,
            usage_restrictions=("Community discussion: discovery signal only.",),
            documentation_urls=("https://hn.algolia.com/api",),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def status(self) -> ProviderStatusReport:
        return simple_status(self.NAME, self._enabled, "No credentials needed.", self._clock())

    async def aclose(self) -> None:
        return None

    def supports(self, source: DiscoverySource) -> bool:
        return source.source_type in {
            SourceType.COMMUNITY,
            SourceType.API_QUERY,
            SourceType.EXPLORATION_QUERY,
        }

    def can_afford(self, source: DiscoverySource) -> tuple[bool, str]:
        return afford(self._fetcher.quota, REQUEST_BUCKET, 1)

    async def discover(self, request: DiscoveryRequest, ctx: RequestContext) -> DiscoveryResult:
        source = request.source
        params = {"hitsPerPage": str(min(request.max_items, 100))}
        if source.reference == "front_page":
            path, params["tags"] = "search", "front_page"
        else:
            path, params["tags"], params["query"] = "search_by_date", "story", source.reference
            if request.since:
                params["numericFilters"] = f"created_at_i>{int(request.since.timestamp())}"
        data = await self._fetcher.get_json(
            f"{self.API}/{path}", ctx, params=params, headers={"User-Agent": self._ua}
        )
        hits = data.get("hits") if isinstance(data, dict) else None
        if not isinstance(hits, list):
            raise MalformedResponseError("HN response has no hits list", provider=self.NAME)
        now = self._clock()
        items: list[DiscoveryItem] = []
        for hit in hits:
            if not isinstance(hit, dict) or not hit.get("title") or not hit.get("objectID"):
                continue
            discussion = f"https://news.ycombinator.com/item?id={hit['objectID']}"
            story_url = hit.get("url") if isinstance(hit.get("url"), str) else None
            items.append(
                DiscoveryItem(
                    provider_name=self.NAME,
                    source_key=source.key,
                    source_reference=discussion,
                    evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
                    title=strip_html(str(hit["title"]))[:1000],
                    text=strip_html(str(hit.get("story_text") or ""))[:MAX_TEXT] or None,
                    # The linked article is the origin: an HN post about an announcement
                    # must not count as independent corroboration of that announcement.
                    url=story_url or discussion,
                    publisher=domain_of(story_url) if story_url else None,
                    author=hit.get("author"),
                    published_at=_dt(hit.get("created_at")),
                    collected_at=now,
                    category_hint=source.category,
                    metadata={"points": hit.get("points"), "comments": hit.get("num_comments")},
                )
            )
        return DiscoveryResult(provider_name=self.NAME, source_key=source.key, items=tuple(items))


# ---------------------------------------------------------------------------- Reddit


class RedditProvider:
    NAME = "reddit"
    TOKEN_URL = "https://www.reddit.com/api/v1/access_token"  # noqa: S105 - URL, not a secret
    API = "https://oauth.reddit.com"
    CLIENT_ID_ENV = "TREND_AGENT_REDDIT_CLIENT_ID"
    CLIENT_SECRET_ENV = "TREND_AGENT_REDDIT_CLIENT_SECRET"  # noqa: S105 - variable name

    def __init__(
        self,
        *,
        fetcher: HttpFetcher,
        client: httpx.AsyncClient,
        client_id: SecretStr | None,
        client_secret: SecretStr | None,
        user_agent: str,
        enabled: bool,
        retention_days: int | None,
        clock: Clock,
    ) -> None:
        self._fetcher = fetcher
        self._client = client
        self._id = client_id
        self._secret = client_secret
        self._ua = user_agent
        self._enabled = enabled
        self._clock = clock
        self._token: tuple[str, datetime] | None = None
        self._descriptor = discovery_descriptor(
            name=self.NAME,
            display_name="Reddit (OAuth Data API)",
            version="1.0.0",
            provider_type=ProviderType.OFFICIAL_API,
            capabilities=frozenset({Capability.TREND_DISCOVERY}),
            requires_authentication=True,
            enabled=enabled,
            retention_days=retention_days,
            usage_restrictions=(
                "Requires a registered Reddit app and acceptance of Reddit's Data API terms;"
                " commercial use may need a separate agreement.",
                "Community discussion: discovery signal only.",
            ),
            documentation_urls=("https://www.reddit.com/dev/api",),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def _missing(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, value in (
                (self.CLIENT_ID_ENV, self._id),
                (self.CLIENT_SECRET_ENV, self._secret),
            )
            if value is None
        )

    def status(self) -> ProviderStatusReport:
        missing = self._missing()
        if missing:
            return simple_status(
                self.NAME,
                self._enabled,
                "Reddit API requires OAuth app credentials: " + ", ".join(missing),
                self._clock(),
                status=ProviderStatus.NOT_CONFIGURED if self._enabled else None,
                missing=missing,
            )
        return simple_status(
            self.NAME, self._enabled, "OAuth credentials configured.", self._clock()
        )

    async def aclose(self) -> None:
        return None

    def supports(self, source: DiscoverySource) -> bool:
        return source.source_type is SourceType.COMMUNITY and bool(
            _SUBREDDIT.match(source.reference)
        )

    def can_afford(self, source: DiscoverySource) -> tuple[bool, str]:
        return afford(self._fetcher.quota, REQUEST_BUCKET, 2)

    async def _access_token(self, ctx: RequestContext) -> str:
        if self._id is None or self._secret is None:
            raise ProviderNotConfiguredError(
                "Reddit credentials are not configured", missing=self._missing(), provider=self.NAME
            )
        now = self._clock()
        if self._token is not None and self._token[1] > now:
            return self._token[0]
        self._fetcher.quota.reserve("request", ctx.ledger)
        try:
            response = await self._client.post(
                self.TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self._id.get_secret_value(), self._secret.get_secret_value()),
                headers={"User-Agent": self._ua},
            )
        except httpx.ProxyError:
            raise AccessRestrictedError(
                "network policy denied access to www.reddit.com", provider=self.NAME
            ) from None
        except httpx.HTTPError as exc:
            raise TemporarilyUnavailableError(
                f"token request failed ({type(exc).__name__})", provider=self.NAME
            ) from None
        if response.status_code in (401, 403):
            raise InvalidCredentialsError("Reddit rejected the app credentials", provider=self.NAME)
        try:
            body = response.json()
            token = str(body["access_token"])
            ttl = int(body.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError):
            raise MalformedResponseError("unexpected token response", provider=self.NAME) from None
        self._token = (token, now + timedelta(seconds=max(60, ttl - 60)))
        return token

    async def discover(self, request: DiscoveryRequest, ctx: RequestContext) -> DiscoveryResult:
        source = request.source
        if not _SUBREDDIT.match(source.reference):
            raise InvalidRequestError(f"invalid subreddit {source.reference!r}", provider=self.NAME)
        token = await self._access_token(ctx)
        data = await self._fetcher.get_json(
            f"{self.API}/r/{source.reference}/new",
            ctx,
            params={"limit": str(min(request.max_items, 100)), "raw_json": "1"},
            headers={"Authorization": f"Bearer {token}", "User-Agent": self._ua},
        )
        children = data.get("data", {}).get("children") if isinstance(data, dict) else None
        if not isinstance(children, list):
            raise MalformedResponseError("listing has no children", provider=self.NAME)
        now = self._clock()
        items: list[DiscoveryItem] = []
        for child in children:
            post = child.get("data") if isinstance(child, dict) else None
            if not isinstance(post, dict) or not post.get("title") or not post.get("permalink"):
                continue
            published = _dt(post.get("created_utc"))
            if request.since and published and published < request.since:
                continue
            permalink = f"https://www.reddit.com{post['permalink']}"
            external = post.get("url") if not post.get("is_self") else None
            items.append(
                DiscoveryItem(
                    provider_name=self.NAME,
                    source_key=source.key,
                    source_reference=permalink,
                    evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
                    title=str(post["title"])[:1000],
                    text=strip_html(str(post.get("selftext") or ""))[:MAX_TEXT] or None,
                    url=external if isinstance(external, str) else permalink,
                    publisher=domain_of(external) if isinstance(external, str) else None,
                    author=post.get("author"),
                    published_at=published,
                    collected_at=now,
                    category_hint=source.category,
                    metadata={"subreddit": source.reference, "score": post.get("score")},
                )
            )
        return DiscoveryResult(provider_name=self.NAME, source_key=source.key, items=tuple(items))
