"""Production configuration guard, metrics exposition, operations config, release state."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.helpers import make_settings
from trend_agent.ops.config import load_operations_config
from trend_agent.ops.metrics import Counter, Histogram
from trend_agent.ops.production import ProductionConfigError, check, enforce
from trend_agent.ops.readiness import Item, ItemStatus, ReleaseState, release_state

GOOD_TOKEN = "x" * 40
PROD_DB = (
    "postgresql+psycopg://app:Very-Long-Random-Password-1@db.internal:5432/trend?sslmode=require"
)


def test_production_refuses_development_defaults() -> None:
    s = make_settings(
        environment="production",
        database_url="postgresql+psycopg://trend_agent:trend_agent_dev@127.0.0.1:5432/x",
    )
    with pytest.raises(ProductionConfigError) as e:
        enforce(s)
    codes = {p.code for p in e.value.problems}
    assert {"dev_database_credentials", "api_token_missing", "signing_key_missing"} <= codes


def test_production_refuses_weak_token_and_demo_mode() -> None:
    s = make_settings(
        environment="production",
        database_url=PROD_DB,
        api_token="short",
        verification_signing_key="k" * 40,
        demo_mode=True,
    )
    codes = {p.code for p in check(s) if p.severity == "error"}
    assert codes == {"api_token_weak", "demo_mode"}


def test_a_clean_production_configuration_starts_and_reports_warnings() -> None:
    s = make_settings(
        environment="production",
        database_url=PROD_DB,
        api_token=GOOD_TOKEN,
        verification_signing_key="k" * 40,
        mock_provider_enabled=False,
        api_cors_origins="https://dash.example.com",
    )
    assert enforce(s) == []


def test_development_never_blocks() -> None:
    warnings = enforce(make_settings(environment="development"))
    assert all(w.severity == "warning" for w in warnings)


def test_metrics_text_format_escapes_labels() -> None:
    c = Counter("t_total", "help", ("route",))
    c.inc('/a"b')
    c.inc('/a"b')
    lines = list(c.render())
    assert lines[1] == "# TYPE t_total counter"
    assert 't_total{route="/a\\"b"} 2.0' in lines
    h = Histogram("t_seconds", "help", ("m",), buckets=(0.1, 1.0))
    h.observe(0.05, "GET")
    h.observe(0.5, "GET")
    out = "\n".join(h.render())
    assert 't_seconds_bucket{m="GET",le="0.1"} 1.0' in out
    assert 't_seconds_bucket{m="GET",le="+Inf"} 2.0' in out
    assert 't_seconds_count{m="GET"} 2.0' in out


def test_operations_config_loads_and_rejects_unknown_tasks(tmp_path: Path) -> None:
    cfg = load_operations_config(str(Path(__file__).resolve().parents[3] / "config"))
    assert cfg.scheduler.tasks["ops_checks"].interval_seconds == 60
    assert cfg.scheduler.tasks["content_analysis"].max_per_run == 10  # from operations.yaml
    assert cfg.alerts.stage_failure_threshold == 3
    # unattended discovery (GitHub included); runs only with TREND_AGENT_SCHEDULED_DISCOVERY
    assert cfg.scheduler.tasks["discovery"].max_per_run == 24
    assert set(cfg.scheduler.tasks) == {
        "ops_checks",
        "discovery",
        "monitor_plan",
        "content_analysis",
        "forecasting",
        "recommendations",
        "retention",
    }
    (tmp_path / "operations.yaml").write_text(
        "scheduler:\n  tasks:\n    mystery: {interval_seconds: 60}\n"
    )
    with pytest.raises(ValueError, match="unknown scheduler task"):
        load_operations_config(str(tmp_path))
    assert load_operations_config(str(tmp_path / "missing")).version == 1


def _items(**over: ItemStatus) -> list[Item]:
    base = {
        "backend_tests": ("build", ItemStatus.PASS),
        "deployment_artifacts": ("build", ItemStatus.PASS),
        "scheduler_running": ("runtime", ItemStatus.PASS),
        "backup_restore_drill": ("runtime", ItemStatus.PASS),
        "proxy_provider_live": ("external", ItemStatus.PASS),
        "direct_search_provider": ("external", ItemStatus.PASS),
        "detection_real_accuracy": ("external", ItemStatus.PASS),
        "youtube_api_compliance_review": ("external", ItemStatus.PASS),
        "recommendation_outcomes": ("external", ItemStatus.NOT_APPLICABLE),
    }
    return [Item(k, "a", scope, over.get(k, st), "") for k, (scope, st) in base.items()]


def test_release_state_rules() -> None:
    assert release_state(_items())[0] is ReleaseState.PRODUCTION_READY
    assert release_state(_items(backend_tests=ItemStatus.FAIL))[0] is ReleaseState.NOT_READY
    assert release_state(_items(deployment_artifacts=ItemStatus.BLOCKED))[0] is ReleaseState.BLOCKED
    limited = _items(
        direct_search_provider=ItemStatus.BLOCKED, detection_real_accuracy=ItemStatus.BLOCKED
    )
    assert release_state(limited)[0] is ReleaseState.LIMITED_PRODUCTION_READY
    # BLOCKED is never PASS: a blocked compliance review keeps it out of production
    blocked_review = _items(
        direct_search_provider=ItemStatus.BLOCKED, youtube_api_compliance_review=ItemStatus.BLOCKED
    )
    assert release_state(blocked_review)[0] is ReleaseState.STAGING_READY
    no_runtime = _items(scheduler_running=ItemStatus.FAIL)
    state, reasons = release_state(no_runtime)
    assert state is ReleaseState.STAGING_READY
    assert any("scheduler_running" in r for r in reasons)


def test_scorecard_counts_never_merge_blocked_into_pass() -> None:
    from trend_agent.ops.readiness import Scorecard

    card = Scorecard(
        datetime(2026, 11, 14, tzinfo=UTC),
        ReleaseState.STAGING_READY,
        tuple(_items(direct_search_provider=ItemStatus.BLOCKED)),
        (),
    )
    assert card.counts()["BLOCKED"] == 1
    assert card.counts()["PASS"] == 7
