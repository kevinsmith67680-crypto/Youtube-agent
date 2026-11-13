"""Probability calibration on a separate chronological calibration segment.

* isotonic regression when there are at least ``isotonic_min_examples`` rows and
  ``isotonic_min_positives`` positives;
* sigmoid (Platt) scaling when there are at least ``min_positives`` positives and
  ``min_negatives`` negatives;
* otherwise ``INSUFFICIENT_CALIBRATION_DATA`` — the model output is then reported as an
  uncalibrated *score*, never as a probability.

The calibrator is stored as plain parameters (no pickled objects). The test segment is
never used for calibration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from trend_agent.forecasting.config import Calibration as CalibrationSettings
from trend_agent.forecasting.evaluation import ece
from trend_agent.schemas.forecast import CalibrationStatus

_EPS = 1e-6


@dataclass
class Calibrator:
    method: str  # isotonic | sigmoid | none
    status: CalibrationStatus
    params: dict[str, Any] = field(default_factory=dict)
    support: dict[str, int] = field(default_factory=dict)
    ece_before: float | None = None
    ece_after: float | None = None
    bin_support: list[dict[str, float]] = field(default_factory=list)

    def apply(self, scores: np.ndarray) -> np.ndarray | None:
        s = np.clip(np.asarray(scores, dtype=float), _EPS, 1 - _EPS)
        if self.method == "isotonic":
            x = np.array(self.params["x"], dtype=float)
            y = np.array(self.params["y"], dtype=float)
            out: np.ndarray = np.interp(s, x, y)
            return out
        if self.method == "sigmoid":
            z = np.log(s / (1 - s))
            res: np.ndarray = 1.0 / (1.0 + np.exp(-(self.params["a"] * z + self.params["b"])))
            return res
        return None

    def interval(self, p: float, level: float = 0.9) -> tuple[float, float] | None:
        """Wilson interval of the observed rate in the calibration bin of ``p``."""
        for b in self.bin_support:
            if b["lower"] <= p <= b["upper"] and b["count"] > 0:
                from scipy.stats import norm

                n, rate = float(b["count"]), float(b["observed_rate"])
                z = float(norm.ppf(0.5 + level / 2))
                denom = 1 + z * z / n
                centre = (rate + z * z / (2 * n)) / denom
                half = z * np.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / denom
                return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "status": self.status.value,
            "params": self.params,
            "support": self.support,
            "ece_before": self.ece_before,
            "ece_after": self.ece_after,
            "bin_support": self.bin_support,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Calibrator:
        return cls(
            method=d["method"],
            status=CalibrationStatus(d["status"]),
            params=d.get("params", {}),
            support=d.get("support", {}),
            ece_before=d.get("ece_before"),
            ece_after=d.get("ece_after"),
            bin_support=d.get("bin_support", []),
        )


def fit(scores: np.ndarray, y: np.ndarray, cfg: CalibrationSettings) -> Calibrator:
    s = np.clip(np.asarray(scores, dtype=float), _EPS, 1 - _EPS)
    y = np.asarray(y, dtype=float)
    pos, n = int(y.sum()), int(y.size)
    neg = n - pos
    support = {"examples": n, "positives": pos, "negatives": neg}
    before = ece(y, s, cfg.bins)
    if pos < cfg.min_positives or neg < cfg.min_negatives:
        return Calibrator(
            "none", CalibrationStatus.INSUFFICIENT_CALIBRATION_DATA, {}, support, before
        )
    if n >= cfg.isotonic_min_examples and pos >= cfg.isotonic_min_positives:
        from sklearn.isotonic import IsotonicRegression

        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        iso.fit(s, y)
        cal = Calibrator(
            "isotonic",
            CalibrationStatus.CALIBRATED,
            {
                "x": [float(v) for v in iso.X_thresholds_],
                "y": [float(v) for v in iso.y_thresholds_],
            },
            support,
            before,
        )
    else:
        from sklearn.linear_model import LogisticRegression

        z = np.log(s / (1 - s)).reshape(-1, 1)
        lr = LogisticRegression(C=1e4, max_iter=1000)
        lr.fit(z, y.astype(int))
        cal = Calibrator(
            "sigmoid",
            CalibrationStatus.CALIBRATED,
            {"a": float(lr.coef_[0][0]), "b": float(lr.intercept_[0])},
            support,
            before,
        )
    p = cal.apply(s)
    assert p is not None
    cal.ece_after = ece(y, p, cfg.bins)
    edges = np.linspace(0, 1, cfg.bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, cfg.bins - 1)
    cal.bin_support = [
        {
            "lower": float(edges[b]),
            "upper": float(edges[b + 1]),
            "count": int(np.sum(idx == b)),
            "observed_rate": float(y[idx == b].mean()) if np.any(idx == b) else 0.0,
        }
        for b in range(cfg.bins)
    ]
    return cal
