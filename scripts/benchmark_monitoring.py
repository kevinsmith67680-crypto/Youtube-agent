"""Phase 3 monitoring benchmarks (synthetic data, isolated database).

Measures target selection, planning, job claiming (single and concurrent), historical
range retrieval, ingestion throughput through the real write path, executor throughput
(collect + validate + write via the Phase 1 mock provider) and replay throughput, for an
engineering-capacity dataset: 1,000 active and 10,000 dormant targets.

The database is RESET (downgrade/upgrade): the script refuses to run unless the database
name contains "test" or "bench".

    TREND_AGENT_BENCH_DATABASE_URL=postgresql+psycopg://...trend_agent_test \
        python scripts/benchmark_monitoring.py --json-out bench.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from trend_agent.config import Settings  # noqa: E402
from trend_agent.container import build_container  # noqa: E402
from trend_agent.db.discovery_models import (  # noqa: E402
    DiscoveredKeywordRow,
    IntentClusterRow,
    TopicClusterRow,
)
from trend_agent.db.models import SearchQueryRow  # noqa: E402
from trend_agent.db.monitoring_models import MonitoringJobRow, MonitoringTargetRow  # noqa: E402
from trend_agent.db.repositories.collection_jobs import JobRepository  # noqa: E402
from trend_agent.db.repositories.time_series import TimeSeriesRepository  # noqa: E402
from trend_agent.db.session import transaction  # noqa: E402
from trend_agent.discovery.models import (  # noqa: E402
    CandidateStatus,
    Category,
    DiscoveryMethod,
    KeywordOrigin,
    NoveltyKind,
    SearchIntent,
)
from trend_agent.monitoring.replay import replay  # noqa: E402
from trend_agent.schemas.enums import (  # noqa: E402
    DataNature,
    SampleQuality,
    ScaleType,
    SearchProperty,
    TimeGranularity,
)
from trend_agent.schemas.monitoring import (  # noqa: E402
    JobStatus,
    JobType,
    MeasurementCategory,
    MonitoringStatus,
    MonitoringTier,
)
from trend_agent.schemas.observation import NormalizationContext  # noqa: E402
from trend_agent.schemas.scheduling import RevisionBehaviour  # noqa: E402
from trend_agent.schemas.time_series import ObservationDraft  # noqa: E402

DEFAULT_URL = "postgresql+psycopg://trend_agent:trend_agent_dev@127.0.0.1:5432/trend_agent_test"
NOW = datetime(2026, 6, 1, 12, tzinfo=UTC)
CATS = [Category.AI, Category.TECHNOLOGY, Category.GAMING]


def hardware() -> dict[str, Any]:
    info: dict[str, Any] = {"python": platform.python_version(), "platform": platform.platform()}
    try:
        exe = shutil.which("lscpu")
        lscpu = (
            subprocess.run([exe], capture_output=True, text=True, check=False).stdout
            if exe
            else ""
        )
        for line in lscpu.splitlines():
            if line.startswith(("Model name", "CPU(s):", "Thread(s) per core")):
                key, _, value = line.partition(":")
                info[key.strip()] = value.strip()
    except OSError:
        pass
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            info["MemTotal"] = handle.readline().split(":")[1].strip()
    except OSError:
        pass
    return info


def timed(fn: Any) -> tuple[Any, float]:
    start = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - start


def seed(factory: Any, active: int, dormant: int) -> list[int]:
    """Bulk-insert keywords + targets (active ones due now). Returns active target ids."""
    total = active + dormant
    with transaction(factory) as s:
        q_ids = (
            s.execute(
                sa.insert(SearchQueryRow).returning(SearchQueryRow.id),
                [
                    {
                        "canonical_query": f"bench query {i}",
                        "original_query": f"bench query {i}",
                        "language": "en",
                        "region": None,
                    }
                    for i in range(total)
                ],
            )
            .scalars()
            .all()
        )
        topic_ids = (
            s.execute(
                sa.insert(TopicClusterRow).returning(TopicClusterRow.id),
                [
                    {
                        "topic_key": f"bench:{i}",
                        "canonical_topic": f"bench {i}",
                        "category": CATS[i % 3],
                    }
                    for i in range(total)
                ],
            )
            .scalars()
            .all()
        )
        intent_ids = (
            s.execute(
                sa.insert(IntentClusterRow).returning(IntentClusterRow.id),
                [
                    {
                        "topic_cluster_id": topic_ids[i],
                        "cluster_key": "other:_",
                        "canonical_intent": SearchIntent.OTHER,
                        "representative_query": f"bench {i}",
                    }
                    for i in range(total)
                ],
            )
            .scalars()
            .all()
        )
        kw_ids = (
            s.execute(
                sa.insert(DiscoveredKeywordRow).returning(DiscoveredKeywordRow.id),
                [
                    {
                        "query_id": q_ids[i],
                        "entity_id": None,
                        "topic_cluster_id": topic_ids[i],
                        "intent_cluster_id": intent_ids[i],
                        "origin": KeywordOrigin.GENERATED_HYPOTHESIS,
                        "discovery_type": DiscoveryMethod.SEED_TERM,
                        "template_id": None,
                        "primary_category": CATS[i % 3],
                        "category_scores": {CATS[i % 3].value: 1.0},
                        "primary_intent": SearchIntent.OTHER,
                        "intent_scores": {"other": 1.0},
                        "candidate_status": CandidateStatus.MONITORING
                        if i < active
                        else CandidateStatus.VALIDATED,  # dormant: low-priority keywords
                        "status_reason": "bench",
                        "first_discovered_at": NOW,
                        "last_discovered_at": NOW,
                        "novelty_score": 50.0,
                        "novelty_kind": NoveltyKind.RECURRING,
                        "novelty_components": {},
                        "priority_score": 60.0 + (i % 40) if i < active else 30.0,
                        "scoring_version": "bench",
                        "score_components": {"independent_origins": 2},
                        "topic_assignment": {},
                        "intent_assignment": {},
                        "grounding": [],
                        "is_synthetic": True,
                    }
                    for i in range(total)
                ],
            )
            .scalars()
            .all()
        )
        target_ids = (
            s.execute(
                sa.insert(MonitoringTargetRow).returning(MonitoringTargetRow.id),
                [
                    {
                        "keyword_id": kw_ids[i],
                        "query_id": q_ids[i],
                        "category": CATS[i % 3],
                        "topic_cluster_id": topic_ids[i],
                        "intent_cluster_id": intent_ids[i],
                        "region": "US",
                        "language": "en",
                        "measurement_category": MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST,
                        "is_proxy": False,
                        "preferred_provider": "mock",
                        "fallback_providers": [],
                        "provider_in_use": "mock",
                        "tier": MonitoringTier.NORMAL if i < active else MonitoringTier.DORMANT,
                        "tier_reason": "bench",
                        "tier_changed_at": NOW - timedelta(days=30),
                        "status": MonitoringStatus.ACTIVE
                        if i < active
                        else MonitoringStatus.DORMANT,
                        "status_reason": "bench",
                        "status_changed_at": NOW - timedelta(days=30),
                        "priority_score": 60.0 + (i % 40) if i < active else 30.0,
                        "novelty_kind": "recurring",
                        "activated_at": NOW - timedelta(days=30),
                        "next_collection_at": NOW,
                        "config_version": "bench",
                        "is_synthetic": True,
                    }
                    for i in range(total)
                ],
            )
            .scalars()
            .all()
        )
    return list(target_ids[:active])


def drafts(query_id: int, target_id: int, days: int) -> list[ObservationDraft]:
    ctx = NormalizationContext(scale_type=ScaleType.CONSISTENT_INDEX, method="bench")
    out = []
    for d in range(days):
        start = NOW - timedelta(days=days - d + 2)
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        out.append(
            ObservationDraft(
                query_id=query_id,
                target_id=target_id,
                provider_name="bench",
                provider_version="1",
                measurement_category=MeasurementCategory.SYNTHETIC_TEST_OBSERVATION,
                simulated_category=MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST,
                unit="index",
                period_start=start,
                period_end=start + timedelta(days=1),
                granularity=TimeGranularity.DAY,
                provider_published_at=start + timedelta(days=2),
                collected_at=NOW - timedelta(hours=1),
                value=float(40 + d % 20),
                scale_type=ScaleType.CONSISTENT_INDEX,
                normalization_context=ctx,
                region="US",
                language="en",
                search_property=SearchProperty.YOUTUBE_SEARCH,
                data_nature=DataNature.SYNTHETIC,
                sample_quality=SampleQuality.COMPLETE,
                source_reference="synthetic://bench",
                data_policy_id="synthetic-test-data@1",
            )
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--active", type=int, default=1000)
    parser.add_argument("--dormant", type=int, default=10_000)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    url = os.environ.get("TREND_AGENT_BENCH_DATABASE_URL", DEFAULT_URL)
    dbname = url.rsplit("/", 1)[-1]
    if "test" not in dbname and "bench" not in dbname:
        print(f"refusing to reset database {dbname!r}: name must contain 'test' or 'bench'")
        return 2
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["database_url"] = url
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    clock = [NOW]
    container = build_container(
        Settings(_env_file=None, database_url=url, discovery_config_dir=str(ROOT / "config")),
        clock=lambda: clock[0],
    )
    svc = container.monitoring(clock=lambda: clock[0], include_synthetic=True)
    factory = svc.session_factory
    results: dict[str, Any] = {
        "dataset": {
            "active_targets": args.active,
            "dormant_targets": args.dormant,
            "history_days": args.days,
        },
        "hardware": hardware(),
    }
    with factory() as s:
        results["hardware"]["postgres"] = s.execute(sa.text("SHOW server_version")).scalar_one()

    active_ids, t = timed(lambda: seed(factory, args.active, args.dormant))
    results["seed_seconds"] = round(t, 2)

    # 1. Ingestion throughput through the real (idempotent, revision-aware) write path.
    with transaction(factory) as s:
        q_by_target = dict(
            s.execute(
                sa.select(MonitoringTargetRow.id, MonitoringTargetRow.query_id).where(
                    MonitoringTargetRow.id.in_(active_ids)
                )
            ).all()
        )
    written = 0
    start = time.perf_counter()
    for tid in active_ids:
        with transaction(factory) as s:
            ts = TimeSeriesRepository(s)
            for d in drafts(q_by_target[tid], tid, args.days):
                ts.write(d, revision_behaviour=RevisionBehaviour.REVISABLE, ingested_at=NOW)
                written += 1
    ingest = time.perf_counter() - start
    results["ingestion"] = {
        "observations": written,
        "seconds": round(ingest, 2),
        "observations_per_second": round(written / ingest, 1),
    }
    start = time.perf_counter()
    with transaction(factory) as s:  # re-write the same data: all unchanged (idempotent)
        ts = TimeSeriesRepository(s)
        for d in drafts(q_by_target[active_ids[0]], active_ids[0], args.days):
            ts.write(d, revision_behaviour=RevisionBehaviour.REVISABLE, ingested_at=NOW)
    results["ingestion"]["idempotent_rewrite_ms_per_obs"] = round(
        (time.perf_counter() - start) * 1000 / args.days, 3
    )

    # 2. Target selection (full sync over all keywords and targets).
    report, t = timed(svc.sync_targets)
    results["target_selection"] = {
        "seconds": round(t, 2),
        "candidates": report.candidates_seen,
        "targets_evaluated": args.active + args.dormant,
    }

    # 3. Planning (1,000 due targets, capacity 200), dry run and real.
    with transaction(factory) as s:
        s.execute(
            sa.update(MonitoringTargetRow)
            .where(MonitoringTargetRow.id.in_(active_ids))
            .values(
                next_collection_at=NOW,
                latest_period_end=None,
                last_attempt_at=None,
                status=MonitoringStatus.ACTIVE,
            )
        )
    dry, t_dry = timed(lambda: svc.plan(dry_run=True))
    plan, t_plan = timed(svc.plan)
    results["planning"] = {
        "due_targets": dry.due_targets,
        "dry_run_seconds": round(t_dry, 2),
        "seconds": round(t_plan, 2),
        "jobs_created": plan.jobs_created,
        "categories": {k: v.used for k, v in plan.categories.items()},
    }

    # 4. Job claiming: 2,000 jobs, single worker, then 4 concurrent workers.
    def enqueue(n: int, prefix: str) -> None:
        with transaction(factory) as s:
            s.execute(sa.delete(MonitoringJobRow))
            jobs = JobRepository(s)
            for i in range(n):
                jobs.enqueue(
                    {
                        "idempotency_key": f"{prefix}:{i}",
                        "target_id": active_ids[i % len(active_ids)],
                        "provider_name": "mock",
                        "job_type": JobType.MANUAL_COLLECTION,
                        "category": CATS[i % 3],
                        "scheduled_at": NOW,
                        "priority": i % 100,
                        "max_attempts": 3,
                        "created_at": NOW,
                        "updated_at": NOW,
                    }
                )

    def drain(worker: str, out: list[int]) -> None:
        n = 0
        while True:
            with transaction(factory) as s:
                rows = JobRepository(s).claim(
                    worker=worker, now=NOW, lease=timedelta(minutes=5), limit=50
                )
            if not rows:
                break
            n += len(rows)
        out.append(n)

    enqueue(2000, "a")
    counts: list[int] = []
    _, t1 = timed(lambda: drain("solo", counts))
    enqueue(2000, "b")
    counts4: list[int] = []
    threads = [threading.Thread(target=drain, args=(f"w{i}", counts4)) for i in range(4)]
    start = time.perf_counter()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    t4 = time.perf_counter() - start
    results["claiming"] = {
        "jobs": 2000,
        "batch": 50,
        "single_worker_jobs_per_second": round(counts[0] / t1, 1),
        "four_workers_jobs_per_second": round(sum(counts4) / t4, 1),
        "four_worker_split": counts4,
        "duplicates": sum(counts4) - 2000,
    }

    # 5. Historical range retrieval (90-day window, 100 targets).
    latencies = []
    with transaction(factory) as s:
        ts = TimeSeriesRepository(s)
        sample = active_ids[:100]
        for tid in sample:
            start = time.perf_counter()
            pts = ts.points(target_id=tid, start=NOW - timedelta(days=90), end=NOW)
            latencies.append((time.perf_counter() - start) * 1000)
    results["range_query"] = {
        "targets": len(sample),
        "points_each": len(pts),
        "p50_ms": round(statistics.median(latencies), 2),
        "p95_ms": round(sorted(latencies)[int(0.95 * (len(latencies) - 1))], 2),
    }

    # 6. Executor end-to-end (Phase 1 mock: request, validation, write) on 200 targets.
    with transaction(factory) as s:
        s.execute(sa.delete(MonitoringJobRow))
        s.execute(
            sa.update(MonitoringTargetRow)
            .where(MonitoringTargetRow.id.in_(active_ids[:200]))
            .values(next_collection_at=NOW, latest_period_end=None, last_attempt_at=None)
        )
    cfg_small = svc.config.model_copy(
        update={"capacity": svc.config.capacity.model_copy(update={"max_jobs_per_cycle": 200})}
    )
    svc.planner._config = cfg_small
    svc.plan()
    start = time.perf_counter()
    reports = asyncio.run(svc.executor("bench").drain())
    t_exec = time.perf_counter() - start
    executed = sum(r.claimed for r in reports)
    new = sum(r.totals().get("new", 0) for r in reports)
    statuses: dict[str, int] = {}
    for r in reports:
        for k, v in r.by_status().items():
            statuses[k] = statuses.get(k, 0) + v
    results["executor"] = {
        "jobs": executed,
        "seconds": round(t_exec, 2),
        "jobs_per_second": round(executed / t_exec, 1) if t_exec else None,
        "new_observations": new,
        "statuses": statuses,
    }

    # 7. Replay throughput.
    with transaction(factory) as s:
        pts = TimeSeriesRepository(s).points(target_id=None, provider_name="bench", limit=30_000)
    start = time.perf_counter()
    rep = replay(pts, start=NOW - timedelta(days=30), end=NOW, step=timedelta(days=1))
    t_rep = time.perf_counter() - start
    results["replay"] = {
        "events": rep.events,
        "steps": len(rep.steps),
        "seconds": round(t_rep, 2),
        "events_per_second": round(rep.events / t_rep, 1),
    }
    with transaction(factory) as s:
        results["dataset"]["observations_stored"] = TimeSeriesRepository(s).count()
        results["dataset"]["jobs_status"] = JobRepository(s).counts_by_status()
    container.engine.dispose()
    _ = JobStatus
    text = json.dumps(results, indent=2, default=str)
    print(text)
    if args.json_out:
        args.json_out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
