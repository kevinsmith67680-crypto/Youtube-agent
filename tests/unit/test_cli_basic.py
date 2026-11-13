"""CLI behaviour that needs neither a database nor the network.

The database URL points at a closed local port so the "database unavailable" paths are
exercised deterministically.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from tests.helpers import (
    TEST_API_KEY,
    FakeYouTube,
    MutableClock,
    json_response,
    load_fixture,
    make_settings,
)
from trend_agent import cli
from trend_agent.container import build_container

UNREACHABLE_DB = "postgresql+psycopg://nobody:secretpw@127.0.0.1:1/none"
runner = CliRunner()


@pytest.fixture(autouse=True)
def cli_overrides(
    monkeypatch: pytest.MonkeyPatch, fake_youtube: FakeYouTube, clock: MutableClock
) -> dict[str, Any]:
    overrides: dict[str, Any] = {}

    def settings() -> Any:
        return make_settings(
            database_url=UNREACHABLE_DB, database_connect_timeout_seconds=1, **overrides
        )

    def container(settings_obj: Any, **kw: Any) -> Any:
        return build_container(
            settings_obj,
            clock=clock,
            http_client=httpx.AsyncClient(transport=fake_youtube.transport()),
            **kw,
        )

    monkeypatch.setattr(cli, "SETTINGS_FACTORY", settings)
    monkeypatch.setattr(cli, "CONTAINER_FACTORY", container)
    return overrides


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--help"],
        ["youtube", "--help"],
        ["youtube", "search", "--help"],
        ["ingest", "--help"],
        ["data", "inspect", "--help"],
        ["quota", "status", "--help"],
        ["trends", "status", "--help"],
        ["providers", "list", "--help"],
        ["doctor", "--help"],
    ],
)
def test_help_text(args: list[str]) -> None:
    result = runner.invoke(cli.app, args)
    assert result.exit_code in (0, 2)  # Typer exits 2 when showing help for a bare group
    assert "Usage" in result.output


def test_version() -> None:
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == "0.1.0"


def test_youtube_search_missing_key_names_the_variable(fake_youtube: FakeYouTube) -> None:
    result = runner.invoke(cli.app, ["youtube", "search", "--query", "ai", "--region", "GB"])
    assert result.exit_code == 2
    assert "TREND_AGENT_YOUTUBE_API_KEY" in result.output
    assert "[not_configured]" in result.output
    assert "database unavailable" in result.output  # falls back to in-memory quota
    assert fake_youtube.requests == []


def test_youtube_search_with_key_and_db_down(
    cli_overrides: dict[str, Any], fake_youtube: FakeYouTube
) -> None:
    cli_overrides["youtube_api_key"] = TEST_API_KEY
    fake_youtube.queue("search", json_response(200, load_fixture("search_page2.json")))
    result = runner.invoke(
        cli.app, ["youtube", "search", "-q", "AI agents", "--region", "gb", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["results"][0]["video_id"] == "vidAAAAAAA3"
    assert payload["measurement_type"] == "video_activity"
    assert TEST_API_KEY not in result.output


def test_invalid_region_gives_one_line_hint() -> None:
    result = runner.invoke(cli.app, ["youtube", "popular", "--region", "UK"])
    assert result.exit_code == 1
    assert "did you mean 'GB'" in result.output
    assert "pydantic" not in result.output


def test_persisting_requires_database() -> None:
    result = runner.invoke(cli.app, ["ingest", "--provider", "mock", "-q", "AI agents"])
    assert result.exit_code == 3
    assert "database unavailable" in result.output
    assert "secretpw" not in result.output  # credentials never echoed


def test_providers_list_json_without_database() -> None:
    result = runner.invoke(cli.app, ["providers", "list", "--json"])
    assert result.exit_code == 0
    rows = json.loads(result.output[result.output.index("[") :])
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {"google_trends", "mock", "youtube_data_api"}
    assert by_name["youtube_data_api"]["status"] == "not_configured"
    assert by_name["mock"]["data_nature"] == "synthetic"
    assert "search_interest" not in by_name["youtube_data_api"]["capabilities"]
    assert by_name["google_trends"]["granularities"] == []


def test_trends_status_reports_not_configured() -> None:
    result = runner.invoke(cli.app, ["trends", "status", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output[result.output.index("{") :])
    assert data["status"] == "not_configured"
    assert data["supported_granularities"] is None
    assert data["measurement_types"] == ["external_search_interest"]


def test_doctor_reports_unreachable_database_without_secrets() -> None:
    result = runner.invoke(cli.app, ["doctor", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.output[result.output.index("{") :])
    assert data["database"]["ok"] is False
    assert "secretpw" not in result.output
    assert data["configuration"]["database_url"].endswith("nobody:***@127.0.0.1:1/none")


def test_quota_status_in_memory_when_db_down() -> None:
    result = runner.invoke(cli.app, ["quota", "status", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output[result.output.index("{") :])
    assert data["store"] == "InMemoryQuotaStore"
    assert {b["bucket"] for b in data["buckets"]} == {"search", "default"}
