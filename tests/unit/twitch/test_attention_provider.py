"""Twitch attention detector, provider (status, catalog items, signal class), settings policy."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.helpers import MutableClock
from tests.twitch_fixtures import NOW, FakeTwitch, make_client
from trend_agent.discovery.config import ConfigError
from trend_agent.discovery.models import DiscoveryRequest, EvidenceType, SourceType
from trend_agent.discovery.providers.twitch import TwitchDiscoveryProvider, game_source
from trend_agent.ops.signals import SEARCH_INTEREST_CLASSES, SignalClass, classify
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import InvalidCredentialsError, ProviderDisabledError
from trend_agent.schemas.enums import Capability, ProviderStatus
from trend_agent.twitch import attention as att
from trend_agent.twitch.config import AttentionSettings, TwitchSettings, load_twitch_settings
from trend_agent.twitch.models import category_kind, parse_stream

S = AttentionSettings()


def vp(
    minutes: int,
    viewers: int,
    *,
    status: str = "complete",
    basis: str = "game_streams",
    cap: int = 500,
    streams: int = 10,
    langs: tuple[str, ...] = (),
) -> att.ViewerPoint:
    return att.ViewerPoint(
        NOW + timedelta(minutes=minutes), viewers, streams, status, basis, cap, langs
    )


def rp(minutes: int, rank: int) -> att.RankPoint:
    return att.RankPoint(NOW + timedelta(minutes=minutes), rank)


def tops(*minutes: int) -> list:
    return [NOW + timedelta(minutes=m) for m in minutes]


# ----------------------------------------------------------------------------- attention
def test_one_observation_is_never_growth() -> None:
    a = att.assess([], [], [vp(0, 5000)], S)
    assert a.state == "insufficient_observations"
    assert a.score == 0.0
    assert a.viewers["pct_per_hour"] is None
    assert "OBSERVED_VIEWERS" in a.labels
    assert "RANK_CHANGE" not in a.labels
    d = a.as_dict()
    assert d["signal_class"] == "LIVESTREAM_ACTIVITY"
    assert any("not YouTube search interest" in c for c in d["caveats"])


def test_rates_use_the_actual_interval_between_comparable_samples() -> None:
    a = att.assess([], [], [vp(0, 1000), vp(30, 1300)], S)
    v = a.viewers
    assert v["interval_hours"] == 0.5
    assert v["viewer_delta"] == 300
    assert v["pct_change"] == 30.0
    assert v["pct_per_hour"] == 60.0
    assert a.state == "rising"
    assert v["comparison"] == "both complete samples"


def test_incompatible_samples_are_never_compared() -> None:
    mixed = att.assess([], [], [vp(0, 1000, status="partial"), vp(30, 5000, status="complete")], S)
    assert mixed.state == "incomparable_samples"
    assert mixed.score == 0.0
    assert mixed.viewers["pct_per_hour"] is None
    caps = att.assess([], [], [vp(0, 1000, cap=300), vp(30, 5000, cap=500)], S)
    assert caps.viewers["status"] == "incomparable_samples"
    basis = att.comparable(vp(0, 1, basis="top_streams"), vp(1, 1))
    assert basis == (False, "different sampling basis")
    langs = att.comparable(vp(0, 1, langs=("en",)), vp(1, 1))
    assert langs[0] is False
    unknown = att.comparable(vp(0, 1, status="unknown"), vp(1, 1, status="unknown"))
    assert unknown[0] is False
    partial = att.comparable(vp(0, 1, status="partial"), vp(1, 1, status="partial"))
    assert partial == (True, "both partial with the same cap (top-N streams vs top-N streams)")


def test_partial_samples_are_labelled() -> None:
    a = att.assess([], [], [vp(0, 1000, status="partial"), vp(30, 1100, status="partial")], S)
    assert "PARTIAL_SAMPLE" in a.labels
    assert "top-N" in a.viewers["comparison"]


def test_too_close_observations_report_no_rate() -> None:
    a = att.assess([], [], [vp(0, 1000), vp(3, 2000)], S)
    assert a.viewers["status"] == "interval_too_short"
    assert a.viewers["pct_per_hour"] is None


def test_falling_and_steady() -> None:
    assert att.assess([], [], [vp(0, 1000), vp(60, 600)], S).state == "falling"
    assert att.assess([], [], [vp(0, 1000), vp(60, 1020)], S).state == "steady"


def test_rank_climb_and_absence_is_not_zero() -> None:
    climb = att.assess([rp(0, 30), rp(30, 22), rp(60, 12)], tops(0, 30, 60), [], S)
    assert climb.rank["window_positions_gained"] == 18
    assert climb.rank["positions_gained"] == 10
    assert climb.state == "rising"
    assert "RANK_CHANGE" in climb.labels
    gone = att.assess([rp(0, 30)], tops(0, 30), [], S)
    assert gone.rank["status"] == "outside_top_n_in_latest_poll"
    assert gone.rank["latest_rank"] is None
    assert any("position unknown" in n for n in gone.notes)


def test_new_in_top_needs_observed_absence() -> None:
    newly = att.assess([rp(60, 40)], tops(0, 15, 30, 45, 60), [], S)
    assert newly.rank["new_in_top"] is True
    assert newly.state == "rising"
    first_poll = att.assess([rp(0, 40)], tops(0), [], S)
    assert first_poll.rank["new_in_top"] is False  # no earlier polls: absence not observed


def test_sustained_rise_uses_the_phase4_cusum() -> None:
    rising = [vp(i * 30, int(1000 * 1.25**i)) for i in range(7)]
    a = att.assess([], [], rising, S)
    assert a.viewers["sustained"]["method"] == "cusum-v1"
    assert a.viewers["sustained"]["status"] in ("alarm", "watch")
    assert a.state in ("sustained_rise", "rising")
    flat = [vp(i * 30, 1000 + (i % 2) * 5) for i in range(7)]
    assert att.assess([], [], flat, S).viewers["sustained"]["status"] == "not_triggered"


def test_score_components_are_bounded_interpretable_and_versioned() -> None:
    a = att.assess(
        [rp(0, 30), rp(60, 10)], tops(0, 60), [vp(0, 1000), vp(30, 1500), vp(60, 1600)], S
    )
    assert a.version == "twitch-attention-v1"
    assert set(a.components) == {
        "rank_improvement", "viewer_growth", "stream_growth", "acceleration", "sustained",
        "new_in_top", "volatility_penalty",
    }  # fmt: skip
    assert 0 <= a.score <= 100
    for c in a.components.values():
        assert 0 <= c["value"] <= 1
        assert c["basis"]
    assert a.viewers["acceleration"] is not None  # three comparable observations


# ----------------------------------------------------------------------------- models / config
def test_category_kind_never_treats_non_games_as_games() -> None:
    non = ("Just Chatting",)
    assert category_kind("Just Chatting", None, non) == "non_game"
    assert category_kind("just chatting", "123", non) == "non_game"
    assert category_kind("Fortnite", "1905", non) == "game"
    assert category_kind("Something", None, non) == "unknown"


def test_stream_records_with_missing_fields_are_rejected() -> None:
    ok, problem = parse_stream({"id": "1", "user_id": "2", "user_login": "x", "viewer_count": 3,
                                "started_at": "2026-11-14T10:00:00Z", "game_id": ""})  # fmt: skip
    assert problem is None
    assert ok is not None
    assert ok.game_id is None
    assert parse_stream({"id": "1"})[1].startswith("missing:")
    negative = {"id": "1", "user_id": "2", "user_login": "x", "viewer_count": -5,
                "started_at": "2026-11-14T10:00:00Z"}  # fmt: skip
    assert parse_stream(negative)[1] == "invalid:viewer_count"


def test_retention_beyond_24h_needs_written_authorisation() -> None:
    assert TwitchSettings().retention.hours == 24
    with pytest.raises(ConfigError, match="written authorisation"):
        load_twitch_settings({"retention": {"hours": 72}})
    ok = load_twitch_settings({"retention": {"hours": 72, "extended_storage_authorized": True}})
    assert ok.retention.hours == 72
    with pytest.raises(ConfigError, match="https"):
        load_twitch_settings({"helix_base_url": "http://api.twitch.tv/helix"})


# ----------------------------------------------------------------------------- provider
def provider(fake: FakeTwitch, **kw: object) -> TwitchDiscoveryProvider:
    clock = MutableClock(NOW)
    enabled = bool(kw.pop("enabled", True))
    api = bool(kw.pop("api_enabled", True))
    return TwitchDiscoveryProvider(
        make_client(fake, clock=clock, **kw),  # type: ignore[arg-type]
        settings=TwitchSettings(),
        enabled=enabled,
        api_enabled=api,
        retention_days=1,
        clock=clock,
    )


def req(gid: str, name: str = "x") -> DiscoveryRequest:
    return DiscoveryRequest(source=game_source(gid, name, {"state": "rising", "score": 70}, 24))


async def test_promoted_game_becomes_a_game_catalog_item() -> None:
    fake = FakeTwitch()
    p = provider(fake)
    res = await p.discover(req("777001"), RequestContext())
    (item,) = res.items
    assert item.evidence_type is EvidenceType.GAME_CATALOG
    assert item.title == "Starfall Tactics"
    assert item.entity_hint == "Starfall Tactics"
    assert item.entity_hint_type == "game"
    assert item.source_reference == "twitch:game:777001"
    assert item.url is None
    assert item.text is None
    meta = item.metadata
    assert meta["signal_class"] == "LIVESTREAM_ACTIVITY"
    assert meta["entity_hint_origin"] == "discovered"
    assert meta["twitch"]["igdb_id"] == "424242"
    assert meta["twitch"]["verified_via"] == "GET /helix/games?id="
    assert meta["twitch"]["attention"]["state"] == "rising"
    assert "no cause" in meta["twitch"]["basis"]


async def test_non_games_unknown_and_missing_games_are_not_promoted() -> None:
    fake = FakeTwitch()
    p = provider(fake)
    chat = await p.discover(req("509658"), RequestContext())
    assert chat.items == ()
    assert "non_game" in chat.warnings[0]
    unlisted = await p.discover(req("888002"), RequestContext())
    assert unlisted.items == ()
    assert "unknown" in unlisted.warnings[0]
    gone = await p.discover(req("123456"), RequestContext())
    assert gone.items == ()
    assert "not found" in gone.warnings[0]


async def test_provider_status_and_signal_class() -> None:
    fake = FakeTwitch()
    assert provider(fake, api_enabled=False).status().status is ProviderStatus.DISABLED
    missing = provider(fake, client_secret=None).status()
    assert missing.status is ProviderStatus.NOT_CONFIGURED
    assert missing.missing_configuration == ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET")
    bad = provider(fake, client_secret="wrong-secret-000000000000000000")
    with pytest.raises(InvalidCredentialsError):
        await bad.discover(req("777001"), RequestContext())
    assert bad.status().status is ProviderStatus.ACCESS_RESTRICTED
    with pytest.raises(ProviderDisabledError):
        await provider(fake, enabled=False).discover(req("777001"), RequestContext())
    p = provider(fake)
    assert p.status().status is ProviderStatus.AVAILABLE
    for cap in (
        Capability.TOP_GAMES, Capability.GAME_LOOKUP, Capability.ACTIVE_STREAMS,
        Capability.GAME_STREAMS, Capability.VIEWER_OBSERVATIONS,
        Capability.GAME_RANKING_HISTORY, Capability.ATTENTION_MOMENTUM,
    ):  # fmt: skip
        assert cap in p.descriptor.capabilities
    classes = classify(p.descriptor)
    assert classes == (SignalClass.LIVESTREAM_ACTIVITY,)
    assert not set(classes) & set(SEARCH_INTEREST_CLASSES)
    src = game_source("1", "x", {}, 24)
    assert p.supports(src)
    assert src.source_type is SourceType.API_QUERY
    assert not p.supports(src.model_copy(update={"reference": "abc"}))


async def test_smoke_probe_is_one_helix_request() -> None:
    from trend_agent.ops.providers import smoke_test

    fake = FakeTwitch()
    res = await smoke_test(provider(fake))
    assert res.ok
    assert res.units_spent == 1
    assert len(fake.calls("/helix/games/top")) == 1
    off = FakeTwitch()
    res2 = await smoke_test(provider(off, client_secret=None))
    assert not res2.attempted
    assert off.requests == []
