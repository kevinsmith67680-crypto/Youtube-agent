"""``trend-agent evaluate pipeline``: every component evaluation, the pipeline integrity
check, backtest validity, the experiment registry and the quality scorecard in one run."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from trend_agent.db.repositories.ops import LabelRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.detection.config import load_detection_config
from trend_agent.evaluation import (
    content_gaps,
    detection,
    forecasting,
    integrity,
    replay,
    scorecard,
)
from trend_agent.evaluation import recommendations as rec_eval
from trend_agent.evaluation.registry import code_version, record
from trend_agent.ops.system import MIN_REAL_SURGE_LABELS


def real_status(factory: SessionFactory | None) -> dict[str, Any]:
    if factory is None:
        return {}
    with transaction(factory) as s:
        counts = LabelRepository(s).counts().get("real", {})
    out: dict[str, Any] = {}
    det = replay.real_detection(factory)
    out["detection"] = (
        det | {"status": "MEASURED"}
        if det.get("status") == "MEASURED"
        else {
            "status": "UNKNOWN",
            "reason": f"{counts.get('surge_event', 0)} real surge labels "
            f"(minimum {MIN_REAL_SURGE_LABELS})",
        }
    )
    for comp, kind in (
        ("content_gaps", "content_gap"),
        ("forecasting", "forecast_outcome"),
        ("recommendations", "recommendation_outcome"),
    ):
        out[comp] = {"status": "UNKNOWN", "reason": f"{counts.get(kind, 0)} real '{kind}' labels"}
    out["recommendations"]["outcomes"] = rec_eval.outcome_summary(factory)
    return out


def run(
    root: Path,
    *,
    factory: SessionFactory | None,
    now: datetime,
    size: str = "small",
    forecast_scale: float = 0.4,
    components: tuple[str, ...] = ("detection", "forecasting", "content_gaps", "recommendations"),
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    seconds: dict[str, float] = {}
    ids: dict[str, Any] = {}

    def timed(name: str, fn: Any) -> None:
        t0 = time.perf_counter()
        results[name] = fn()
        seconds[name] = round(time.perf_counter() - t0, 1)

    if "detection" in components:
        loaded = load_detection_config(root)
        timed("detection", lambda: detection.evaluate_synthetic(loaded, now=now, size=size))
        d = results["detection"]
        ids["detection"] = record(
            factory,
            name="detection synthetic held-out",
            component="detection",
            hypothesis="current thresholds meet the offline targets on held-out synthetic data",
            config=d["current"]["params"],
            dataset={"generator": "phase4 benchmark_specs", "size": size, "splits": d["splits"]},
            data_origin="synthetic",
            split="test",
            metrics=d["current"]["test"],
            baseline_metrics=d["current"]["dev"],
        )
    if "forecasting" in components:
        timed("forecasting", lambda: forecasting.evaluate(root, scale=forecast_scale))
        f = results["forecasting"]
        ids["forecasting"] = record(
            factory,
            name="forecasting per-horizon vs baselines",
            component="forecasting",
            hypothesis="selected models beat the best simple baseline per horizon",
            config={"scale": forecast_scale},
            dataset=f["dataset"],
            data_origin="synthetic",
            split="chronological test",
            metrics=f["summary"],
        )
    if "content_gaps" in components:
        timed("content_gaps", lambda: content_gaps.evaluate(root))
        g = results["content_gaps"]["splits"].get("heldout", {})
        ids["content_gaps"] = record(
            factory,
            name="content gaps four-class held-out",
            component="content_gaps",
            hypothesis="no false VERIFIED_GAP; false gap rate within target",
            config={},
            dataset={"fixtures": "config/content_analysis/eval_*.yaml"},
            data_origin="synthetic",
            split="heldout",
            metrics={k: v for k, v in g.items() if k != "rows"},
        )
    if "recommendations" in components:
        timed("recommendations", lambda: rec_eval.evaluate_offline(root))
        ids["recommendations"] = record(
            factory,
            name="recommendation quality held-out",
            component="recommendations",
            hypothesis="no grounding violations or false approvals on held-out fixtures",
            config={},
            dataset={"fixtures": "config/recommendations_eval/eval_*.yaml"},
            data_origin="synthetic",
            split="heldout",
            metrics=results["recommendations"],
        )
    if factory is not None:
        timed("integrity", lambda: integrity.check(factory))
        timed("backtest_validity", lambda: replay.validity(factory))
    real = real_status(factory)
    card = scorecard.build(results, real)
    return {
        "generated_at": now.isoformat(),
        "code_version": code_version(),
        "size": size,
        "forecast_scale": forecast_scale,
        "experiments": {k: str(v) if v else None for k, v in ids.items()},
        "scorecard": card,
        "results": results,
        "real_world": real,
        "timings_seconds": seconds,
    }
