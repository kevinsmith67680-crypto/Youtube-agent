"""Offline builders for Phase 5 unit tests (fictional world, fixture result source)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from tests.unit.discovery.helpers import CONFIG_DIR
from trend_agent.content_analysis.config import LoadedContentConfig, load_content_config
from trend_agent.content_analysis.demand import from_statistics, manual
from trend_agent.content_analysis.llm_client import AnalysisLLM
from trend_agent.content_analysis.orchestrator import AnalysisOutcome
from trend_agent.content_analysis.synthetic import _video, analyzer_for, inputs_for
from trend_agent.verification.config import load_verification_config
from trend_agent.verification.world import World, load_world

LOADED = load_content_config(CONFIG_DIR)
VLOADED = load_verification_config(CONFIG_DIR)
WORLD_PATH = CONFIG_DIR / "content_analysis" / "world.yaml"


def world() -> World:
    return load_world(WORLD_PATH, VLOADED)


def v(
    vid: str, title: str, desc: str = "", p: str = "2026-11-09", s: int = 600, **kw: Any
) -> dict[str, Any]:
    return {"id": vid, "t": title, "d": desc, "ch": kw.pop("ch", "Fictional"), "p": p, "s": s, **kw}


def analyse(
    target: str,
    videos: list[dict[str, Any]],
    *,
    query: str | None = None,
    category: str | None = None,
    llm: AnalysisLLM | None = None,
    loaded: LoadedContentConfig = LOADED,
    no_demand: bool = False,
    now: Any = None,
) -> AnalysisOutcome:
    w = world()
    inp = inputs_for(w, target, category=category)
    t = w.targets[target]
    q = query or t.query
    analyzer, _ = analyzer_for(w, loaded, VLOADED, [_video(x) for x in videos], q, llm=llm)
    demand = (
        manual(q, t.query_id, synthetic=True)
        if no_demand
        else from_statistics(inp.stats, query=q, query_id=t.query_id, category=t.category)
    )
    inp = replace(inp, query=q, demand=demand, now=now or inp.now)
    return asyncio.run(analyzer.analyse(inp))
