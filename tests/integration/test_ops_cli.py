"""Phase 8 CLI and evaluation-pipeline coverage against the test database: every
read-only `system` command returns valid JSON describing the real state, and the
evaluation pipeline records experiments and a scorecard with real-world UNKNOWN."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.helpers import ROOT, MutableClock, make_settings
from trend_agent import cli
from trend_agent.container import build_container
from trend_agent.db.repositories.ops import ExperimentRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.providers.quota import InMemoryQuotaStore

pytestmark = pytest.mark.db
runner = CliRunner()
NOW = datetime(2026, 11, 14, 12, tzinfo=UTC)


@pytest.fixture
def wired(session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = MutableClock(NOW)
    settings = make_settings(
        discovery_config_dir=str(ROOT / "config"), operations_config_dir=str(ROOT / "config")
    )
    monkeypatch.setattr(cli, "SETTINGS_FACTORY", lambda: settings)
    monkeypatch.setattr(
        cli,
        "CONTAINER_FACTORY",
        lambda s=None, **kw: build_container(
            settings, clock=clock, quota_store=InMemoryQuotaStore()
        ),
    )


def _json(args: list[str], code: int = 0) -> Any:
    out = runner.invoke(cli.app, args)
    assert out.exit_code == code, out.output
    return json.loads(out.stdout)


def test_system_read_commands_report_the_real_state(wired: None) -> None:
    prov = _json(["system", "providers", "--json", "--no-discovery"])
    assert prov["operating_mode"]["direct_search_claims_enabled"] is False
    assert {p["name"] for p in prov["providers"]} >= {"youtube_data_api", "google_trends", "mock"}
    costs = _json(["system", "costs", "--json"])
    assert {b["name"] for b in costs["budgets"]} == {
        "daily_api_budget",
        "daily_llm_budget",
        "monthly_llm_budget",
    }
    card = _json(["system", "readiness", "--json"])
    assert card["release_state"] in ("NOT_READY", "STAGING_READY")
    assert card["counts"]["BLOCKED"] >= 1
    findings = _json(["system", "audit", "--json"])
    codes = {f["code"] for f in findings}
    assert "dev_database_credentials" in codes  # the test database uses the dev password
    assert _json(["system", "alerts", "--json"]) == []
    health = runner.invoke(cli.app, ["system", "health", "--json"])
    assert health.exit_code == 1  # no scheduler or worker runs in the test: workers fail
    checks = {c["check"]: c["status"] for c in json.loads(health.stdout)}
    assert checks["live"] == "ok"
    assert checks["database"] == "ok"
    out = runner.invoke(cli.app, ["system", "audit", "--strict"])
    assert out.exit_code == 1


def test_scheduler_and_worker_run_once_and_heartbeat_check(wired: None) -> None:
    out = runner.invoke(cli.app, ["system", "scheduler", "--once", "--id", "cli-sched"])
    assert out.exit_code == 0, out.output
    assert "leader=True" in out.output
    out = runner.invoke(cli.app, ["system", "worker", "--once", "--id", "cli-worker"])
    assert out.exit_code == 0, out.output
    # the --once runs mark themselves stopped, so the container health check fails
    out = runner.invoke(cli.app, ["system", "heartbeat-check", "--role", "worker"])
    assert out.exit_code == 1


def test_evaluation_pipeline_records_experiments_and_keeps_real_world_unknown(
    wired: None, session_factory: SessionFactory, tmp_path: Path
) -> None:
    from trend_agent.evaluation.pipeline import run

    rep = run(
        ROOT / "config",
        factory=session_factory,
        now=NOW,
        components=("content_gaps", "recommendations"),
    )
    assert set(rep["experiments"]) == {"content_gaps", "recommendations"}
    assert all(rep["experiments"].values())
    card = rep["scorecard"]
    assert card["components"]["content_gaps"]["offline"] == "MEETS_TARGET"
    assert card["components"]["recommendations"]["offline"] == "MEETS_TARGET"
    assert card["components"]["detection"]["offline"] == "NOT_RUN"
    assert card["real_world_accuracy"] == "UNKNOWN"
    assert card["pipeline_integrity"] == "PASS"
    with transaction(session_factory) as s:
        exps = ExperimentRepository(s).list()
    assert {e.component for e in exps} == {"content_gaps", "recommendations"}
    assert all(e.data_origin == "synthetic" for e in exps)
    out = tmp_path / "r.json"
    res = runner.invoke(
        cli.app, ["evaluate", "recommendations", "--json", "--no-db", "--out", str(out)]
    )
    assert res.exit_code == 0, res.output
    assert json.loads(out.read_text())["real_outcomes"]["status"] == "UNKNOWN"


def test_forecast_scope_summaries() -> None:
    from trend_agent.evaluation.forecasting import summarise_scope

    def cand(name: str, mtype: str, **metrics: Any) -> Any:
        return SimpleNamespace(name=name, model_type=SimpleNamespace(value=mtype), metrics=metrics)

    unsupported = SimpleNamespace(
        family="f",
        granularity="day",
        horizon=6,
        task="all",
        notes=["horizon not supported for this granularity"],
        report=None,
    )
    assert summarise_scope(unsupported)["status"] == "UNSUPPORTED"
    traj = SimpleNamespace(
        family="f",
        granularity="hour",
        horizon=24,
        task="trajectory",
        notes=[],
        report=SimpleNamespace(
            selected="lightgbm_regressor",
            candidates=[
                cand("persistence", "persistence", mae=0.12),
                cand("seasonal_naive", "seasonal_naive", mae=0.15),
                cand("lightgbm_regressor", "lightgbm_regressor", mae=0.10, interval_coverage=0.8),
            ],
        ),
    )
    s = summarise_scope(traj)
    assert s["baseline"] == "persistence"
    assert s["beats_baseline"] is True
    assert s["relative_mae_gain"] == pytest.approx(0.1667, abs=1e-3)
    assert s["lightgbm_meaningful"] is True
    marginal = SimpleNamespace(
        family="f",
        granularity="day",
        horizon=24,
        task="breakout",
        notes=[],
        report=SimpleNamespace(
            selected="lightgbm",
            candidates=[
                cand("logistic_regression", "logistic_regression", brier_skill=0.10),
                cand("lightgbm", "lightgbm_classifier", brier_skill=0.11),
            ],
        ),
    )
    m = summarise_scope(marginal)
    assert m["beats_baseline"] is True
    assert m["lightgbm_margin_over_logistic"] == pytest.approx(0.01)
    assert m["lightgbm_meaningful"] is False  # below the 0.02 Brier-skill threshold
    none = SimpleNamespace(
        family="f", granularity="day", horizon=72, task="peak", notes=[], report=None
    )
    assert summarise_scope(none)["status"] == "NO_MODEL"
