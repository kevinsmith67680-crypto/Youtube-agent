"""Operating modes: what the system may claim and do, derived from evidence.

The mode is **computed**, never configured: from provider readiness (which signal classes
a real, usable provider supplies), real-world validation status, and current degradations
(database, budgets, scheduler, workers). An operator cannot switch the system into a more
capable mode; only the underlying evidence can.

=============================  ==========================================================
FULL_SEARCH_INTELLIGENCE        a live-verified DIRECT_YOUTUBE_SEARCH_INTEREST provider AND
                                detection validated against real labelled surges.
LIMITED_SEARCH_INTELLIGENCE     search-interest data exists (external search interest, or
                                direct data not yet validated); no confirmed-surge claims.
PROXY_OPPORTUNITY_RESEARCH      only proxy signals (video, community, official events,
                                suggestion presence); opportunities are hypotheses.
DEMONSTRATION                   synthetic data only (demo mode, or no real provider).
DEGRADED                        one of the above with capabilities withdrawn because of
                                a failure or an exhausted budget (``base_mode`` keeps it).
=============================  ==========================================================
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from trend_agent.ops.providers import ProviderReadiness, available_classes
from trend_agent.ops.signals import PROXY_CLASSES, SignalClass


class OperatingMode(StrEnum):
    FULL_SEARCH_INTELLIGENCE = "FULL_SEARCH_INTELLIGENCE"
    LIMITED_SEARCH_INTELLIGENCE = "LIMITED_SEARCH_INTELLIGENCE"
    PROXY_OPPORTUNITY_RESEARCH = "PROXY_OPPORTUNITY_RESEARCH"
    DEMONSTRATION = "DEMONSTRATION"
    DEGRADED = "DEGRADED"


MODE_RANK = {
    OperatingMode.DEMONSTRATION: 0,
    OperatingMode.PROXY_OPPORTUNITY_RESEARCH: 1,
    OperatingMode.LIMITED_SEARCH_INTELLIGENCE: 2,
    OperatingMode.FULL_SEARCH_INTELLIGENCE: 3,
}


class SystemCapability(StrEnum):
    COLLECT_REAL_DATA = "collect_real_data"
    DIRECT_SEARCH_SURGE_CLAIMS = "direct_search_surge_claims"
    DIRECT_SEARCH_FORECASTS = "direct_search_forecasts"
    EXTERNAL_SEARCH_SIGNALS = "external_search_signals"
    PROXY_SIGNAL_DETECTION = "proxy_signal_detection"
    PROXY_FORECASTS = "proxy_forecasts"
    CONTENT_GAP_ANALYSIS = "content_gap_analysis"
    RECOMMENDATIONS = "recommendations"
    LLM_GENERATION = "llm_generation"
    CREATOR_ALERTS = "creator_alerts"
    SYNTHETIC_DEMO = "synthetic_demo"


C = SystemCapability
_REAL_COMMON = frozenset(
    {
        C.COLLECT_REAL_DATA,
        C.PROXY_SIGNAL_DETECTION,
        C.PROXY_FORECASTS,
        C.CONTENT_GAP_ANALYSIS,
        C.RECOMMENDATIONS,
        C.LLM_GENERATION,
        C.CREATOR_ALERTS,
        C.SYNTHETIC_DEMO,
    }
)
MODE_CAPABILITIES: dict[OperatingMode, frozenset[SystemCapability]] = {
    OperatingMode.FULL_SEARCH_INTELLIGENCE: _REAL_COMMON
    | {C.DIRECT_SEARCH_SURGE_CLAIMS, C.DIRECT_SEARCH_FORECASTS, C.EXTERNAL_SEARCH_SIGNALS},
    OperatingMode.LIMITED_SEARCH_INTELLIGENCE: _REAL_COMMON | {C.EXTERNAL_SEARCH_SIGNALS},
    OperatingMode.PROXY_OPPORTUNITY_RESEARCH: _REAL_COMMON,
    OperatingMode.DEMONSTRATION: frozenset(
        {C.SYNTHETIC_DEMO, C.RECOMMENDATIONS, C.CONTENT_GAP_ANALYSIS}
    ),
}

# Capabilities that also need a particular signal class to be available.
_NEEDS_CLASS: dict[SystemCapability, frozenset[SignalClass]] = {
    C.CONTENT_GAP_ANALYSIS: frozenset({SignalClass.VIDEO_ACTIVITY}),
    C.EXTERNAL_SEARCH_SIGNALS: frozenset({SignalClass.EXTERNAL_SEARCH_INTEREST}),
    C.PROXY_SIGNAL_DETECTION: PROXY_CLASSES,
    C.PROXY_FORECASTS: PROXY_CLASSES,
}


class Degradation(StrEnum):
    DATABASE_UNAVAILABLE = "database_unavailable"
    SCHEMA_OUTDATED = "schema_outdated"
    LLM_BUDGET_EXHAUSTED = "llm_budget_exhausted"
    API_BUDGET_EXHAUSTED = "api_budget_exhausted"
    SCHEDULER_NOT_RUNNING = "scheduler_not_running"
    NO_LIVE_WORKERS = "no_live_workers"
    PROVIDER_FAILING = "provider_failing"
    DATA_STALE = "data_stale"


# What each degradation withdraws (None: everything except the synthetic demo).
_WITHDRAWS: dict[Degradation, frozenset[SystemCapability] | None] = {
    Degradation.DATABASE_UNAVAILABLE: None,
    Degradation.SCHEMA_OUTDATED: None,
    Degradation.LLM_BUDGET_EXHAUSTED: frozenset({C.LLM_GENERATION}),
    Degradation.API_BUDGET_EXHAUSTED: frozenset({C.COLLECT_REAL_DATA, C.CONTENT_GAP_ANALYSIS}),
    Degradation.SCHEDULER_NOT_RUNNING: frozenset({C.CREATOR_ALERTS}),
    Degradation.NO_LIVE_WORKERS: frozenset(),
    Degradation.PROVIDER_FAILING: frozenset(),
    Degradation.DATA_STALE: frozenset({C.CREATOR_ALERTS}),
}


@dataclass(frozen=True)
class DegradationFact:
    kind: Degradation
    detail: str


@dataclass(frozen=True)
class CapabilityState:
    allowed: bool
    reason: str


class CapabilityBlockedError(RuntimeError):
    def __init__(self, capability: SystemCapability, mode: OperatingMode, reason: str) -> None:
        super().__init__(f"{capability.value} is not available in mode {mode.value}: {reason}")
        self.capability = capability
        self.mode = mode
        self.reason = reason


@dataclass(frozen=True)
class ValidationStatus:
    """Real-world validation evidence (from the quality scorecard). Unknown = False."""

    direct_detection_validated: bool = False
    detail: str = "no labelled real surges: real-world detection accuracy is UNKNOWN"


@dataclass
class ModeDecision:
    mode: OperatingMode
    base_mode: OperatingMode
    decided_at: datetime
    reasons: list[str]
    degradations: list[DegradationFact]
    capabilities: dict[SystemCapability, CapabilityState]
    classes_usable: list[SignalClass]
    classes_verified: list[SignalClass]
    next_mode_requirements: list[str] = field(default_factory=list)
    demo: bool = False

    def allows(self, capability: SystemCapability) -> bool:
        state = self.capabilities.get(capability)
        return bool(state and state.allowed)

    def require(self, capability: SystemCapability) -> None:
        state = self.capabilities.get(capability)
        if state is None or not state.allowed:
            raise CapabilityBlockedError(
                capability, self.mode, state.reason if state else "unknown capability"
            )

    @property
    def direct_search_claims_enabled(self) -> bool:
        return self.allows(C.DIRECT_SEARCH_SURGE_CLAIMS)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "base_mode": self.base_mode.value,
            "decided_at": self.decided_at.isoformat(),
            "demo": self.demo,
            "direct_search_claims_enabled": self.direct_search_claims_enabled,
            "reasons": self.reasons,
            "degradations": [{"kind": d.kind.value, "detail": d.detail} for d in self.degradations],
            "capabilities": {
                c.value: {"allowed": s.allowed, "reason": s.reason}
                for c, s in sorted(self.capabilities.items(), key=lambda kv: kv[0].value)
            },
            "signal_classes_usable": [c.value for c in self.classes_usable],
            "signal_classes_verified": [c.value for c in self.classes_verified],
            "next_mode_requirements": self.next_mode_requirements,
        }


def _base_mode(
    readiness: Sequence[ProviderReadiness],
    validation: ValidationStatus,
    demo: bool,
) -> tuple[OperatingMode, list[str], list[str]]:
    reasons: list[str] = []
    needs: list[str] = []
    if demo:
        reasons.append("demo mode is enabled: only the isolated synthetic dataset is served")
        needs.append("disable demo mode and configure a real provider")
        return OperatingMode.DEMONSTRATION, reasons, needs
    usable = available_classes(readiness)
    verified = available_classes(readiness, verified_only=True)
    direct = SignalClass.DIRECT_YOUTUBE_SEARCH_INTEREST
    if direct in verified and validation.direct_detection_validated:
        reasons.append("a live-verified direct YouTube Search-interest provider is available")
        reasons.append("surge detection is validated against real labelled surges")
        return OperatingMode.FULL_SEARCH_INTELLIGENCE, reasons, needs
    full_needs = []
    if direct not in usable:
        full_needs.append(
            "a legitimate provider of DIRECT_YOUTUBE_SEARCH_INTEREST data (none exists in this "
            "deployment; the Google Trends alpha with a verified YouTube-property transport "
            "would qualify)"
        )
    elif direct not in verified:
        full_needs.append("a successful live smoke test of the direct search-interest provider")
    if not validation.direct_detection_validated:
        full_needs.append(f"real-world detection validation ({validation.detail})")
    if direct in usable or SignalClass.EXTERNAL_SEARCH_INTEREST in usable:
        if direct in usable:
            reasons.append(
                "direct YouTube Search-interest data is available but not validated: surges "
                "are reported as candidates, never as confirmed"
            )
        if SignalClass.EXTERNAL_SEARCH_INTEREST in usable:
            reasons.append("external (non-YouTube) search-interest data is available")
        return OperatingMode.LIMITED_SEARCH_INTELLIGENCE, reasons, full_needs
    proxy = usable & PROXY_CLASSES
    if proxy:
        reasons.append(
            "no search-interest provider is usable; opportunities come from proxy signals ("
            + ", ".join(sorted(c.value for c in proxy))
            + ") and are research hypotheses, not measured search demand"
        )
        needs = [
            "an EXTERNAL_SEARCH_INTEREST provider (Google Trends API alpha access) for "
            "LIMITED_SEARCH_INTELLIGENCE",
            *full_needs,
        ]
        return OperatingMode.PROXY_OPPORTUNITY_RESEARCH, reasons, needs
    reasons.append(
        "no real data provider is usable (not configured, restricted or failing): only "
        "synthetic data can be shown"
    )
    needs = [
        "configure at least one real provider, e.g. TREND_AGENT_YOUTUBE_API_KEY for "
        "VIDEO_ACTIVITY (PROXY_OPPORTUNITY_RESEARCH)",
        *full_needs,
    ]
    return OperatingMode.DEMONSTRATION, reasons, needs


def decide_mode(
    readiness: Sequence[ProviderReadiness],
    *,
    now: datetime,
    validation: ValidationStatus | None = None,
    degradations: Iterable[DegradationFact] = (),
    demo: bool = False,
) -> ModeDecision:
    validation = validation or ValidationStatus()
    base, reasons, needs = _base_mode(readiness, validation, demo)
    usable = available_classes(readiness) if not demo else set()
    verified = available_classes(readiness, verified_only=True) if not demo else set()
    degr = list(degradations)
    allowed = set(MODE_CAPABILITIES[base])
    caps: dict[SystemCapability, CapabilityState] = {}
    for cap in SystemCapability:
        if cap not in allowed:
            caps[cap] = CapabilityState(False, f"not part of {base.value}")
            continue
        need = _NEEDS_CLASS.get(cap)
        if base is not OperatingMode.DEMONSTRATION and need is not None and not (usable & need):
            caps[cap] = CapabilityState(
                False, "needs a usable provider of " + " or ".join(sorted(c.value for c in need))
            )
            continue
        caps[cap] = CapabilityState(True, f"allowed in {base.value}")
    for fact in degr:
        withdrawn = _WITHDRAWS[fact.kind]
        targets = (
            [c for c in SystemCapability if c is not C.SYNTHETIC_DEMO]
            if withdrawn is None
            else list(withdrawn)
        )
        for cap in targets:
            if caps[cap].allowed:
                caps[cap] = CapabilityState(False, f"withdrawn: {fact.kind.value} ({fact.detail})")
    mode = OperatingMode.DEGRADED if degr else base
    if degr:
        reasons = [*reasons, *(f"degraded: {d.kind.value} - {d.detail}" for d in degr)]
    order = list(SignalClass)
    return ModeDecision(
        mode=mode,
        base_mode=base,
        decided_at=now,
        reasons=reasons,
        degradations=degr,
        capabilities=caps,
        classes_usable=sorted(usable, key=order.index),
        classes_verified=sorted(verified, key=order.index),
        next_mode_requirements=needs,
        demo=demo,
    )


def transition(previous: dict[str, Any] | None, decision: ModeDecision) -> dict[str, Any] | None:
    """Describe a mode change (None when unchanged) for the history and an ops alert."""
    if (
        previous is not None
        and previous.get("mode") == decision.mode.value
        and previous.get("base_mode") == decision.base_mode.value
    ):
        return None
    before = previous.get("base_mode") if previous else None
    rank_before = MODE_RANK.get(OperatingMode(before), -1) if before else -1
    downgrade = rank_before > MODE_RANK[decision.base_mode] or (
        decision.mode is OperatingMode.DEGRADED
        and (previous is None or previous.get("mode") != OperatingMode.DEGRADED.value)
    )
    lost_claims = bool(previous and previous.get("direct_search_claims_enabled")) and not (
        decision.direct_search_claims_enabled
    )
    return {
        "from": previous.get("mode") if previous else None,
        "from_base": before,
        "to": decision.mode.value,
        "to_base": decision.base_mode.value,
        "downgrade": downgrade,
        "direct_search_claims_withdrawn": lost_claims,
        "at": decision.decided_at.isoformat(),
        "reasons": decision.reasons,
    }
