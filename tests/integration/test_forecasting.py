"""Phase 6 forecasting against the real test database (synthetic data only, no network).

Training uses the isolated SYNTHETIC corpus (``data_origin = synthetic``); forecasts are
made for synthetic demo series written through the Phase 3 storage path. Nothing here
says anything about real-world accuracy.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from typer.testing import CliRunner

from tests.helpers import FakeYouTube, make_settings
from tests.integration.test_detection import DAY, World, _deny, count
from tests.unit.discovery.helpers import CONFIG_DIR
from trend_agent import cli
from trend_agent.container import AppContainer, build_container
from trend_agent.db.content_models import (
    AnalysisRunStatus,
    ContentAnalysisRunRow,
    ContentGapRow,
)
from trend_agent.db.forecast_models import (
    ForecastEvaluationRow,
    ForecastModelEventRow,
    ForecastModelRow,
    ForecastPredictionRow,
    ForecastTrainingRunRow,
    ForecastTrajectoryRow,
    OpportunityWindowRow,
    PeakEstimateRow,
)
from trend_agent.db.models import SearchQueryRow
from trend_agent.db.monitoring_models import ObservationRow, SeriesRow
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.detection import demo as ddemo
from trend_agent.forecasting.service import ForecastingService
from trend_agent.ingestion.retention import RetentionService
from trend_agent.schemas.forecast import ForecastOutput, ForecastStatus

runner = CliRunner()
NOW = ddemo.START + DAY * 75 + timedelta(hours=12)


def _container(tmp: Path, fake_youtube: FakeYouTube) -> AppContainer:
    return build_container(
        make_settings(discovery_config_dir=str(CONFIG_DIR), forecast_artifact_dir=str(tmp)),
        clock=lambda: NOW,
        http_client=httpx.AsyncClient(transport=fake_youtube.transport()),
        discovery_http_client=httpx.AsyncClient(transport=httpx.MockTransport(_deny)),
    )


@pytest.fixture
def world(
    session_factory: SessionFactory, fake_youtube: FakeYouTube, tmp_path: Path
) -> Iterator[World]:
    container = _container(tmp_path / "models", fake_youtube)
    w = World(container)
    for day in range(70, 76):
        w.advance(day)
    yield w
    container.engine.dispose()


def _service(w: World) -> ForecastingService:
    return w.container.forecasting(clock=lambda: NOW)


def test_training_run_registers_models_without_silent_promotion(world: World) -> None:
    svc = _service(world)
    res = svc.train(dataset="synthetic", scale=0.05, workers=2)
    assert res["status"] == "completed"
    assert res["data_origin"] == "synthetic"
    assert res["manifest"]["leakage_audit"]["mismatches"] == 0
    assert res["registered"] == count(world.factory, ForecastModelRow) > 0
    assert res["promotions"] == []
    with transaction(world.factory) as s:
        run = s.execute(sa.select(ForecastTrainingRunRow)).scalars().one()
        assert run.status == "completed"
        assert run.manifest["synthetic"] is True
        models = s.execute(sa.select(ForecastModelRow)).scalars().all()
        assert {m.data_origin for m in models} == {"synthetic"}
        assert all(m.status != "champion" for m in models)  # nothing promoted unasked
        assert all(len(m.artifact_checksum) == 64 for m in models)
    assert count(world.factory, ForecastEvaluationRow) > 0
    assert count(world.factory, ForecastModelEventRow) == res["registered"]
    # promotion is explicit and only for validated models
    validated = [m for m in svc.models() if m.status.value == "validated"]
    experimental = [m for m in svc.models() if m.status.value == "experimental"]
    assert validated
    champ = svc.promote(validated[0].model_id)
    assert champ.status.value == "champion"
    if experimental:
        with pytest.raises(ValueError, match="only VALIDATED or CHALLENGER"):
            svc.promote(experimental[0].model_id)
    svc.rollback(champ.model_id)
    assert {m.model_id: m.status.value for m in svc.models()}[champ.model_id] == "retired"
    with transaction(world.factory) as s:
        statuses = [
            e.to_status
            for e in s.execute(
                sa.select(ForecastModelEventRow)
                .where(ForecastModelEventRow.model_id == champ.model_id)
                .order_by(ForecastModelEventRow.id)
            ).scalars()
        ]
    assert statuses == ["validated", "champion", "retired"]


def test_database_training_without_real_data_fails_gracefully(world: World) -> None:
    res = _service(world).train(dataset="database")  # the database holds only synthetic data
    assert res["status"] == "failed"
    assert "no eligible series" in res["message"]
    assert count(world.factory, ForecastModelRow) == 0
    with transaction(world.factory) as s:
        run = s.execute(sa.select(ForecastTrainingRunRow)).scalars().one()
        assert run.status == "failed"


def test_predict_persist_idempotent_explain_retention(world: World) -> None:
    svc = _service(world)
    svc.train(dataset="synthetic", scale=0.05, workers=2, promote=True)
    with transaction(world.factory) as s:  # give the demo observations a retention period
        s.execute(sa.update(ObservationRow).values(expires_at=NOW + DAY * 30))
    assert svc.predict() == []  # real series only by default: there are none
    first = svc.predict(include_synthetic=True)
    assert first
    assert all(created for _, created in first)
    outs = [o for o, _ in first]
    assert all(o.synthetic for o in outs)
    assert all(o.prediction_timestamp == NOW.replace(minute=0) for o in outs)
    # daily demo series: never a 6-hour forecast
    assert all(o.status is ForecastStatus.UNSUPPORTED_GRANULARITY
               for o in outs if o.forecast_horizon_hours == 6)  # fmt: skip
    n = count(world.factory, ForecastPredictionRow)
    assert n == len(outs)
    second = svc.predict(include_synthetic=True)
    assert not any(created for _, created in second)  # identical forecasts stored once
    assert count(world.factory, ForecastPredictionRow) == n
    assert count(world.factory, ForecastTrajectoryRow) == sum(len(o.trajectory) for o in outs)
    assert count(world.factory, PeakEstimateRow) == sum(o.peak_estimate is not None for o in outs)
    assert count(world.factory, OpportunityWindowRow) == sum(
        len(o.opportunity_windows) for o in outs
    )
    stored = svc.explain(outs[1].prediction_id)
    assert stored is not None
    assert ForecastOutput.model_validate(stored) == outs[1]
    assert svc.explain("00000000-0000-0000-0000-000000000000") is None
    assert len(svc.peaks()) == 0  # synthetic rows hidden unless asked for
    assert len(svc.peaks(include_synthetic=True)) == count(world.factory, PeakEstimateRow)
    assert len(svc.windows(include_synthetic=True)) == count(world.factory, OpportunityWindowRow)
    drift = svc.drift(since_days=7)
    assert all(r["recommendation"]["automatic"] is False for r in drift)
    status = svc.status()
    assert status["predictions"] == n
    # retention: forecasts expire with the observations they were computed from
    with transaction(world.factory) as s:
        expiry = set(s.execute(sa.select(ForecastPredictionRow.expires_at)).scalars())
    assert expiry == {NOW + DAY * 30}
    assert (
        RetentionService(world.factory, lambda: NOW + DAY).purge_expired()["forecast_predictions"]
        == 0
    )
    deleted = RetentionService(world.factory, lambda: NOW + DAY * 31).purge_expired()
    assert deleted["forecast_predictions"] == n
    assert count(world.factory, ForecastTrajectoryRow) == 0
    assert count(world.factory, OpportunityWindowRow) == 0


def test_tampered_champion_artifact_is_refused(world: World, tmp_path: Path) -> None:
    svc = _service(world)
    svc.train(dataset="synthetic", scale=0.05, workers=2, promote=True)
    champions = [m for m in svc.models() if m.status.value == "champion"]
    daily = [m for m in champions if m.granularity == "day" and m.target_type.value != "peak"]
    if not daily:
        pytest.skip("no daily champion at this corpus scale")
    target = daily[0]
    (tmp_path / "models" / target.model_id / "model.json").write_text('{"model": {}}')
    fresh = _service(world)  # new registry: no cached artifact
    outs = [o for o, _ in fresh.predict(include_synthetic=True, persist=False)]
    hit = [o for o in outs if o.forecast_horizon_hours == target.horizon_hours]
    assert hit
    for o in hit:
        assert o.status in (
            ForecastStatus.FORECAST_UNAVAILABLE,
            ForecastStatus.INSUFFICIENT_HISTORY,
            ForecastStatus.SOURCE_UNAVAILABLE,
        )
        if o.status is ForecastStatus.FORECAST_UNAVAILABLE:
            assert "failed verification" in "; ".join(o.status_reasons)
            assert target.model_id not in o.evidence.get("models", [])


# ----------------------------------------------------------------------------- CLI
@pytest.fixture
def wired_cli(monkeypatch: pytest.MonkeyPatch, world: World, tmp_path: Path) -> None:
    settings = make_settings(
        discovery_config_dir=str(CONFIG_DIR), forecast_artifact_dir=str(tmp_path / "models")
    )
    monkeypatch.setattr(cli, "SETTINGS_FACTORY", lambda **kw: settings)
    monkeypatch.setattr(
        cli,
        "CONTAINER_FACTORY",
        lambda s, **kw: build_container(
            s,
            clock=lambda: NOW,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(_deny)),
            discovery_http_client=httpx.AsyncClient(transport=httpx.MockTransport(_deny)),
            **kw,
        ),
    )


def invoke(*args: str, code: int = 0) -> str:
    result = runner.invoke(cli.app, list(args))
    assert result.exit_code == code, result.output
    return result.output


def as_json(*args: str) -> Any:
    return json.loads(invoke(*args, "--json"))


@pytest.mark.usefixtures("wired_cli")
def test_cli_forecast_commands(world: World) -> None:
    assert "no monitored forecastable series" in invoke("forecast", "eligibility")
    elig = as_json("forecast", "eligibility", "--include-synthetic")
    assert elig
    assert all(r["horizons"]["6"]["status"] == "UNSUPPORTED_GRANULARITY" for r in elig)
    query = elig[0]["query"]
    feats = as_json("forecast", "features", "-q", query, "--include-synthetic")
    assert feats[0]["features"]
    invoke("forecast", "features", "-q", "no such query", code=1)
    assert "no eligible series" in invoke("forecast", "train", code=1)
    out = invoke("forecast", "train", "--dataset", "synthetic", "--scale", "0.05",
                 "--workers", "2", "--horizon", "24h")  # fmt: skip
    assert "nothing promoted" in out
    models = as_json("forecast", "models")
    assert models
    # the requested horizon; peak-window analogs always cover the configured peak window
    assert {m["horizon_hours"] for m in models if m["target_type"] != "peak"} == {24}
    assert {m["horizon_hours"] for m in models if m["target_type"] == "peak"} <= {72}
    assert all(m["status"] != "champion" for m in models)
    validated = [m for m in models if m["status"] == "validated"]
    assert validated
    promoted = as_json("forecast", "promote", validated[0]["model_id"])
    assert promoted["status"] == "champion"
    invoke("forecast", "promote", "fm_does_not_exist", code=2)
    preds = as_json("forecast", "predict", "--include-synthetic")
    assert preds
    assert all(p["created"] for p in preds)
    again = as_json("forecast", "predict", "--include-synthetic")
    assert not any(p["created"] for p in again)
    refused = next(p for p in preds if p["status"] == "UNSUPPORTED_GRANULARITY")
    assert "never" in invoke("forecast", "explain", refused["prediction_id"])
    made = next(p for p in preds if p["explanation"] is not None)
    explained = invoke("forecast", "explain", made["prediction_id"])
    assert "limitation" in explained
    assert (
        as_json("forecast", "explain", made["prediction_id"])["prediction_id"]
        == made["prediction_id"]
    )
    invoke("forecast", "explain", "00000000-0000-0000-0000-000000000000", code=1)
    invoke("forecast", "explain", code=2)
    assert isinstance(as_json("forecast", "peaks", "--include-synthetic"), list)
    windows = invoke("forecast", "windows", "--include-synthetic")
    assert "not predictions of video performance" in windows
    assert isinstance(as_json("forecast", "drift"), list)
    status = as_json("forecast", "status")
    assert status["predictions"] == len(preds)
    invoke("forecast", "rollback", validated[0]["model_id"])
    invoke("forecast", "train", "--horizon", "soon", code=2)


def test_phase4_and_phase5_inputs_and_classification_preserved(world: World) -> None:
    """Detector state (Phase 4) and content analyses (Phase 5) feed features only from the
    time they were known; each forecast keeps its series' measurement classification."""
    svc = _service(world)
    item = world.items[0]
    with transaction(world.factory) as s:
        row = s.execute(
            sa.select(SeriesRow, SearchQueryRow)
            .join(SearchQueryRow, SearchQueryRow.id == SeriesRow.query_id)
            .where(SearchQueryRow.original_query == item.query)
        ).first()
        assert row is not None
        series, q = row
        for done, strength in ((NOW - DAY * 2, "strong"), (NOW + timedelta(hours=1), "weak")):
            run_id = uuid.uuid4()
            s.add(
                ContentAnalysisRunRow(
                    id=run_id, query_id=q.id, trigger="manual", canonical_query=q.original_query,
                    status=AnalysisRunStatus.COMPLETED, started_at=done - timedelta(minutes=5),
                    completed_at=done, model_version="t", analysis_version="t",
                    configuration_version="t", inputs_fingerprint="0" * 64,
                    demand_basis="direct_search", opportunity_status="candidate",
                    is_synthetic=True,
                )
            )  # fmt: skip
            s.flush()
            s.add(
                ContentGapRow(
                    query_id=q.id, analysis_run_id=run_id, gap_type="missing_specific_answer",
                    evidence_strength=strength, explanation="synthetic", verification_status="ok",
                    is_synthetic=True,
                )
            )  # fmt: skip
    (feat,) = [f for f in svc.features(item.query, include_synthetic=True)
               if f["series_id"] == series.id]  # fmt: skip
    values = {f["name"]: f["value"] for f in feat["features"]}
    assert values["gap_strength_ord"] == 3  # the analysis completed before T, not the later one
    assert values["gap_is_opportunity"] == 1
    assert values["content_age_h"] == pytest.approx(48.0)
    assert values["det_baseline_ready"] is not None  # causal Phase 4 detector state
    assert "content" in feat["provenance"]
    # classification preserved end to end
    with transaction(world.factory) as s:
        kinds = {
            sr.id: (sr.simulated_category or sr.measurement_category, sr.is_proxy)
            for sr in s.execute(sa.select(SeriesRow)).scalars()
        }
    for o, _ in svc.predict(include_synthetic=True, persist=False):
        measurement, is_proxy = kinds[o.series_id]  # type: ignore[index]
        assert o.source_classification.name == measurement.name
        assert o.is_proxy is is_proxy
        assert o.synthetic
        assert (o.target_family == "proxy_attention") is is_proxy
