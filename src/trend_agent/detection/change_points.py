"""Causal change-point detection: Bayesian Online Change Point Detection (BOCPD).

**Why BOCPD** (Adams & MacKay, 2007): it models the series as segments with their own
level and variance and maintains, after every observation, the posterior distribution of
the *run length* (observations since the last change). That complements the other two
detectors instead of repeating them: it does not use the rolling baseline at all, it
estimates *where* a level shift happened and the levels before and after it, and a single
spike cannot produce a shift with several observations behind it.

**Model.** Within a segment, values are i.i.d. Normal with unknown mean and variance
(Normal-Gamma prior: mu0 = first value, kappa0, alpha0, beta0 = alpha0 * s0^2 with s0 a
scale floor); the posterior predictive is Student-t. Change points arrive with a constant
hazard 1/lambda. The run-length posterior is truncated at ``max_run_length`` (tail mass
merged), so each update costs O(max_run_length). For seasonal series the input is the
deseasonalised value (the weekday/hour profile known at that time).

**Decision.** A recent upward change is reported when the posterior probability that the
current segment started 'min_post_points'..'recent_window' observations ago reaches
``probability_threshold``, and the segment's median exceeds the median of the values
before it by at least ``min_shift_sigma`` robust standard deviations and the minimum
absolute effect. The result includes the change position, the pre/post levels and the
probability.

**Assumptions / failure modes.** Segments are assumed piecewise stationary and roughly
symmetric; gradual ramps appear as several small shifts (or a delayed one); strongly
autocorrelated noise inflates change probabilities; bounded indices near 0 or 100 violate
normality. Missing values are skipped (run length counts observations, not time).
Irregular series do not run this detector. Out-of-order data triggers recomputation.
"""

from __future__ import annotations

import base64
import math
import statistics
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt
from scipy.special import gammaln

from trend_agent.detection.config import ChangePointConfig
from trend_agent.detection.statistical_tests import logsumexp
from trend_agent.schemas.detection import Reason, ResultStatus

VERSION = "bocpd-v1"
PRE_WINDOW = 28  # values before the change used for the pre-change level
Array = npt.NDArray[np.float64]


def _enc(a: Array) -> str:
    return base64.b64encode(np.ascontiguousarray(a, dtype="<f8").tobytes()).decode("ascii")


def _dec(s: str) -> Array:
    return np.frombuffer(base64.b64decode(s), dtype="<f8").astype(np.float64)


def student_t_logpdf(x: float, mu: Array, kappa: Array, alpha: Array, beta: Array) -> Array:
    df = 2.0 * alpha
    scale2 = beta * (kappa + 1.0) / (alpha * kappa)
    out: Array = (
        gammaln((df + 1.0) / 2.0)
        - gammaln(df / 2.0)
        - 0.5 * np.log(df * math.pi * scale2)
        - (df + 1.0) / 2.0 * np.log1p((x - mu) ** 2 / (df * scale2))
    )
    return out


@dataclass
class BocpdState:
    mu0: float
    kappa0: float
    alpha0: float
    beta0: float
    log_p: Array
    mu: Array
    kappa: Array
    alpha: Array
    beta: Array
    values: list[float] = field(default_factory=list)
    slots: list[int] = field(default_factory=list)
    n: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "prior": [self.mu0, self.kappa0, self.alpha0, self.beta0],
            "log_p": _enc(self.log_p),
            "mu": _enc(self.mu),
            "kappa": _enc(self.kappa),
            "alpha": _enc(self.alpha),
            "beta": _enc(self.beta),
            "values": _enc(np.asarray(self.values, dtype=np.float64)),
            "slots": self.slots,
            "n": self.n,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BocpdState:
        mu0, kappa0, alpha0, beta0 = (float(x) for x in data["prior"])
        return cls(
            mu0,
            kappa0,
            alpha0,
            beta0,
            _dec(data["log_p"]),
            _dec(data["mu"]),
            _dec(data["kappa"]),
            _dec(data["alpha"]),
            _dec(data["beta"]),
            [float(x) for x in _dec(data["values"])],
            [int(x) for x in data["slots"]],
            int(data["n"]),
        )


@dataclass(frozen=True)
class ChangePointStep:
    status: ResultStatus
    probability: float | None
    change_slot: int | None
    run_length: int | None  # observations in the current segment (MAP within the window)
    pre_level: float | None
    post_level: float | None
    shift_sigma: float | None
    reasons: tuple[str, ...]
    version: str = VERSION

    @property
    def triggered(self) -> bool:
        return self.status is ResultStatus.TRIGGERED

    def as_dict(self) -> dict[str, Any]:
        def r(v: float | None) -> float | None:
            return None if v is None else round(v, 4)

        return {
            "status": self.status.value,
            "probability": r(self.probability),
            "change_slot": self.change_slot,
            "run_length": self.run_length,
            "pre_level": r(self.pre_level),
            "post_level": r(self.post_level),
            "shift_sigma": r(self.shift_sigma),
            "reasons": list(self.reasons),
            "version": self.version,
        }


def skip(status: ResultStatus, reason: str) -> ChangePointStep:
    return ChangePointStep(status, None, None, None, None, None, None, (reason,))


def init_state(x: float, slot: int, scale: float, cfg: ChangePointConfig) -> BocpdState:
    k0, a0 = cfg.prior.kappa0, cfg.prior.alpha0
    b0 = a0 * scale * scale
    return BocpdState(
        mu0=x,
        kappa0=k0,
        alpha0=a0,
        beta0=b0,
        log_p=np.zeros(1),
        mu=np.array([(k0 * x + x) / (k0 + 1.0)]),
        kappa=np.array([k0 + 1.0]),
        alpha=np.array([a0 + 0.5]),
        beta=np.array([b0]),
        values=[x],
        slots=[slot],
        n=1,
    )


def _robust_sigma(values: list[float], floor: float) -> float:
    if not values:
        return floor
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values])
    return max(1.4826 * mad, floor)


def update(
    state: BocpdState,
    x: float,
    slot: int,
    cfg: ChangePointConfig,
    *,
    floor: float,
    min_effect: float,
) -> ChangePointStep:
    """Absorb observation ``x`` (mutates ``state``) and report a recent upward shift."""
    lam = cfg.hazard_lambda
    log_h, log_1mh = -math.log(lam), math.log1p(-1.0 / lam)
    pred = student_t_logpdf(x, state.mu, state.kappa, state.alpha, state.beta)
    growth = state.log_p + pred + log_1mh
    prior_pred = float(
        student_t_logpdf(
            x,
            np.array([state.mu0]),
            np.array([state.kappa0]),
            np.array([state.alpha0]),
            np.array([state.beta0]),
        )[0]
    )
    cp = log_h + float(logsumexp(state.log_p)) + prior_pred
    log_p = np.concatenate([[cp], growth])
    log_p -= logsumexp(log_p)
    k0, m0, a0, b0 = state.kappa0, state.mu0, state.alpha0, state.beta0
    mu = np.concatenate(
        [[(k0 * m0 + x) / (k0 + 1.0)], (state.kappa * state.mu + x) / (state.kappa + 1.0)]
    )
    kappa = np.concatenate([[k0 + 1.0], state.kappa + 1.0])
    alpha = np.concatenate([[a0 + 0.5], state.alpha + 0.5])
    beta = np.concatenate(
        [
            [b0 + k0 * (x - m0) ** 2 / (2.0 * (k0 + 1.0))],
            state.beta + state.kappa * (x - state.mu) ** 2 / (2.0 * (state.kappa + 1.0)),
        ]
    )
    limit = cfg.max_run_length + 1
    if log_p.size > limit:
        log_p[limit - 1] = logsumexp(log_p[limit - 1 :])  # merge the tail mass
        log_p = log_p[:limit]
        mu, kappa, alpha, beta = mu[:limit], kappa[:limit], alpha[:limit], beta[:limit]
        log_p -= logsumexp(log_p)
    state.log_p, state.mu, state.kappa, state.alpha, state.beta = log_p, mu, kappa, alpha, beta
    state.values.append(x)
    state.slots.append(slot)
    keep = cfg.max_run_length + PRE_WINDOW + 1
    if len(state.values) > keep:
        del state.values[: len(state.values) - keep]
        del state.slots[: len(state.slots) - keep]
    state.n += 1
    return _assess(state, cfg, floor=floor, min_effect=min_effect)


def _assess(
    state: BocpdState, cfg: ChangePointConfig, *, floor: float, min_effect: float
) -> ChangePointStep:
    lo, hi = cfg.min_post_points - 1, cfg.recent_window - 1
    probs = np.exp(state.log_p)
    if probs.size <= lo:
        return skip(ResultStatus.INSUFFICIENT_DATA, Reason.INSUFFICIENT_HISTORY.value)
    window = probs[lo : hi + 1]
    probability = float(window.sum())
    r_star = lo + int(np.argmax(window))
    length = r_star + 1
    values = state.values
    if len(values) < length + 7:
        return ChangePointStep(
            ResultStatus.INSUFFICIENT_DATA,
            probability,
            None,
            length,
            None,
            None,
            None,
            (Reason.INSUFFICIENT_HISTORY.value,),
        )
    post = values[-length:]
    pre = values[max(0, len(values) - length - PRE_WINDOW) : len(values) - length]
    pre_level, post_level = statistics.median(pre), statistics.median(post)
    sigma = _robust_sigma(pre, floor)
    shift = (post_level - pre_level) / sigma
    change_slot = state.slots[-length]
    reasons: list[str] = []
    upward = shift >= cfg.min_shift_sigma and post_level - pre_level >= min_effect
    if shift <= -cfg.min_shift_sigma:
        reasons.append(Reason.DOWNWARD_CHANGE.value)
    if upward and probability >= cfg.probability_threshold:
        status = ResultStatus.TRIGGERED
    elif upward and probability >= cfg.watch_probability:
        status = ResultStatus.WATCH
    else:
        status = ResultStatus.NOT_TRIGGERED
    return ChangePointStep(
        status, probability, change_slot, length, pre_level, post_level, shift, tuple(reasons)
    )
