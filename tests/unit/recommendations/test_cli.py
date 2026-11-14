"""`trend-agent recommend demo` and `trend-agent dashboard serve` (no database)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.helpers import make_settings
from trend_agent import cli, cli_recommend
from trend_agent.recommendations.demo import DemoBackend, DemoSnapshot

runner = CliRunner()


@pytest.fixture
def cached(demo_snapshot: DemoSnapshot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "snapshot.json"
    cache.write_text(demo_snapshot.model_dump_json(), "utf-8")
    monkeypatch.setattr(cli_recommend, "demo_cache_path", lambda: cache)
    return cache


def test_demo_prints_labelled_synthetic_ideas(cached: Path) -> None:
    r = runner.invoke(cli.app, ["recommend", "demo"])
    assert r.exit_code == 0, r.output
    out = " ".join(r.stdout.split())
    assert "SYNTHETIC DEMONSTRATION" in out
    assert "not probabilities of success" in out
    assert "Research candidates (not ranked)" in out
    assert "synthetic, never sent" in out


def test_demo_json_and_category_filter(cached: Path) -> None:
    r = runner.invoke(cli.app, ["recommend", "demo", "--category", "gaming", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert data["synthetic"] is True
    assert data["recommendations"]
    assert all(x["category"] == "gaming" for x in data["recommendations"])
    assert all(x["synthetic"] for x in data["recommendations"])
    assert runner.invoke(cli.app, ["recommend", "demo", "--category", "cooking"]).exit_code == 2


def test_generate_requires_a_category_or_all() -> None:
    assert runner.invoke(cli.app, ["recommend", "generate"]).exit_code == 2
    assert runner.invoke(cli.app, ["recommend", "generate", "--category", "x"]).exit_code == 2


def test_dashboard_serve_demo_builds_the_api_without_a_database(
    cached: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    seen: dict[str, Any] = {}

    def fake_run(app: Any, **kw: Any) -> None:
        seen["app"], seen["kw"] = app, kw

    monkeypatch.setattr(uvicorn, "run", fake_run)
    settings = make_settings(
        api_token="serve-test-token-0123456789abcdef",
        database_url="postgresql+psycopg://nobody:x@127.0.0.1:1/none",
    )
    monkeypatch.setattr(cli, "SETTINGS_FACTORY", lambda: settings)
    r = runner.invoke(cli.app, ["dashboard", "serve", "--demo", "--port", "8123"])
    assert r.exit_code == 0, r.output
    assert "SYNTHETIC" in r.stdout
    assert seen["kw"]["port"] == 8123
    assert seen["kw"]["host"] == "127.0.0.1"
    backend = seen["app"].state.backend
    assert isinstance(backend, DemoBackend)
    assert backend.synthetic
