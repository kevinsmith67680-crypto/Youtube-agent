"""Model registry: checksums, trusted loading only, explicit promotion, origin isolation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from trend_agent.forecasting.feature_engineering import FEATURE_NAMES, FEATURE_SCHEMA_VERSION
from trend_agent.forecasting.model_registry import (
    ArtifactError,
    ArtifactStore,
    MemoryModelStore,
    Registry,
    new_model_id,
)
from trend_agent.schemas.model_metadata import (
    DataOrigin,
    ModelMetadata,
    ModelStatus,
    ModelType,
    Period,
    TargetFamily,
    TargetType,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
NO_PERIOD = Period(start=None, end=None)


def meta(
    *,
    status: ModelStatus = ModelStatus.VALIDATED,
    origin: DataOrigin = DataOrigin.SYNTHETIC,
    category: str = "all",
    horizon: int = 24,
    names: tuple[str, ...] = FEATURE_NAMES,
) -> ModelMetadata:
    return ModelMetadata(
        model_id=new_model_id("logistic", horizon, "hour"),
        model_name="logistic",
        version="1",
        model_type=ModelType.LOGISTIC_REGRESSION,
        target_family=TargetFamily.YOUTUBE_SEARCH_INTEREST,
        target_type=TargetType.BREAKOUT,
        target_definition={},
        horizon_hours=horizon,
        granularity="hour",
        category_scope=category,
        data_origin=origin,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        feature_names=names,
        dataset_version="ds-test",
        dataset_hash="0" * 64,
        training_period=NO_PERIOD,
        calibration_period=NO_PERIOD,
        test_period=NO_PERIOD,
        trained_at=T0,
        code_version="test",
        status=status,
    )


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return Registry(MemoryModelStore(), ArtifactStore(tmp_path))


def _register(reg: Registry, m: ModelMetadata, text: str | None = None) -> ModelMetadata:
    return reg.register(m, {"kind": "constant", "value": 0.1}, text)


def test_roundtrip_with_checksum(registry: Registry) -> None:
    m = _register(registry, meta(), "tree text")
    assert len(m.artifact_checksum) == 64
    got, payload, text = registry.load(m.model_id)
    assert payload == {"kind": "constant", "value": 0.1}
    assert text == "tree text"
    assert got.artifact_checksum == m.artifact_checksum


def test_tampered_artifact_is_refused(registry: Registry, tmp_path: Path) -> None:
    m = _register(registry, meta())
    (tmp_path / m.model_id / "model.json").write_text('{"kind": "constant", "value": 0.9}')
    with pytest.raises(ArtifactError, match="checksum mismatch"):
        registry.load(m.model_id)


def test_unexpected_file_is_refused(registry: Registry, tmp_path: Path) -> None:
    m = _register(registry, meta())
    (tmp_path / m.model_id / "model.pkl").write_bytes(b"\x80\x04 not loaded")
    with pytest.raises(ArtifactError, match="unexpected files"):
        registry.load(m.model_id)


@pytest.mark.parametrize("bad", ["../escape", "a/b", "x;rm", ""])
def test_path_traversal_is_refused(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ArtifactError):
        ArtifactStore(tmp_path).read(bad, "0" * 64)


def test_unknown_model_and_duplicate_registration(registry: Registry) -> None:
    with pytest.raises(ArtifactError):
        registry.load("fm_missing")
    m = _register(registry, meta())
    with pytest.raises(FileExistsError):
        registry.register(m, {"kind": "constant"}, None)


def test_promotion_is_explicit_and_logged(registry: Registry) -> None:
    exp = _register(registry, meta(status=ModelStatus.EXPERIMENTAL))
    with pytest.raises(ValueError, match="only VALIDATED or CHALLENGER"):
        registry.promote(exp.model_id, at=T0, reason="try", evidence={})
    val = _register(registry, meta())
    with pytest.raises(ValueError, match="promote"):
        registry.mark(val.model_id, ModelStatus.CHAMPION, at=T0, reason="sneaky")
    scope: dict[str, Any] = {
        "family": "youtube_search_interest",
        "granularity": "hour",
        "horizon": 24,
        "target_type": "breakout",
        "data_origin": DataOrigin.SYNTHETIC,
    }
    assert registry.champion(**scope) is None  # validated is not champion until promoted
    registry.promote(val.model_id, at=T0, reason="beat baseline", evidence={"brier": -0.01})
    champ = registry.champion(**scope)
    assert champ is not None
    assert champ.model_id == val.model_id
    events = registry.store.events(val.model_id)
    assert [e.to_status for e in events] == ["validated", "champion"]
    assert events[-1].evidence == {"brier": -0.01}


def test_single_champion_per_scope(registry: Registry) -> None:
    first = _register(registry, meta())
    second = _register(registry, meta(status=ModelStatus.CHALLENGER))
    registry.promote(first.model_id, at=T0, reason="first", evidence={})
    registry.promote(second.model_id, at=T0, reason="better", evidence={})
    statuses = {m.model_id: m.status for m in registry.store.all()}
    assert statuses[first.model_id] is ModelStatus.RETIRED
    assert statuses[second.model_id] is ModelStatus.CHAMPION
    assert sum(s is ModelStatus.CHAMPION for s in statuses.values()) == 1
    retired = registry.store.events(first.model_id)[-1]
    assert retired.reason == f"replaced by {second.model_id}"


def test_category_champion_preferred_then_pooled(registry: Registry) -> None:
    pooled = _register(registry, meta())
    ai = _register(registry, meta(category="ai"))
    registry.promote(pooled.model_id, at=T0, reason="pooled", evidence={})
    registry.promote(ai.model_id, at=T0, reason="ai", evidence={})
    scope: dict[str, Any] = {
        "family": "youtube_search_interest",
        "granularity": "hour",
        "horizon": 24,
        "target_type": "breakout",
        "data_origin": DataOrigin.SYNTHETIC,
    }
    assert registry.champion(**scope, category="ai").model_id == ai.model_id  # type: ignore[union-attr]
    assert registry.champion(**scope, category="gaming").model_id == pooled.model_id  # type: ignore[union-attr]
    # promoting a category champion does not retire the pooled one
    assert registry.store.get(pooled.model_id)[0].status is ModelStatus.CHAMPION  # type: ignore[index]


def test_synthetic_champion_never_serves_real_data(registry: Registry) -> None:
    syn_model = _register(registry, meta(origin=DataOrigin.SYNTHETIC))
    registry.promote(syn_model.model_id, at=T0, reason="synthetic experiment", evidence={})
    found = registry.champion(
        family="youtube_search_interest",
        granularity="hour",
        horizon=24,
        target_type="breakout",
        data_origin=DataOrigin.REAL,
    )
    assert found is None


def test_retire_and_rollback_events(registry: Registry) -> None:
    m = _register(registry, meta())
    registry.promote(m.model_id, at=T0, reason="go", evidence={})
    registry.retire(m.model_id, at=T0, reason="rollback")
    assert registry.store.get(m.model_id)[0].status is ModelStatus.RETIRED  # type: ignore[index]
    assert [e.to_status for e in registry.store.events(m.model_id)][-1] == "retired"
