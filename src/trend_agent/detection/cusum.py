"""One-sided (upper) CUSUM on standardised residuals: sustained upward drift.

    G_t = max(0, G_{t-1} + min(z_t, z_clip) - k)          alarm when G_t > h

* ``z_t`` is the robust standardised residual of the same causal baseline the anomaly
  detector uses; residuals are winsorised at ``z_clip`` so a single extreme value adds at
  most ``z_clip - k`` (< h, enforced by the configuration): an alarm needs several
  elevated periods - a sustained shift, not one spike.
* ``k = delta / 2`` targets a minimum meaningful drift of ``delta`` robust standard
  deviations; ``h`` sets the in-control average run length (ARL0). Both are configured,
  and ``calibration.py`` estimates ARL0 for a given (k, h) on null residuals, alongside
  Siegmund's normal-theory approximation.
* The statistic is updated only for periods with a usable value and a READY baseline.
  Missing periods add no evidence; a gap longer than ``max_gap_periods`` resets G to 0
  (``gap_reset``). Irregular series do not run CUSUM at all (preprocessing status).
* After an alarm G resets to 0 and no new alarm is raised for ``refractory_periods``, so
  one sustained event yields one alarm (episodes deduplicate further).
* Out-of-order or revised observations trigger a deterministic recomputation from the
  start of the loaded history (``engine.py``); the state is never patched in place.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from trend_agent.detection.config import CusumConfig
from trend_agent.schemas.detection import Reason, ResultStatus

VERSION = "cusum-v1"


@dataclass
class CusumState:
    g: float = 0.0
    last_slot: int | None = None
    run_start_slot: int | None = None
    refractory_until: int | None = None
    alarms: int = 0
    last_alarm_slot: int | None = None
    updates: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CusumState:
        return cls(**data)


@dataclass(frozen=True)
class CusumStep:
    status: ResultStatus
    statistic: float | None  # G after the update (before any post-alarm reset)
    threshold: float
    alarm: bool
    onset_slot: int | None  # first period of the accumulating run
    reasons: tuple[str, ...]
    z_used: float | None
    version: str = VERSION

    @property
    def triggered(self) -> bool:
        return self.alarm

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "statistic": None if self.statistic is None else round(self.statistic, 4),
            "threshold": self.threshold,
            "alarm": self.alarm,
            "onset_slot": self.onset_slot,
            "reasons": list(self.reasons),
            "z_used": None if self.z_used is None else round(self.z_used, 4),
            "version": self.version,
        }


def skip(cfg: CusumConfig, status: ResultStatus, reason: str) -> CusumStep:
    return CusumStep(status, None, cfg.h, False, None, (reason,), None)


def update(state: CusumState, slot: int, z: float, cfg: CusumConfig) -> CusumStep:
    """Advance the statistic with the residual of ``slot`` (mutates ``state``)."""
    if not math.isfinite(z):
        return skip(cfg, ResultStatus.INSUFFICIENT_DATA, Reason.MISSING_VALUE.value)
    reasons: list[str] = []
    if state.last_slot is not None and slot - state.last_slot - 1 > cfg.max_gap_periods:
        if state.g > 0:
            reasons.append(Reason.GAP_RESET.value)
        state.g = 0.0
        state.run_start_slot = None
    z_used = min(z, cfg.z_clip)
    before = state.g
    state.g = max(0.0, state.g + z_used - cfg.k)
    if state.g > 0 and before == 0:
        state.run_start_slot = slot
    if state.g == 0:
        state.run_start_slot = None
    state.last_slot = slot
    state.updates += 1
    g = state.g
    onset = state.run_start_slot
    alarm = False
    status = ResultStatus.NOT_TRIGGERED
    if g > cfg.h:
        if state.refractory_until is not None and slot <= state.refractory_until:
            status = ResultStatus.WATCH
            reasons.append(Reason.REFRACTORY.value)
        else:
            alarm = True
            status = ResultStatus.TRIGGERED
            state.alarms += 1
            state.last_alarm_slot = slot
            state.refractory_until = slot + cfg.refractory_periods
            state.g = 0.0
            state.run_start_slot = None
    elif g >= cfg.watch_fraction * cfg.h:
        status = ResultStatus.WATCH
    return CusumStep(status, g, cfg.h, alarm, onset, tuple(reasons), z_used)


def siegmund_arl0(k: float, h: float) -> float:
    """Siegmund's approximation of the in-control ARL of a one-sided CUSUM (N(0,1) data)."""
    b = h + 1.166
    return (math.exp(2 * k * b) - 2 * k * b - 1) / (2 * k * k)
