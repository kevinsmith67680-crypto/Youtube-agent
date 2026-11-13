"""Temporal evaluation: chronological splits with embargo, rolling origins, metrics, bootstrap.

Splits are by prediction time, never random across rows of one series:

    | train ............ | embargo | calibration | embargo | test (untouched) |
                ^ inner validation (last part of train, embargoed) for early stopping

The embargo equals the horizon (plus the longest publication lag) so a label window of
one segment never overlaps the next segment. Optionally a fraction of topic groups is
held out of training and calibration entirely and appears only in the test segment
("unseen queries/topics"). Thresholds and model choices use only train/validation/
calibration data; the test segment is used once, for the final numbers.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from trend_agent.forecasting.dataset_builder import Dataset


@dataclass(frozen=True)
class Split:
    train: np.ndarray  # boolean masks over the dataset rows
    validation: np.ndarray
    calibration: np.ndarray
    test: np.ndarray
    boundaries: dict[str, float]  # hours since epoch
    unseen_groups: tuple[str, ...]

    def describe(self) -> dict[str, Any]:
        from trend_agent.forecasting.feature_store import from_hours

        return {
            "rows": {
                k: int(np.sum(getattr(self, k)))
                for k in ("train", "validation", "calibration", "test")
            },
            "boundaries": {k: from_hours(v).isoformat() for k, v in self.boundaries.items()},
            "unseen_groups": len(self.unseen_groups),
        }


def chronological_split(
    ds: Dataset,
    *,
    train_fraction: float,
    calibration_fraction: float,
    embargo_hours: float,
    unseen_group_fraction: float = 0.0,
    seed: int = 0,
    validation_fraction: float = 0.25,
) -> Split:
    t = ds.meta["t"].astype(float)
    lo, hi = float(t.min()), float(t.max())
    span = hi - lo
    t_train = lo + span * train_fraction
    t_cal = lo + span * (train_fraction + calibration_fraction)
    groups = np.array(sorted({str(g) for g in ds.meta["group"]}))
    rng = np.random.default_rng(seed)
    n_unseen = round(len(groups) * unseen_group_fraction)
    unseen = set(rng.choice(groups, size=n_unseen, replace=False)) if n_unseen else set()
    is_unseen = np.array([str(g) in unseen for g in ds.meta["group"]])
    t_val = t_train - (t_train - lo) * validation_fraction
    train = (t < t_val - embargo_hours) & ~is_unseen
    validation = (t >= t_val) & (t < t_train - embargo_hours) & ~is_unseen
    calibration = (t >= t_train) & (t < t_cal - embargo_hours) & ~is_unseen
    test = t >= t_cal
    return Split(
        train,
        validation,
        calibration,
        test,
        {"start": lo, "validation": t_val, "calibration": t_train, "test": t_cal, "end": hi},
        tuple(sorted(unseen)),
    )


def rolling_origins(
    ds: Dataset, *, folds: int, embargo_hours: float, min_train_fraction: float = 0.4
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Expanding-window walk-forward folds (train mask, evaluation mask)."""
    t = ds.meta["t"].astype(float)
    lo, hi = float(t.min()), float(t.max())
    edges = np.linspace(lo + (hi - lo) * min_train_fraction, hi, folds + 1)
    out = []
    for a, b in itertools.pairwise(edges):
        out.append((t < a - embargo_hours, (t >= a) & (t < b)))
    return out


# ------------------------------------------------------------------- metrics
def average_precision(y: np.ndarray, s: np.ndarray) -> float | None:
    if y.size == 0 or y.sum() == 0:
        return None
    order = np.argsort(-s, kind="mergesort")
    ys = y[order]
    tp = np.cumsum(ys)
    precision = tp / np.arange(1, ys.size + 1)
    return float(np.sum(precision * ys) / ys.sum())


def roc_auc(y: np.ndarray, s: np.ndarray) -> float | None:
    pos, neg = s[y == 1], s[y == 0]
    if pos.size == 0 or neg.size == 0:
        return None
    from scipy.stats import rankdata

    ranks = rankdata(np.concatenate([pos, neg]))
    return float((ranks[: pos.size].sum() - pos.size * (pos.size + 1) / 2) / (pos.size * neg.size))


def ece(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float | None:
    if y.size == 0:
        return None
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    total = 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            total += m.sum() / y.size * abs(float(p[m].mean()) - float(y[m].mean()))
    return float(total)


def reliability(y: np.ndarray, p: np.ndarray, bins: int = 10) -> list[dict[str, float | int]]:
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    out = []
    for b in range(bins):
        m = idx == b
        if m.any():
            out.append(
                {
                    "bin": b,
                    "lower": float(edges[b]),
                    "upper": float(edges[b + 1]),
                    "count": int(m.sum()),
                    "mean_predicted": round(float(p[m].mean()), 4),
                    "observed_rate": round(float(y[m].mean()), 4),
                }
            )
    return out


def classification_metrics(
    y: np.ndarray, p: np.ndarray, *, threshold: float, k: int, bins: int = 10
) -> dict[str, Any]:
    y = y.astype(float)
    n = int(y.size)
    pos = int(y.sum())
    pred = p >= threshold
    tp = int(np.sum(pred & (y == 1)))
    fp = int(np.sum(pred & (y == 0)))
    fn = int(np.sum(~pred & (y == 1)))
    tn = int(np.sum(~pred & (y == 0)))
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall > 0
        else None
    )
    order = np.argsort(-p, kind="mergesort")
    kk = min(k, n)
    top = y[order][:kk]
    base_rate = pos / n if n else None
    brier = float(np.mean((p - y) ** 2)) if n else None
    brier_clim = float(np.mean((base_rate - y) ** 2)) if n and base_rate is not None else None
    return {
        "examples": n,
        "positives": pos,
        "base_rate": _r(base_rate),
        "threshold": threshold,
        "precision": _r(precision),
        "recall": _r(recall),
        "f1": _r(f1),
        "false_positive_rate": _r(fp / (fp + tn) if fp + tn else None),
        "pr_auc": _r(average_precision(y, p)),
        "roc_auc": _r(roc_auc(y, p)),
        f"precision_at_{k}": _r(float(top.mean()) if kk else None),
        f"recall_at_{k}": _r(float(top.sum()) / pos if pos else None),
        "brier": _r(brier),
        "brier_climatology": _r(brier_clim),
        "brier_skill": _r(1 - brier / brier_clim if brier is not None and brier_clim else None),
        "ece": _r(ece(y, p, bins)),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "reliability": reliability(y, p, bins),
    }


def regression_metrics(
    y: np.ndarray,
    pred: np.ndarray,
    *,
    reference_mae: float | None = None,
    lower: np.ndarray | None = None,
    upper: np.ndarray | None = None,
) -> dict[str, Any]:
    if y.size == 0:
        return {"examples": 0}
    err = pred - y
    mae = float(np.mean(np.abs(err)))
    out: dict[str, Any] = {
        "examples": int(y.size),
        "mae": _r(mae),
        "rmse": _r(float(np.sqrt(np.mean(err**2)))),
        "bias": _r(float(np.mean(err))),
        "scaled_mae": _r(mae / reference_mae if reference_mae else None),
    }
    if lower is not None and upper is not None:
        out["interval_coverage"] = _r(float(np.mean((y >= lower) & (y <= upper))))
        out["interval_width"] = _r(float(np.mean(upper - lower)))
    return out


def best_f1_threshold(y: np.ndarray, p: np.ndarray, *, min_precision: float = 0.0) -> float:
    """Decision threshold chosen on calibration data only (never on the test period)."""
    best_t, best = 0.5, -1.0
    for t in np.unique(np.round(p, 3)):
        pred = p >= t
        tp = np.sum(pred & (y == 1))
        fp = np.sum(pred & (y == 0))
        fn = np.sum(~pred & (y == 1))
        if tp == 0:
            continue
        prec, rec = tp / (tp + fp), tp / (tp + fn)
        f1 = 2 * prec * rec / (prec + rec)
        if prec >= min_precision and f1 > best:
            best_t, best = float(t), float(f1)
    return best_t


def paired_bootstrap(
    y: np.ndarray,
    a: np.ndarray,
    b: np.ndarray,
    groups: Sequence[Any] | np.ndarray,
    *,
    metric: str,
    samples: int,
    seed: int,
    ci: float,
) -> dict[str, float | None]:
    """Difference metric(a) - metric(b) with a block bootstrap over series (groups).

    Resampling whole series respects within-series dependence; the interval is still an
    approximation (series are not independent either)."""
    keys = np.array([str(g) for g in groups])
    uniq = np.unique(keys)
    idx = {g: np.flatnonzero(keys == g) for g in uniq}
    rng = np.random.default_rng(seed)

    def m(yy: np.ndarray, pp: np.ndarray) -> float | None:
        if metric == "brier":
            return float(np.mean((pp - yy) ** 2))
        if metric == "pr_auc":
            return average_precision(yy, pp)
        if metric == "mae":
            return float(np.mean(np.abs(pp - yy)))
        raise ValueError(metric)

    base_a, base_b = m(y, a), m(y, b)
    diffs = []
    for _ in range(samples):
        pick = np.concatenate([idx[g] for g in rng.choice(uniq, size=uniq.size, replace=True)])
        ma, mb = m(y[pick], a[pick]), m(y[pick], b[pick])
        if ma is not None and mb is not None:
            diffs.append(ma - mb)
    if not diffs or base_a is None or base_b is None:
        return {"difference": None, "lower": None, "upper": None}
    lo, hi = np.quantile(diffs, [(1 - ci) / 2, 1 - (1 - ci) / 2])
    return {"difference": _r(base_a - base_b), "lower": _r(float(lo)), "upper": _r(float(hi))}


def peak_metrics(
    offset: np.ndarray,
    censored: np.ndarray,
    est_lo: np.ndarray,
    est_hi: np.ndarray,
    est_mid: np.ndarray,
    estimable: np.ndarray,
) -> dict[str, Any]:
    n = int(offset.size)
    ok = np.isfinite(offset)
    identified = ok & (censored == 0)
    both = identified & estimable
    return {
        "examples": n,
        "censoring_rate": _r(float(np.mean(censored[ok] == 1)) if ok.any() else None),
        "not_estimable_rate": _r(float(np.mean(~estimable)) if n else None),
        "identified_peaks": int(identified.sum()),
        "median_abs_error_hours": _r(
            float(np.median(np.abs(est_mid[both] - offset[both]))) if both.any() else None
        ),
        "interval_coverage": _r(
            float(np.mean((offset[both] >= est_lo[both]) & (offset[both] <= est_hi[both])))
            if both.any()
            else None
        ),
    }


def _r(x: float | None, d: int = 4) -> float | None:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return None
    return round(float(x), d)


def slices(
    ds_meta: dict[str, np.ndarray],
    mask: np.ndarray,
    y: np.ndarray,
    p: np.ndarray,
    threshold: float,
    k: int,
) -> dict[str, dict[str, Any]]:
    """Per-category / provider / granularity / novelty metrics with sample sizes."""
    out: dict[str, dict[str, Any]] = {}
    for col in ("category", "provider", "granularity"):
        vals = ds_meta[col][mask]
        for v in sorted({str(x) for x in vals}):
            m = vals == v
            if m.sum() == 0:
                continue
            res = classification_metrics(y[m], p[m], threshold=threshold, k=k)
            res.pop("reliability", None)
            out[f"{col}={v}"] = res
    return out
