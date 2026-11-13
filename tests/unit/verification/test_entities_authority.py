"""Entity verification and deterministic statistical authority."""

from __future__ import annotations

from datetime import timedelta

from tests.unit.verification.helpers import LOADED, base_draft, claim, run, verdict, world
from trend_agent.schemas.evidence import AlertStage, SurgeClassification
from trend_agent.schemas.verification import (
    AbstentionState,
    AuthoritativeStatistics,
    ClaimStatus,
    ViolationCode,
)
from trend_agent.verification.authority import check_trend, statement
from trend_agent.verification.claims import extract
from trend_agent.verification.entities import (
    EntityState,
    EntityVerifier,
    RecordView,
    denied_names,
)


def _verifier(target: str) -> tuple[EntityVerifier, object]:
    w = world()
    ctx = w.context(target)
    views = {r.evidence_id: RecordView.build(r, ctx.catalog) for r in ctx.records}
    return (
        EntityVerifier(ctx.catalog, views, now=w.now, topic_entity_keys=ctx.topic_entity_keys),
        ctx,
    )


def test_entity_states() -> None:
    v, _ = _verifier("nimbus")
    assert v.check_name("Nimbus 4").state is EntityState.CONFIRMED  # official source
    assert v.check_name("Nimbus 5").state is EntityState.REPORTED  # two lower-trust sources
    assert v.check_name("Nimbus 9 Ultra").state is EntityState.UNKNOWN  # invented
    h, _ = _verifier("helix")
    assert h.check_name("Helix Coder").state is EntityState.REPORTED
    o, _ = _verifier("orion")
    orion = o.check_name("Orion Assistant")
    assert orion.state is EntityState.REJECTED
    assert orion.denied_by


def test_ambiguity_is_resolved_only_by_context() -> None:
    v, _ctx = _verifier("atlas")
    assert v.check_name("Atlas").state is EntityState.AMBIGUOUS
    ai = v.check_name("Atlas", "Atlas, the open-weight model from Meridian AI")
    assert ai.entity is not None
    assert ai.entity.key == "ai_model:atlas-meridian"
    game = v.check_name("Atlas", "Atlas, the survival game")
    assert game.entity is not None
    assert game.entity.key == "game:atlas-survival"


def test_release_dates_conflicts_and_upcoming() -> None:
    q, _ = _verifier("quill")
    chk = q.check_name("Quill 2")
    assert chk.conflicts
    assert len({d for d, _ in chk.release_dates}) == 2
    v, _ = _verifier("voltx9")
    assert v.check_name("Volt X9").release == "upcoming"


def test_denied_names_follow_the_denial_cue() -> None:
    assert denied_names("There is no Phoenix Blade in Skyforge Arena.") == ["Phoenix Blade"]
    assert denied_names("Aurora Labs has no plans to release an Orion Assistant.") == [
        "Orion Assistant"
    ]
    assert denied_names("Videos promising a free Phoenix Blade are fake.") == ["Phoenix Blade"]
    assert denied_names("The leaked screenshots are fake.") == []


def test_unknown_products_and_items_are_not_verified() -> None:
    w = world()
    d, _ = run(
        w,
        "nimbus",
        base_draft(
            factual_claims=[
                claim("Aurora Labs also launched Nimbus 4 Ultra.", "{{ev:off:nimbus4-launch}}")
            ]
        ),
    )
    assert ViolationCode.UNKNOWN_ENTITY in verdict(d, "Nimbus 4 Ultra").violations
    d, _ = run(
        w,
        "skyforge",
        {
            "proposed_video_title": "Skyforge Arena Season 7 Guide",
            "topic_id": 31,
            "search_query_id": 301,
            "audience_search_intent": "game_guide",
            "factual_claims": [
                claim(
                    "The Phoenix Blade drops from snow map chests.", "{{ev:yt:skyforge-fake-item}}"
                ),
                claim("The Aurora Glider costs 1,200 coins.", "{{ev:off:skyforge-s7}}"),
            ],
        },
    )
    assert verdict(d, "Phoenix Blade").status is ClaimStatus.CONTRADICTED
    assert verdict(d, "1,200 coins").status is ClaimStatus.VERIFIED
    assert d.recommendation.verification_status is AbstentionState.PARTIALLY_VERIFIED


def test_missing_product_details_are_not_invented() -> None:
    w = world()
    d, _ = run(
        w,
        "voltx9",
        {
            "proposed_video_title": "Volt X9 Release Date",
            "topic_id": 21,
            "search_query_id": 201,
            "audience_search_intent": "news",
            "factual_claims": [
                claim("Volt X9 draws 350 W of power.", "{{ev:off:voltx9}}"),
                claim("Volt X9 has 32GB of memory.", "{{ev:yt:voltx9-leak}}"),
                claim("Volt X9 will be released on December 2, 2026.", "{{ev:off:voltx9}}"),
            ],
        },
    )
    assert verdict(d, "350 W").status is ClaimStatus.UNVERIFIED
    assert verdict(d, "32GB").status is ClaimStatus.CONTRADICTED  # official: 24GB
    assert verdict(d, "December 2").status is ClaimStatus.VERIFIED


def _stats(**kw: object) -> AuthoritativeStatistics:
    base: dict[str, object] = {
        "target_id": 1,
        "available": True,
        "classification": SurgeClassification.DIRECT_SEARCH_SURGE,
        "highest_stage": AlertStage.CONFIRMED,
        "measures_youtube_search": True,
        "measures_search": True,
        "severity": 74.0,
        "relative_change": 1.4,
        "last_updated_at": world().now,
    }
    base.update(kw)
    return AuthoritativeStatistics.model_validate(base)


def _check(text: str, **kw: object) -> tuple[ClaimStatus, tuple[ViolationCode, ...]]:
    f = extract(text)
    assert f.trend is not None, text
    result = check_trend(f, _stats(**kw), now=world().now, config=LOADED.config)
    return result.status, result.violations


def test_statistics_come_only_from_the_engine() -> None:
    assert _check("YouTube search interest is up 140%") == (ClaimStatus.VERIFIED, ())
    assert _check("YouTube searches jumped 300%")[0] is ClaimStatus.CONTRADICTED
    status, codes = _check("It gets 50,000 searches a month")
    assert status is ClaimStatus.UNVERIFIED
    assert ViolationCode.SEARCH_VOLUME in codes
    assert _check("The surge has a severity of 95")[0] is ClaimStatus.CONTRADICTED
    _, codes = _check("Searches are statistically significant and rising")
    assert ViolationCode.STATISTIC_OVERRIDE in codes
    assert _check("Searches are falling")[0] is ClaimStatus.CONTRADICTED


def test_proxy_external_watch_stale_and_missing_statistics() -> None:
    proxy = {
        "classification": SurgeClassification.PROXY_ATTENTION_SIGNAL,
        "measures_youtube_search": False,
        "measures_search": False,
        "relative_change": None,
    }
    _, codes = _check("YouTube searches are exploding", **proxy)
    assert ViolationCode.PROXY_AS_SEARCH in codes
    _, codes = _check("Demand for it doubled", **proxy)
    assert ViolationCode.PROXY_AS_SEARCH in codes
    external = {
        "classification": SurgeClassification.EXTERNAL_SEARCH_SURGE,
        "measures_youtube_search": False,
    }
    _, codes = _check("YouTube search interest is surging", **external)
    assert ViolationCode.EXTERNAL_AS_YOUTUBE in codes
    assert _check("Search interest on Google is rising", **external)[0] is ClaimStatus.VERIFIED
    _, codes = _check("Searches are surging", highest_stage=AlertStage.WATCH)
    assert ViolationCode.UNSUPPORTED_TREND in codes
    _, codes = _check("Searches are surging", last_updated_at=world().now - timedelta(days=30))
    assert ViolationCode.STALE_EVIDENCE in codes
    _, codes = _check("Searches are surging", available=False, highest_stage=None)
    assert ViolationCode.UNSUPPORTED_TREND in codes


def test_engine_statement_never_converts_an_index_into_searches() -> None:
    text = statement(_stats(), "nimbus 4 tutorial")
    assert "relative index, not a number of searches" in text
    proxy = statement(
        _stats(
            classification=SurgeClassification.PROXY_ATTENTION_SIGNAL,
            relative_change=None,
            measures_youtube_search=False,
        ),
        "q",
    )
    assert "not measured YouTube search growth" in proxy
    empty = statement(AuthoritativeStatistics(target_id=1, available=False), "q")
    assert "no claim about rising search demand" in empty
