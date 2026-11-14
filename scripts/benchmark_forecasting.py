"""Phase 6 forecasting benchmarks (synthetic data; isolated database for the DB part).

In memory (no database):
  * point-in-time feature generation per prediction time (fast and strict paths);
  * dataset construction for the synthetic corpus (1 and N worker processes);
  * training time (all scopes, horizons and candidate models);
  * single-forecast latency (one hourly series, all horizons) and batch throughput
    (1,000 hourly series);
  * causal replay through the Phase 3 replay engine.

Database (RESET; refuses names without "test"/"bench"):
  * 1,000 active targets with daily history + 10,000 dormant targets (Phase 4 seeder),
    labelled synthetic so the synthetic-trained champions apply;
  * training run persisted to the registry; explicit promotion;
  * reading all forecastable series, batch prediction with persistence, the idempotent
    re-run, explanation lookup and the drift report.

    TREND_AGENT_BENCH_DATABASE_URL=postgresql+psycopg://...trend_agent_test \\
        python scripts/benchmark_forecasting.py --json-out bench.json

All numbers are measured on the machine reported under "hardware"; they are not
estimates and say nothing about accuracy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import benchmark_detection as bd  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from trend_agent.config import Settings  # noqa: E402
from trend_agent.container import build_container  # noqa: E402
from trend_agent.db.forecast_models import ForecastPredictionRow  # noqa: E402
from trend_agent.db.monitoring_models import (  # noqa: E402
    MonitoringTargetRow,
    ObservationRow,
    SeriesRow,
)
from trend_agent.db.session import transaction  # noqa: E402
from trend_agent.detection.config import load_detection_config  # noqa: E402
from trend_agent.forecasting import replay_adapter  # noqa: E402
from trend_agent.forecasting import synthetic as syn  # noqa: E402
from trend_agent.forecasting.config import load_forecasting_config  # noqa: E402
from trend_agent.forecasting.dataset_builder import (  # noqa: E402
    _support_views,
    build_dataset,
    row_features,
)
from trend_agent.forecasting.feature_store import DetectorFeed, PointInTimeSeries  # noqa: E402
from trend_agent.forecasting.model_registry import (  # noqa: E402
    ArtifactStore,
    MemoryModelStore,
    Registry,
)
from trend_agent.forecasting.model_selection import (  # noqa: E402
    promote_selected,
    train_and_register,
)
from trend_agent.forecasting.prediction import ForecastEngine  # noqa: E402
from trend_agent.schemas.model_metadata import DataOrigin  # noqa: E402
from trend_agent.schemas.monitoring import MeasurementCategory  # noqa: E402

DEFAULT_URL = bd.DEFAULT_URL
DAY = timedelta(days=1)


def pct(values: list[float], q: float) -> float:
    return round(float(np.quantile(np.array(values), q)) * 1000, 3)


def corpus(loaded: Any, scale: float) -> list[Any]:
    c = loaded.config.corpus
    return syn.materialise(
        syn.corpus(
            hourly=max(12, int(c.hourly_series * scale)),
            daily=max(6, int(c.daily_series * scale)),
            hourly_days=c.hourly_days,
            daily_days=c.daily_days,
            seed=c.seed,
            allocation=loaded.config.category_allocation,
        )
    )


# ----------------------------------------------------------------------------- memory
def memory_benchmarks(loaded: Any, dloaded: Any, scale: float, workers: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    # feature generation (one 56-day hourly series, 30 prediction times)
    b = corpus(loaded, 0.05)[0]
    pit = PointInTimeSeries(b.points)
    feed = DetectorFeed(b.points, dloaded, b.meta.category, pit=pit)
    sup = _support_views(b, pit.mode)
    t_end = b.points[-1].period_end
    times = [t_end - timedelta(hours=6 * k) for k in range(30)]
    fast, strict = [], []
    for t in times:
        t0 = time.perf_counter()
        row_features(b, pit, feed, sup, t, loaded, dloaded)
        fast.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        row_features(b, pit, None, sup, t, loaded, dloaded, strict=True)
        strict.append(time.perf_counter() - t0)
    out["feature_generation"] = {
        "series_points": len(b.points),
        "fast_path_ms": {"p50": pct(fast, 0.5), "p95": pct(fast, 0.95)},
        "strict_path_ms": {"p50": pct(strict, 0.5), "p95": pct(strict, 0.95)},
        "note": "fast path: one causal detector run reused (revision-free series); strict "
        "path: detector re-run from the data visible at T (used for inference and audits)",
    }
    # dataset construction
    bundles = corpus(loaded, scale)
    timings: dict[str, Any] = {}
    ds = None
    for w in sorted({1, workers}):
        t0 = time.perf_counter()
        ds = build_dataset(bundles, loaded, dloaded, source="synthetic", workers=w)
        secs = time.perf_counter() - t0
        timings[f"workers_{w}"] = {
            "seconds": round(secs, 2),
            "rows_per_second": round(ds.n / secs, 1),
        }
    assert ds is not None
    out["dataset_construction"] = {
        "series": len(bundles),
        "observations": sum(len(x.points) for x in bundles),
        "rows": ds.n,
        "features": len(ds.feature_names),
        "leakage_audit": ds.manifest["leakage_audit"]["mismatches"],
        **timings,
    }
    # training
    registry = Registry(MemoryModelStore(), ArtifactStore(tempfile.mkdtemp(prefix="p6bench")))
    at = syn.START + DAY * loaded.config.corpus.daily_days
    t0 = time.perf_counter()
    results = train_and_register(ds, loaded, registry, origin=DataOrigin.SYNTHETIC, trained_at=at)
    train_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    decisions = promote_selected(registry, results, ds, loaded, at=at)
    out["training"] = {
        "seconds": round(train_s, 2),
        "promotion_seconds": round(time.perf_counter() - t0, 2),
        "models_registered": len(registry.store.all()),
        "champions": sum(1 for d in decisions if d.get("promoted")),
        "scopes": len(results),
    }
    # single forecast latency and batch throughput
    engine = ForecastEngine(loaded, dloaded, registry)
    spec = syn.Spec(key="bench_single", seed=5, periods=24 * 30, category="ai")
    one, _ = syn.Bundle(spec, {}).build(990_001, 9_900_000)
    as_of = spec.start + timedelta(hours=24 * 30)
    engine.forecast(one, as_of)  # warm the model cache
    lat = []
    for k in range(50):
        t0 = time.perf_counter()
        engine.forecast(one, as_of + timedelta(hours=k))
        lat.append(time.perf_counter() - t0)
    out["single_forecast_ms"] = {
        "horizons": len(loaded.config.horizons_hours),
        "p50": pct(lat, 0.5),
        "p95": pct(lat, 0.95),
        "series_points": len(one.points),
    }
    batch = []
    for i in range(1000):
        s = syn.Spec(key=f"bench_batch_{i}", seed=1000 + i, periods=24 * 21, category="ai")
        bb, _ = syn.Bundle(s, {}).build(800_000 + i, 8_000_000 + i * 1000)
        batch.append(bb)
    t0 = time.perf_counter()
    statuses: dict[str, int] = {}
    for bb in batch:
        for o in engine.forecast(bb, syn.START + timedelta(hours=24 * 21)):
            statuses[o.status.value] = statuses.get(o.status.value, 0) + 1
    secs = time.perf_counter() - t0
    out["batch_forecast"] = {
        "series": len(batch),
        "forecasts": sum(statuses.values()),
        "seconds": round(secs, 2),
        "series_per_second": round(len(batch) / secs, 1),
        "statuses": statuses,
    }
    # replay
    held = corpus(loaded, 0.05)[:20]
    t0 = time.perf_counter()
    rep = replay_adapter.run(
        engine,
        held,
        start=syn.START + DAY * 10,
        end=syn.START + DAY * 40,
        step=timedelta(hours=12),
    )
    secs = time.perf_counter() - t0
    out["replay"] = {
        "series": len(held),
        "steps": rep["replay"]["steps"],
        "forecasts": rep["forecasts"]["forecasts"],
        "seconds": round(secs, 2),
        "latency_ms_per_series_step": rep["latency_ms"],
    }
    return out


# ----------------------------------------------------------------------------- database
def database_benchmarks(
    url: str, loaded: Any, active: int, dormant: int, days: int, scale: float, workers: int
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    bd.reset_database(url)
    settings = Settings(
        _env_file=None,
        database_url=SecretStr(url),
        forecast_artifact_dir=tempfile.mkdtemp(prefix="p6bench_db"),
    )
    now = bd.START + DAY * days + timedelta(hours=40)
    container = build_container(settings, clock=lambda: now)
    try:
        factory = container.session_factory
        t0 = time.perf_counter()
        bd.seed(factory, active, dormant, days)
        with transaction(factory) as s:  # benchmark data is synthetic: label it so
            for model in (SeriesRow, ObservationRow):
                s.execute(
                    sa.update(model).values(
                        is_synthetic=True,
                        measurement_category=MeasurementCategory.SYNTHETIC_TEST_OBSERVATION,
                        simulated_category=MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST,
                    )
                )
            s.execute(sa.update(MonitoringTargetRow).values(is_synthetic=True))
        out["seed"] = {
            "active_targets": active,
            "dormant_targets": dormant,
            "days": days,
            "seconds": round(time.perf_counter() - t0, 2),
            "note": "setup only (bulk insert), not a measured path",
        }
        svc = container.forecasting(clock=lambda: now)
        t0 = time.perf_counter()
        res = svc.train(dataset="synthetic", scale=scale, workers=workers, promote=True)
        out["training_run"] = {
            "seconds": round(time.perf_counter() - t0, 2),
            "status": res["status"],
            "registered": res.get("registered"),
            "champions": sum(1 for d in res.get("promotions", []) if d.get("promoted")),
            "rows": res.get("manifest", {}).get("rows"),
        }
        t0 = time.perf_counter()
        bundles = svc.bundles(include_synthetic=True)
        out["read_series"] = {
            "series": len(bundles),
            "observations": sum(len(b.points) for b in bundles),
            "seconds": round(time.perf_counter() - t0, 2),
        }
        t0 = time.perf_counter()
        first = svc.predict(include_synthetic=True)
        secs = time.perf_counter() - t0
        statuses: dict[str, int] = {}
        for o, _ in first:
            statuses[o.status.value] = statuses.get(o.status.value, 0) + 1
        out["predict_persist"] = {
            "forecasts": len(first),
            "stored": sum(c for _, c in first),
            "seconds": round(secs, 2),
            "forecasts_per_second": round(len(first) / secs, 1),
            "statuses": statuses,
            "note": "includes reading every series, features, scoring and one transaction "
            "per forecast (forecast row + trajectory, peak and window rows)",
        }
        t0 = time.perf_counter()
        again = svc.predict(include_synthetic=True)
        out["predict_idempotent_rerun"] = {
            "seconds": round(time.perf_counter() - t0, 2),
            "stored": sum(c for _, c in again),
        }
        with transaction(factory) as s:
            ids = [
                str(i) for i in s.execute(sa.select(ForecastPredictionRow.id).limit(200)).scalars()
            ]
            out["stored_predictions"] = int(
                s.execute(
                    sa.select(sa.func.count()).select_from(ForecastPredictionRow)
                ).scalar_one()
            )
        lat = []
        for pid in ids:
            t0 = time.perf_counter()
            svc.explain(pid)
            lat.append(time.perf_counter() - t0)
        out["explain_lookup_ms"] = {"p50": pct(lat, 0.5), "p95": pct(lat, 0.95), "n": len(lat)}
        t0 = time.perf_counter()
        drift = svc.drift(since_days=7)
        out["drift_report"] = {"seconds": round(time.perf_counter() - t0, 2), "models": len(drift)}
    finally:
        container.engine.dispose()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-out")
    ap.add_argument("--scale", type=float, default=0.5, help="synthetic corpus scale")
    ap.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    ap.add_argument("--active", type=int, default=1000)
    ap.add_argument("--dormant", type=int, default=10_000)
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--skip-db", action="store_true")
    ap.add_argument("--skip-memory", action="store_true")
    args = ap.parse_args()
    loaded = load_forecasting_config(ROOT / "config")
    dloaded = load_detection_config(ROOT / "config")
    report: dict[str, Any] = {
        "phase": 6,
        "hardware": bd.hardware(),
        "configuration": loaded.version_tag,
        "synthetic": True,
        "corpus_scale": args.scale,
        "workers": args.workers,
    }
    if not args.skip_memory:
        t0 = time.perf_counter()
        report["memory"] = memory_benchmarks(loaded, dloaded, args.scale, args.workers)
        report["memory"]["total_seconds"] = round(time.perf_counter() - t0, 1)
    if not args.skip_db:
        url = os.environ.get("TREND_AGENT_BENCH_DATABASE_URL", DEFAULT_URL)
        name = url.rsplit("/", 1)[-1]
        if "test" not in name and "bench" not in name:
            raise SystemExit(f"refusing to reset database {name!r}: use a *test*/*bench* database")
        t0 = time.perf_counter()
        report["database"] = database_benchmarks(
            url, loaded, args.active, args.dormant, args.days, 0.1, args.workers
        )
        report["database"]["total_seconds"] = round(time.perf_counter() - t0, 1)
    text = json.dumps(report, indent=2, default=str)
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
