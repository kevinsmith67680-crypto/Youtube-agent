"""Detection accuracy: dev / calibration / test splits, per-source metrics and a bounded
optimisation loop.

Splits (disjoint seeds and topic clusters, from the Phase 4 synthetic generator):

* calibration  null series (no events): fits the empirical null thresholds.
* dev          mixed series with injected events: the ONLY split the tuning loop sees.
* test         mixed series, never used for tuning: evaluated once per configuration
               that the loop selected, and once for the current configuration.

The loop searches a small grid (CUSUM ``h``, FDR ``q``) on dev, keeps a candidate only if
its dev false-alert rate is not higher than the current configuration's (fewer alerts is
not "more accurate" by itself), and then compares candidate and current on test. A
candidate is ``adopted`` only if test F1 improves by at least ``min_gain`` without losing
recall or raising false alerts. Nothing is written to the configuration: an adopted
candidate is a proposal for a human to review.

Per-source metrics replay the test series twice: as DIRECT_YOUTUBE_SEARCH_INTEREST and as a
proxy (VIDEO_ACTIVITY) source, so precision, recall, false alerts and delays are reported
separately for each measurement class.

Real labels (``evaluation_labels`` with origin ``real``) are scored separately; below the
minimum sample the real-world result is ``UNKNOWN``.
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from trend_agent.detection.calibration import calibrate
from trend_agent.detection.config import LoadedDetectionConfig
from trend_agent.detection.evaluation import EvaluationMetrics, build_world, evaluate_world
from trend_agent.detection.synthetic import benchmark_specs
from trend_agent.schemas.monitoring import MeasurementCategory

SIZES = {
    "tiny": {"dev": 40, "test": 40, "days": 150},
    "small": {"dev": 120, "test": 200, "days": 150},
    "full": {"dev": 300, "test": 400, "days": 150},
}
EVAL_START = 90
GRID = [(h, q) for h in (5.0, 6.0, 7.0) for q in (0.05, 0.10)]
KEYS = (
    "precision",
    "recall",
    "false_alerts_per_day",
    "false_alerts_per_1000_series_day",
    "delay_median_days",
    "delay_p90_days",
    "duplication_rate",
    "confirmed_precision",
    "confirmed_recall",
    "labels",
    "alerts",
    "tp",
    "fp",
    "fn",
)


def _f1(m: dict[str, Any]) -> float | None:
    p, r = m.get("precision"), m.get("recall")
    if p is None or r is None or p + r == 0:
        return None
    return round(2 * float(p) * float(r) / (float(p) + float(r)), 4)


def _summary(m: EvaluationMetrics) -> dict[str, Any]:
    d = m.as_dict()
    out = {k: d.get(k) for k in KEYS}
    out["f1"] = _f1(out)
    out["recall_by_kind"] = d.get("recall_by_kind")
    out["false_alerts_by_category"] = d.get("false_alerts_by_category")
    out["dataset"] = d.get("dataset")
    return out


def with_params(loaded: LoadedDetectionConfig, h: float, q: float) -> LoadedDetectionConfig:
    cfg = loaded.config
    new = cfg.model_copy(
        update={
            "cusum": cfg.cusum.model_copy(update={"h": h}),
            "multiple_testing": cfg.multiple_testing.model_copy(update={"fdr_q": q}),
        }
    )
    return loaded.model_copy(update={"config": new})


def evaluate_synthetic(
    loaded: LoadedDetectionConfig,
    *,
    now: datetime,
    size: str = "small",
    tune: bool = False,
    min_gain: float = 0.01,
) -> dict[str, Any]:
    sz = SIZES[size]
    timings: dict[str, float] = {}
    t0 = time.perf_counter()
    cal_specs = [s for s in benchmark_specs("train", sz["dev"], days=sz["days"]) if not s.events]
    cal_world = build_world(cal_specs)
    nulls = calibrate(
        cal_world.points,
        loaded,
        version="phase8-eval",
        source="synthetic",
        dataset=f"synthetic-calibration-null-{len(cal_specs)}",
        now=now,
    ).lookup()
    timings["calibration"] = round(time.perf_counter() - t0, 2)
    dev_specs = benchmark_specs("train", sz["dev"], days=sz["days"], eval_start=EVAL_START)
    test_specs = benchmark_specs("test", sz["test"], days=sz["days"], eval_start=EVAL_START)
    dev_world, test_world = build_world(dev_specs), build_world(test_specs)

    def run(cfg: LoadedDetectionConfig, world: Any, split: str) -> dict[str, Any]:
        return _summary(
            evaluate_world(
                world, cfg, nulls, dataset=f"synthetic-{split}", split=split, eval_start=EVAL_START
            )
        )

    t0 = time.perf_counter()
    current_dev = run(loaded, dev_world, "dev")
    current_test = run(loaded, test_world, "test")
    timings["current"] = round(time.perf_counter() - t0, 2)
    by_source: dict[str, Any] = {}
    t0 = time.perf_counter()
    for cls in (
        MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST,
        MeasurementCategory.VIDEO_ACTIVITY,
    ):
        world = build_world([replace(s, measurement=cls) for s in test_specs])
        by_source[cls.value] = run(loaded, world, f"test-{cls.value}")
    timings["by_source"] = round(time.perf_counter() - t0, 2)
    report: dict[str, Any] = {
        "origin": "synthetic",
        "splits": {
            "calibration": {"series": len(cal_specs), "kind": "null (no events)"},
            "dev": {"series": len(dev_specs)},
            "test": {"series": len(test_specs), "used_for_tuning": False},
        },
        "current": {"params": _params(loaded), "dev": current_dev, "test": current_test},
        "by_source_test": by_source,
        "tuning": None,
        "timings_seconds": timings,
    }
    if tune:
        t0 = time.perf_counter()
        report["tuning"] = _tune(
            loaded, run, dev_world, test_world, current_dev, current_test, min_gain
        )
        timings["tuning"] = round(time.perf_counter() - t0, 2)
    return report


def _params(loaded: LoadedDetectionConfig) -> dict[str, float]:
    return {"cusum_h": loaded.config.cusum.h, "fdr_q": loaded.config.multiple_testing.fdr_q}


def _tune(
    loaded: LoadedDetectionConfig,
    run: Any,
    dev_world: Any,
    test_world: Any,
    current_dev: dict[str, Any],
    current_test: dict[str, Any],
    min_gain: float,
) -> dict[str, Any]:
    trials = []
    fa_cap = current_dev.get("false_alerts_per_1000_series_day") or 0.0
    for h, q in GRID:
        cand = with_params(loaded, h, q)
        dev = run(cand, dev_world, "dev")
        eligible = (dev.get("false_alerts_per_1000_series_day") or 0.0) <= fa_cap + 1e-9
        trials.append({"params": {"cusum_h": h, "fdr_q": q}, "dev": dev, "eligible": eligible})
    eligible = [t for t in trials if t["eligible"] and t["dev"]["f1"] is not None]
    if not eligible:
        return {
            "trials": trials,
            "selected": None,
            "decision": "inconclusive",
            "reason": "no candidate kept the false-alert rate at or below the current one",
        }
    best = max(eligible, key=lambda t: (t["dev"]["f1"], t["dev"]["recall"] or 0.0))
    if best["params"] == _params(loaded):
        return {
            "trials": trials,
            "selected": best["params"],
            "decision": "rejected",
            "reason": "the current configuration is already the best on dev",
        }
    p = best["params"]
    test = run(with_params(loaded, p["cusum_h"], p["fdr_q"]), test_world, "test")
    gain = (test["f1"] or 0.0) - (current_test["f1"] or 0.0)
    recall_ok = (test["recall"] or 0.0) >= (current_test["recall"] or 0.0) - 1e-9
    fa_ok = (test.get("false_alerts_per_1000_series_day") or 0.0) <= (
        current_test.get("false_alerts_per_1000_series_day") or 0.0
    ) + 1e-9
    adopt = gain >= min_gain and recall_ok and fa_ok
    reason = (
        f"test F1 {current_test['f1']} -> {test['f1']} (gain {gain:+.4f}, required "
        f">= {min_gain}); recall kept: {recall_ok}; false alerts not higher: {fa_ok}"
    )
    return {
        "trials": trials,
        "selected": p,
        "selected_test": test,
        "decision": "adopted" if adopt else "rejected",
        "reason": reason + ("; proposal only - configuration unchanged" if adopt else ""),
    }


# ---------------------------------------------------------------------- real labels
def score_real(
    labels: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    *,
    tolerance: timedelta = timedelta(days=3),
    min_labels: int = 30,
) -> dict[str, Any]:
    """Match stored real alerts to real surge labels (same subject, alert within
    ``tolerance`` of the labelled onset, and only alerts raised after the label's subject
    became observable). Returns UNKNOWN below ``min_labels``."""
    n = len(labels)
    if n < min_labels:
        return {
            "status": "UNKNOWN",
            "labels": n,
            "minimum": min_labels,
            "reason": "not enough labelled real surges",
        }
    used: set[int] = set()
    tp, delays = 0, []
    for lab in labels:
        hit = None
        for i, a in enumerate(alerts):
            if i in used or a["subject_key"] != lab["subject_key"]:
                continue
            if abs(a["alerted_at"] - lab["event_time"]) <= tolerance:
                hit = i
                break
        if hit is not None:
            used.add(hit)
            tp += 1
            delays.append((alerts[hit]["alerted_at"] - lab["event_time"]).total_seconds() / 86400)
    fp = len(alerts) - len(used)
    precision = tp / (tp + fp) if tp + fp else None
    delays.sort()
    return {
        "status": "MEASURED",
        "labels": n,
        "alerts": len(alerts),
        "precision": round(precision, 4) if precision is not None else None,
        "recall": round(tp / n, 4),
        "delay_median_days": round(delays[len(delays) // 2], 2) if delays else None,
    }
