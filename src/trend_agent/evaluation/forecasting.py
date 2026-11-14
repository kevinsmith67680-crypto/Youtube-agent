"""Forecast evaluation per horizon against simple baselines (SYNTHETIC corpus).

Runs the Phase 6 pipeline (dataset with leakage audit, chronological train / validation /
calibration / test split, training, explicit acceptance checks, explicit promotion) at a
reduced scale and summarises every scope:

* classification scopes (breakout / continuation): the selected model's Brier skill
  against climatology (the reference, skill 0) and, where LightGBM was a candidate, its
  margin over logistic regression - LightGBM is only promoted by Phase 6 when it passes
  its acceptance checks, so a "selected" LightGBM is never the default.
* trajectory scopes: the selected model's MAE against the best of persistence, seasonal
  naive and damped trend.
* unsupported scopes (e.g. 6-hour forecasts from daily data) are listed as unsupported,
  never forecast.

Nothing here touches the database; results are synthetic and say so.
"""

from __future__ import annotations

import tempfile
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from trend_agent.detection.config import load_detection_config
from trend_agent.forecasting import synthetic as syn
from trend_agent.forecasting.config import load_forecasting_config
from trend_agent.forecasting.dataset_builder import build_dataset
from trend_agent.forecasting.model_registry import ArtifactStore, MemoryModelStore, Registry
from trend_agent.forecasting.model_selection import promote_selected, train_and_register
from trend_agent.schemas.model_metadata import DataOrigin, ModelStatus

BASELINES = {"persistence", "seasonal_naive", "damped_trend"}
MEANINGFUL_MAE_GAIN = 0.05  # LightGBM regressor must cut MAE by >= 5 % vs the best baseline
MEANINGFUL_SKILL_GAIN = 0.02  # LightGBM classifier: >= 0.02 Brier skill over logistic


def _corpus(loaded: Any, scale: float, seed: int) -> list[Any]:
    c = loaded.config.corpus
    return syn.materialise(
        syn.corpus(
            hourly=max(12, int(c.hourly_series * scale)),
            daily=max(6, int(c.daily_series * scale)),
            hourly_days=c.hourly_days,
            daily_days=c.daily_days,
            seed=seed,
            allocation=loaded.config.category_allocation,
        )
    )


def summarise_scope(res: Any) -> dict[str, Any]:
    scope = f"{res.family}/{res.granularity}/{res.horizon}h/{res.task}"
    out: dict[str, Any] = {
        "scope": scope,
        "horizon_hours": res.horizon,
        "granularity": res.granularity,
        "task": res.task,
    }
    notes = list(res.notes)
    if any("not supported" in n for n in notes):
        return out | {"status": "UNSUPPORTED", "reason": "; ".join(notes)}
    rep = res.report
    if rep is None or not rep.candidates:
        return out | {"status": "NO_MODEL", "reason": "; ".join(notes) or "no candidates trained"}
    cands = {c.name: c for c in rep.candidates}
    sel = cands.get(rep.selected) if rep.selected else None
    out["selected"] = rep.selected
    if res.task in ("breakout", "continuation"):
        lgbm = next(
            (c for c in rep.candidates if c.model_type.value == "lightgbm_classifier"), None
        )
        logit = next(
            (c for c in rep.candidates if c.model_type.value == "logistic_regression"), None
        )
        skill = sel.metrics.get("brier_skill") if sel else None
        out |= {
            "metric": "brier_skill_vs_climatology",
            "selected_value": skill,
            "baseline": "climatology",
            "baseline_value": 0.0,
            "beats_baseline": bool(skill is not None and skill > 0),
            "lightgbm_candidate": lgbm is not None,
            "lightgbm_selected": bool(sel is not None and sel is lgbm),
            "lightgbm_margin_over_logistic": (
                round(lgbm.metrics["brier_skill"] - logit.metrics["brier_skill"], 4)
                if lgbm
                and logit
                and lgbm.metrics.get("brier_skill") is not None
                and logit.metrics.get("brier_skill") is not None
                else None
            ),
            "pr_auc": sel.metrics.get("pr_auc") if sel else None,
            "ece": sel.metrics.get("ece") if sel else None,
        }
    elif res.task == "trajectory":
        base = [
            c
            for c in rep.candidates
            if c.model_type.value in BASELINES and c.metrics.get("mae") is not None
        ]
        best = min(base, key=lambda c: c.metrics["mae"]) if base else None
        mae = sel.metrics.get("mae") if sel else None
        out |= {
            "metric": "mae",
            "selected_value": mae,
            "baseline": best.name if best else None,
            "baseline_value": best.metrics["mae"] if best else None,
            "beats_baseline": bool(mae is not None and best and mae < best.metrics["mae"]),
            "relative_mae_gain": round(1 - mae / best.metrics["mae"], 4)
            if mae is not None and best and best.metrics["mae"]
            else None,
            "lightgbm_selected": bool(
                sel is not None and sel.model_type.value == "lightgbm_regressor"
            ),
            "interval_coverage": sel.metrics.get("interval_coverage") if sel else None,
        }
    else:
        out |= {"metric": None, "note": "peak timing is evaluated on episodes (see Phase 6)"}
    out["status"] = "SELECTED" if sel else "NO_VALIDATED_MODEL"
    if out.get("lightgbm_selected"):
        gain = out.get("lightgbm_margin_over_logistic") or out.get("relative_mae_gain") or 0.0
        threshold = MEANINGFUL_SKILL_GAIN if res.task != "trajectory" else MEANINGFUL_MAE_GAIN
        out["lightgbm_meaningful"] = gain >= threshold
    return out


def evaluate(root: Path, *, scale: float = 0.4, workers: int = 2) -> dict[str, Any]:
    loaded = load_forecasting_config(root)
    dloaded = load_detection_config(root)
    seed = loaded.config.corpus.seed
    t0 = time.perf_counter()
    ds = build_dataset(
        _corpus(loaded, scale, seed), loaded, dloaded, source="synthetic", workers=workers
    )
    t1 = time.perf_counter()
    registry = Registry(MemoryModelStore(), ArtifactStore(tempfile.mkdtemp(prefix="p8fc")))
    at = syn.START + timedelta(days=loaded.config.corpus.daily_days)
    results = train_and_register(ds, loaded, registry, origin=DataOrigin.SYNTHETIC, trained_at=at)
    decisions = promote_selected(registry, results, ds, loaded, at=at)
    t2 = time.perf_counter()
    scopes = [summarise_scope(r) for r in results]
    selected = [s for s in scopes if s.get("status") == "SELECTED" and s.get("metric")]
    beats = [s for s in selected if s.get("beats_baseline")]
    lgbm = [s for s in selected if s.get("lightgbm_selected")]
    return {
        "origin": "synthetic",
        "note": "SYNTHETIC corpus: behaviour of the pipeline, not real-world accuracy.",
        "scale": scale,
        "dataset": {
            "rows": ds.manifest["rows"],
            "dataset_version": ds.manifest["dataset_version"],
            "rows_hash": ds.manifest["rows_hash"],
            "leakage_audit": ds.manifest["leakage_audit"],
        },
        "splits": "chronological train / validation / calibration / test per scope",
        "scopes": scopes,
        "promotions": decisions,
        "models_by_status": {
            s.value: sum(1 for m in registry.store.all() if m.status is s) for s in ModelStatus
        },
        "summary": {
            "scopes": len(scopes),
            "unsupported": [s["scope"] for s in scopes if s.get("status") == "UNSUPPORTED"],
            "selected": len(selected),
            "beats_best_baseline": len(beats),
            "beats_best_baseline_share": round(len(beats) / len(selected), 4) if selected else None,
            "lightgbm_selected": len(lgbm),
            "lightgbm_selected_without_meaningful_gain": [
                s["scope"] for s in lgbm if not s.get("lightgbm_meaningful")
            ],
        },
        "timings_seconds": {"dataset": round(t1 - t0, 1), "training": round(t2 - t1, 1)},
    }
