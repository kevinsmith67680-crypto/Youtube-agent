"""Shared fixtures.

* No test touches the network: YouTube responses come from ``httpx.MockTransport``.
* Database tests use a separate database (``TREND_AGENT_TEST_DATABASE_URL``, default:
  the ``trend_agent_test`` DB created by docker compose). They are skipped when it is
  unreachable unless ``TREND_AGENT_REQUIRE_DB_TESTS=1`` (then they fail instead).
"""

from __future__ import annotations

import os
import random
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from pydantic import SecretStr

from tests.helpers import (
    REQUIRE_DB,
    ROOT,
    TEST_API_KEY,
    TEST_DB_URL,
    FakeYouTube,
    MutableClock,
    SleepRecorder,
    make_settings,
)
from trend_agent.db.session import (
    SessionFactory,
    check_connection,
    create_db_engine,
    create_session_factory,
)
from trend_agent.providers.http import RetryPolicy
from trend_agent.providers.quota import InMemoryQuotaStore, QuotaManager, QuotaStore
from trend_agent.providers.youtube import (
    YouTubeDataApiProvider,
    YouTubeRetentionConfig,
    youtube_quota_policy,
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if "/integration/" in str(item.fspath):
            item.add_marker(pytest.mark.db)
        if "/live/" in str(item.fspath):
            item.add_marker(pytest.mark.live)


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tests never read the developer's .env or TREND_AGENT_* variables."""
    for key in list(os.environ):
        if key.startswith("TREND_AGENT_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env in cwd


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def fake_youtube() -> FakeYouTube:
    return FakeYouTube()


@pytest.fixture
def sleeper() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
def make_youtube(
    fake_youtube: FakeYouTube, clock: MutableClock, sleeper: SleepRecorder
) -> Callable[..., YouTubeDataApiProvider]:
    def _make(
        *,
        api_key: str | None = TEST_API_KEY,
        store: QuotaStore | None = None,
        max_attempts: int = 3,
        max_pages: int = 3,
        persist_statistics: bool = False,
        enabled: bool = True,
        **settings_overrides: Any,
    ) -> YouTubeDataApiProvider:
        settings = make_settings(**settings_overrides)
        return YouTubeDataApiProvider(
            api_key=SecretStr(api_key) if api_key else None,
            http_client=httpx.AsyncClient(transport=fake_youtube.transport()),
            quota=QuotaManager(
                youtube_quota_policy(settings), store or InMemoryQuotaStore(), clock
            ),
            retry=RetryPolicy(
                max_attempts=max_attempts, base_delay=0.5, max_delay=4.0, rng=random.Random(7)
            ),
            enabled=enabled,
            max_pages=max_pages,
            retention=YouTubeRetentionConfig(persist_statistics=persist_statistics),
            clock=clock,
            sleep=sleeper,
            owns_client=True,
        )

    return _make


# ----------------------------------------------------------------------------- database
TABLES = [
    # Phase 4 (detection)
    "surge_evidence",
    "surge_event_transitions",
    "surge_events",
    "anomaly_results",
    "detector_states",
    "detection_queue",
    "detection_runs",
    "detection_evaluations",
    "detection_calibrations",
    # Phase 3 (monitoring)
    "monitoring_job_attempts",
    "ts_observations",
    "ts_series",
    "monitoring_jobs",
    "monitoring_backfills",
    "monitoring_target_events",
    "monitoring_targets",
    "monitoring_plan_runs",
    "monitoring_provider_health",
    "data_policies",
    # Phase 2 (keyword discovery)
    "keyword_status_history",
    "discovery_evidence",
    "discovered_keywords",
    "intent_clusters",
    "topic_clusters",
    "entity_mentions",
    "entity_aliases",
    "discovered_entities",
    "discovery_items",
    "discovery_run_sources",
    "discovery_runs",
    "discovery_sources",
    # Phase 1
    "video_search_results",
    "video_statistics",
    "rejected_records",
    "search_interest_observations",
    "videos",
    "channels",
    "ingestion_runs",
    "search_queries",
    "providers",
    "provider_rate_limits",
]


def alembic_config(url: str) -> Any:
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["database_url"] = url
    return cfg


@pytest.fixture(scope="session")
def db_engine() -> Iterator[sa.Engine]:
    from alembic import command

    engine = create_db_engine(TEST_DB_URL, connect_timeout=2)
    ok, detail = check_connection(engine)
    if not ok:
        engine.dispose()
        message = f"test database unavailable ({detail}); start it with `docker compose up -d`"
        if REQUIRE_DB:
            pytest.fail(message)
        pytest.skip(message)
    cfg = alembic_config(TEST_DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(db_engine: sa.Engine) -> SessionFactory:
    with db_engine.begin() as conn:
        conn.execute(sa.text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    return create_session_factory(db_engine)
