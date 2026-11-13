"""Uncertainty: out-of-distribution checks and empirical prediction intervals.

* **Out of distribution** — for each feature, the training range [q0.5 %, q99.5 %] widened
  by half an interquartile range; the share of a row's observed features outside it, plus
  categorical novelty (an unseen category or granularity). Shares above the limits give
  ``LIMITED_CONFIDENCE`` or ``OUT_OF_DISTRIBUTION``.
* **Intervals** — split-conformal intervals from absolute residuals on the chronological
  calibration segment. Because observations are serially dependent and the data may
  drift, the nominal level is *not* a coverage guarantee; observed coverage on the test
  segment is reported next to it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from trend_agent.forecasting.config import Uncertainty as UncertaintySettings


@dataclass
class OODReference:
    names: tuple[str, ...]
    lower: list[float]
    upper: list[float]
    categories: tuple[str, ...]
    granularities: tuple[str, ...]
    limited_share: float
    reject_share: float

    @classmethod
    def fit(
        cls,
        x: np.ndarray,
        names: tuple[str, ...],
        categories: set[str],
        granularities: set[str],
        cfg: UncertaintySettings,
    ) -> OODReference:
        lo_q, hi_q = cfg.ood_quantiles
        lower, upper = [], []
        for j in range(x.shape[1]):
            col = x[:, j][np.isfinite(x[:, j])]
            if col.size < 20:
                lower.append(float("-inf"))
                upper.append(float("inf"))
                continue
            q1, q3 = np.quantile(col, [0.25, 0.75])
            margin = cfg.ood_margin_iqr * (q3 - q1)
            a, b = np.quantile(col, [lo_q, hi_q])
            lower.append(float(a - margin))
            upper.append(float(b + margin))
        return cls(
            names,
            lower,
            upper,
            tuple(sorted(categories)),
            tuple(sorted(granularities)),
            cfg.ood_limited_share,
            cfg.ood_reject_share,
        )

    def assess(self, row: np.ndarray, category: str | None, granularity: str) -> dict[str, Any]:
        outside: list[str] = []
        observed = 0
        for j, name in enumerate(self.names):
            v = row[j]
            if not math.isfinite(v):
                continue
            observed += 1
            if v < self.lower[j] or v > self.upper[j]:
                outside.append(name)
        share = len(outside) / observed if observed else 1.0
        novel = []
        if category and self.categories and category not in self.categories:
            novel.append(f"category {category} not in training data")
        if granularity not in self.granularities:
            novel.append(f"granularity {granularity} not in training data")
        if novel or share >= self.reject_share:
            status = "out_of_distribution"
        elif share > self.limited_share:
            status = "limited"
        else:
            status = "in_distribution"
        return {"status": status, "share": round(share, 4), "features": outside, "novel": novel}

    def as_dict(self) -> dict[str, Any]:
        def clean(v: list[float]) -> list[float | None]:
            return [x if math.isfinite(x) else None for x in v]

        return {
            "names": list(self.names),
            "lower": clean(self.lower),
            "upper": clean(self.upper),
            "categories": list(self.categories),
            "granularities": list(self.granularities),
            "limited_share": self.limited_share,
            "reject_share": self.reject_share,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OODReference:
        return cls(
            tuple(d["names"]),
            [float("-inf") if x is None else float(x) for x in d["lower"]],
            [float("inf") if x is None else float(x) for x in d["upper"]],
            tuple(d["categories"]),
            tuple(d["granularities"]),
            float(d["limited_share"]),
            float(d["reject_share"]),
        )


@dataclass
class ConformalInterval:
    level: float
    half_width: float | None
    calibration_examples: int
    method: str = "split_conformal_abs_residual"
    notes: list[str] = field(default_factory=list)

    @classmethod
    def fit(cls, y: np.ndarray, pred: np.ndarray, level: float) -> ConformalInterval:
        ok = np.isfinite(y) & np.isfinite(pred)
        res = np.abs(y[ok] - pred[ok])
        n = int(res.size)
        if n < 30:
            return cls(level, None, n, notes=["fewer than 30 calibration residuals"])
        q = min(1.0, math.ceil((n + 1) * level) / n)
        return cls(
            level,
            float(np.quantile(res, q)),
            n,
            notes=["nominal level only: serial dependence and drift can reduce coverage"],
        )

    def bounds(self, pred: float) -> tuple[float | None, float | None]:
        if self.half_width is None or not math.isfinite(pred):
            return None, None
        return pred - self.half_width, pred + self.half_width

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "half_width": self.half_width,
            "calibration_examples": self.calibration_examples,
            "method": self.method,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ConformalInterval:
        return cls(
            float(d["level"]),
            d["half_width"],
            int(d["calibration_examples"]),
            d.get("method", "split_conformal_abs_residual"),
            list(d.get("notes", [])),
        )
