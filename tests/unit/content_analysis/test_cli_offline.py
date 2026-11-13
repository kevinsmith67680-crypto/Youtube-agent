"""Offline `trend-agent analyze` commands (no database, no network)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from tests.unit.discovery.helpers import CONFIG_DIR
from trend_agent import cli

runner = CliRunner()


@pytest.fixture(autouse=True)
def _config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TREND_AGENT_DISCOVERY_CONFIG_DIR", str(CONFIG_DIR))


def test_demo_runs_scenarios_and_reports_synthetic_nature() -> None:
    res = runner.invoke(cli.app, ["analyze", "demo", "-s", "B", "-s", "H"])
    assert res.exit_code == 0, res.output
    assert "PASS" in res.output
    assert "not real-world accuracy" in res.output
    data = json.loads(runner.invoke(cli.app, ["analyze", "demo", "-s", "D", "--json"]).output)
    assert data[0]["passed"]
    assert data[0]["report"]["synthetic"]


def test_evaluate_and_sweep_run_offline() -> None:
    res = runner.invoke(cli.app, ["analyze", "evaluate", "--split", "dev", "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["splits"]["dev"]["cases"] == 12
    assert out["splits"]["dev"]["future_evidence_leaks"] == 0
    assert runner.invoke(cli.app, ["analyze", "evaluate", "--split", "nope"]).exit_code == 2
    sweep = json.loads(runner.invoke(cli.app, ["analyze", "evaluate", "--sweep", "--json"]).output)
    assert {r["threshold"] for r in sweep} >= {45, 60}


def test_offline_intent_command() -> None:
    res = runner.invoke(cli.app, ["analyze", "intent", "-q", "how to install x", "--offline"])
    assert res.exit_code == 0, res.output
    assert "tutorial" in res.output
    assert "not probabilities" in res.output
