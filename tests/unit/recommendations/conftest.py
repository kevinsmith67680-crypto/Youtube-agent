"""Phase 7 fixtures: the synthetic demo snapshot is built once per test session (about a
minute: Phase 5 scenario analyses plus Phase 6 synthetic model training) and every test
gets a fresh in-memory backend over it."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import ROOT
from trend_agent.recommendations.config import (
    LoadedRecommendationConfig,
    load_recommendation_config,
)
from trend_agent.recommendations.demo import DemoBackend, DemoSnapshot, load_or_build

CONFIG = ROOT / "config"
# optional reuse across sessions while developing (the snapshot key still has to match)
_CACHE = os.environ.get("TREND_AGENT_TEST_DEMO_CACHE")


@pytest.fixture(scope="session")
def rec_loaded() -> LoadedRecommendationConfig:
    return load_recommendation_config(CONFIG)


@pytest.fixture(scope="session")
def demo_snapshot(tmp_path_factory: pytest.TempPathFactory) -> DemoSnapshot:
    cache = Path(_CACHE) if _CACHE else tmp_path_factory.mktemp("demo") / "snapshot.json"
    return load_or_build(CONFIG, cache)


@pytest.fixture
def demo(demo_snapshot: DemoSnapshot, rec_loaded: LoadedRecommendationConfig) -> DemoBackend:
    return DemoBackend(demo_snapshot, rec_loaded)


@pytest.fixture
def make_demo(
    demo_snapshot: DemoSnapshot, rec_loaded: LoadedRecommendationConfig
) -> Callable[[], DemoBackend]:
    return lambda: DemoBackend(demo_snapshot, rec_loaded)


# ------------------------------------------------------------ scenario contexts
@dataclass
class Scenario:
    key: str
    report: Any
    evidence: Any
    inputs: Any


@pytest.fixture(scope="session")
def scenarios(rec_loaded: LoadedRecommendationConfig) -> dict[str, Scenario]:
    """Phase 5 synthetic scenarios analysed once (fast, no forecasting)."""
    from trend_agent.content_analysis import synthetic as ca_syn
    from trend_agent.content_analysis.config import load_content_config
    from trend_agent.recommendations.context_builder import EvidenceContext
    from trend_agent.verification.config import load_verification_config
    from trend_agent.verification.world import load_world

    lc, vc = load_content_config(CONFIG), load_verification_config(CONFIG)
    world = load_world(CONFIG / "content_analysis" / "world.yaml", vc)
    scen = {s["id"]: s for s in ca_syn.load_scenarios(CONFIG)}
    out: dict[str, Scenario] = {}
    for key in ("A", "B", "C", "G", "I", "J"):
        s = scen[key]
        target = world.targets[s["target"]]
        analyzer, _ = ca_syn.analyzer_for(
            world, lc, vc, [ca_syn._video(v) for v in s.get("videos", ())], target.query
        )
        inp = ca_syn.inputs_for(world, s["target"], category=s.get("category"))
        res = asyncio.run(analyzer.analyse(inp))
        ev = None
        if res.bundle is not None:
            records = world.registry.store.get_many(res.bundle.evidence_ids)
            ev = EvidenceContext(
                world.registry, vc, res.bundle, tuple(records.values()), inp.catalog, inp.stats,
                world.now,
            )  # fmt: skip
        out[key] = Scenario(key, res.report, ev, inp)
    return out
