"""Phase 7 recommendation and dashboard benchmarks.

In memory (no database, no network):
  * concept generation latency per opportunity (deterministic template generator) and per
    concept, including verification and ranking;
  * the LLM path with a scripted client returning a fixed valid answer: the engine's own
    overhead (prompting, strict parsing, verification) and the prompt size. Token counts and
    costs for that path are ESTIMATES (characters / 4 at the configured list prices); no
    live LLM call is made;
  * API latency against the in-memory demo backend (FastAPI TestClient, auth on).

Database (isolated; refuses database names without "test"/"bench"; data is truncated):
  * 500 opportunities and 2,500 recommendations (synthetic clones of the demo records)
    persisted through the repository, then the idempotent re-save;
  * list/filter/sort/paginate latency of the database backend at that size.

Dashboard (optional, needs the built Next.js app running against the demo API):
  * initial page-load timings with Chromium (scripts/benchmark_dashboard.cjs).

    TREND_AGENT_BENCH_DATABASE_URL=postgresql+psycopg://...trend_agent_test \\
        python scripts/benchmark_recommendations.py --dashboard-url http://127.0.0.1:3000 \\
        --json-out docs/benchmarks/phase7_benchmark.json

All numbers are measured on the machine reported under "hardware"; they are not
estimates (except where labelled) and say nothing about recommendation quality.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trend_agent.recommendations.config import load_recommendation_config  # noqa: E402
from trend_agent.recommendations.evaluation import load_fixtures  # noqa: E402
from trend_agent.recommendations.idea_generator import (  # noqa: E402
    LLMConceptGenerator,
    build_prompt,
)
from trend_agent.recommendations.orchestrator import RecommendationEngine, cost_usd  # noqa: E402
from trend_agent.schemas.recommendation import CreatorProfile  # noqa: E402

CONFIG = ROOT / "config"


def stats(samples: list[float]) -> dict[str, float]:
    s = sorted(samples)
    return {
        "n": len(s),
        "mean_ms": round(statistics.fmean(s) * 1000, 2),
        "p50_ms": round(s[len(s) // 2] * 1000, 2),
        "p95_ms": round(s[min(len(s) - 1, int(len(s) * 0.95))] * 1000, 2),
        "max_ms": round(s[-1] * 1000, 2),
    }


def timed(fn: Callable[[], Any], n: int) -> dict[str, float]:
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t0)
    return stats(out)


# ----------------------------------------------------------------- generation
def bench_generation(repeats: int) -> dict[str, Any]:
    loaded = load_recommendation_config(CONFIG)
    fixtures = {k: v for k, v in load_fixtures(CONFIG).items() if v.evidence is not None}
    engine = RecommendationEngine(loaded)
    per_opp: list[float] = []
    concepts = 0
    for _ in range(repeats):
        for fx in fixtures.values():
            t0 = time.perf_counter()
            oc = engine.recommend(fx.report, [], fx.evidence, CreatorProfile())  # type: ignore[arg-type]
            per_opp.append(time.perf_counter() - t0)
            concepts += len(oc.recommendations)
    total = sum(per_opp)
    template = {
        "opportunities_per_run": len(fixtures),
        "per_opportunity": stats(per_opp),
        "per_concept_mean_ms": round(total / max(1, concepts) * 1000, 2),
        "llm_calls": 0,
        "tokens": 0,
        "cost_usd": 0.0,
    }

    # LLM path: a scripted client that always returns the template's own drafts as JSON
    from trend_agent.content_analysis.llm_client import LLMResult, LLMUsage
    from trend_agent.recommendations.idea_generator import ConceptSet

    class Scripted:
        name = "scripted"
        model = "scripted"

        def __init__(self) -> None:
            self.raw = ""
            self.prompt_chars = 0

        def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> LLMResult:
            self.prompt_chars = len(system) + len(prompt) + len(json.dumps(schema))
            return LLMResult("ok", self.raw, "scripted", LLMUsage(0, 0))

    llm_lat: list[float] = []
    prompt_chars: list[int] = []
    output_chars: list[int] = []
    per_opp_concepts: list[int] = []
    for fx in fixtures.values():
        ctx, _ = engine.context(fx.report, [], fx.evidence, CreatorProfile())  # type: ignore[arg-type]
        if ctx is None:
            continue
        formats = engine.formats(ctx)
        drafts = engine.template.generate(ctx, formats, len(formats)).concepts
        s = Scripted()
        s.raw = ConceptSet(concepts=drafts).model_dump_json()
        gen_engine = RecommendationEngine(loaded, generator=LLMConceptGenerator(s))
        for _ in range(repeats):
            t0 = time.perf_counter()
            oc = gen_engine.recommend(fx.report, [], fx.evidence, CreatorProfile())  # type: ignore[arg-type]
            llm_lat.append(time.perf_counter() - t0)
        system, prompt = build_prompt(ctx, formats, len(formats))
        prompt_chars.append(s.prompt_chars or len(system) + len(prompt))
        output_chars.append(len(s.raw))
        per_opp_concepts.append(max(1, len(oc.recommendations)))
    from trend_agent.recommendations.idea_generator import Usage

    est_in = statistics.fmean(prompt_chars) / 4
    est_out = statistics.fmean(output_chars) / 4
    est_cost = cost_usd(Usage(1, int(est_in), int(est_out)), loaded)
    per_concept = statistics.fmean(per_opp_concepts)
    llm = loaded.config.llm
    return {
        "template_generator": template,
        "llm_path_engine_overhead": {
            "note": "scripted client: excludes model latency; measures prompting, strict "
            "parsing, verification and ranking",
            "per_opportunity": stats(llm_lat),
        },
        "llm_token_and_cost_ESTIMATE": {
            "method": "characters / 4 of the actual prompt (system + request + schema) and of "
            "a schema-valid answer of the same size as the template's output; no live call",
            "model": llm.model,
            "usd_per_million_input_tokens": llm.usd_per_million_input_tokens,
            "usd_per_million_output_tokens": llm.usd_per_million_output_tokens,
            "estimated_input_tokens_per_call": round(est_in),
            "estimated_output_tokens_per_call": round(est_out),
            "estimated_cost_usd_per_opportunity": est_cost,
            "estimated_cost_usd_per_recommendation": round(est_cost / per_concept, 6)
            if est_cost is not None
            else None,
            "max_calls_per_opportunity": llm.max_attempts,
            "daily_token_budget": llm.daily_token_budget,
            "llm_enabled_by_default": llm.enabled,
        },
    }


# ------------------------------------------------------------------------ API
def bench_api(requests: int, cache: Path | None) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    from trend_agent.api.app import create_app
    from trend_agent.config import Settings
    from trend_agent.recommendations.demo import demo_backend

    t0 = time.perf_counter()
    backend = demo_backend(CONFIG, cache)
    build = time.perf_counter() - t0
    # every request opens a new rate-limit window: this measures latency, not throttling
    tick = itertools.count()
    token = "bench-token-0123456789abcdef0123"  # noqa: S105  (local benchmark only)
    client = TestClient(
        create_app(
            backend, Settings(_env_file=None, api_token=token), clock=lambda: next(tick) * 61.0
        )
    )
    h = {"Authorization": f"Bearer {token}"}
    rec = client.get("/api/recommendations?sort=rank", headers=h).json()["items"][0]["id"]
    opp = client.get("/api/opportunities", headers=h).json()["items"][0]["id"]
    trend = client.get("/api/trends", headers=h).json()["items"][0]["id"]
    paths = {
        "overview": "/api/overview",
        "opportunities_filtered": "/api/opportunities?category=gaming&sort=breakout&order=desc",
        "opportunity_detail": f"/api/opportunities/{opp}",
        "recommendations_ranked": "/api/recommendations?sort=rank&page_size=25",
        "recommendations_search": "/api/recommendations?q=install&include_research=true",
        "recommendation_detail": f"/api/recommendations/{rec}",
        "trend_detail": f"/api/trends/{trend}",
        "gaps": "/api/gaps?strength=strong",
        "system_status": "/api/system/status",
    }
    out: dict[str, Any] = {"backend": "demo (in memory)", "snapshot_load_seconds": round(build, 2)}
    for name, path in paths.items():
        out[name] = timed(lambda p=path: client.get(p, headers=h).raise_for_status(), requests)  # type: ignore[misc]
    return out


# ------------------------------------------------------------------- database
def bench_database(url: str, cache: Path | None) -> dict[str, Any]:
    import sqlalchemy as sa
    from pydantic import SecretStr

    from trend_agent.config import Settings
    from trend_agent.container import build_container
    from trend_agent.db.content_models import ContentOpportunityRow
    from trend_agent.db.repositories.recommendations import RecommendationRepository
    from trend_agent.db.session import transaction
    from trend_agent.recommendations.demo import load_or_build

    name = url.rsplit("/", 1)[-1]
    if "test" not in name and "bench" not in name:
        raise SystemExit(f"refusing to reset database {name!r} (name needs 'test' or 'bench')")
    settings = Settings(_env_file=None, database_url=SecretStr(url))
    c = build_container(settings)
    tables = [
        "notification_deliveries", "notifications", "production_tracking",
        "recommendation_feedback", "saved_recommendations", "recommendation_scores",
        "recommendation_outlines", "recommendation_hooks", "video_recommendations",
        "recommendation_generation_runs", "creator_profiles", "content_opportunities",
    ]  # fmt: skip
    with c.engine.begin() as conn:
        conn.execute(sa.text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    snap = load_or_build(CONFIG, cache)
    n_opp, per_opp = 500, 5
    reports = snap.reports
    recs = [r for r in snap.recommendations if not r.research_hypothesis]
    now = snap.now
    t0 = time.perf_counter()
    rec_docs = []
    with transaction(c.session_factory) as s:
        for i in range(n_opp):
            base = reports[i % len(reports)]
            key = uuid.uuid5(uuid.NAMESPACE_URL, f"bench-opp-{i}")
            rep = base.model_copy(
                update={
                    "opportunity_id": str(key),
                    "canonical_query": f"{base.canonical_query} {i}",
                    "category": ("ai", "technology", "gaming")[i % 3],
                }
            )
            s.add(
                ContentOpportunityRow(
                    opportunity_key=key, canonical_query=rep.canonical_query,
                    category=rep.category, region=rep.region, language=rep.language,
                    score=(i * 37) % 100, score_version="bench", status=rep.status.value,
                    evidence_quality=rep.evidence_quality.value if rep.evidence_quality else None,
                    verification_state=rep.verification_state, demand_basis=rep.demand.basis.value,
                    report=rep.model_dump(mode="json"), is_synthetic=True, updated_at=now,
                )
            )  # fmt: skip
            for j in range(per_opp):
                src = recs[(i * per_opp + j) % len(recs)]
                rid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"bench-rec-{i}-{j}"))
                rec_docs.append(
                    src.model_copy(
                        update={
                            "recommendation_id": rid,
                            "opportunity_id": str(key),
                            "title": f"{src.title} #{i}-{j}",
                            "category": rep.category,
                            "ranking": src.ranking.model_copy(update={"rank": i * per_opp + j + 1}),
                        }
                    )
                )
    seed_opps = time.perf_counter() - t0
    t0 = time.perf_counter()
    with transaction(c.session_factory) as s:
        repo = RecommendationRepository(s)
        for r in rec_docs:
            repo.save(r, run_id=None, opportunity_row_id=None, opportunity_score=50.0,
                      expires_at=None, now=now)  # fmt: skip
    insert = time.perf_counter() - t0
    t0 = time.perf_counter()
    with transaction(c.session_factory) as s:
        repo = RecommendationRepository(s)
        created = sum(
            repo.save(r, run_id=None, opportunity_row_id=None, opportunity_score=50.0,
                      expires_at=None, now=now)
            for r in rec_docs
        )  # fmt: skip
    resave = time.perf_counter() - t0
    backend = c.dashboard_backend(include_synthetic=True)
    queries: dict[str, Callable[[], Any]] = {
        "recommendations_rank_page1": lambda: backend.recommendations(sort="rank", page_size=25),
        "recommendations_rank_page40": lambda: backend.recommendations(
            sort="rank", page=40, page_size=25
        ),
        "recommendations_category_score": lambda: backend.recommendations(
            category="gaming", sort="score", page_size=25
        ),
        "recommendations_text_search": lambda: backend.recommendations(q="install", page_size=25),
        "recommendations_filters_combined": lambda: backend.recommendations(
            category="ai", status="APPROVED", min_score=50, include_research=True, page_size=25
        ),
        "opportunities_score_page1": lambda: backend.opportunities(sort="score", page_size=25),
        "opportunities_filtered": lambda: backend.opportunities(
            category="technology", min_score=40, sort="updated", page_size=25
        ),
        "opportunities_sort_breakout_in_memory": lambda: backend.opportunities(
            sort="breakout", page_size=25
        ),
        "recommendation_detail": lambda: backend.recommendation(rec_docs[1234].recommendation_id),
        "overview": backend.overview,
    }
    out: dict[str, Any] = {
        "opportunities": n_opp,
        "recommendations": len(rec_docs),
        "seed_opportunities_seconds": round(seed_opps, 2),
        "insert_recommendations_seconds": round(insert, 2),
        "insert_per_recommendation_ms": round(insert / len(rec_docs) * 1000, 2),
        "idempotent_resave_seconds": round(resave, 2),
        "idempotent_resave_created": int(created),
    }
    for name_, fn in queries.items():
        fn()  # warm
        out[name_] = timed(fn, 20)
    with c.engine.begin() as conn:
        conn.execute(sa.text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    c.engine.dispose()
    return out


# ------------------------------------------------------------------ dashboard
def bench_dashboard(url: str) -> dict[str, Any]:
    node = shutil.which("node") or "/opt/node22/bin/node"
    script = ROOT / "scripts" / "benchmark_dashboard.cjs"
    res = subprocess.run(
        [node, str(script), url], capture_output=True, text=True, timeout=600, check=False
    )
    if res.returncode != 0:
        return {"error": res.stderr.strip()[-500:]}
    data: dict[str, Any] = json.loads(res.stdout)
    return data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=20)
    ap.add_argument("--api-requests", type=int, default=50)
    ap.add_argument("--demo-cache", type=Path, default=None)
    ap.add_argument("--dashboard-url", default=None)
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args()
    report: dict[str, Any] = {
        "hardware": {
            "machine": platform.machine(),
            "processor": platform.processor() or "unknown",
            "cpus": os.cpu_count(),
            "python": platform.python_version(),
            "system": platform.system(),
        },
        "generation": bench_generation(args.repeats),
        "api": bench_api(args.api_requests, args.demo_cache),
    }
    url = os.environ.get("TREND_AGENT_BENCH_DATABASE_URL")
    report["database"] = (
        bench_database(url, args.demo_cache)
        if url
        else {"skipped": "set TREND_AGENT_BENCH_DATABASE_URL"}
    )
    report["dashboard"] = (
        bench_dashboard(args.dashboard_url)
        if args.dashboard_url
        else {"skipped": "no --dashboard-url"}
    )
    text = json.dumps(report, indent=2, default=str)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text + "\n", "utf-8")
    print(text)


if __name__ == "__main__":
    main()
