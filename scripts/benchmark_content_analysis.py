"""Phase 5 content-analysis benchmarks (fixtures only; isolated database for the DB part).

In memory (no database, no network):
  * intent classification + query decomposition throughput over 1,000 synthetic queries;
  * keyword de-duplication of 1,000 queries with case/spacing variants;
  * end-to-end deterministic analysis latency (fixture result source, 25 results each);
  * queue planning over 1,000 candidates, and the daily quota arithmetic for 1,000
    keywords under the configured search budget.

Database (optional, ``--db``; refuses database names without "test"/"bench"):
  * evidence registration + idempotent persistence of N analyses (all Phase 5 tables).

    python scripts/benchmark_content_analysis.py --json-out bench.json
    TREND_AGENT_BENCH_DATABASE_URL=postgresql+psycopg://...trend_agent_test \\
        python scripts/benchmark_content_analysis.py --db --analyses 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trend_agent.content_analysis.config import (  # noqa: E402
    ANALYSIS_VERSION,
    load_content_config,
)
from trend_agent.content_analysis.demand import manual  # noqa: E402
from trend_agent.content_analysis.entity_verification import QueryEntities  # noqa: E402
from trend_agent.content_analysis.intent_classifier import classify  # noqa: E402
from trend_agent.content_analysis.orchestrator import (  # noqa: E402
    ContentAnalyzer,
    MemoryRegistrar,
)
from trend_agent.content_analysis.query_decomposition import decompose  # noqa: E402
from trend_agent.content_analysis.result_retrieval import FixtureResultSource  # noqa: E402
from trend_agent.content_analysis.scheduling import QueueItem, plan  # noqa: E402
from trend_agent.content_analysis.synthetic import inputs_for  # noqa: E402
from trend_agent.schemas.content_opportunity import DemandBasis  # noqa: E402
from trend_agent.utils.text import canonicalize_query  # noqa: E402
from trend_agent.verification.config import load_verification_config  # noqa: E402
from trend_agent.verification.world import load_world  # noqa: E402

CONFIG = ROOT / "config"
TEMPLATES = {
    "ai": ["how to install {e}", "{e} api tutorial", "{e} vs {f}", "{e} local setup",
           "{e} pricing", "what is {e}", "{e} agent tutorial", "{e} benchmark"],
    "technology": ["{e} review", "{e} not working fix", "how to migrate to {e}",
                   "{e} tutorial for beginners", "{e} battery life", "{e} setup guide"],
    "gaming": ["where to find the {e} season 8", "{e} best settings", "{e} event guide",
               "{e} tier list", "{e} challenge guide", "{e} crash fix"],
}  # fmt: skip
NAMES = ["Nimbus", "Aster", "Quill", "Borealis", "Cinder", "Helix", "Lumen", "Orchid",
         "Pylon", "Rook", "Sable", "Tessel", "Umbra", "Vesper", "Wren", "Zephyr"]  # fmt: skip
CATS = ["ai"] * 45 + ["technology"] * 25 + ["gaming"] * 30


def queries(n: int) -> list[tuple[str, str]]:
    out = []
    for i in range(n):
        cat = CATS[i % 100]
        t = TEMPLATES[cat][i % len(TEMPLATES[cat])]
        e = f"{NAMES[i % len(NAMES)]} {1 + i % 7}"
        f = f"{NAMES[(i + 3) % len(NAMES)]} {1 + (i + 1) % 7}"
        out.append((t.format(e=e, f=f), cat))
    return out


def pct(values: list[float], q: float) -> float:
    vals = sorted(values)
    return vals[min(len(vals) - 1, int(round(q * (len(vals) - 1))))]


def fixture_videos(query: str, n: int = 25) -> list[dict[str, Any]]:
    words = query.split()
    return [
        {
            "video_id": f"bench{abs(hash((query, i))) % 10**8:08d}",
            "title": " ".join(words[: 2 + i % 3]) + (" tutorial" if i % 2 else " news"),
            "description": f"{query} part {i}.",
            "channel_id": f"UC{i % 7}",
            "channel_title": f"Channel {i % 7}",
            "published_at": f"2026-{1 + i % 10:02d}-15T12:00:00+00:00",
            "duration_seconds": 120 + 60 * i,
        }
        for i in range(n)
    ]


def bench_memory(n_queries: int, n_analyses: int) -> dict[str, Any]:
    loaded = load_content_config(CONFIG)
    vloaded = load_verification_config(CONFIG)
    cfg = loaded.config
    qs = queries(n_queries)
    out: dict[str, Any] = {}
    # 1. classification + decomposition
    t0 = time.perf_counter()
    for q, cat in qs:
        intent = classify(q, QueryEntities(), cfg, category=cat)
        decompose(intent, QueryEntities())
    sec = time.perf_counter() - t0
    out["intent_and_decomposition"] = {
        "queries": n_queries,
        "seconds": round(sec, 4),
        "per_query_ms": round(1000 * sec / n_queries, 4),
    }
    # 2. de-duplication of near-duplicate keywords
    variants = []
    for i, (q, _) in enumerate(qs):
        variants.append(q)
        variants.append(q.upper() if i % 2 else f"  {q}  ".replace(" ", "  "))
    t0 = time.perf_counter()
    canon = {canonicalize_query(v) for v in variants}
    out["dedupe"] = {
        "inputs": len(variants),
        "distinct_original_queries": len({q for q, _ in qs}),
        "unique_after_canonicalisation": len(canon),
        "seconds": round(time.perf_counter() - t0, 4),
    }
    # 3. end-to-end deterministic analyses (in-memory registry, fixture results)
    world = load_world(CONFIG / "content_analysis" / "world.yaml", vloaded)
    base = inputs_for(world, "helios")
    lat: list[float] = []
    searches: list[int] = []
    for q, cat in qs[:n_analyses]:
        src = FixtureResultSource(
            {q: fixture_videos(q)}, collected_at=world.now - timedelta(hours=1)
        )
        analyzer = ContentAnalyzer(
            loaded,
            MemoryRegistrar(world.registry),
            verification_config=vloaded.config,
            source=src,
            clock=lambda: world.now,
        )
        inp = replace(
            base, query=q, category=cat, demand=manual(q, None, synthetic=True), target_id=None
        )
        t0 = time.perf_counter()
        outcome = asyncio.run(analyzer.analyse(inp))
        lat.append(time.perf_counter() - t0)
        searches.append(outcome.cost.search_requests)
    out["analysis_latency"] = {
        "analyses": len(lat),
        "results_per_analysis": 25,
        "p50_ms": round(1000 * statistics.median(lat), 2),
        "p95_ms": round(1000 * pct(lat, 0.95), 2),
        "max_ms": round(1000 * max(lat), 2),
        "mean_search_requests": round(statistics.mean(searches), 3),
        "note": "deterministic mode, fixture source: excludes network and database time",
    }
    # 4. queue planning and quota arithmetic for 1,000 keywords
    items = [
        QueueItem(key=f"q{i}", query=q, category=cat, basis=DemandBasis.DIRECT_SEARCH_CANDIDATE)
        for i, (q, cat) in enumerate(queries(1000))
    ]
    t0 = time.perf_counter()
    p = plan(
        items,
        cfg,
        now=world.now,
        analysis_version=ANALYSIS_VERSION,
        configuration_version=loaded.version_tag,
        search_requests_used=0,
        llm_tokens_used=0,
        llm_enabled=False,
        limit=cfg.scheduling.max_per_run,
    )
    per = statistics.mean(searches) if searches else 1.0
    per_day = cfg.scheduling.daily_search_requests / max(per, 1e-9)
    out["queue_for_1000_keywords"] = {
        "planning_seconds": round(time.perf_counter() - t0, 4),
        "selected_this_run": len(p.selected),
        "max_per_run": cfg.scheduling.max_per_run,
        "daily_search_requests_budget": cfg.scheduling.daily_search_requests,
        "estimated_analyses_per_day": round(per_day, 1),
        "days_to_analyse_all_once": round(1000 / per_day, 1),
        "youtube_quota_units_per_analysis": round(per * 100, 1),
        "note": (
            "search.list costs 100 units; 1,000 analyses need ~"
            f"{round(1000 * per * 100):,} units, far above the default 10,000/day project "
            "quota - the queue prioritises instead of analysing every keyword daily"
        ),
    }
    return out


def bench_db(n: int) -> dict[str, Any]:
    from pydantic import SecretStr

    from trend_agent.config import Settings
    from trend_agent.container import build_container
    from trend_agent.content_analysis.service import SqlRegistrar
    from trend_agent.db.repositories.content_analyses import ContentAnalysisRepository
    from trend_agent.db.session import transaction

    url = os.environ.get("TREND_AGENT_BENCH_DATABASE_URL", "")
    name = url.rsplit("/", 1)[-1]
    if "test" not in name and "bench" not in name:
        raise SystemExit("refusing: TREND_AGENT_BENCH_DATABASE_URL must name a test/bench DB")
    from alembic import command
    from alembic.config import Config

    acfg = Config(str(ROOT / "alembic.ini"))
    acfg.set_main_option("script_location", str(ROOT / "migrations"))
    acfg.set_main_option("sqlalchemy.url", url)
    command.downgrade(acfg, "base")
    command.upgrade(acfg, "head")
    settings = Settings(_env_file=None, database_url=SecretStr(url))  # type: ignore[call-arg]
    container = build_container(settings)
    loaded = load_content_config(CONFIG)
    vloaded = load_verification_config(CONFIG)
    world = load_world(CONFIG / "content_analysis" / "world.yaml", vloaded)
    signer = container.verification().signer
    lat_an: list[float] = []
    lat_save: list[float] = []
    lat_again: list[float] = []
    from trend_agent.content_analysis.orchestrator import inputs_fingerprint

    for q, cat in queries(n):
        reg = SqlRegistrar(container.session_factory, signer, vloaded, clock=lambda: world.now)
        src = FixtureResultSource(
            {q: fixture_videos(q)}, collected_at=world.now - timedelta(hours=1)
        )
        analyzer = ContentAnalyzer(
            loaded, reg, verification_config=vloaded.config, source=src, clock=lambda: world.now
        )
        inp = replace(
            inputs_for(world, "helios"),
            query=q,
            category=cat,
            demand=manual(q, None, synthetic=True),
            target_id=None,
            context_records=[],
        )
        t0 = time.perf_counter()
        outcome = asyncio.run(analyzer.analyse(inp))
        lat_an.append(time.perf_counter() - t0)
        fp = inputs_fingerprint(inp, loaded.version_tag)
        for bucket in (lat_save, lat_again):
            t0 = time.perf_counter()
            with transaction(container.session_factory) as s:
                ContentAnalysisRepository(s).save(
                    outcome, inp, inputs_fingerprint=fp, started_at=world.now,
                    completed_at=world.now, model_version="none",
                )  # fmt: skip
            bucket.append(time.perf_counter() - t0)
    container.engine.dispose()
    return {
        "analyses": n,
        "analysis_with_db_registration_p50_ms": round(1000 * statistics.median(lat_an), 2),
        "analysis_with_db_registration_p95_ms": round(1000 * pct(lat_an, 0.95), 2),
        "save_p50_ms": round(1000 * statistics.median(lat_save), 2),
        "save_p95_ms": round(1000 * pct(lat_save, 0.95), 2),
        "idempotent_resave_p50_ms": round(1000 * statistics.median(lat_again), 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", type=int, default=1000)
    ap.add_argument("--analyses", type=int, default=200)
    ap.add_argument("--db", action="store_true")
    ap.add_argument("--json-out", type=Path)
    args = ap.parse_args()
    result: dict[str, Any] = {
        "python": platform.python_version(),
        "machine": platform.machine(),
        "memory": bench_memory(args.queries, args.analyses),
    }
    if args.db:
        result["database"] = bench_db(min(args.analyses, 100))
    text = json.dumps(result, indent=2)
    print(text)
    if args.json_out:
        args.json_out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
