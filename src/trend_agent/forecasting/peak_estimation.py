"""Experimental peak-timing and persistence estimates from comparable historical states.

An *analog* model: training examples (states at historical prediction times, from the
training segment only) are described by a few standardised state features. For a new
state, the ``k`` nearest analogs supply what *actually happened* afterwards:

* the offset of their highest value within the peak window, or that it was right-censored
  (still rising at the window's end) or had already passed;
* whether interest was still elevated (≥ the breakout threshold) at each publication lead.

Outputs are empirical frequencies over real (here: synthetic) historical outcomes, with
Wilson intervals — never an invented timestamp. Too few analogs, or no analog close enough,
gives ``PEAK_NOT_ESTIMABLE``. Peak timing stays experimental until validated on real data,
and a local maximum inside the window is never presented as a topic's lifetime peak.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from trend_agent.forecasting.dataset_builder import Dataset
from trend_agent.forecasting.target_definition import LEADS_HOURS
from trend_agent.schemas.forecast import PeakEstimate, PeakStatus

STATE_FEATURES = (
    "level_vs_baseline_log",
    "change_24h_log",
    "change_72h_log",
    "slope_recent_log_per_h",
    "accel_log_per_h2",
    "growth_persistence",
    "window_mean_change_log",
    "window_growth_persistence",
    "threshold_gap_log",
    "det_elevated_streak_h",
    "det_cusum_ratio",
)


def wilson(k: float, n: float, level: float = 0.9) -> tuple[float, float] | None:
    if n <= 0:
        return None
    from scipy.stats import norm

    z = float(norm.ppf(0.5 + level / 2))
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


@dataclass
class AnalogModel:
    granularity: str
    window_hours: int
    center: list[float]
    scale: list[float]
    states: np.ndarray  # standardised state features (NaN -> 0 = centre)
    peak_offset: np.ndarray
    peak_censored: np.ndarray
    peak_passed: np.ndarray
    elevated: dict[int, np.ndarray]  # lead hours -> 0/1/NaN
    k: int
    min_analogs: int
    max_distance: float

    @classmethod
    def fit(
        cls,
        ds: Dataset,
        mask: np.ndarray,
        *,
        window_hours: int,
        k: int,
        min_analogs: int,
        max_rows: int,
        seed: int = 0,
    ) -> AnalogModel:
        idx = np.flatnonzero(mask & np.isfinite(ds.labels["peak_offset"]))
        if idx.size > max_rows:
            idx = np.sort(np.random.default_rng(seed).choice(idx, size=max_rows, replace=False))
        raw = (
            np.column_stack([ds.column(f)[idx] for f in STATE_FEATURES])
            if idx.size
            else (np.empty((0, len(STATE_FEATURES))))
        )
        center = [float(np.nanmedian(c)) if np.isfinite(c).any() else 0.0 for c in raw.T]
        scale = []
        for j, c in enumerate(raw.T):
            ok = c[np.isfinite(c)]
            q = np.quantile(ok, [0.25, 0.75]) if ok.size else np.array([0.0, 1.0])
            scale.append(float(max(q[1] - q[0], 1e-6)) if ok.size else 1.0)
            del j
        states = cls._standardise(raw, center, scale)
        dists = []
        if states.shape[0] > k:
            rng = np.random.default_rng(seed)
            for i in rng.choice(states.shape[0], size=min(200, states.shape[0]), replace=False):
                d = np.sqrt(np.sum((states - states[i]) ** 2, axis=1))
                dists.append(np.sort(d)[min(k, d.size - 1)])
        max_distance = float(np.quantile(dists, 0.99) * 1.5) if dists else 1.0
        return cls(
            granularity=str(ds.meta["granularity"][0]) if ds.n else "unknown",
            window_hours=window_hours,
            center=center,
            scale=scale,
            states=states,
            peak_offset=ds.labels["peak_offset"][idx],
            peak_censored=ds.labels["peak_censored"][idx],
            peak_passed=ds.labels["peak_passed"][idx],
            elevated={d: ds.labels[f"elevated_lead_{d}"][idx] for d in LEADS_HOURS},
            k=k,
            min_analogs=min_analogs,
            max_distance=max_distance,
        )

    @staticmethod
    def _standardise(raw: np.ndarray, center: list[float], scale: list[float]) -> np.ndarray:
        z = (raw - np.array(center)) / np.array(scale)
        return np.where(np.isfinite(z), z, 0.0)

    def neighbours(self, state: dict[str, float]) -> tuple[np.ndarray, float]:
        raw = np.array([[state.get(f, math.nan) for f in STATE_FEATURES]])
        z = self._standardise(raw, self.center, self.scale)[0]
        if self.states.shape[0] == 0:
            return np.array([], dtype=int), math.inf
        d = np.sqrt(np.sum((self.states - z) ** 2, axis=1))
        order = np.argsort(d, kind="mergesort")[: self.k]
        return order, float(d[order[-1]]) if order.size else math.inf

    def peak(self, state: dict[str, float]) -> PeakEstimate:
        nb, far = self.neighbours(state)
        if nb.size < self.min_analogs or far > self.max_distance:
            return PeakEstimate(
                status=PeakStatus.PEAK_NOT_ESTIMABLE,
                horizon_hours=self.window_hours,
                analogs=int(nb.size),
                note="too few comparable historical states"
                if nb.size < self.min_analogs
                else "no sufficiently similar historical state",
            )
        cens = self.peak_censored[nb]
        passed = self.peak_passed[nb]
        off = self.peak_offset[nb]
        p_beyond = float(np.mean(cens == 1))
        p_passed = float(np.mean(passed == 1))
        inside = off[(cens == 0) & (passed == 0)]
        if p_beyond >= 0.5:
            return PeakEstimate(
                status=PeakStatus.PEAK_LIKELY_BEYOND_HORIZON,
                horizon_hours=self.window_hours,
                probability_beyond_horizon=round(p_beyond, 3),
                analogs=int(nb.size),
                note="comparable states were mostly still rising at the window's end "
                "(right-censored): the peak may come later",
            )
        if p_passed >= 0.5:
            return PeakEstimate(
                status=PeakStatus.PEAK_MAY_HAVE_PASSED,
                horizon_hours=self.window_hours,
                probability_beyond_horizon=round(p_beyond, 3),
                analogs=int(nb.size),
                note="comparable states did not exceed their current level afterwards",
            )
        if inside.size < max(5, self.min_analogs // 3):
            return PeakEstimate(
                status=PeakStatus.PEAK_NOT_ESTIMABLE,
                horizon_hours=self.window_hours,
                probability_beyond_horizon=round(p_beyond, 3),
                analogs=int(nb.size),
                note="too few comparable states peaked inside the window",
            )
        lo, mid, hi = np.quantile(inside, [0.1, 0.5, 0.9])
        return PeakEstimate(
            status=PeakStatus.ESTIMATED,
            horizon_hours=self.window_hours,
            earliest_hours=round(float(lo), 1),
            latest_hours=round(float(hi), 1),
            median_hours=round(float(mid), 1),
            probability_beyond_horizon=round(p_beyond, 3),
            analogs=int(nb.size),
            note="a local maximum within the window, not a lifetime peak",
        )

    def elevated_at(
        self, state: dict[str, float], lead: float
    ) -> tuple[float | None, tuple[float, float] | None, int]:
        """Share of analogs still elevated at the nearest observable lead >= ``lead``."""
        nb, far = self.neighbours(state)
        if nb.size < self.min_analogs or far > self.max_distance:
            return None, None, int(nb.size)
        for d in LEADS_HOURS:
            if d >= lead - 1e-9:
                vals = self.elevated[d][nb]
                ok = vals[np.isfinite(vals)]
                if ok.size >= self.min_analogs:
                    k = float(ok.sum())
                    return round(k / ok.size, 3), wilson(k, float(ok.size)), int(ok.size)
                return None, None, int(ok.size)
        return None, None, 0

    def as_dict(self) -> dict[str, Any]:
        def clean(a: np.ndarray) -> list[float | None]:
            return [None if not math.isfinite(float(x)) else float(x) for x in a]

        return {
            "granularity": self.granularity,
            "window_hours": self.window_hours,
            "center": self.center,
            "scale": self.scale,
            "states": [[float(x) for x in r] for r in self.states],
            "peak_offset": clean(self.peak_offset),
            "peak_censored": clean(self.peak_censored),
            "peak_passed": clean(self.peak_passed),
            "elevated": {str(k): clean(v) for k, v in self.elevated.items()},
            "k": self.k,
            "min_analogs": self.min_analogs,
            "max_distance": self.max_distance,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AnalogModel:
        def arr(v: list[float | None]) -> np.ndarray:
            return np.array([math.nan if x is None else x for x in v], dtype=float)

        states = np.array(d["states"], dtype=float).reshape(-1, len(STATE_FEATURES))
        return cls(
            d["granularity"],
            int(d["window_hours"]),
            list(d["center"]),
            list(d["scale"]),
            states,
            arr(d["peak_offset"]),
            arr(d["peak_censored"]),
            arr(d["peak_passed"]),
            {int(k): arr(v) for k, v in d["elevated"].items()},
            int(d["k"]),
            int(d["min_analogs"]),
            float(d["max_distance"]),
        )


def evaluate_peaks(model: AnalogModel, ds: Dataset, mask: np.ndarray) -> dict[str, Any]:
    """Peak-interval coverage / error on identifiable (uncensored) peaks of the test segment."""
    from trend_agent.forecasting.evaluation import peak_metrics

    idx = np.flatnonzero(mask & np.isfinite(ds.labels["peak_offset"]))
    lo, hi, mid, est = [], [], [], []
    for i in idx:
        state = {f: float(ds.column(f)[i]) for f in STATE_FEATURES}
        pe = model.peak(state)
        ok = pe.status is PeakStatus.ESTIMATED
        est.append(ok)
        lo.append(pe.earliest_hours if ok else math.nan)
        hi.append(pe.latest_hours if ok else math.nan)
        mid.append(pe.median_hours if ok else math.nan)
    return peak_metrics(
        ds.labels["peak_offset"][idx],
        ds.labels["peak_censored"][idx],
        np.array(lo, dtype=float),
        np.array(hi, dtype=float),
        np.array(mid, dtype=float),
        np.array(est, dtype=bool),
    )
