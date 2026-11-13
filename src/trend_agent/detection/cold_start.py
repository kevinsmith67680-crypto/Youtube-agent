"""Cold-start assessment for series without a reliable baseline.

New AI models and limited-time game events often have only a handful of observations.
Forcing the historical anomaly detector onto them would produce meaningless z-scores, so
this module classifies them separately and **always reports low statistical confidence**:

* NEW_TOPIC                    no usable observation yet
* EARLY_INTEREST_OBSERVED      first non-zero measurements of a new topic
* INSUFFICIENT_BASELINE        some data, no upward pattern (or not a new topic)
* COLD_START_GROWTH_CANDIDATE  repeated upward movement in the measurements themselves
                               (>= min_growth_points values, last >= growth_ratio x first,
                               last is the maximum, minimum absolute increase) AND either
                               independent corroboration (official release / code release /
                               suggestions / creator coverage / community discussion from
                               >= min_independent_providers sources in the window) or
                               strong measured monotone growth (Kendall tau >= tau_min over
                               >= strong_growth_points values)
* BASELINE_ESTABLISHED         enough comparable observations: conventional detection

Corroboration explains *why* interest might rise; it never substitutes for measurement. A
topic is never labelled a measured search surge because it is new, discussed or has many
videos: the highest stage reachable here is the configured ``cold_start_max_stage``
(CANDIDATE with corroboration, WATCH otherwise), never CONFIRMED.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
from scipy import stats

from trend_agent.detection.config import ColdStartConfig
from trend_agent.discovery.models import NoveltyKind
from trend_agent.schemas.detection import ColdStartState
from trend_agent.schemas.evidence import AlertStage

VERSION = "cold_start-v1"


@dataclass(frozen=True)
class CorroborationContext:
    """Discovery context of the keyword (Phase 2), already restricted to the time window."""

    novelty_kind: NoveltyKind | None = None
    entity_type: str | None = None
    entity_first_seen_at: datetime | None = None
    evidence_types: dict[str, int] = field(default_factory=dict)  # evidence type -> items
    providers: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "novelty_kind": self.novelty_kind.value if self.novelty_kind else None,
            "entity_type": self.entity_type,
            "entity_first_seen_at": self.entity_first_seen_at.isoformat()
            if self.entity_first_seen_at
            else None,
            "evidence_types": dict(sorted(self.evidence_types.items())),
            "providers": list(self.providers),
        }


@dataclass(frozen=True)
class ColdStartAssessment:
    state: ColdStartState
    stage: AlertStage | None  # highest stage this evidence supports (None: no alert)
    usable_values: int
    first_value: float | None
    last_value: float | None
    growth_ratio: float | None
    kendall_tau: float | None
    corroborated: bool
    corroboration: dict[str, Any]
    reasons: tuple[str, ...]
    version: str = VERSION

    def as_dict(self) -> dict[str, Any]:
        def r(v: float | None) -> float | None:
            return None if v is None else round(v, 4)

        return {
            "state": self.state.value,
            "stage": self.stage.value if self.stage else None,
            "usable_values": self.usable_values,
            "first_value": r(self.first_value),
            "last_value": r(self.last_value),
            "growth_ratio": r(self.growth_ratio),
            "kendall_tau": r(self.kendall_tau),
            "corroborated": self.corroborated,
            "corroboration": self.corroboration,
            "reasons": list(self.reasons),
            "version": self.version,
        }


def assess(
    values: list[float],
    *,
    min_baseline: int,
    min_effect: float,
    ctx: CorroborationContext | None,
    cfg: ColdStartConfig,
    max_stage: AlertStage,
) -> ColdStartAssessment:
    """Classify the usable values seen so far (oldest first, missing ones omitted)."""
    ctx = ctx or CorroborationContext()
    n = len(values)
    new_topic = ctx.novelty_kind in cfg.new_topic_novelty
    wanted = {e.value for e in cfg.corroborating_evidence}
    kinds = {k: v for k, v in ctx.evidence_types.items() if k in wanted and v > 0}
    corroborated = bool(kinds) and len(set(ctx.providers)) >= cfg.min_independent_providers
    corroboration = {**ctx.as_dict(), "corroborating_types": sorted(kinds)}
    reasons: list[str] = []
    if corroborated:
        reasons.append(
            f"corroborated by {', '.join(sorted(kinds))} from {len(set(ctx.providers))} "
            "independent source(s)"
        )
    if n >= min_baseline:
        return ColdStartAssessment(
            ColdStartState.BASELINE_ESTABLISHED, None, n, values[0], values[-1], None, None,
            corroborated, corroboration, (f"{n} comparable values >= {min_baseline}",),
        )  # fmt: skip
    if n == 0:
        return ColdStartAssessment(
            ColdStartState.NEW_TOPIC, None, 0, None, None, None, None, corroborated,
            corroboration, ("no usable observation yet", *reasons),
        )  # fmt: skip
    first, last = values[0], values[-1]
    ratio = last / first if first > 0 else None
    tau: float | None = None
    if n >= 3 and np.ptp(values) > 0:
        tau = float(stats.kendalltau(np.arange(n), np.asarray(values)).statistic)
    growing = (
        n >= cfg.min_growth_points
        and last >= max(values)
        and last - first >= min_effect
        and (ratio is None or ratio >= cfg.growth_ratio)
    )
    strong = growing and n >= cfg.strong_growth_points and (tau or 0.0) >= cfg.kendall_tau_min
    if growing and (corroborated or strong):
        stage = max_stage if corroborated else AlertStage.WATCH
        reasons.insert(
            0,
            f"{n} values rising from {first:g} to {last:g}"
            + (f" (x{ratio:.1f})" if ratio is not None else "")
            + (f", Kendall tau {tau:.2f}" if tau is not None else ""),
        )
        return ColdStartAssessment(
            ColdStartState.COLD_START_GROWTH_CANDIDATE, stage, n, first, last, ratio, tau,
            corroborated, corroboration, tuple(reasons),
        )  # fmt: skip
    if last > 0 and (new_topic or growing):
        early: AlertStage | None = (
            AlertStage.WATCH if (new_topic and corroborated and last > first) else None
        )
        return ColdStartAssessment(
            ColdStartState.EARLY_INTEREST_OBSERVED, early, n, first, last, ratio, tau,
            corroborated, corroboration,
            (f"{n} early value(s); latest {last:g}", *reasons),
        )  # fmt: skip
    return ColdStartAssessment(
        ColdStartState.INSUFFICIENT_BASELINE, None, n, first, last, ratio, tau, corroborated,
        corroboration, (f"{n} value(s) without an upward pattern", *reasons),
    )  # fmt: skip
