"""Phase 7 fixtures: the synthetic demo snapshot is built once per test session (about a
minute: Phase 5 scenario analyses plus Phase 6 synthetic model training) and every test
gets a fresh in-memory backend over it."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

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
