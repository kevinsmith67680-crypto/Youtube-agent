"""Phase 6 evaluation on the SYNTHETIC corpus (reproducible; no network, no database).

1. Builds the synthetic training corpus at ``--scale``, trains every scope (baselines,
   logistic regression, LightGBM, trajectory models, analog peaks), applies the explicit
   acceptance checks and the explicit promotion step, and records per-scope results on the
   untouched chronological test period, the rolling-origin folds and the slices.
2. Re-scores the promoted champions on a held-out corpus built with a different seed:
   series the models never saw, evaluated on that corpus's chronological test period.

The output is written to ``docs/benchmarks/phase6_evaluation.json``. Every number is
measured on synthetic, fictional data: it shows how the pipeline behaves, not how
accurate it would be on real YouTube Search data.

    python scripts/evaluate_forecasting.py --scale 1.0 --workers 4
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trend_agent.detection.config import load_detection_config  # noqa: E402
from trend_agent.forecasting import synthetic as syn  # noqa: E402
from trend_agent.forecasting.config import load_forecasting_config  # noqa: E402
from trend_agent.forecasting.dataset_builder import build_dataset  # noqa: E402
from trend_agent.forecasting.model_registry import (  # noqa: E402
    ArtifactStore,
    MemoryModelStore,
    Registry,
)
from trend_agent.forecasting.model_selection import (  # noqa: E402
    compare,
    promote_selected,
    train_and_register,
)
from trend_agent.schemas.model_metadata import DataOrigin, ModelStatus  # noqa: E402

KEEP = (
    "examples",
    "positives",
    "base_rate",
    "brier",
    "brier_climatology",
    "brier_skill",
    "pr_auc",
    "roc_auc",
    "ece",
    "precision",
    "recall",
    "false_positive_rate",
    "precision_at_50",
    "recall_at_50",
    "train_pr_auc",
    "calibration_status",
    "mae",
    "rmse",
    "scaled_mae",
    "interval_coverage",
    "interval_width",
    "mae_persistence",
    "lead_time",
)


def corpus(loaded: Any, scale: float, seed: int) -> list[Any]:
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


def _metrics(m: dict[str, Any]) -> dict[str, Any]:
    return {k: m[k] for k in KEEP if k in m}


def scope_summary(res: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "scope": f"{res.family}/{res.granularity}/{res.horizon}h/{res.task}",
        "notes": list(res.notes),
        "split": res.split,
    }
    if res.report is not None:
        out["rows"] = res.report.rows
        out["selected"] = res.report.selected
        out["report_notes"] = res.report.notes
        out["candidates"] = [
            {
                "name": c.name,
                "type": c.model_type.value,
                "validated": c.validated,
                "failed_checks": sorted(k for k, ok in c.checks.items() if not ok),
                "reasons": c.reasons,
                "metrics": _metrics(c.metrics),
                "comparisons": c.comparisons,
                "slices": c.metrics.get("slices"),
            }
            for c in res.report.candidates
        ]
    if res.rolling:
        out["rolling_origin"] = res.rolling
    if res.task == "peak":
        out["peak_models"] = [
            {"model_id": m.model_id, "status": m.status.value, "metrics": m.metrics}
            for m in res.registered
        ]
    return out


def heldout(registry: Registry, ds: Any, loaded: Any) -> list[dict[str, Any]]:
    rows = []
    for m in registry.store.all():
        if m.status is not ModelStatus.CHAMPION or m.target_type.value not in (
            "breakout",
            "continuation",
        ):
            continue
        sub = ds.subset(
            (ds.meta["family"] == m.target_family.value) & (ds.meta["granularity"] == m.granularity)
        )
        if sub.n == 0:
            continue
        res = compare(registry, m.model_id, m.model_id, sub, loaded)
        met = dict(res.get("champion", {}).get("metrics", {}))
        rows.append(
            {
                "scope": f"{m.target_family.value}/{m.granularity}/{m.horizon_hours}h/"
                f"{m.target_type.value}",
                "model": m.model_name,
                "test_rows": res.get("test_rows"),
                "test_positives": res.get("test_positives"),
                "metrics": _metrics(met),
                "stability_mean_abs_change": met.get("stability_mean_abs_change"),
            }
        )
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=str(ROOT / "docs" / "benchmarks" / "phase6_evaluation.json"))
    args = ap.parse_args()
    loaded = load_forecasting_config(ROOT / "config")
    dloaded = load_detection_config(ROOT / "config")
    seed = loaded.config.corpus.seed
    t0 = time.perf_counter()
    bundles = corpus(loaded, args.scale, seed)
    ds = build_dataset(bundles, loaded, dloaded, source="synthetic", workers=args.workers)
    t1 = time.perf_counter()
    registry = Registry(MemoryModelStore(), ArtifactStore(tempfile.mkdtemp(prefix="p6eval")))
    at = syn.START + timedelta(days=loaded.config.corpus.daily_days)
    results = train_and_register(ds, loaded, registry, origin=DataOrigin.SYNTHETIC, trained_at=at)
    decisions = promote_selected(registry, results, ds, loaded, at=at)
    t2 = time.perf_counter()
    held_bundles = corpus(loaded, max(0.25, args.scale / 2), seed + 1)
    held_ds = build_dataset(
        held_bundles, loaded, dloaded, source="synthetic", workers=args.workers, audit_rows=4
    )
    t3 = time.perf_counter()
    man = ds.manifest
    report: dict[str, Any] = {
        "synthetic": True,
        "note": "SYNTHETIC, fictional data only. These numbers describe pipeline behaviour "
        "and are not evidence of real-world forecasting accuracy.",
        "configuration": loaded.version_tag,
        "corpus": {
            "scale": args.scale,
            "seed": seed,
            "series": len(bundles),
            "rows": man["rows"],
            "dataset_version": man["dataset_version"],
            "rows_hash": man["rows_hash"],
            "leakage_audit": man["leakage_audit"],
            "label_counts": man["label_counts"],
            "skipped_prediction_times": man["skipped_prediction_times"],
        },
        "timings_seconds": {
            "dataset": round(t1 - t0, 1),
            "training_and_promotion": round(t2 - t1, 1),
            "heldout_dataset": round(t3 - t2, 1),
        },
        "scopes": [scope_summary(r) for r in results],
        "promotions": decisions,
        "models": {
            s.value: sum(1 for m in registry.store.all() if m.status is s) for s in ModelStatus
        },
        "heldout_corpus": {
            "seed": seed + 1,
            "series": len(held_bundles),
            "rows": held_ds.n,
            "leakage_audit": held_ds.manifest["leakage_audit"],
            "champions": heldout(registry, held_ds, loaded),
        },
    }
    text = json.dumps(report, indent=2, default=_default)
    Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("corpus", "timings_seconds", "models")}, indent=2))
    for s in report["scopes"]:
        print(s["scope"], "selected:", s.get("selected"), s.get("notes") or "")
    for h in report["heldout_corpus"]["champions"]:
        m = h["metrics"]
        print("heldout", h["scope"], h["model"], m.get("brier"), m.get("brier_skill"),
              m.get("pr_auc"), m.get("ece"))  # fmt: skip


def _default(o: Any) -> Any:
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


if __name__ == "__main__":
    main()
