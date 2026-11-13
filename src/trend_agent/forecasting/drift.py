"""Drift monitoring: data drift vs concept drift, with conservative retraining triggers.

* **Data drift** — the inputs changed: population stability index (PSI) of each feature
  against the training reference deciles stored with the model, missing-rate changes and
  provider/category coverage changes.
* **Concept drift** — the relationship changed: once labels of past predictions have
  matured (their horizon and publication lag are over), the observed breakout base rate,
  Brier score and calibration error are compared with the model's held-out test values.

Nothing retrains or rolls back automatically. The report recommends ``retrain`` only when
several important features drift strongly *or* matured performance degrades beyond the
configured margin with enough matured labels, and ``revert_to_baseline`` when matured
performance is worse than climatology. Acting on it is an explicit, logged operation.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np

from trend_agent.forecasting.config import Drift as DriftSettings


def psi(reference: Mapping[str, Any], values: np.ndarray) -> float | None:
    edges = reference.get("edges")
    if not edges or values.size == 0:
        return None
    ok = values[np.isfinite(values)]
    if ok.size == 0:
        return None
    e = np.array(edges, dtype=float)
    counts, _ = np.histogram(np.clip(ok, e[0], e[-1]), bins=e)
    cur = counts / ok.size
    ref = np.array(reference["proportions"], dtype=float)
    eps = 1e-4
    cur, ref = np.clip(cur, eps, None), np.clip(ref, eps, None)
    return float(np.sum((cur - ref) * np.log(cur / ref)))


def data_drift(
    reference: Mapping[str, Mapping[str, Any]],
    x: np.ndarray,
    names: tuple[str, ...],
    cfg: DriftSettings,
) -> dict[str, Any]:
    rows = int(x.shape[0])
    out: dict[str, Any] = {"rows": rows, "features": {}, "alerts": [], "warnings": []}
    if rows < cfg.min_recent_predictions:
        out["status"] = "insufficient_data"
        out["note"] = f"{rows} recent rows; at least {cfg.min_recent_predictions} needed"
        return out
    for j, n in enumerate(names):
        ref = reference.get(n)
        if not ref:
            continue
        col = x[:, j]
        value = psi(ref, col)
        miss_now = float(np.mean(~np.isfinite(col)))
        miss_ref = float(ref.get("missing_rate", 0.0))
        entry = {
            "psi": None if value is None else round(value, 4),
            "missing_rate": round(miss_now, 4),
            "missing_rate_training": round(miss_ref, 4),
        }
        out["features"][n] = entry
        if value is not None and value >= cfg.psi_alert:
            out["alerts"].append(n)
        elif value is not None and value >= cfg.psi_warn:
            out["warnings"].append(n)
        if abs(miss_now - miss_ref) >= 0.2:
            out["warnings"].append(f"{n}: missing rate {miss_ref:.0%} -> {miss_now:.0%}")
    out["status"] = (
        "data_drift"
        if len(out["alerts"]) >= cfg.retrain_min_alert_features
        else "minor_drift"
        if out["alerts"] or out["warnings"]
        else "stable"
    )
    return out


def concept_drift(
    y: np.ndarray,
    p: np.ndarray,
    *,
    model_metrics: Mapping[str, Any],
    cfg: DriftSettings,
) -> dict[str, Any]:
    ok = np.isfinite(y) & np.isfinite(p)
    y, p = y[ok], p[ok]
    n = int(y.size)
    out: dict[str, Any] = {"matured_labels": n}
    if n < cfg.min_matured_labels:
        out["status"] = "insufficient_matured_labels"
        out["note"] = f"{n} matured labels; at least {cfg.min_matured_labels} needed"
        return out
    from trend_agent.forecasting.evaluation import ece

    brier = float(np.mean((p - y) ** 2))
    rate = float(y.mean())
    clim = float(np.mean((rate - y) ** 2))
    ref_brier = model_metrics.get("brier")
    ref_rate = model_metrics.get("base_rate")
    out.update(
        {
            "brier": round(brier, 5),
            "brier_at_validation": ref_brier,
            "brier_climatology_now": round(clim, 5),
            "base_rate": round(rate, 5),
            "base_rate_at_validation": ref_rate,
            "ece": ece(y, p),
        }
    )
    degraded = ref_brier is not None and brier - float(ref_brier) > cfg.brier_degradation
    base_shift = (
        ref_rate is not None
        and float(ref_rate) > 0
        and abs(rate - float(ref_rate)) / float(ref_rate) > cfg.base_rate_change
    )
    worse_than_climatology = brier > clim
    out["status"] = (
        "worse_than_climatology"
        if worse_than_climatology
        else "concept_drift"
        if degraded or base_shift
        else "stable"
    )
    out["base_rate_shift"] = bool(base_shift)
    out["performance_degraded"] = bool(degraded)
    return out


def recommendation(data: Mapping[str, Any], concept: Mapping[str, Any]) -> dict[str, Any]:
    if concept.get("status") == "worse_than_climatology":
        action = "revert_to_baseline"
        why = "matured predictions are worse than the climatology baseline"
    elif concept.get("status") == "concept_drift":
        action = "retrain"
        why = "matured performance degraded or the base rate shifted"
    elif data.get("status") == "data_drift":
        action = "retrain"
        why = "several features drifted strongly (data drift); retrain after reviewing inputs"
    else:
        action = "none"
        why = "no drift beyond the configured limits"
    return {
        "action": action,
        "reason": why,
        "automatic": False,
        "note": "No model is retrained or replaced automatically; act explicitly.",
    }


def finite(x: float | None) -> float | None:
    return None if x is None or not math.isfinite(x) else x
