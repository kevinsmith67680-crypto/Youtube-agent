"""Operating modes are derived from evidence, never configured."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trend_agent.ops.modes import (
    CapabilityBlockedError,
    Degradation,
    DegradationFact,
    OperatingMode,
    SystemCapability,
    ValidationStatus,
    decide_mode,
    transition,
)
from trend_agent.ops.providers import ProviderReadiness, ReadinessLevel
from trend_agent.ops.signals import SignalClass
from trend_agent.schemas.enums import ProviderStatus

NOW = datetime(2026, 11, 14, 12, tzinfo=UTC)
C = SystemCapability
S = SignalClass


def prov(
    name: str, cls: SignalClass, level: ReadinessLevel, synthetic: bool = False
) -> ProviderReadiness:
    return ProviderReadiness(
        name=name,
        display_name=name,
        signal_classes=(cls,),
        synthetic=synthetic,
        enabled=True,
        status=ProviderStatus.AVAILABLE,
        level=level,
        detail="",
        missing_configuration=(),
        credentials="set",
        last_check=None,
        approvals_required=(),
    )


READY = ReadinessLevel.READY
UNVERIFIED = ReadinessLevel.CONFIGURED_UNVERIFIED
VALIDATED = ValidationStatus(True, "validated on 40 real surges")


def test_demo_mode_is_demonstration_with_no_real_capabilities() -> None:
    d = decide_mode([prov("yt", S.VIDEO_ACTIVITY, READY)], now=NOW, demo=True)
    assert d.mode is OperatingMode.DEMONSTRATION
    assert not d.allows(C.COLLECT_REAL_DATA)
    assert not d.direct_search_claims_enabled
    assert d.allows(C.SYNTHETIC_DEMO)


def test_only_synthetic_providers_means_demonstration() -> None:
    d = decide_mode(
        [prov("mock", S.SYNTHETIC_TEST_DATA, ReadinessLevel.SYNTHETIC_ONLY, True)], now=NOW
    )
    assert d.mode is OperatingMode.DEMONSTRATION
    assert any("no real data provider" in r for r in d.reasons)
    assert not d.allows(C.COLLECT_REAL_DATA)


def test_proxy_signals_give_proxy_research_without_search_claims() -> None:
    d = decide_mode([prov("feeds", S.OFFICIAL_EVENT_SIGNAL, READY)], now=NOW)
    assert d.mode is OperatingMode.PROXY_OPPORTUNITY_RESEARCH
    assert d.allows(C.PROXY_SIGNAL_DETECTION)
    assert not d.allows(C.DIRECT_SEARCH_SURGE_CLAIMS)
    assert not d.allows(C.EXTERNAL_SEARCH_SIGNALS)
    # content-gap analysis needs YouTube search results (VIDEO_ACTIVITY)
    assert not d.allows(C.CONTENT_GAP_ANALYSIS)
    assert "VIDEO_ACTIVITY" in d.capabilities[C.CONTENT_GAP_ANALYSIS].reason


def test_external_search_interest_gives_limited_mode() -> None:
    d = decide_mode(
        [
            prov("trends", S.EXTERNAL_SEARCH_INTEREST, UNVERIFIED),
            prov("yt", S.VIDEO_ACTIVITY, READY),
        ],
        now=NOW,
    )
    assert d.mode is OperatingMode.LIMITED_SEARCH_INTELLIGENCE
    assert d.allows(C.EXTERNAL_SEARCH_SIGNALS)
    assert d.allows(C.CONTENT_GAP_ANALYSIS)
    assert not d.direct_search_claims_enabled


def test_direct_provider_without_validation_stays_limited() -> None:
    d = decide_mode([prov("direct", S.DIRECT_YOUTUBE_SEARCH_INTEREST, READY)], now=NOW)
    assert d.mode is OperatingMode.LIMITED_SEARCH_INTELLIGENCE
    assert not d.direct_search_claims_enabled
    assert any("validation" in r for r in d.next_mode_requirements)


def test_unverified_direct_provider_cannot_reach_full_even_if_validated() -> None:
    d = decide_mode(
        [prov("direct", S.DIRECT_YOUTUBE_SEARCH_INTEREST, UNVERIFIED)],
        now=NOW,
        validation=VALIDATED,
    )
    assert d.mode is OperatingMode.LIMITED_SEARCH_INTELLIGENCE
    assert any("smoke test" in r for r in d.next_mode_requirements)


def test_full_mode_needs_verified_direct_provider_and_validation() -> None:
    d = decide_mode(
        [prov("direct", S.DIRECT_YOUTUBE_SEARCH_INTEREST, READY)], now=NOW, validation=VALIDATED
    )
    assert d.mode is OperatingMode.FULL_SEARCH_INTELLIGENCE
    assert d.direct_search_claims_enabled
    assert d.allows(C.DIRECT_SEARCH_FORECASTS)


def test_synthetic_direct_data_never_unlocks_search_modes() -> None:
    mock = prov("mock", S.DIRECT_YOUTUBE_SEARCH_INTEREST, READY, synthetic=True)
    d = decide_mode([mock], now=NOW, validation=VALIDATED)
    assert d.mode is OperatingMode.DEMONSTRATION


def test_degradations_withdraw_capabilities_and_keep_base_mode() -> None:
    d = decide_mode(
        [prov("yt", S.VIDEO_ACTIVITY, READY)],
        now=NOW,
        degradations=[
            DegradationFact(Degradation.LLM_BUDGET_EXHAUSTED, "5.0/5.0 USD"),
            DegradationFact(Degradation.API_BUDGET_EXHAUSTED, "9000/9000"),
        ],
    )
    assert d.mode is OperatingMode.DEGRADED
    assert d.base_mode is OperatingMode.PROXY_OPPORTUNITY_RESEARCH
    assert not d.allows(C.LLM_GENERATION)
    assert not d.allows(C.COLLECT_REAL_DATA)
    assert d.allows(C.RECOMMENDATIONS)  # template generation still works
    assert "llm_budget_exhausted" in d.capabilities[C.LLM_GENERATION].reason


def test_database_failure_withdraws_everything_but_the_demo() -> None:
    d = decide_mode(
        [prov("yt", S.VIDEO_ACTIVITY, READY)],
        now=NOW,
        degradations=[DegradationFact(Degradation.DATABASE_UNAVAILABLE, "down")],
    )
    allowed = {c for c in SystemCapability if d.allows(c)}
    assert allowed == {C.SYNTHETIC_DEMO}


def test_require_raises_with_reason() -> None:
    d = decide_mode([prov("feeds", S.OFFICIAL_EVENT_SIGNAL, READY)], now=NOW)
    with pytest.raises(CapabilityBlockedError, match="direct_search_surge_claims"):
        d.require(C.DIRECT_SEARCH_SURGE_CLAIMS)
    d.require(C.RECOMMENDATIONS)


def test_transition_reports_withdrawn_direct_claims_and_downgrades() -> None:
    full = decide_mode(
        [prov("direct", S.DIRECT_YOUTUBE_SEARCH_INTEREST, READY)], now=NOW, validation=VALIDATED
    )
    first = transition(None, full)
    assert first is not None
    assert first["to"] == "FULL_SEARCH_INTELLIGENCE"
    assert transition(full.as_dict(), full) is None
    proxy = decide_mode([prov("feeds", S.OFFICIAL_EVENT_SIGNAL, READY)], now=NOW)
    change = transition(full.as_dict(), proxy)
    assert change is not None
    assert change["downgrade"] is True
    assert change["direct_search_claims_withdrawn"] is True


def test_decision_serialises_every_capability() -> None:
    d = decide_mode([], now=NOW)
    data = d.as_dict()
    assert set(data["capabilities"]) == {c.value for c in SystemCapability}
    assert data["direct_search_claims_enabled"] is False
