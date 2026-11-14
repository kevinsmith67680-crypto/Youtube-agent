"""Phase 8 end-to-end scenarios: shared fixtures (offline, isolated test database)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.helpers import MutableClock, make_settings
from trend_agent.container import AppContainer, build_container
from trend_agent.db.session import SessionFactory
from trend_agent.providers.quota import InMemoryQuotaStore
from trend_agent.schemas.enums import SearchProperty, TimeGranularity

NOW = datetime(2026, 11, 14, 12, tzinfo=UTC)
TOKEN = "e2e-api-token-0123456789abcdef-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@dataclass
class VerifiedYouTubeTrends:
    """A stand-in for an operator-installed, verified Google Trends alpha transport that
    serves the YouTube Search property. It is only used to test mode logic; it never
    returns data (fetch raises), so nothing can be fabricated from it."""

    verified: bool = True
    supported_properties: frozenset[SearchProperty] = frozenset({SearchProperty.YOUTUBE_SEARCH})
    supported_granularities: frozenset[TimeGranularity] = frozenset({TimeGranularity.DAY})

    async def fetch(self, request: Any) -> Any:
        raise RuntimeError("test transport: no data")

    async def aclose(self) -> None:
        return None


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(NOW)


def make_container(clock: MutableClock, **settings: Any) -> AppContainer:
    transport = settings.pop("trends_transport", None)
    base: dict[str, Any] = {"api_token": TOKEN}
    base.update(settings)
    return build_container(
        make_settings(**base),
        clock=clock,
        quota_store=InMemoryQuotaStore(),
        trends_transport=transport,
    )


@pytest.fixture
def container(session_factory: SessionFactory, clock: MutableClock) -> Iterator[AppContainer]:
    c = make_container(clock)
    yield c
    c.engine.dispose()
