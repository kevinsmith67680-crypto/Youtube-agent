"""Shared offline fixtures for Phase 6 tests (synthetic data, fixed seeds)."""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache

from tests.unit.discovery.helpers import CONFIG_DIR
from trend_agent.detection.config import load_detection_config
from trend_agent.forecasting import scenarios
from trend_agent.forecasting import synthetic as syn
from trend_agent.forecasting.config import load_forecasting_config
from trend_agent.forecasting.feature_store import SeriesBundle

LOADED = load_forecasting_config(CONFIG_DIR)
DLOADED = load_detection_config(CONFIG_DIR)
CFG = LOADED.config


def bundle(spec: syn.Spec, sid: int = 1, support: dict | None = None) -> SeriesBundle:  # type: ignore[type-arg]
    sb, _ = syn.Bundle(spec, support or {}).build(sid, sid * 100_000)
    return sb


def at(spec: syn.Spec, h: float):  # type: ignore[no-untyped-def]
    return spec.start + timedelta(hours=h)


@lru_cache(maxsize=1)
def trained() -> scenarios.Context:
    """One small synthetic training run shared by the tests (about a minute)."""
    return scenarios.prepare(LOADED, DLOADED, scale=0.3, workers=2)
