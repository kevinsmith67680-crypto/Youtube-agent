"""Phase 4 detection benchmarks (synthetic data; isolated database for the DB part).

In memory (no database):
  * single-series latency: incremental update with one new value on a 365-day series
    (state restored from its serialised form) and full recomputation of the 365 days;
  * baseline calculation cost per evaluated period (robust + seasonal);
  * batch throughput: 1,000 series x 1 new observation (incremental);
  * causal replay: fast evaluation of 1,000 series x 120 days (decisions and episodes)
    and step-wise replay through the Phase 3 replay engine (100 series x 60 days).

Database (RESET; refuses names without "test"/"bench"):
  * 1,000 active targets with 120 days of daily history + 10,000 dormant targets;
  * initial detection run over all history (state, results, episodes persisted);
  * daily incremental run: one new observation per series via the outbox queue;
  * retrieval latency per series, state persistence and episode-store operations.

    TREND_AGENT_BENCH_DATABASE_URL=postgresql+psycopg://...trend_agent_test \\
        python scripts/benchmark_detection.py --json-out bench.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from trend_agent.config import Settings  # noqa: E402
from trend_agent.container import build_container  # noqa: E402
from trend_agent.db.detection_models import AnomalyResultRow, SurgeEventRow  # noqa: E402
from trend_agent.db.models import SearchQueryRow  # noqa: E402
from trend_agent.db.monitoring_models import (  # noqa: E402
    DataPolicyRow,
    MonitoringTargetRow,
    ObservationRow,
    SeriesRow,
)
from trend_agent.db.repositories.detection_runs import DetectionQueueRepository  # noqa: E402
from trend_agent.db.repositories.detector_state import DetectorStateRepository  # noqa: E402
from trend_agent.db.repositories.time_series import TimeSeriesRepository  # noqa: E402
from trend_agent.db.session import transaction  # noqa: E402
from trend_agent.detection.config import load_detection_config  # noqa: E402
from trend_agent.detection.engine import EngineState, SeriesEngine  # noqa: E402
from trend_agent.detection.evaluation import build_world, replay_world  # noqa: E402
from trend_agent.detection.preprocessing import prepare  # noqa: E402
from trend_agent.detection.replay_adapter import DetectionReplayer  # noqa: E402
from trend_agent.detection.service import DetectionService  # noqa: E402
from trend_agent.detection.synthetic import (  # noqa: E402
    SyntheticSeriesSpec,
    benchmark_specs,
    generate,
    null_specs,
)
from trend_agent.discovery.models import Category  # noqa: E402
from trend_agent.monitoring.replay import replay  # noqa: E402
from trend_agent.schemas.detection import RunType  # noqa: E402
from trend_agent.schemas.enums import (  # noqa: E402
    DataNature,
    SampleQuality,
    ScaleType,
    SearchProperty,
    TimeGranularity,
)
from trend_agent.schemas.monitoring import (  # noqa: E402
    MeasurementCategory,
    MonitoringStatus,
    MonitoringTier,
    QualityStatus,
    RevisionKind,
)
from trend_agent.schemas.observation import NormalizationContext  # noqa: E402

DEFAULT_URL = "postgresql+psycopg://trend_agent:trend_agent_dev@127.0.0.1:5432/trend_agent_test"
DAY = timedelta(days=1)
START = datetime(2026, 1, 5, tzinfo=UTC)
PROVIDER = "bench_trends"


def hardware() -> dict[str, Any]:
    info: dict[str, Any] = {"python": platform.python_version(), "platform": platform.platform()}
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                info["cpu"] = line.split(":", 1)[1].strip()
                break
        info["cpus"] = os.cpu_count()
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal"):
                info["memory"] = line.split(":", 1)[1].strip()
    except OSError:
        pass
    return info


def timed(fn: Any, *args: Any, **kw: Any) -> tuple[Any, float]:
    t = time.perf_counter()
    out = fn(*args, **kw)
    return out, time.perf_counter() - t


# ----------------------------------------------------------------------------- memory
def memory_benchmarks(loaded: Any) -> dict[str, Any]:
    cfg = loaded.config
    out: dict[str, Any] = {}
    spec = SyntheticSeriesSpec(key="lat", seed=1, days=366, base=35, weekly=0.4)
    world = build_world([spec])
    pts = world.points[1]
    engine = SeriesEngine(loaded)
    head = engine.run(prepare(pts[:-1], cfg, already_known=True), None)
    payload = head.state.payloads()
    samples = []
    for _ in range(200):
        state = EngineState.from_payloads(payload)
        series = prepare(pts, cfg, already_known=True)
        t = time.perf_counter()
        engine.run(series, state)
        samples.append(time.perf_counter() - t)
    prep = []
    for _ in range(50):
        t = time.perf_counter()
        prepare(pts, cfg, already_known=True)
        prep.append(time.perf_counter() - t)
    full = [timed(engine.run, prepare(pts, cfg, already_known=True), None)[1] for _ in range(10)]
    out["single_series"] = {
        "history_days": 365,
        "incremental_update_ms_p50": round(statistics.median(samples) * 1000, 3),
        "incremental_update_ms_p95": round(sorted(samples)[int(0.95 * len(samples))] * 1000, 3),
        "preprocess_365_days_ms_p50": round(statistics.median(prep) * 1000, 3),
        "full_recompute_365_days_ms_p50": round(statistics.median(full) * 1000, 2),
        "per_period_full_recompute_us": round(statistics.median(full) / 366 * 1e6, 1),
    }
    from trend_agent.detection.baseline import BaselineModeler

    series = prepare(pts, cfg, already_known=True)
    assert series.family is not None
    modeler = BaselineModeler(cfg.baseline, series.granularity, series.family)
    idx = list(range(series.n_slots))
    _, secs = timed(modeler.estimates, series.values, series.first_slot, idx, {})
    out["baseline"] = {
        "periods": len(idx),
        "seconds_cold_cache": round(secs, 4),
        "us_per_period": round(secs / len(idx) * 1e6, 1),
        "note": "robust window statistics vectorised; seasonal profile refitted once per week",
    }
    # batch incremental: 1,000 series x 1 new observation
    specs = null_specs(1000, days=121)
    worlds = build_world(specs)
    states = {}
    for sid, p in worlds.points.items():
        states[sid] = engine.run(prepare(p[:-1], cfg, already_known=True), None).state.payloads()
    t = time.perf_counter()
    for sid, p in worlds.points.items():
        engine.run(prepare(p, cfg, already_known=True), EngineState.from_payloads(states[sid]))
    secs = time.perf_counter() - t
    out["batch_incremental"] = {
        "series": 1000,
        "history_days": 120,
        "seconds": round(secs, 2),
        "series_per_second": round(1000 / secs, 1),
        "note": "includes preprocessing (knowledge -> grid) and state restore per series",
    }
    # causal replay
    world_null = build_world(null_specs(1000, days=120))
    (run, _), secs = timed(replay_world, world_null, loaded, None)
    out["fast_causal_replay"] = {
        "series": 1000,
        "days": 120,
        "evaluations": len(run.decisions),
        "seconds": round(secs, 2),
        "evaluations_per_second": round(len(run.decisions) / secs, 1),
    }
    small = build_world(benchmark_specs("test", 100, days=60, eval_start=30))
    allpts = [p for pts_ in small.points.values() for p in pts_]
    rp = DetectionReplayer(loaded, small.metas, categories=small.categories)
    _, secs = timed(
        replay,
        allpts,
        start=START,
        end=START + DAY * 63,
        step=DAY,
        consumers=[rp.consumer()],
    )
    out["stepwise_replay"] = {
        "series": 100,
        "days": 60,
        "steps": rp.run.steps,
        "evaluations": len(rp.run.decisions),
        "seconds": round(secs, 2),
        "evaluations_per_second": round(len(rp.run.decisions) / secs, 1),
        "note": "re-prepares each series from its visible snapshot at every step",
    }
    return out


# ----------------------------------------------------------------------------- database
def reset_database(url: str) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["database_url"] = url
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


def seed(
    factory: Any, active: int, dormant: int, days: int
) -> tuple[dict[int, int], dict[int, list[float | None]]]:
    """Bulk-load targets, series and history (setup only; not the measured path)."""
    norm = NormalizationContext(
        scale_type=ScaleType.CONSISTENT_INDEX,
        method="bench",
        granularity=TimeGranularity.DAY,
        search_property=SearchProperty.YOUTUBE_SEARCH,
    )
    nkey = norm.key()
    cats = [Category.AI, Category.TECHNOLOGY, Category.GAMING]
    with transaction(factory) as s:
        s.add(
            DataPolicyRow(
                id="bench@1",
                policy_key="bench",
                version="1",
                description="benchmark",
                retention_days=None,
                storage_allowed=True,
                requires_authorization=False,
                authorized=True,
                permissions={},
                config_hash="x",
            )
        )
        s.flush()  # sessions do not autoflush; the policy must exist before observations
        s.execute(
            sa.insert(SearchQueryRow),
            [
                {
                    "canonical_query": f"bench query {i}",
                    "original_query": f"bench query {i}",
                    "language": "en",
                }
                for i in range(active + dormant)
            ],
        )
        qids = [
            r[0] for r in s.execute(sa.select(SearchQueryRow.id).order_by(SearchQueryRow.id)).all()
        ]
        s.execute(
            sa.insert(MonitoringTargetRow),
            [
                {
                    "query_id": q,
                    "category": cats[i % 3],
                    "region": "US",
                    "language": "en",
                    "measurement_category": MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST,
                    "is_proxy": False,
                    "preferred_provider": PROVIDER,
                    "provider_in_use": PROVIDER,
                    "tier": MonitoringTier.NORMAL if i < active else MonitoringTier.DORMANT,
                    "status": MonitoringStatus.ACTIVE if i < active else MonitoringStatus.DORMANT,
                    "status_reason": "benchmark",
                    "status_changed_at": START,
                    "priority_score": 60.0,
                    "config_version": "bench",
                    "is_synthetic": False,
                }
                for i, q in enumerate(qids)
            ],
        )
        targets = s.execute(
            sa.select(MonitoringTargetRow.id, MonitoringTargetRow.query_id).order_by(
                MonitoringTargetRow.id
            )
        ).all()
        active_targets = targets[:active]
        series_rows = [
            {
                "series_key": hashlib.sha256(f"bench|{tid}".encode()).hexdigest(),
                "target_id": tid,
                "query_id": qid,
                "provider_name": PROVIDER,
                "measurement_category": MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST,
                "is_proxy": False,
                "is_synthetic": False,
                "search_property": SearchProperty.YOUTUBE_SEARCH,
                "region": "US",
                "language": "en",
                "granularity": TimeGranularity.DAY,
                "scale_type": ScaleType.CONSISTENT_INDEX,
                "unit": "index",
                "normalization_key": nkey,
                "normalization_context": norm.model_dump(mode="json"),
            }
            for tid, qid in active_targets
        ]
        s.execute(sa.insert(SeriesRow), series_rows)
        sids = s.execute(
            sa.select(SeriesRow.id, SeriesRow.target_id, SeriesRow.query_id).order_by(SeriesRow.id)
        ).all()
        specs = benchmark_specs("test", active, days=days + 1, eval_start=days - 30)
        values = {sid: generate(sp).values for (sid, _, _), sp in zip(sids, specs, strict=True)}
        rows = []
        for sid, tid, qid in sids:
            for d in range(days):
                rows.append(_obs(sid, tid, qid, d, values[sid][d], nkey))
            if len(rows) >= 20_000:
                s.execute(sa.insert(ObservationRow), rows)
                rows = []
        if rows:
            s.execute(sa.insert(ObservationRow), rows)
    return {sid: tid for sid, tid, _ in sids}, values


def _obs(sid: int, tid: int, qid: int, d: int, v: float | None, nkey: str) -> dict[str, Any]:
    start = START + DAY * d
    published = start + DAY + timedelta(hours=36)
    return {
        "series_id": sid,
        "target_id": tid,
        "query_id": qid,
        "provider_name": PROVIDER,
        "provider_version": "1",
        "measurement_category": MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST,
        "is_proxy": False,
        "unit": "index",
        "dedup_key": hashlib.sha256(f"{sid}|{d}".encode()).hexdigest(),
        "period_start": start,
        "period_end": start + DAY,
        "granularity": TimeGranularity.DAY,
        "provider_published_at": published,
        "first_available_at": published,
        "collected_at": published,
        "ingested_at": published,
        "value": v,
        "scale_type": ScaleType.CONSISTENT_INDEX,
        "normalization_key": nkey,
        "region": "US",
        "language": "en",
        "search_property": SearchProperty.YOUTUBE_SEARCH,
        "data_nature": DataNature.ACTUAL,
        "sample_quality": SampleQuality.COMPLETE if v is not None else SampleQuality.MISSING,
        "quality_status": QualityStatus.VALID,
        "source_reference": f"bench://{sid}/{d}",
        "data_policy_id": "bench@1",
        "is_synthetic": False,
        "revision": 1,
        "revision_kind": RevisionKind.INITIAL,
        "is_current": True,
        "last_confirmed_at": published,
        "confirm_count": 1,
    }


def database_benchmarks(
    url: str, loaded: Any, active: int, dormant: int, days: int
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    reset_database(url)
    settings = Settings(
        _env_file=None,
        database_url=SecretStr(url),
        discovery_config_dir=str(ROOT / "config"),
    )
    container = build_container(settings)
    factory = container.session_factory
    (mapping, values), secs = timed(seed, factory, active, dormant, days)
    out["seed_seconds"] = round(secs, 2)
    clock_now = START + DAY * (days + 1) + timedelta(hours=12)

    class Clock:
        now = clock_now

        def __call__(self) -> datetime:
            return self.now

    clock = Clock()
    svc = DetectionService(
        session_factory=factory, loaded=loaded, clock=clock, include_synthetic=False
    )
    report, secs = timed(svc.run, run_type=RunType.LIVE)
    with factory() as s:
        results = s.execute(sa.select(sa.func.count()).select_from(AnomalyResultRow)).scalar_one()
        episodes = s.execute(sa.select(sa.func.count()).select_from(SurgeEventRow)).scalar_one()
    out["initial_run"] = {
        "series": report.series_evaluated,
        "observations": report.observations_evaluated,
        "seconds": round(secs, 2),
        "series_per_second": round(report.series_evaluated / secs, 1),
        "observations_per_second": round(report.observations_evaluated / secs, 1),
        "anomaly_results_rows": int(results),
        "episodes": int(episodes),
        "errors": len(report.errors),
    }
    # daily incremental: one new observation per series through the outbox
    nkey = NormalizationContext(
        scale_type=ScaleType.CONSISTENT_INDEX,
        method="bench",
        granularity=TimeGranularity.DAY,
        search_property=SearchProperty.YOUTUBE_SEARCH,
    ).key()
    with transaction(factory) as s:
        qmap: dict[int, int] = {
            int(a): int(b) for a, b in s.execute(sa.select(SeriesRow.id, SeriesRow.query_id)).all()
        }
        s.execute(
            sa.insert(ObservationRow),
            [
                _obs(sid, tid, qmap[sid], days, values[sid][days], nkey)
                for sid, tid in mapping.items()
            ],
        )
        DetectionQueueRepository(s).mark(
            [(sid, tid, START + DAY * days) for sid, tid in mapping.items()],
            clock_now,
            channel=None,
        )
    clock.now = clock_now + DAY
    report, secs = timed(svc.run, run_type=RunType.WORKER, from_queue=True, queue_limit=10_000)
    out["daily_incremental_run"] = {
        "series": report.series_evaluated,
        "observations": report.observations_evaluated,
        "seconds": round(secs, 2),
        "series_per_second": round(report.series_evaluated / secs, 1),
        "hypotheses": report.hypotheses,
        "notifications": report.notifications,
        "errors": len(report.errors),
    }
    # retrieval latency
    lat = []
    with factory() as s:
        ts = TimeSeriesRepository(s)
        for sid in list(mapping)[:200]:
            t = time.perf_counter()
            ts.points(series_ids=[sid], current_only=False, limit=1_000_000)
            lat.append(time.perf_counter() - t)
    lat.sort()
    out["retrieval"] = {
        "series": len(lat),
        "points_each": days + 1,
        "p50_ms": round(lat[len(lat) // 2] * 1000, 2),
        "p95_ms": round(lat[int(0.95 * len(lat))] * 1000, 2),
    }
    # state persistence (load + save with optimistic version check)
    sids = list(mapping)[:300]
    t = time.perf_counter()
    with transaction(factory) as s:
        repo = DetectorStateRepository(s)
        for sid in sids:
            stored = repo.load(sid)
            assert stored is not None
            repo.save(
                series_id=sid,
                target_id=mapping[sid],
                provider=PROVIDER,
                measurement=MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST,
                normalization_key=nkey,
                payloads=stored.payloads,
                expected_version=stored.version,
                last_observation_id=None,
                last_period_end=None,
                watermark=0,
                algorithm_version=loaded.config.algorithm_version,
                configuration_version=loaded.version_tag,
                is_synthetic=False,
                now=clock.now,
            )
    secs = time.perf_counter() - t
    out["state_persistence"] = {
        "series": len(sids),
        "ms_per_series_load_and_save": round(secs / len(sids) * 1000, 2),
    }
    # episode store operations
    from trend_agent.db.repositories.surge_events import SqlEpisodeStore
    from trend_agent.detection.events import Episode, TransitionRecord
    from trend_agent.schemas.detection import SupportStatus
    from trend_agent.schemas.evidence import AlertStage, SurgeClassification

    t = time.perf_counter()
    n = 300
    with transaction(factory) as s:
        store = SqlEpisodeStore(s)
        tids = list(mapping.values())[:n]
        qids: dict[int, int] = {
            int(a): int(b)
            for a, b in s.execute(
                sa.select(MonitoringTargetRow.id, MonitoringTargetRow.query_id)
            ).all()
        }
        for i, tid in enumerate(tids):
            key = hashlib.sha256(f"bench-ep|{i}".encode()).hexdigest()
            assert store.open_episode(key) is None
            ep = store.insert(
                Episode(
                    key=key, target_id=tid, series_id=None, query_id=qids[tid], keyword_id=None,
                    topic_cluster_id=None, category=Category.AI, provider=PROVIDER,
                    measurement=MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST, is_proxy=False,
                    classification=SurgeClassification.DIRECT_SEARCH_SURGE, region="US",
                    language="en",
                    status=AlertStage.CANDIDATE, confidence=SupportStatus.EMPIRICALLY_THRESHOLDED,
                    episode_start=clock.now, first_detected_at=clock.now, last_updated_at=clock.now,
                    last_observation_period_end=clock.now, severity=50.0, maximum_severity=50.0,
                    peak_value=50.0, peak_period_start=clock.now, peak_observation_id=None,
                    baseline_expected=20.0, baseline_dispersion=3.0, algorithm_version="bench",
                    configuration_version="bench", is_synthetic=False,
                )
            )  # fmt: skip
            store.transition(
                ep, TransitionRecord("created", None, "candidate", 50.0, True, "bench", clock.now)
            )
    secs = time.perf_counter() - t
    out["episode_creation"] = {
        "episodes": n,
        "ms_per_episode": round(secs / n * 1000, 2),
        "note": "advisory key lock + open-episode lookup + insert + transition",
    }
    container.engine.dispose()
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--active", type=int, default=1000)
    parser.add_argument("--dormant", type=int, default=10000)
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--skip-db", action="store_true")
    args = parser.parse_args()
    url = os.environ.get("TREND_AGENT_BENCH_DATABASE_URL", DEFAULT_URL)
    name = url.rsplit("/", 1)[-1]
    if not args.skip_db and "test" not in name and "bench" not in name:
        sys.exit(f"refusing to reset database {name!r}: name must contain 'test' or 'bench'")
    loaded = load_detection_config(ROOT / "config")
    result: dict[str, Any] = {
        "label": "SYNTHETIC benchmark data; measurements on this machine only",
        "hardware": hardware(),
        "config_version": loaded.version_tag,
    }
    t = time.perf_counter()
    result["memory"] = memory_benchmarks(loaded)
    if not args.skip_db:
        result["database"] = database_benchmarks(url, loaded, args.active, args.dormant, args.days)
        result["dataset"] = {
            "active_targets": args.active,
            "dormant_targets": args.dormant,
            "history_days": args.days,
            "observations": args.active * (args.days + 1),
        }
    result["total_seconds"] = round(time.perf_counter() - t, 1)
    text = json.dumps(result, indent=2, default=str)
    print(text)
    if args.json_out:
        args.json_out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
