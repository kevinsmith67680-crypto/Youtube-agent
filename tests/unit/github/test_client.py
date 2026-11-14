"""GitHub REST client: authentication, versioning, pagination, rate limits, errors."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest

from tests.github_fixtures import (
    NOW,
    TOKEN,
    FakeGitHub,
    limits,
    make_client,
    release_json,
    repo_json,
    search_page,
)
from tests.helpers import MutableClock, SleepRecorder
from trend_agent.logging import redact_text
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import (
    AccessRestrictedError,
    InvalidCredentialsError,
    InvalidRequestError,
    MalformedResponseError,
    ProviderTimeoutError,
    QuotaExhaustedError,
    RateLimitedError,
    TemporarilyUnavailableError,
)


def ctx() -> RequestContext:
    return RequestContext()


async def test_authenticated_requests_use_documented_headers_and_never_the_url() -> None:
    fake = FakeGitHub().on("/search/repositories", search_page([repo_json(1, "a/agentkit")]))
    client = make_client(fake, token=TOKEN)
    result = await client.search_repositories("topic:ai-agent", ctx())
    sent = fake.requests[0]
    assert sent.method == "GET"
    assert sent.headers["Authorization"] == f"Bearer {TOKEN}"
    assert sent.headers["Accept"] == "application/vnd.github+json"
    assert sent.headers["X-GitHub-Api-Version"] == "2026-03-10"
    assert sent.headers["User-Agent"]
    assert TOKEN not in str(sent.url)
    assert client.auth_mode() == "authenticated"
    assert [r.full_name for r in result.repositories] == ["a/agentkit"]
    assert TOKEN not in repr(client.rate_limit_snapshot())


async def test_public_unauthenticated_access_sends_no_authorization() -> None:
    fake = FakeGitHub().on("/search/repositories", search_page([repo_json(1, "a/b-tool")]))
    client = make_client(fake, token=None)
    await client.search_repositories("topic:llm", ctx())
    assert "Authorization" not in fake.requests[0].headers
    assert client.auth_mode() == "unauthenticated"


async def test_api_version_is_configurable_and_validated() -> None:
    fake = FakeGitHub().on("/search/repositories", search_page([]))
    client = make_client(fake, api_version="2022-11-28")
    await client.search_repositories("topic:llm", ctx())
    assert fake.requests[0].headers["X-GitHub-Api-Version"] == "2022-11-28"
    with pytest.raises(ValueError, match="unsupported GitHub API version"):
        make_client(FakeGitHub(), api_version="2099-01-01")


async def test_search_pagination_is_bounded_follows_next_and_dedupes() -> None:
    page1 = search_page(
        [repo_json(1, "a/one"), repo_json(2, "a/two")],
        total=500,
        link='<https://api.github.com/search/repositories?q=x&page=2>; rel="next"',
    )
    page2 = search_page(
        [repo_json(2, "a/two"), repo_json(3, "a/three")],
        total=500,
        link='<https://api.github.com/search/repositories?q=x&page=3>; rel="next"',
    )
    fake = FakeGitHub().on("/search/repositories", page1, page2, search_page([repo_json(4, "a/f")]))
    client = make_client(fake, max_pages=2)
    result = await client.search_repositories("topic:ai-agent", ctx(), per_page=2, max_pages=5)
    assert len(fake.requests) == 2  # bounded by the client's max_pages
    assert [r.id for r in result.repositories] == [1, 2, 3]  # duplicate across pages once
    assert result.more_available is True
    assert result.total_count == 500


async def test_pagination_never_follows_links_off_the_api_host() -> None:
    evil = search_page(
        [repo_json(1, "a/one")], link='<https://evil.example/steal?page=2>; rel="next"'
    )
    fake = FakeGitHub().on("/search/repositories", evil)
    client = make_client(fake, max_pages=3)
    with pytest.raises(InvalidRequestError):
        await client.search_repositories("topic:x", ctx())
    assert all(r.url.host == "api.github.com" for r in fake.requests)


async def test_search_query_limits() -> None:
    client = make_client(FakeGitHub())
    with pytest.raises(InvalidRequestError):
        await client.search_repositories("x" * 300, ctx())
    with pytest.raises(InvalidRequestError):
        await client.search_repositories("topic:x", ctx(), sort="popularity")


async def test_invalid_credentials_are_detected_and_not_retried() -> None:
    fake = FakeGitHub().always(
        "/search/repositories", httpx.Response(401, json={"message": "Bad credentials"})
    )
    client = make_client(fake, token=TOKEN, attempts=4)
    with pytest.raises(InvalidCredentialsError) as err:
        await client.search_repositories("topic:llm", ctx())
    assert len(fake.requests) == 1
    assert err.value.reason == "bad_credentials"
    assert client.auth_mode() == "invalid_credentials"
    assert TOKEN not in err.value.message


async def test_insufficient_permissions_are_reported_with_accepted_permissions() -> None:
    fake = FakeGitHub().always(
        "/repos/a/private-thing",
        httpx.Response(
            403,
            json={"message": "Resource not accessible by personal access token"},
            headers={"x-accepted-github-permissions": "metadata=read", **limits("core")},
        ),
    )
    client = make_client(fake, token=TOKEN)
    with pytest.raises(AccessRestrictedError) as err:
        await client.get_repository("a/private-thing", ctx())
    assert err.value.reason == "insufficient_permissions"
    assert "metadata=read" in err.value.message
    assert not isinstance(err.value, InvalidCredentialsError)


async def test_primary_rate_limit_waits_for_reset_without_further_requests() -> None:
    exhausted = httpx.Response(
        403,
        json={"message": "API rate limit exceeded"},
        headers=limits("search", remaining=0, limit=30, reset_in=50),
    )
    fake = FakeGitHub().always("/search/repositories", exhausted)
    clock = MutableClock(NOW)
    client = make_client(fake, clock=clock, attempts=3)
    with pytest.raises(RateLimitedError) as err:
        await client.search_repositories("topic:llm", ctx())
    assert err.value.reason in ("primary_rate_limit", "rate_limit_reserve")
    sent = len(fake.requests)
    assert sent == 1  # the reset is 50 s away (> max retry delay): no waiting, no retry
    ok, why = client.rate_limit_ok("search")
    assert not ok
    assert "paused until" in why
    with pytest.raises(RateLimitedError):
        await client.search_repositories("topic:other", ctx())
    assert len(fake.requests) == sent  # blocked locally until the reset
    clock.advance(timedelta(seconds=60))
    fake.always("/search/repositories", search_page([]))
    await client.search_repositories("topic:other", ctx())
    assert len(fake.requests) == sent + 1


async def test_secondary_rate_limit_honours_retry_after() -> None:
    secondary = httpx.Response(
        403,
        json={"message": "You have exceeded a secondary rate limit"},
        headers={"retry-after": "2", **limits("search", remaining=10, limit=30)},
    )
    clock = MutableClock(NOW)

    class AdvancingSleep(SleepRecorder):
        async def __call__(self, delay: float) -> None:
            self.delays.append(delay)
            clock.advance(timedelta(seconds=delay))

    sleeper = AdvancingSleep()
    fake = FakeGitHub().on("/search/repositories", secondary, search_page([repo_json(1, "a/x")]))
    client = make_client(fake, attempts=3, sleeper=sleeper, clock=clock)
    result = await client.search_repositories("topic:llm", ctx())
    assert len(result.repositories) == 1
    assert sleeper.delays
    assert sleeper.delays[0] >= 2.0


async def test_429_without_headers_is_a_rate_limit_and_429_long_waits_fail_fast() -> None:
    fake = FakeGitHub().always(
        "/search/repositories", httpx.Response(429, json={"message": "slow down"})
    )
    client = make_client(fake, attempts=3)
    with pytest.raises(RateLimitedError) as err:
        await client.search_repositories("topic:llm", ctx())
    assert err.value.reason == "secondary_rate_limit"
    assert len(fake.requests) == 1  # default 60 s wait exceeds the retry budget


async def test_low_remaining_stops_before_github_limit() -> None:
    fake = FakeGitHub().always(
        "/search/repositories", search_page([], **limits("search", remaining=1, limit=30))
    )
    client = make_client(fake)
    await client.search_repositories("topic:a", ctx())
    ok, why = client.rate_limit_ok("search")
    assert not ok
    assert "1/30 left" in why


async def test_network_failures_are_typed_and_retried() -> None:
    sleeper = SleepRecorder()
    fake = FakeGitHub().on(
        "/search/repositories",
        httpx.ConnectError("refused"),
        search_page([repo_json(1, "a/x")]),
    )
    client = make_client(fake, attempts=3, sleeper=sleeper)
    assert (await client.search_repositories("topic:a", ctx())).repositories
    assert len(sleeper.delays) == 1
    fake = FakeGitHub().always("/search/repositories", httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderTimeoutError):
        await make_client(fake, attempts=2).search_repositories("topic:a", ctx())
    fake = FakeGitHub().always("/search/repositories", httpx.ConnectError("down"))
    with pytest.raises(TemporarilyUnavailableError):
        await make_client(fake, attempts=2).search_repositories("topic:a", ctx())
    fake = FakeGitHub().always("/search/repositories", httpx.ProxyError("denied"))
    with pytest.raises(AccessRestrictedError) as err:
        await make_client(fake).search_repositories("topic:a", ctx())
    assert err.value.reason == "network_policy"


async def test_server_errors_are_retried_with_backoff() -> None:
    sleeper = SleepRecorder()
    fake = FakeGitHub().on(
        "/repos/a/b", httpx.Response(502), httpx.Response(200, json=repo_json(1, "a/b"))
    )
    repo, _ = await make_client(fake, attempts=3, sleeper=sleeper).get_repository("a/b", ctx())
    assert repo is not None
    assert repo.id == 1
    assert len(sleeper.delays) == 1


async def test_schema_change_is_detected_and_single_bad_items_are_skipped() -> None:
    broken = dict(repo_json(2, "a/broken"))
    del broken["stargazers_count"]
    fake = FakeGitHub().on(
        "/search/repositories", search_page([repo_json(1, "a/ok"), broken, repo_json(3, "a/x")])
    )
    result = await make_client(fake).search_repositories("topic:a", ctx())
    assert [r.id for r in result.repositories] == [1, 3]
    assert any("stargazers_count" in w for w in result.warnings)
    fake = FakeGitHub().on(
        "/search/repositories",
        httpx.Response(200, json={"total_count": 1, "items": [{"id": 1}, {"id": 2}]}),
    )
    with pytest.raises(MalformedResponseError) as err:
        await make_client(fake).search_repositories("topic:a", ctx())
    assert err.value.reason == "schema_changed"
    fake = FakeGitHub().on("/search/repositories", httpx.Response(200, json=["not", "an object"]))
    with pytest.raises(MalformedResponseError):
        await make_client(fake).search_repositories("topic:a", ctx())
    fake = FakeGitHub().on("/repos/a/b/releases", httpx.Response(200, json={"oops": 1}))
    with pytest.raises(MalformedResponseError):
        await make_client(fake).list_releases("a/b", ctx())


async def test_conditional_requests_reuse_the_cached_body_on_304() -> None:
    first = httpx.Response(200, json=repo_json(1, "a/b"), headers={"etag": 'W/"abc"'})
    fake = FakeGitHub().on("/repos/a/b", first, httpx.Response(304))
    client = make_client(fake)
    repo1, nm1 = await client.get_repository("a/b", ctx())
    repo2, nm2 = await client.get_repository("a/b", ctx())
    assert not nm1
    assert nm2
    assert repo2 == repo1
    assert fake.requests[1].headers["If-None-Match"] == 'W/"abc"'


async def test_releases_use_the_stored_etag_and_report_not_modified() -> None:
    fake = FakeGitHub().on("/repos/a/b/releases", httpx.Response(304, headers={"etag": '"e1"'}))
    result = await make_client(fake).list_releases("a/b", ctx(), etag='"e1"')
    assert result.not_modified
    assert result.releases == []
    assert fake.requests[0].headers["If-None-Match"] == '"e1"'


async def test_drafts_are_excluded_from_releases() -> None:
    fake = FakeGitHub().on(
        "/repos/a/b/releases",
        httpx.Response(
            200,
            json=[release_json(1, "a/b", "v1.0.0"), release_json(2, "a/b", "v2.0.0", draft=True)],
        ),
    )
    result = await make_client(fake).list_releases("a/b", ctx())
    assert [r.tag_name for r in result.releases] == ["v1.0.0"]


async def test_identical_requests_are_deduplicated() -> None:
    fake = FakeGitHub().always("/search/repositories", search_page([repo_json(1, "a/x")]))
    client = make_client(fake, dedup_ttl=timedelta(minutes=10))
    await asyncio.gather(
        client.search_repositories("topic:a", ctx()), client.search_repositories("topic:a", ctx())
    )
    await client.search_repositories("topic:a", ctx())
    assert len(fake.requests) == 1
    await client.search_repositories("topic:b", ctx())
    assert len(fake.requests) == 2


async def test_local_daily_budget_is_enforced_per_bucket() -> None:
    fake = FakeGitHub().always("/search/repositories", search_page([]))
    client = make_client(fake, search_daily=2)
    await client.search_repositories("topic:a", ctx())
    await client.search_repositories("topic:b", ctx())
    with pytest.raises(QuotaExhaustedError):
        await client.search_repositories("topic:c", ctx())
    assert len(fake.requests) == 2
    assert client.quota.usage("core").used == 0  # searches never spend the core budget


async def test_rate_limit_endpoint_reads_resources_only() -> None:
    body = {
        "resources": {
            "core": {"limit": 5000, "remaining": 4990, "used": 10, "reset": 1_900_000_000},
            "search": {"limit": 30, "remaining": 30, "used": 0, "reset": 1_900_000_000},
        }
    }  # API version 2026-03-10 removed the top-level "rate" object
    fake = FakeGitHub().on("/rate_limit", httpx.Response(200, json=body))
    client = make_client(fake)
    out = await client.rate_limit(ctx())
    assert out["core"]["remaining"] == 4990
    assert out["search"]["limit"] == 30
    assert client.quota.usage("core").used == 0  # /rate_limit spends no core budget
    fake = FakeGitHub().on("/rate_limit", httpx.Response(200, json={"rate": {}}))
    with pytest.raises(MalformedResponseError):
        await make_client(fake).rate_limit(ctx())


async def test_invalid_repository_names_never_reach_the_network() -> None:
    fake = FakeGitHub()
    client = make_client(fake)
    for bad in ("../x", "a/..", "a/b/c", "", "a"):
        with pytest.raises(InvalidRequestError):
            await client.get_repository(bad, ctx())
    assert fake.requests == []


def test_tokens_are_redacted_from_log_text() -> None:
    text = f"failed with {TOKEN} and ghp_{'a' * 36}"
    assert "github_pat_" not in redact_text(text)
    assert "ghp_" not in redact_text(text)
