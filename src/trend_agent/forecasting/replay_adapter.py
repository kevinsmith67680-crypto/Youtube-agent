"""Causal forecasting replay through the Phase 3 replay engine.

``ForecastReplayConsumer`` is a ``ReplayConsumer``: at each replay step it receives exactly
the observations that were *available* at that simulated time (Phase 3 handles publication
lags, revisions and late arrivals) and asks the forecast engine for forecasts from them
alone. After the replay, the forecasts are scored against outcomes computed from the final
data with the same target definitions used in training — a walk-forward check of the
deployed models, never used for training or threshold choice.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from trend_agent.forecasting.feature_store import PointInTimeSeries, SeriesBundle, hours
from trend_agent.forecasting.prediction import ForecastEngine
from trend_agent.forecasting.target_definition import outcome_labels, reference
from trend_agent.monitoring.replay import AvailabilityMode, ReplaySnapshot, replay
from trend_agent.schemas.forecast import ForecastOutput, ForecastStatus
from trend_agent.schemas.time_series import StoredPoint


@dataclass
class ForecastReplayConsumer:
    engine: ForecastEngine
    bundles: dict[int, SeriesBundle]
    every: int = 1
    outputs: list[ForecastOutput] = field(default_factory=list)
    latencies: list[float] = field(default_factory=list)
    _step: int = 0

    def on_step(self, snapshot: ReplaySnapshot) -> None:
        self._step += 1
        if (self._step - 1) % self.every:
            return
        for sid, b in self.bundles.items():
            visible = list(snapshot.series.get(sid, ()))
            if not visible:
                continue
            support = {}
            for cat, pts in b.supporting.items():
                if pts:
                    support[cat] = list(snapshot.series.get(pts[0].series_id, ()))
            view = SeriesBundle(
                b.meta, visible, support, [c for c in b.content if c.completed_at <= snapshot.as_of]
            )
            t0 = time.perf_counter()
            self.outputs += self.engine.forecast(view, snapshot.as_of)
            self.latencies.append(time.perf_counter() - t0)


def run(
    engine: ForecastEngine,
    bundles: Sequence[SeriesBundle],
    *,
    start: datetime,
    end: datetime,
    step: timedelta,
    every: int = 1,
) -> dict[str, Any]:
    points: list[StoredPoint] = []
    for b in bundles:
        points += b.points
        for pts in b.supporting.values():
            points += pts
    consumer = ForecastReplayConsumer(engine, {b.meta.series_id: b for b in bundles}, every)
    report = replay(
        points,
        start=start,
        end=end,
        step=step,
        mode=AvailabilityMode(engine.cfg.availability_mode),
        consumers=[consumer],
    )
    return {
        "replay": report.as_dict() | {"steps": len(report.steps)},
        "forecasts": score(consumer.outputs, bundles, engine),
        "latency_ms": _latency(consumer.latencies),
    }


def score(
    outputs: Sequence[ForecastOutput], bundles: Sequence[SeriesBundle], engine: ForecastEngine
) -> dict[str, Any]:
    """Score replayed forecasts against matured outcomes (final values)."""
    by_series = {b.meta.series_id: b for b in bundles}
    finals = {sid: PointInTimeSeries(b.points).final() for sid, b in by_series.items()}
    status_counts: dict[str, int] = {}
    y, p = [], []
    for o in outputs:
        status_counts[o.status.value] = status_counts.get(o.status.value, 0) + 1
        if o.breakout is None or o.breakout.probability is None or o.series_id is None:
            continue
        b = by_series.get(o.series_id)
        if b is None:
            continue
        issued = o.prediction_timestamp  # the forecast covers (T, T + H] from its issue time
        vis = PointInTimeSeries(b.points).at(issued)
        ref = reference(vis, b.meta, engine.cfg.targets)
        t, v = finals[o.series_id]
        lab = outcome_labels(
            ref, t, v, hours(issued), b.meta, (o.forecast_horizon_hours,), engine.cfg.targets
        )
        label = lab.get(f"breakout_{o.forecast_horizon_hours}", math.nan)
        if math.isfinite(label):
            y.append(label)
            p.append(o.breakout.probability)
    total = len(outputs)
    out: dict[str, Any] = {
        "forecasts": total,
        "status_counts": status_counts,
        "availability": round(status_counts.get(ForecastStatus.READY.value, 0) / total, 4)
        if total
        else None,
        "abstention_rate": round(
            sum(
                n
                for k, n in status_counts.items()
                if k not in ("READY", "LIMITED_CONFIDENCE", "BASELINE_ONLY")
            )
            / total,
            4,
        )
        if total
        else None,
        "ood_rate": round(status_counts.get("OUT_OF_DISTRIBUTION", 0) / total, 4)
        if total
        else None,
        "scored_probabilities": len(y),
    }
    if y:
        ya, pa = np.array(y), np.array(p)
        out["brier"] = round(float(np.mean((pa - ya) ** 2)), 5)
        out["observed_rate"] = round(float(ya.mean()), 4)
        out["mean_probability"] = round(float(pa.mean()), 4)
    return out


def _latency(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None}
    a = np.array(values) * 1000
    return {"p50": round(float(np.median(a)), 2), "p95": round(float(np.quantile(a, 0.95)), 2)}
