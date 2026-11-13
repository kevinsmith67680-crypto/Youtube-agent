"""CLI against the real test database (network still faked)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from tests.helpers import FakeYouTube, MutableClock, make_settings
from trend_agent import cli
from trend_agent.container import build_container
from trend_agent.db.session import SessionFactory

runner = CliRunner()


def body(output: str) -> Any:
    start = min(i for i in (output.find("{"), output.find("[")) if i >= 0)
    return json.loads(output[start:])


@pytest.fixture(autouse=True)
def _wire_cli(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: SessionFactory,
    fake_youtube: FakeYouTube,
    clock: MutableClock,
) -> None:
    monkeypatch.setattr(cli, "SETTINGS_FACTORY", make_settings)
    monkeypatch.setattr(
        cli,
        "CONTAINER_FACTORY",
        lambda settings, **kw: build_container(
            settings,
            clock=clock,
            http_client=httpx.AsyncClient(transport=fake_youtube.transport()),
            **kw,
        ),
    )


def test_mock_ingestion_then_inspection() -> None:
    result = runner.invoke(
        cli.app,
        [
            "ingest",
            "--provider",
            "mock",
            "-q",
            "AI agents",
            "--region",
            "GB",
            "--days",
            "30",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    summary = body(result.output)
    assert summary["status"] == "succeeded"
    assert summary["totals"]["stored"] == 30
    # default window ends at 00:00 UTC "today" (fixed clock: 2026-03-01)
    assert summary["extra"]["queries"] == ["AI agents"]

    again = body(
        runner.invoke(
            cli.app,
            [
                "ingest",
                "--provider",
                "mock",
                "-q",
                "ai agents",
                "--region",
                "GB",
                "--days",
                "30",
                "--json",
            ],
        ).output
    )
    assert again["totals"]["already_present"] == 30  # idempotent CLI re-run

    inspect = runner.invoke(cli.app, ["data", "inspect", "-q", "AI Agents", "--json"])
    assert inspect.exit_code == 0, inspect.output
    data = body(inspect.output)
    assert data["canonical_query"] == "ai agents"
    assert len(data["groups"]) == 1
    group = data["groups"][0]
    assert group["synthetic"] is True
    assert group["count"] == 30
    assert group["measurement_type"] == "direct_search_interest"
    obs = data["observations"][-1]
    assert obs["source_reference"].startswith("synthetic://mock/stable")
    assert obs["observation_end"] == "2026-03-01T00:00:00Z"
    assert obs["collected_at"] == "2026-03-01T12:00:00Z"

    table = runner.invoke(cli.app, ["data", "inspect", "-q", "ai agents"])
    assert "SYNTHETIC" in table.output


def test_inspect_flags_incomparable_groups() -> None:
    runner.invoke(
        cli.app,
        ["ingest", "-q", "AI agents", "--scenario", "mixed_normalization", "--days", "10"],
    )
    result = runner.invoke(cli.app, ["data", "inspect", "-q", "ai agents"])
    assert result.exit_code == 0
    assert "NOT directly comparable" in result.output


def test_runs_retention_quota_and_doctor() -> None:
    runner.invoke(cli.app, ["ingest", "-q", "x", "--scenario", "invalid_records", "--days", "10"])
    runs = runner.invoke(cli.app, ["data", "runs", "--rejections", "--json"])
    assert runs.exit_code == 0
    rows = body(runs.output)
    assert rows[0]["rejected"] == 6
    assert len(rows[0]["rejections"]) == 6

    retention = runner.invoke(cli.app, ["data", "retention", "--purge", "--json"])
    assert retention.exit_code == 0
    assert body(retention.output)["deleted"]["search_interest_observations"] == 0

    quota = runner.invoke(cli.app, ["quota", "status", "--json"])
    assert body(quota.output)["store"] == "database"

    doctor = runner.invoke(cli.app, ["doctor", "--json"])
    assert doctor.exit_code == 0, doctor.output
    report = body(doctor.output)
    assert report["database"]["schema_current"] is True
    assert {p["provider_name"] for p in report["providers"]} == {
        "google_trends",
        "mock",
        "youtube_data_api",
    }


def test_ingest_unconfigured_provider_exit_code() -> None:
    result = runner.invoke(
        cli.app,
        ["ingest", "--provider", "google_trends", "-q", "x", "--property", "google_web_search"],
    )
    assert result.exit_code == 2
    assert "TREND_AGENT_GOOGLE_TRENDS_ACCESS_MODE" in result.output


@pytest.fixture
def with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.helpers import TEST_API_KEY

    monkeypatch.setattr(
        cli, "SETTINGS_FACTORY", lambda: make_settings(youtube_api_key=TEST_API_KEY)
    )


@pytest.mark.usefixtures("with_key")
def test_youtube_commands_render_and_persist(fake_youtube: FakeYouTube) -> None:
    from tests.helpers import json_response, load_fixture

    fake_youtube.default("videoCategories", json_response(200, load_fixture("categories.json")))
    fake_youtube.default("videos", json_response(200, load_fixture("popular.json")))
    fake_youtube.default("channels", json_response(200, load_fixture("channels.json")))
    fake_youtube.default("search", json_response(200, load_fixture("search_page2.json")))

    categories = runner.invoke(cli.app, ["youtube", "categories", "--region", "GB"])
    assert categories.exit_code == 0, categories.output
    assert "Science & Technology" in categories.output

    popular = runner.invoke(cli.app, ["youtube", "popular", "--region", "GB", "--limit", "2"])
    assert popular.exit_code == 0, popular.output
    assert "Popular one" in popular.output
    assert "not a measure of search demand" in popular.output

    search = runner.invoke(cli.app, ["youtube", "search", "-q", "AI agents", "--region", "GB"])
    assert search.exit_code == 0, search.output
    assert "Agents explained" in search.output
    assert "not search volume" in search.output

    persisted = runner.invoke(
        cli.app, ["youtube", "search", "-q", "AI agents", "--region", "GB", "--persist"]
    )
    assert persisted.exit_code == 0, persisted.output
    assert "succeeded" in persisted.output
    # The fake videos.list returns other IDs, so the search hit is reported as unavailable.
    assert "deleted or private since" in persisted.output

    popular_persist = runner.invoke(
        cli.app, ["youtube", "popular", "--region", "GB", "--limit", "2", "--persist", "--json"]
    )
    popular_summary = body(popular_persist.output)
    assert popular_summary["status"] == "succeeded"
    assert popular_summary["by_kind"]["video_statistics"] == {"received": 2, "withheld": 2}

    refresh = runner.invoke(cli.app, ["youtube", "refresh", "--horizon-days", "30"])
    assert refresh.exit_code == 0, refresh.output
    assert "Due:" in refresh.output

    quota = body(runner.invoke(cli.app, ["quota", "status", "--json"]).output)
    used = {b["bucket"]: b["used"] for b in quota["buckets"]}
    assert used["search"] == 2
    assert used["default"] >= 4
