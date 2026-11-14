"""Twitch app token and Helix client: auth lifecycle, endpoints, pagination, limits, errors."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from tests.helpers import MutableClock, SleepRecorder
from tests.twitch_fixtures import (
    CLIENT_ID,
    CLIENT_SECRET,
    NOW,
    FakeTwitch,
    body,
    make_client,
    stream,
)
from trend_agent.logging import redact_text
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import (
    AccessRestrictedError,
    InvalidCredentialsError,
    InvalidRequestError,
    MalformedResponseError,
    ProviderNotConfiguredError,
    ProviderTimeoutError,
    QuotaExhaustedError,
    RateLimitedError,
    TemporarilyUnavailableError,
)


def ctx() -> RequestContext:
    return RequestContext()


# ----------------------------------------------------------------------------- auth
async def test_app_token_is_requested_once_cached_and_sent_correctly() -> None:
    fake = FakeTwitch()
    c = make_client(fake)
    await c.top_games(ctx(), count=2)
    await c.games(ctx(), names=["Fortnite"])
    assert fake.tokens_issued == 1
    token_req = fake.calls("/oauth2/token")[0]
    assert token_req.method == "POST"
    assert CLIENT_SECRET not in str(token_req.url)  # secret only in the form body
    helix = fake.calls("/helix/games/top")[0]
    assert helix.headers["client-id"] == CLIENT_ID
    assert helix.headers["authorization"].startswith("Bearer apptoken")
    state = c.tokens.state()
    assert state["has_token"] is True
    assert state["validation"] == "issued"
    flat = repr(c.tokens) + str(state)
    assert "apptoken" not in flat
    assert CLIENT_SECRET not in flat


async def test_token_is_replaced_before_expiry() -> None:
    fake = FakeTwitch()
    fake.expires_in = 7200  # 2 h; refresh margin 1 h
    clock = MutableClock(NOW)
    c = make_client(fake, clock=clock)
    await c.top_games(ctx(), count=1)
    clock.advance(timedelta(minutes=30))
    await c.top_games(ctx(), count=1)
    assert fake.tokens_issued == 1
    clock.advance(timedelta(minutes=35))  # within 1 h of expiry
    await c.top_games(ctx(), count=1)
    assert fake.tokens_issued == 2


async def test_token_is_validated_hourly_and_a_revoked_token_is_replaced() -> None:
    fake = FakeTwitch()
    clock = MutableClock(NOW)
    c = make_client(fake, clock=clock)
    await c.top_games(ctx(), count=1)
    assert fake.calls("/oauth2/validate") == []  # a fresh token needs no validation
    clock.advance(timedelta(minutes=61))
    fake.revoke_all()
    await c.top_games(ctx(), count=1)
    assert len(fake.calls("/oauth2/validate")) == 1
    assert fake.tokens_issued == 2  # validation found it revoked: replaced
    assert c.tokens.state()["validation"] == "issued"


async def test_helix_401_renews_the_token_once() -> None:
    fake = FakeTwitch()
    c = make_client(fake)
    await c.top_games(ctx(), count=1)
    fake.revoke_all()  # revoked between validations
    res = await c.top_games(ctx(), count=1)
    assert res.ranked
    assert fake.tokens_issued == 2


async def test_rejected_after_renewal_is_invalid_credentials() -> None:
    fake = FakeTwitch()
    fake.override["/helix/games/top"] = body({"status": 401, "message": "Invalid OAuth token"}, 401)
    c = make_client(fake)
    with pytest.raises(InvalidCredentialsError, match="freshly issued"):
        await c.top_games(ctx(), count=1)
    assert fake.tokens_issued == 2


async def test_invalid_credentials_are_never_retried() -> None:
    fake = FakeTwitch()
    c = make_client(fake, client_secret="wrong-secret-000000000000000000", attempts=3)
    with pytest.raises(InvalidCredentialsError) as exc:
        await c.top_games(ctx(), count=1)
    assert exc.value.http_status == 403
    assert len(fake.calls("/oauth2/token")) == 1
    assert fake.calls("/helix/games/top") == []
    assert "wrong-secret" not in exc.value.message


async def test_missing_credentials_make_no_request() -> None:
    fake = FakeTwitch()
    c = make_client(fake, client_secret=None)
    assert c.tokens.configured is False
    with pytest.raises(ProviderNotConfiguredError):
        await c.top_games(ctx(), count=1)
    assert fake.requests == []


async def test_token_endpoint_outage_is_typed() -> None:
    fake = FakeTwitch()
    fake.override["/oauth2/token"] = httpx.ConnectError("refused")
    c = make_client(fake, attempts=1)
    with pytest.raises(TemporarilyUnavailableError):
        await c.top_games(ctx(), count=1)
    fake.override["/oauth2/token"] = body({"oops": True})
    c2 = make_client(fake, attempts=1)
    with pytest.raises(MalformedResponseError):
        await c2.top_games(ctx(), count=1)


def test_secrets_are_redacted_from_free_text() -> None:
    token = "apptoken0000000000000000000001"
    text = f"POST /oauth2/token client_secret={CLIENT_SECRET} Authorization: OAuth {token}"
    out = redact_text(text)
    assert CLIENT_SECRET not in out
    assert "apptoken0000000000000000000001" not in out


# ----------------------------------------------------------------------------- endpoints
async def test_top_games_rank_across_pages_without_inventing_viewers() -> None:
    fake = FakeTwitch()
    c = make_client(fake, page_size=2)
    res = await c.top_games(ctx(), count=4)
    assert [(r, g.name) for r, g in res.ranked] == [
        (1, "Just Chatting"), (2, "Fortnite"), (3, "League of Legends"), (4, "Unlisted Category"),
    ]  # fmt: skip
    assert res.pages_fetched == 2
    assert res.status == "complete"
    assert fake.calls("/helix/games/top")[1].url.params["after"] == "2"
    assert not hasattr(res.ranked[0][1], "viewer_count")
    assert res.ranked[0][1].igdb_id is None  # empty string -> None (not a game per IGDB)


async def test_game_lookup_by_name_and_id() -> None:
    fake = FakeTwitch()
    c = make_client(fake)
    games, _skipped = await c.games(ctx(), names=["Fortnite"])
    assert [(g.id, g.igdb_id) for g in games] == [("33214", "1905")]
    games, _ = await c.games(ctx(), ids=["777001", "nope"])
    assert [g.name for g in games] == ["Starfall Tactics"]
    with pytest.raises(InvalidRequestError):
        await c.games(ctx())


async def test_streams_filters_pagination_and_duplicates() -> None:
    fake = FakeTwitch()
    c = make_client(fake, page_size=2)
    res = await c.streams(ctx(), max_pages=5)
    assert [s.id for s in res.streams] == ["1", "2", "3", "4", "5"]
    assert res.status == "complete"
    assert res.viewer_sum == 5000 + 3000 + 2500 + 1200 + 300
    fortnite = await c.streams(ctx(), game_ids=["33214"], max_pages=5)
    assert {s.id for s in fortnite.streams} == {"2", "4"}
    req = fake.calls("/helix/streams")[-1]
    assert req.url.params.get_list("game_id") == ["33214"]
    assert req.url.params["type"] == "live"
    german = await c.streams(ctx(), languages=["DE"], max_pages=5)
    assert [s.id for s in german.streams] == ["4"]
    # the list re-orders while it is read: overlapping pages produce duplicates
    fake.page_shift = 1
    shifted = await c.streams(ctx(), max_pages=5)
    assert shifted.duplicates > 0
    assert len({s.id for s in shifted.streams}) == len(shifted.streams)


async def test_page_limit_and_errors_make_partial_samples() -> None:
    fake = FakeTwitch()
    c = make_client(fake, page_size=2, attempts=1)
    res = await c.streams(ctx(), max_pages=1)
    assert (res.status, res.reason) == ("partial", "page_limit_reached")
    first_page_only = FakeTwitch()
    orig = first_page_only.handler

    def fail_second(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/helix/streams" and request.url.params.get("after"):
            return httpx.Response(503)
        return orig(request)

    first_page_only.handler = fail_second  # type: ignore[method-assign]
    c2 = make_client(first_page_only, page_size=2, attempts=1)
    res2 = await c2.streams(ctx(), max_pages=3)
    assert res2.status == "partial"
    assert res2.reason.startswith("error_after_1_pages")
    assert len(res2.streams) == 2


async def test_unknown_pagination_and_missing_fields() -> None:
    fake = FakeTwitch()
    good = stream("9", "zulu", "33214", "Fortnite", 10)
    broken = {k: v for k, v in good.items() if k != "user_login"}
    offline = {**stream("10", "yankee", "33214", "Fortnite", 5), "type": "rerun"}
    fake.override["/helix/streams"] = body(
        {"data": [good, broken, offline, "junk"], "pagination": "weird"}
    )
    c = make_client(fake)
    res = await c.streams(ctx(), max_pages=2)
    assert [s.id for s in res.streams] == ["9"]
    assert res.status == "unknown"
    assert dict(res.skipped) == {
        "missing:user_login": 1,
        "not_live:rerun": 1,
        "malformed:not_an_object": 1,
    }


async def test_rate_limit_headers_are_tracked_and_the_reserve_respected() -> None:
    fake = FakeTwitch()
    sleeper = SleepRecorder()
    c = make_client(fake, sleeper=sleeper, reserve=20, max_wait=120)
    await c.top_games(ctx(), count=1)
    assert c.rate.limit == 800
    assert c.rate.remaining == 798
    fake.rate_remaining = 15
    await c.top_games(ctx(), count=1)  # bucket now at 14 <= reserve: next call waits
    await c.top_games(ctx(), count=1)
    assert sleeper.delays
    assert sleeper.delays[-1] == pytest.approx(60, abs=1)
    strict = make_client(fake, reserve=20, max_wait=5)
    await strict.top_games(ctx(), count=1)
    with pytest.raises(RateLimitedError, match="bucket nearly empty"):
        await strict.top_games(ctx(), count=1)


async def test_429_is_reported_with_the_reset_time() -> None:
    fake = FakeTwitch()
    fake.override["/helix/games/top"] = body(
        {"status": 429, "message": "Too Many Requests"},
        429,
        **{
            "ratelimit-limit": "800",
            "ratelimit-remaining": "0",
            "ratelimit-reset": str(int(NOW.timestamp()) + 30),
        },
    )
    c = make_client(fake, attempts=1)
    with pytest.raises(RateLimitedError) as exc:
        await c.top_games(ctx(), count=1)
    assert exc.value.retry_after == pytest.approx(30, abs=1)


@pytest.mark.parametrize(
    ("override", "error"),
    [
        (httpx.ReadTimeout("slow"), ProviderTimeoutError),
        (httpx.ConnectError("reset"), TemporarilyUnavailableError),
        (httpx.ProxyError("403"), AccessRestrictedError),
        (httpx.Response(500), TemporarilyUnavailableError),
        (httpx.Response(400, json={"message": "bad first"}), InvalidRequestError),
        (httpx.Response(200, content=b"<html>"), MalformedResponseError),
        (httpx.Response(200, json={"nodata": []}), MalformedResponseError),
    ],
)
async def test_errors_are_typed(override: object, error: type) -> None:
    fake = FakeTwitch()
    c = make_client(fake, attempts=1)
    await c.tokens.bearer()
    fake.override["/helix/games/top"] = override  # type: ignore[assignment]
    with pytest.raises(error):
        await c.top_games(ctx(), count=1)
    assert c.last_error is not None


async def test_transient_errors_are_retried() -> None:
    fake = FakeTwitch()
    responses = iter([httpx.Response(503)])
    orig = fake.handler

    def once(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/helix/games/top":
            nxt = next(responses, None)
            if nxt is not None:
                return nxt
        return orig(request)

    fake.handler = once  # type: ignore[method-assign]
    c = make_client(fake, attempts=3)
    res = await c.top_games(ctx(), count=1)
    assert res.ranked
    assert c.last_error is None


async def test_local_daily_budget() -> None:
    fake = FakeTwitch()
    c = make_client(fake, budget=2)
    await c.top_games(ctx(), count=1)
    await c.top_games(ctx(), count=1)
    with pytest.raises(QuotaExhaustedError):
        await c.top_games(ctx(), count=1)
    assert len(fake.calls("/helix/games/top")) == 2
