"""Transparent surge severity (0-100): an operational ranking index, not a probability.

    statistical = 0.75 * max(deviation, persistence, change_point)
                + 0.25 * mean(deviation, persistence, change_point)
    core        = w_stat * statistical + w_mag * magnitude + w_rel * relative
    severity    = 100 * quality_multiplier * core

* deviation    = clip(z / z_cap)                  (robust z vs the baseline)
* persistence  = max(elevated periods / ref, CUSUM statistic / h)
* change_point = posterior probability of a recent *upward* change (else 0)
* magnitude    = clip((observed - expected) / magnitude_ref)   (scale-family reference)
* relative     = clip(log(observed / expected) / log(relative_cap)), only when the
                 expected value is large enough; otherwise its weight moves to magnitude
* multiplier   = sample quality x baseline readiness x freshness x (unstable dispersion)

The three statistical components read the same data, so they are combined as
"strongest plus a small agreement bonus", never summed as independent evidence. Every
component, its raw value, weight and contribution is stored. A severity of 90 does not
mean a 90 % chance that the surge is real; demand severity is also kept separate from
content-opportunity ranking (a later phase).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from trend_agent.detection.config import ScaleFamily, SeverityConfig
from trend_agent.detection.engine import PointEvaluation
from trend_agent.schemas.detection import BaselineReadiness, ResultStatus


@dataclass(frozen=True)
class SeverityComponent:
    name: str
    raw: float | None
    normalized: float
    weight: float
    contribution: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "raw": None if self.raw is None else round(self.raw, 4),
            "normalized": round(self.normalized, 4),
            "weight": round(self.weight, 4),
            "contribution": round(self.contribution, 4),
        }


@dataclass(frozen=True)
class Severity:
    score: float
    components: tuple[SeverityComponent, ...]
    multiplier: float
    multiplier_parts: dict[str, float]
    version: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 2),
            "components": [c.as_dict() for c in self.components],
            "multiplier": round(self.multiplier, 4),
            "multiplier_parts": self.multiplier_parts,
            "version": self.version,
            "note": "operational ranking index, not a probability",
        }


def _clip(x: float) -> float:
    return 0.0 if not math.isfinite(x) else max(0.0, min(1.0, x))


def score(
    ev: PointEvaluation,
    family: ScaleFamily,
    cfg: SeverityConfig,
    *,
    cusum_h: float,
    min_expected_for_relative: float,
    freshness: str | None = None,
    reference: tuple[float, float] | None = None,
) -> Severity:
    """Severity of evaluation ``ev``; ``reference`` = frozen (expected, dispersion) of an
    open episode, so an ongoing surge is still measured against its pre-episode baseline."""
    refs = cfg.references
    value = ev.value
    expected = ev.baseline.expected
    sigma = ev.baseline.dispersion
    z = ev.anomaly.z
    if reference is not None and value is not None:
        expected, sigma = reference
        z = (value - expected) / max(sigma, 1e-9)
    if ev.cold_start is not None and ev.baseline.readiness is BaselineReadiness.INSUFFICIENT:
        # No baseline: magnitude/relative growth across the observed values only.
        expected = ev.cold_start.first_value
        z = None
    dev = _clip((z or 0.0) / refs.z_cap)
    persistence = max(
        ev.persistence / refs.persistence_ref,
        (ev.cusum.statistic or 0.0) / cusum_h if ev.cusum.statistic is not None else 0.0,
    )
    pers = _clip(persistence)
    cp_ok = ev.change_point.status in (ResultStatus.TRIGGERED, ResultStatus.WATCH)
    cp = _clip(ev.change_point.probability or 0.0) if cp_ok else 0.0
    group = [dev, pers, cp]
    comb = cfg.statistical_combination
    statistical = comb.max_weight * max(group) + comb.mean_weight * (sum(group) / len(group))
    diff = (value - expected) if (value is not None and expected is not None) else None
    mag = _clip((diff or 0.0) / refs.magnitude_ref.get(family, 30.0))
    rel_raw: float | None = None
    rel = 0.0
    w = cfg.weights
    w_stat, w_mag, w_rel = w.statistical, w.magnitude, w.relative
    if value is not None and expected is not None and expected >= min_expected_for_relative:
        rel_raw = value / expected - 1.0
        rel = _clip(math.log(max(value / expected, 1e-9)) / math.log(refs.relative_cap))
    else:
        w_mag, w_rel = w_mag + w_rel, 0.0
    if z is None and ev.cold_start is not None:
        # cold start: there is no statistical evidence to weigh
        w_mag, w_rel, w_stat = w_mag + w_stat * 0.5, w_rel + w_stat * 0.5, 0.0
        if rel_raw is None:
            w_mag, w_rel = w_mag + w_rel, 0.0
    components = (
        # The three statistical members carry no weight of their own: only their
        # combination does (no double counting of correlated detectors).
        SeverityComponent("deviation", z, dev, 0.0, 0.0),
        SeverityComponent("persistence", persistence, pers, 0.0, 0.0),
        SeverityComponent("change_point", ev.change_point.probability, cp, 0.0, 0.0),
        SeverityComponent("statistical_evidence", None, statistical, w_stat, w_stat * statistical),
        SeverityComponent("magnitude", diff, mag, w_mag, w_mag * mag),
        SeverityComponent("relative_growth", rel_raw, rel, w_rel, w_rel * rel),
    )
    core = w_stat * statistical + w_mag * mag + w_rel * rel
    q = cfg.quality_multipliers
    parts = {
        "sample": q.sample.get(ev.quality, 1.0),
        "baseline": q.baseline.get(ev.baseline.readiness, 1.0),
        "freshness": q.freshness.get(freshness or "", 1.0) if freshness else 1.0,
        "dispersion": q.unstable_dispersion if ev.baseline.unstable else 1.0,
    }
    multiplier = math.prod(parts.values())
    total = round(max(0.0, min(100.0, 100.0 * multiplier * core)), 2)
    return Severity(total, components, multiplier, parts, cfg.version)
