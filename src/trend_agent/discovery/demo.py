"""End-to-end discovery demonstration on the synthetic fixture (no network, no quota)."""

from __future__ import annotations

from datetime import datetime

from trend_agent.container import AppContainer
from trend_agent.discovery.models import TARGET_CATEGORIES
from trend_agent.discovery.providers.synthetic import (
    PROVIDER_NAME,
    SyntheticDiscoveryProvider,
    SyntheticScenario,
    load_scenario,
)
from trend_agent.discovery.service import DiscoveryRunSummary


class StageClock:
    """Fixed clock the demo moves between fixture stages (deterministic output)."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def run_demo(
    container: AppContainer, scenario: SyntheticScenario | None = None, *, max_fetches: int = 60
) -> list[tuple[str, DiscoveryRunSummary]]:
    scenario = scenario or load_scenario()
    clock = StageClock(scenario.anchor_time)
    if PROVIDER_NAME not in container.registry.names():
        container.registry.register(SyntheticDiscoveryProvider(scenario, clock=clock))
    results: list[tuple[str, DiscoveryRunSummary]] = []
    for stage in scenario.stages:
        clock.now = scenario.stage_time(stage.name)
        service = container.discovery(clock=clock)
        summary = await service.run(
            TARGET_CATEGORIES,
            sources=scenario.sources_for(stage.name),
            synthetic=True,
            force=True,
            max_fetches=max_fetches,
        )
        results.append((stage.name, summary))
    return results
