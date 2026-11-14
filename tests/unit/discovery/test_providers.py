"""Discovery providers. HTTP is served by ``httpx.MockTransport``; nothing touches the network."""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from tests.helpers import FakeYouTube, MutableClock, SleepRecorder, json_response, load_fixture
from tests.unit.discovery.helpers import LOADED, NOW
from trend_agent.config import TrendsAccessMode
from trend_agent.discovery.models import (
    Category,
    DiscoveryRequest,
    DiscoverySource,
    EvidenceType,
    SourceType,
)
from trend_agent.discovery.providers.base import (
    REQUEST_BUCKET,
    DiscoveryProvider,
    HttpFetcher,
    request_quota,
)
from trend_agent.discovery.providers.community import HackerNewsProvider, RedditProvider
from trend_agent.discovery.providers.factory import register_discovery_providers
from trend_agent.discovery.providers.feeds import FeedDiscoveryProvider, parse_feed
from trend_agent.discovery.providers.local import CuratedSeedProvider, TrendsRelatedQueriesProvider
from trend_agent.discovery.providers.synthetic import SyntheticDiscoveryProvider, load_scenario
from trend_agent.discovery.providers.youtube import YouTubeDiscoveryProvider
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import (
    AccessRestrictedError,
    CapabilityNotSupportedError,
    InvalidCredentialsError,
    InvalidRequestError,
    MalformedResponseError,
    ProviderNotConfiguredError,
    QuotaExhaustedError,
    RateLimitedError,
    TemporarilyUnavailableError,
)
from trend_agent.providers.google_trends import GoogleTrendsProvider
from trend_agent.providers.http import RetryPolicy
from trend_agent.providers.quota import InMemoryQuotaStore, QuotaLedger
from trend_agent.providers.registry import ProviderRegistry
from trend_agent.providers.youtube import YouTubeDataApiProvider
from trend_agent.schemas.enums import (
    MeasurementType,
    ProviderStatus,
    RecordKind,
    SearchProperty,
    TimeGranularity,
)

Handler = Callable[[httpx.Request], httpx.Response]
UA = "trend-agent-tests/0"


class Recorder:
    """MockTransport handler that records requests and replays scripted responses."""

    def __init__(self, *responses: httpx.Response | Exception | Handler) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        if callable(item) and not isinstance(item, httpx.Response):
            return item(request)
        return item


def make_fetcher(
    recorder: Recorder, name: str = "feeds", limit: int = 100, attempts: int = 2
) -> tuple[HttpFetcher, SleepRecorder]:
    clock = MutableClock(NOW)
    sleeper = SleepRecorder()
    fetcher = HttpFetcher(
        provider_name=name,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
        quota=request_quota(name, limit, InMemoryQuotaStore(), clock),
        retry=RetryPolicy(
            max_attempts=attempts, base_delay=0.1, max_delay=0.2, rng=random.Random(1)
        ),
        clock=clock,
        sleep=sleeper,
    )
    return fetcher, sleeper


def source(**kw: Any) -> DiscoverySource:
    base: dict[str, Any] = {
        "key": "feed:test",
        "provider": "feeds",
        "name": "Test feed",
        "source_type": SourceType.FEED,
        "category": Category.TECHNOLOGY,
        "reference": "https://example.test/feed.xml",
    }
    base.update(kw)
    return DiscoverySource(**base)


def ctx() -> RequestContext:
    return RequestContext(ledger=QuotaLedger())


RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Lattice releases</title>
<item><title>Lattice 5.0.0</title><link>https://example.test/lattice/5.0.0</link>
<pubDate>Sat, 28 Feb 2026 10:00:00 GMT</pubDate>
<description>&lt;p&gt;Major release&lt;/p&gt;</description></item>
<item><title>Lattice 4.0</title><link>https://example.test/lattice/4.0</link>
<pubDate>Mon, 01 Dec 2025 10:00:00 GMT</pubDate></item>
<item><title>  </title><link>https://example.test/blank</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Blog</title>
<entry><title>Introducing Nimbus Coder 2</title><id>tag:example.test,2026:1</id>
<link rel="alternate" href="https://example.test/blog/nimbus-coder-2"/>
<link rel="enclosure" href="https://example.test/file.bin"/>
<published>2026-03-01T09:00:00Z</published><summary>Open-weight coding model.</summary>
<author><name>Corvid Labs</name></author></entry></feed>"""

RDF = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
<item rdf:about="https://example.test/a"><title>Patch notes 3.2</title>
<link>https://example.test/a</link><dc:date>2026-02-27T08:00:00Z</dc:date></item></rdf:RDF>"""

BOMB = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>
<rss version="2.0"><channel><item><title>&lol2;</title></item></channel></rss>"""


# ---------------------------------------------------------------------------- feeds


def test_parse_rss_atom_and_rdf() -> None:
    rss = parse_feed(RSS)
    assert [e.title for e in rss] == ["Lattice 5.0.0", "Lattice 4.0"]  # blank title dropped
    assert rss[0].published is not None
    assert rss[0].published.tzinfo is not None
    atom = parse_feed(ATOM)
    assert atom[0].link == "https://example.test/blog/nimbus-coder-2"  # alternate, not enclosure
    assert atom[0].author == "Corvid Labs"
    rdf = parse_feed(RDF)
    assert rdf[0].title == "Patch notes 3.2"
    assert rdf[0].published is not None


@pytest.mark.parametrize("payload", [BOMB, b"<html><body>not a feed</body></html>", b"<<<"])
def test_unsafe_or_invalid_xml_is_rejected(payload: bytes) -> None:
    with pytest.raises(MalformedResponseError):
        parse_feed(payload)


async def test_feed_provider_items_and_conditional_requests() -> None:
    rec = Recorder(
        httpx.Response(200, content=RSS, headers={"ETag": '"v1"', "Last-Modified": "x"}),
        httpx.Response(304),
    )
    fetcher, _ = make_fetcher(rec)
    provider = FeedDiscoveryProvider(
        fetcher=fetcher, user_agent=UA, enabled=True, retention_days=90, clock=MutableClock(NOW)
    )
    assert isinstance(provider, DiscoveryProvider)
    src = source(
        evidence_type=EvidenceType.CODE_RELEASE, entity_hint="Lattice", publisher="Lattice"
    )
    assert provider.supports(src)
    assert not provider.supports(source(reference="http://insecure.test/feed"))
    result = await provider.discover(
        DiscoveryRequest(source=src, since=NOW - timedelta(days=14)), ctx()
    )
    assert [i.title for i in result.items] == ["Lattice 5.0.0"]  # older than `since` skipped
    item = result.items[0]
    assert item.evidence_type is EvidenceType.CODE_RELEASE
    assert item.text == "Major release"  # HTML stripped
    assert item.entity_hint == "Lattice"
    assert item.publisher == "Lattice"
    assert result.etag == '"v1"'
    assert rec.requests[0].headers["User-Agent"] == UA

    again = await provider.discover(
        DiscoveryRequest(source=src, etag='"v1"', last_modified="x"), ctx()
    )
    assert again.not_modified
    assert again.items == ()
    assert rec.requests[1].headers["If-None-Match"] == '"v1"'
    assert rec.requests[1].headers["If-Modified-Since"] == "x"
    assert provider.descriptor.retention[0].record_kind is RecordKind.DISCOVERY_ITEM
    assert provider.descriptor.retention[0].max_retention == timedelta(days=90)


async def test_proxy_denial_is_access_restricted_without_retries() -> None:
    rec = Recorder(httpx.ProxyError("403 Forbidden"))
    fetcher, sleeper = make_fetcher(rec, attempts=3)
    with pytest.raises(AccessRestrictedError, match="network policy denied"):
        await fetcher.get("https://blocked.test/feed", ctx())
    assert len(rec.requests) == 1
    assert sleeper.delays == []


@pytest.mark.parametrize(
    ("response", "error", "calls"),
    [
        (httpx.Response(401), InvalidCredentialsError, 1),
        (httpx.Response(403), AccessRestrictedError, 1),
        (httpx.Response(403, headers={"x-ratelimit-remaining": "0"}), RateLimitedError, 2),
        (httpx.Response(429, headers={"Retry-After": "0.1"}), RateLimitedError, 2),
        # A server wait longer than the policy allows is not honoured by sleeping.
        (httpx.Response(429, headers={"Retry-After": "60"}), RateLimitedError, 1),
        (httpx.Response(503), TemporarilyUnavailableError, 2),
        (httpx.Response(404), InvalidRequestError, 1),
        (httpx.ConnectError("boom"), TemporarilyUnavailableError, 2),
        (httpx.Response(200, content=b"x" * 2048), MalformedResponseError, 1),
    ],
)
async def test_http_error_mapping(
    response: httpx.Response | Exception, error: type[Exception], calls: int
) -> None:
    rec = Recorder(response)
    fetcher, _ = make_fetcher(rec, attempts=2)
    fetcher._max_bytes = 1024
    with pytest.raises(error):
        await fetcher.get("https://example.test/x", ctx())
    assert len(rec.requests) == calls


async def test_local_request_quota_blocks_before_network() -> None:
    rec = Recorder(httpx.Response(200, content=RSS))
    fetcher, _ = make_fetcher(rec, limit=1)
    provider = FeedDiscoveryProvider(
        fetcher=fetcher, user_agent=UA, enabled=True, retention_days=None, clock=MutableClock(NOW)
    )
    assert provider.can_afford(source()) == (True, "")
    await provider.discover(DiscoveryRequest(source=source()), ctx())
    ok, why = provider.can_afford(source())
    assert not ok
    assert "0 of 1 left" in why
    with pytest.raises(QuotaExhaustedError):
        await provider.discover(DiscoveryRequest(source=source()), ctx())
    assert len(rec.requests) == 1
    assert fetcher.quota.usage(REQUEST_BUCKET).used == 1


# ---------------------------------------------------------------------------- GitHub


RELEASES = [
    {
        "name": "Lattice 5.0.0",
        "tag_name": "v5.0.0",
        "html_url": "https://github.com/lattice/lattice/releases/tag/v5.0.0",
        "published_at": "2026-02-28T10:00:00Z",
        "body": "## Breaking changes\n* removed legacy API",
        "prerelease": False,
    },
    {"name": "draft", "tag_name": "v6", "html_url": "https://x.test", "draft": True},
    {"tag_name": "v5.1.0-rc1", "html_url": "https://github.com/x/y/rc1", "prerelease": True},
]


@pytest.mark.parametrize("token", [None, "ghp_testtoken0000000000000000000000"])
async def test_github_releases(token: str | None) -> None:
    from tests.github_fixtures import FakeGitHub, make_provider, release_json, repo_json

    fake = FakeGitHub()
    fake.on("/repos/lattice/lattice", httpx.Response(200, json=repo_json(9, "lattice/lattice")))
    fake.on(
        "/repos/lattice/lattice/releases",
        httpx.Response(
            200,
            json=[
                release_json(1, "lattice/lattice", "v5.0.0", name="Lattice 5.0.0"),
                release_json(2, "lattice/lattice", "v6", draft=True),
                release_json(3, "lattice/lattice", "v5.1.0-rc1", prerelease=True),
            ],
        ),
    )
    provider = make_provider(fake, token=token, clock=MutableClock(NOW))
    src = source(
        key="gh:lattice",
        provider="github",
        source_type=SourceType.REPOSITORY,
        reference="lattice/lattice",
        entity_hint="Lattice",
    )
    assert provider.supports(src)
    result = await provider.discover(DiscoveryRequest(source=src, max_items=10), ctx())
    releases = [i for i in result.items if i.evidence_type is EvidenceType.CODE_RELEASE]
    assert [i.title for i in releases] == ["Lattice 5.0.0", "v5.1.0-rc1"]  # draft skipped
    assert releases[1].metadata["prerelease"] is True
    assert result.items[0].evidence_type is EvidenceType.REPOSITORY_METADATA
    sent = fake.calls("/repos/lattice/lattice/releases")[0]
    assert sent.headers["X-GitHub-Api-Version"] == "2026-03-10"
    assert ("Authorization" in sent.headers) is (token is not None)
    if token:
        assert token not in repr(provider.status())
        assert all(token not in i.model_dump_json() for i in result.items)


async def test_github_rejects_invalid_repository_references() -> None:
    from tests.github_fixtures import FakeGitHub, make_provider

    provider = make_provider(FakeGitHub(), clock=MutableClock(NOW))
    for reference in ("../etc", "owner/..", "owner/.", "a/b/c", "owner", "-x/y"):
        bad = source(provider="github", source_type=SourceType.REPOSITORY, reference=reference)
        assert not provider.supports(bad), reference
        with pytest.raises(InvalidRequestError):
            await provider.discover(DiscoveryRequest(source=bad), ctx())
    good = source(
        provider="github", source_type=SourceType.REPOSITORY, reference="org-name/.github"
    )
    assert provider.supports(good)


# ---------------------------------------------------------------------------- Hacker News


async def test_hackernews_search_and_origin_url() -> None:
    hits = {
        "hits": [
            {
                "objectID": "42",
                "title": "Nimbus Coder 2 is out",
                "url": "https://corvid.test/blog/nimbus-coder-2",
                "author": "alice",
                "created_at": "2026-03-01T10:00:00Z",
                "points": 120,
            },
            {"objectID": "43", "title": "Ask HN: local coding models?", "author": "bob"},
            {"title": "no id"},
        ]
    }
    rec = Recorder(json_response(200, hits))
    fetcher, _ = make_fetcher(rec, name="hackernews")
    provider = HackerNewsProvider(
        fetcher=fetcher, user_agent=UA, enabled=True, retention_days=90, clock=MutableClock(NOW)
    )
    src = source(
        key="hn:q",
        provider="hackernews",
        source_type=SourceType.API_QUERY,
        reference="coding model",
        category=Category.AI,
    )
    result = await provider.discover(
        DiscoveryRequest(source=src, since=NOW - timedelta(days=1)), ctx()
    )
    assert len(result.items) == 2
    linked, ask = result.items
    # The linked article (not the HN thread) is the origin, so an HN post about an
    # announcement is not independent corroboration of it.
    assert linked.url == "https://corvid.test/blog/nimbus-coder-2"
    assert linked.publisher == "corvid.test"
    assert linked.source_reference == "https://news.ycombinator.com/item?id=42"
    assert ask.url == "https://news.ycombinator.com/item?id=43"
    assert all(i.evidence_type is EvidenceType.COMMUNITY_DISCUSSION for i in result.items)
    params = rec.requests[0].url.params
    assert rec.requests[0].url.path.endswith("/search_by_date")
    assert params["query"] == "coding model"
    assert params["numericFilters"].startswith("created_at_i>")


# ---------------------------------------------------------------------------- Reddit


def reddit(rec: Recorder, client_id: str | None, secret: str | None) -> RedditProvider:
    fetcher, _ = make_fetcher(rec, name="reddit")
    return RedditProvider(
        fetcher=fetcher,
        client=httpx.AsyncClient(transport=httpx.MockTransport(rec)),
        client_id=SecretStr(client_id) if client_id else None,
        client_secret=SecretStr(secret) if secret else None,
        user_agent=UA,
        enabled=True,
        retention_days=30,
        clock=MutableClock(NOW),
    )


async def test_reddit_without_credentials_is_not_configured_and_makes_no_requests() -> None:
    rec = Recorder(httpx.Response(500))
    provider = reddit(rec, None, None)
    report = provider.status()
    assert report.status is ProviderStatus.NOT_CONFIGURED
    assert "TREND_AGENT_REDDIT_CLIENT_ID" in report.missing_configuration
    src = source(key="r:x", provider="reddit", source_type=SourceType.COMMUNITY, reference="gaming")
    with pytest.raises(ProviderNotConfiguredError):
        await provider.discover(DiscoveryRequest(source=src), ctx())
    assert rec.requests == []


async def test_reddit_oauth_flow_caches_token_and_hides_secrets() -> None:
    listing = {
        "data": {
            "children": [
                {
                    "data": {
                        "title": "Frostfall Festival guide",
                        "permalink": "/r/x/comments/1/a/",
                        "is_self": True,
                        "selftext": "tips",
                        "author": "u1",
                        "created_utc": NOW.timestamp() - 60,
                    }
                },
                {
                    "data": {
                        "title": "link post",
                        "permalink": "/r/x/comments/2/b/",
                        "is_self": False,
                        "url": "https://news.test/a",
                        "created_utc": NOW.timestamp() - 120,
                    }
                },
            ]
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/access_token":
            return json_response(200, {"access_token": "tok-123", "expires_in": 3600})
        return json_response(200, listing)

    rec = Recorder(handler)
    provider = reddit(rec, "client-id", "super-secret")
    src = source(key="r:x", provider="reddit", source_type=SourceType.COMMUNITY, reference="gaming")
    first = await provider.discover(DiscoveryRequest(source=src), ctx())
    await provider.discover(DiscoveryRequest(source=src), ctx())
    token_calls = [r for r in rec.requests if r.url.path == "/api/v1/access_token"]
    assert len(token_calls) == 1  # cached
    assert rec.requests[-1].headers["Authorization"] == "Bearer tok-123"
    assert [i.url for i in first.items] == [
        "https://www.reddit.com/r/x/comments/1/a/",
        "https://news.test/a",
    ]
    assert "super-secret" not in repr(provider.status())


async def test_reddit_rejected_credentials() -> None:
    provider = reddit(Recorder(httpx.Response(401)), "id", "bad")
    src = source(key="r:x", provider="reddit", source_type=SourceType.COMMUNITY, reference="gaming")
    with pytest.raises(InvalidCredentialsError):
        await provider.discover(DiscoveryRequest(source=src), ctx())


# ---------------------------------------------------------------------------- YouTube


def yt_source(source_type: SourceType, reference: str) -> DiscoverySource:
    return source(
        key=f"yt:{source_type.value}",
        provider="youtube_discovery",
        source_type=source_type,
        reference=reference,
        category=Category.AI,
    )


async def test_youtube_discovery_query_uses_search_bucket(
    make_youtube: Callable[..., YouTubeDataApiProvider], fake_youtube: FakeYouTube
) -> None:
    fake_youtube.queue("search", json_response(200, load_fixture("search_page1.json")))
    yt = make_youtube(max_pages=1)
    cap = request_quota("youtube_discovery", 1, InMemoryQuotaStore(), MutableClock(NOW))
    provider = YouTubeDiscoveryProvider(
        yt, enabled=True, retention_days=90, results_per_query=2, discovery_quota=cap
    )
    src = yt_source(SourceType.API_QUERY, "AI agents")
    assert provider.can_afford(src) == (True, "")
    context = ctx()
    result = await provider.discover(DiscoveryRequest(source=src, max_items=10), context)
    assert context.ledger.used == {"search": 1, REQUEST_BUCKET: 1}
    # Discovery's own daily cap is enforced before any YouTube quota is spent.
    ok, why = provider.can_afford(src)
    assert not ok
    assert why.startswith("discovery cap")
    with pytest.raises(QuotaExhaustedError):
        await provider.discover(DiscoveryRequest(source=src), ctx())
    assert len(fake_youtube.calls("search")) == 1
    assert result.items
    assert all(i.evidence_type is EvidenceType.VIDEO_TITLE for i in result.items)
    assert fake_youtube.calls("search")[0].url.params["order"] == "date"
    # Video titles are supply-side evidence, never search volume.
    measurement = provider.descriptor.measurement_types
    assert measurement == frozenset({MeasurementType.VIDEO_ACTIVITY})
    # YouTube API data may be kept at most 30 days, whatever the config says.
    assert provider.descriptor.retention[0].max_retention == timedelta(days=30)


async def test_youtube_discovery_channel_uploads_cost_default_bucket(
    make_youtube: Callable[..., YouTubeDataApiProvider], fake_youtube: FakeYouTube
) -> None:
    fake_youtube.queue(
        "channels",
        json_response(
            200, {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUabc"}}}]}
        ),
    )
    fake_youtube.queue(
        "playlistItems",
        json_response(
            200,
            {
                "items": [
                    {
                        "snippet": {
                            "title": "Frostfall Festival trailer",
                            "channelId": "UCx",
                            "publishedAt": "2026-03-01T08:00:00Z",
                            "resourceId": {"videoId": "vidFROST001"},
                        }
                    },
                    {"snippet": {"title": "broken item"}},
                ]
            },
        ),
    )
    provider = YouTubeDiscoveryProvider(make_youtube(), enabled=True, retention_days=None)
    context = ctx()
    result = await provider.discover(
        DiscoveryRequest(source=yt_source(SourceType.CHANNEL, "@skyforge")), context
    )
    assert [i.title for i in result.items] == ["Frostfall Festival trailer"]
    assert result.warnings  # the malformed item is reported, not silently dropped
    assert context.ledger.used == {"default": 2}
    assert fake_youtube.calls("channels")[0].url.params["forHandle"] == "@skyforge"


async def test_youtube_discovery_quota_and_configuration(
    make_youtube: Callable[..., YouTubeDataApiProvider],
) -> None:
    store = InMemoryQuotaStore()
    yt = make_youtube(store=store, youtube_search_daily_call_limit=0)
    provider = YouTubeDiscoveryProvider(yt, enabled=True, retention_days=None)
    ok, why = provider.can_afford(yt_source(SourceType.API_QUERY, "x"))
    assert not ok
    assert "'search'" in why
    assert provider.can_afford(yt_source(SourceType.CHART, "28"))[0]
    unconfigured = YouTubeDiscoveryProvider(
        make_youtube(api_key=None), enabled=True, retention_days=None
    )
    assert unconfigured.status().status is ProviderStatus.NOT_CONFIGURED
    with pytest.raises(ProviderNotConfiguredError):
        await unconfigured.discover(
            DiscoveryRequest(source=yt_source(SourceType.API_QUERY, "x")), ctx()
        )


# ---------------------------------------------------------------------------- Trends / seeds


class _RelatedTransport:
    verified = True
    supported_properties = frozenset({SearchProperty.GOOGLE_WEB_SEARCH})
    supported_granularities = frozenset({TimeGranularity.DAY})

    async def fetch(self, req: Any) -> Any:  # pragma: no cover - not used here
        raise AssertionError

    async def fetch_related_queries(self, term: str, region: str | None) -> list[str]:
        return [f"{term} tutorial", f"{term} vs claude"]


def trends(
    mode: TrendsAccessMode, transport: Any = None, *, configured: bool = False
) -> GoogleTrendsProvider:
    return GoogleTrendsProvider(
        access_mode=mode,
        credentials_file="/run/secrets/trends.json" if configured else None,
        endpoint="https://trends-alpha.example.test" if configured else None,
        retention_days=None,
        transport=transport,
        clock=MutableClock(NOW),
    )


async def test_trends_related_queries_are_honest_about_access() -> None:
    src = source(
        key="trends:x",
        provider="google_trends_discovery",
        source_type=SourceType.API_QUERY,
        reference="ollama",
    )
    disabled = TrendsRelatedQueriesProvider(
        trends(TrendsAccessMode.DISABLED), enabled=True, clock=MutableClock(NOW)
    )
    assert disabled.status().status is ProviderStatus.NOT_CONFIGURED
    with pytest.raises(ProviderNotConfiguredError):
        await disabled.discover(DiscoveryRequest(source=src), ctx())

    alpha_no_transport = TrendsRelatedQueriesProvider(
        trends(TrendsAccessMode.OFFICIAL_ALPHA, configured=True),
        enabled=True,
        clock=MutableClock(NOW),
    )
    assert alpha_no_transport.status().status is ProviderStatus.ACCESS_RESTRICTED
    with pytest.raises(AccessRestrictedError):
        await alpha_no_transport.discover(DiscoveryRequest(source=src), ctx())


async def test_trends_related_queries_with_capable_transport() -> None:
    src = source(
        key="trends:x",
        provider="google_trends_discovery",
        source_type=SourceType.API_QUERY,
        reference="ollama",
    )
    base = trends(TrendsAccessMode.OFFICIAL_ALPHA, _RelatedTransport(), configured=True)
    assert base.status().status is ProviderStatus.AVAILABLE
    provider = TrendsRelatedQueriesProvider(base, enabled=True, clock=MutableClock(NOW))
    result = await provider.discover(DiscoveryRequest(source=src), ctx())
    (item,) = result.items
    assert item.evidence_type is EvidenceType.RELATED_QUERY
    assert item.observed_queries == ("ollama tutorial", "ollama vs claude")
    assert provider.descriptor.measurement_types == frozenset({MeasurementType.SEARCH_SUGGESTION})


async def test_trends_transport_without_related_queries_is_unsupported() -> None:
    class _NoRelated:
        verified = True
        supported_properties = frozenset({SearchProperty.GOOGLE_WEB_SEARCH})
        supported_granularities = frozenset({TimeGranularity.DAY})

        async def fetch(self, req: Any) -> Any:  # pragma: no cover
            raise AssertionError

    base = trends(TrendsAccessMode.OFFICIAL_ALPHA, _NoRelated(), configured=True)
    assert base.status().status is ProviderStatus.AVAILABLE
    provider = TrendsRelatedQueriesProvider(base, enabled=True, clock=MutableClock(NOW))
    assert provider.status().status is ProviderStatus.UNSUPPORTED
    src = source(
        key="t", provider="google_trends_discovery", source_type=SourceType.API_QUERY, reference="x"
    )
    with pytest.raises(CapabilityNotSupportedError):
        await provider.discover(DiscoveryRequest(source=src), ctx())


async def test_curated_seed_items_are_labelled_as_seeds() -> None:
    provider = CuratedSeedProvider(enabled=True, clock=MutableClock(NOW))
    src = source(
        key="seed:ai:x",
        provider="curated_seeds",
        source_type=SourceType.SEED,
        reference="local llm",
        params={"track_as_keyword": True},
    )
    (item,) = (await provider.discover(DiscoveryRequest(source=src), ctx())).items
    assert item.evidence_type is EvidenceType.CURATED_SEED
    assert item.metadata["track_as_keyword"] is True
    assert item.observed_queries == ()  # a seed is never an observed query


# ---------------------------------------------------------------------------- synthetic


async def test_synthetic_provider_behaviours_and_labels() -> None:
    scenario = load_scenario()
    provider = SyntheticDiscoveryProvider(
        scenario, clock=MutableClock(scenario.stage_time("current"))
    )
    current = {s.key: s for s in scenario.sources_for("current")}
    history = {s.key for s in scenario.sources_for("history")}
    assert current
    assert history
    assert not history & set(current)  # stages are disjoint
    ok_source = next(
        s
        for s in current.values()
        if s.provider == "synthetic_discovery" and "outage" not in s.key and "quota" not in s.key
    )
    result = await provider.discover(DiscoveryRequest(source=ok_source), ctx())
    assert result.items
    for item in result.items:
        assert item.evidence_type is EvidenceType.SYNTHETIC_TEST_DATA
        assert item.is_synthetic
        assert item.source_reference.startswith("synthetic://")
        assert EvidenceType(item.metadata["simulated_evidence_type"])
    errors = {"outage-source": TemporarilyUnavailableError, "quota-source": QuotaExhaustedError}
    for key, error in errors.items():
        matching = [s for k, s in current.items() if key in k]
        assert matching, key
        with pytest.raises(error):
            await provider.discover(DiscoveryRequest(source=matching[0]), ctx())
    unsupported = [s for s in current.values() if s.provider != "synthetic_discovery"]
    assert unsupported  # the unlicensed scraper source is configured but has no provider


# ---------------------------------------------------------------------------- factory


def test_factory_registers_discovery_providers_idempotently() -> None:
    from tests.helpers import make_settings

    registry = ProviderRegistry()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    kwargs: dict[str, Any] = {
        "settings": make_settings(),
        "loaded": LOADED,
        "quota_store": InMemoryQuotaStore(),
        "clock": MutableClock(NOW),
        "http_client": client,
    }
    register_discovery_providers(registry, **kwargs)
    names = set(registry.names())
    register_discovery_providers(registry, **kwargs)
    assert set(registry.names()) == names
    assert {"feeds", "github", "hackernews", "reddit", "curated_seeds"} <= names
    assert registry.get("reddit").status().status is ProviderStatus.NOT_CONFIGURED
