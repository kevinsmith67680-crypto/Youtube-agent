"""Forecast engine on a shared synthetic training run: scenarios A-M, contract, isolation.

``helpers.trained()`` trains once (synthetic corpus at scale 0.3, isolated experimental
registry) and is reused by every test here. Everything is offline and deterministic.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from typer.testing import CliRunner

from tests.unit.forecasting.helpers import at, bundle, trained
from trend_agent import cli
from trend_agent.forecasting import prediction, replay_adapter, scenarios
from trend_agent.forecasting import synthetic as syn
from trend_agent.forecasting.dataset_builder import _truncate
from trend_agent.monitoring.replay import AvailabilityMode
from trend_agent.schemas.enums import ScaleType
from trend_agent.schemas.forecast import ForecastOutput, ForecastStatus
from trend_agent.schemas.model_metadata import DataOrigin, ModelStatus
from trend_agent.schemas.monitoring import MeasurementCategory

M = MeasurementCategory
runner = CliRunner()


def _spec(key: str = "eng", seed: int = 41, **kw: object) -> syn.Spec:
    base: dict[str, object] = {"periods": 24 * 30, "category": "ai", "query": f"fictional {key}"}
    base.update(kw)
    return syn.Spec(key=key, seed=seed, **base)  # type: ignore[arg-type]


@pytest.mark.parametrize("key", list(scenarios.SCENARIOS))
def test_synthetic_scenario(key: str) -> None:
    (res,) = scenarios.run(trained(), [key])
    failed = [f"{n}: {d}" for n, ok, d in res.checks if not ok]
    assert res.passed, f"scenario {key} ({res.name}) failed: {failed}"


def test_training_never_creates_a_champion_silently() -> None:
    ctx = trained()
    models = ctx.registry.store.all()
    assert models
    assert all(m.data_origin is DataOrigin.SYNTHETIC for m in models)
    promoted = {d["promoted"] for d in ctx.decisions if d.get("promoted")}
    champions = {m.model_id for m in models if m.status is ModelStatus.CHAMPION}
    assert champions == promoted  # every champion came from an explicit promotion decision
    for m in champions:
        events = ctx.registry.store.events(m)
        assert events[-1].to_status == "champion"
        assert events[-1].reason


def test_contract_roundtrip_and_labelling() -> None:
    spec = _spec()
    outs = trained().engine.forecast(bundle(spec, 7001), at(spec, 24 * 30))
    assert {o.forecast_horizon_hours for o in outs} == {6, 24, 72}
    for o in outs:
        again = ForecastOutput.model_validate_json(o.model_dump_json())
        assert again == o
        assert o.schema_version == "forecast/1.0"
        assert o.synthetic
        assert o.data_cutoff <= o.prediction_timestamp
        assert "not search counts" in o.notice
        assert o.source_classification.value == "DIRECT_YOUTUBE_SEARCH_INTEREST"


def test_forecasts_are_issued_on_the_hour_and_idempotent() -> None:
    spec = _spec("ident")
    b = bundle(spec, 7002)
    eng = trained().engine
    t = at(spec, 24 * 30)
    first = eng.forecast(b, t + timedelta(minutes=10))
    second = eng.forecast(b, t + timedelta(minutes=40))
    assert first == second  # same issue hour: identical forecasts, ids and explanations
    assert all(o.prediction_timestamp == t for o in first)
    assert all(o.data_cutoff <= t for o in first)
    later = eng.forecast(b, t + timedelta(hours=3))
    assert later[0].prediction_id != first[0].prediction_id  # a different window (T, T+H]


def test_real_series_never_use_synthetic_models() -> None:
    spec = _spec("real")
    b = bundle(spec, 7003)
    real = replace(b, meta=replace(b.meta, is_synthetic=False))
    for o in trained().engine.forecast(real, at(spec, 24 * 30)):
        assert not o.synthetic
        assert o.evidence.get("models", []) == []
        assert o.breakout is None or o.breakout.probability is None
        assert o.breakout is None or o.breakout.model is None
        assert o.status is not ForecastStatus.READY
        assert o.peak_estimate is None


def test_feature_schema_mismatch_disables_models(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _spec("schema")
    b = bundle(spec, 7004)
    eng = trained().engine
    assert any(o.evidence.get("models") for o in eng.forecast(b, at(spec, 24 * 30)))
    monkeypatch.setattr(prediction, "FEATURE_NAMES", ("some", "other", "schema"))
    outs = eng.forecast(b, at(spec, 24 * 30))
    assert all(o.evidence.get("models", []) == [] for o in outs)


def test_proxy_forecast_is_never_presented_as_search_demand() -> None:
    spec = _spec("proxy", measurement=M.VIDEO_ACTIVITY, scale=ScaleType.ABSOLUTE_COUNT)
    for o in trained().engine.forecast(bundle(spec, 7005), at(spec, 24 * 30)):
        assert o.is_proxy
        assert o.target_family == "proxy_attention"
        assert o.source_classification.value == "VIDEO_ACTIVITY"
        assert o.data_quality.measurement_unit.startswith("proxy_")
        if o.explanation is not None:
            text = json.dumps(o.explanation.model_dump(mode="json")).lower()
            assert "not search demand" in text


def test_replay_adapter_has_no_look_ahead() -> None:
    ctx = trained()
    spec = _spec("replay", seed=43, events=(syn.Event("launch", 24 * 20, 2.5),))
    b = bundle(spec, 7006)
    start, end = at(spec, 24 * 12), at(spec, 24 * 25)
    res = replay_adapter.run(ctx.engine, [b], start=start, end=end, step=timedelta(hours=24))
    assert res["replay"]["steps"] == 14
    assert res["forecasts"]["forecasts"] == 14 * 3
    assert res["latency_ms"]["p50"] is not None
    consumer_outputs = []
    cons = replay_adapter.ForecastReplayConsumer(ctx.engine, {b.meta.series_id: b})
    from trend_agent.monitoring.replay import replay

    replay(b.points, start=start, end=end, step=timedelta(hours=24),
           mode=AvailabilityMode.PUBLICATION, consumers=[cons])  # fmt: skip
    consumer_outputs = cons.outputs
    for o in consumer_outputs[::5]:
        assert o.data_cutoff <= o.prediction_timestamp
        cut = _truncate(b, o.prediction_timestamp, AvailabilityMode.PUBLICATION)
        direct = ctx.engine.forecast(
            cut, o.prediction_timestamp, horizons=[o.forecast_horizon_hours]
        )
        assert direct[0].features == o.features  # same as forecasting from truncated data
        assert direct[0].prediction_id == o.prediction_id


# ------------------------------------------------------------- offline CLI paths
@pytest.fixture
def cached_training(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scenarios, "prepare", lambda *a, **k: trained())


@pytest.mark.usefixtures("cached_training")
def test_cli_demo_offline() -> None:
    res = runner.invoke(cli.app, ["forecast", "demo", "-s", "F", "-s", "M"])
    assert res.exit_code == 0, res.output
    assert "2/2 scenarios met their expectations" in res.output
    assert "not real-world accuracy" in res.output
    res = runner.invoke(cli.app, ["forecast", "demo", "-s", "L", "--json"])
    assert res.exit_code == 0, res.output
    doc = json.loads(res.output)
    assert doc["scenarios"][0]["passed"] is True
    assert all(m["data_origin"] == "synthetic" for m in doc["models"])


@pytest.mark.usefixtures("cached_training")
def test_cli_replay_offline() -> None:
    res = runner.invoke(cli.app, ["forecast", "replay", "--step-hours", "48", "--json"])
    assert res.exit_code == 0, res.output
    doc = json.loads(res.output)
    assert doc["synthetic"] is True
    assert doc["forecasts"]["forecasts"] > 0
    bad = runner.invoke(cli.app, ["forecast", "replay", "--dataset", "database"])
    assert bad.exit_code == 2


def test_cli_schema_offline() -> None:
    res = runner.invoke(cli.app, ["forecast", "schema"])
    assert res.exit_code == 0
    assert json.loads(res.output)["title"] == "ForecastOutput"
    res = runner.invoke(cli.app, ["forecast", "schema", "--which", "features"])
    assert res.exit_code == 0
    features = json.loads(res.output)
    assert all(f["availability"] for f in features)  # every feature states when it is known
