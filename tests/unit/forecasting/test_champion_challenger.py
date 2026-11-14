"""Champion-challenger comparison and promotion on a copy of the shared synthetic registry."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.unit.forecasting.helpers import LOADED, trained
from trend_agent.forecasting.model_registry import MemoryModelStore, Registry, new_model_id
from trend_agent.forecasting.model_selection import compare, promote_selected
from trend_agent.schemas.model_metadata import ModelMetadata, ModelStatus, ModelType


def _copy() -> tuple[Registry, ModelMetadata]:
    """An isolated registry sharing the verified artifacts (the shared one is not touched)."""
    ctx = trained()
    reg = Registry(MemoryModelStore(), ctx.registry.artifacts)
    for m in ctx.registry.store.all():
        reg.store.add(m, m.model_id)
    champ = next(
        m
        for m in reg.store.all()
        if m.status is ModelStatus.CHAMPION
        and m.target_type.value == "breakout"
        and m.granularity == "hour"
    )
    return reg, champ


def _clone(reg: Registry, meta: ModelMetadata, **update: object) -> ModelMetadata:
    _, payload, text = reg.load(meta.model_id)
    new = meta.model_copy(
        update={
            "model_id": new_model_id("clone", meta.horizon_hours, meta.granularity),
            "status": ModelStatus.VALIDATED,
            **update,
        }
    )
    return reg.register(new, payload, text)


def _scope(meta: ModelMetadata):  # type: ignore[no-untyped-def]
    ds = trained().dataset
    return ds.subset((ds.meta["family"] == meta.target_family.value)
                     & (ds.meta["granularity"] == meta.granularity))  # fmt: skip


def test_better_challenger_is_promoted_and_identical_one_is_not() -> None:
    reg, champ = _copy()
    at = trained().trained_at + timedelta(days=1)
    weak = reg.register(
        champ.model_copy(
            update={
                "model_id": new_model_id("constant", champ.horizon_hours, "hour"),
                "model_name": "constant_rate_test",
                "model_type": ModelType.CLIMATOLOGY,
                "status": ModelStatus.VALIDATED,
            }
        ),
        {"model": {"kind": "baseline", "rate": 0.02}, "threshold": 0.5},
        None,
    )
    strong = _clone(reg, champ)
    reg.promote(weak.model_id, at=at, reason="deliberately weak champion", evidence={})
    assert reg.store.get(champ.model_id)[0].status is ModelStatus.RETIRED  # type: ignore[index]
    sub = _scope(champ)
    res = compare(reg, weak.model_id, strong.model_id, sub, LOADED)
    assert res["comparable"]
    assert res["test_rows"] > 0
    assert res["brier_difference_challenger_minus_champion"]["upper"] < 0
    assert res["promote"] is True
    assert all(res["criteria"].values())
    for role in ("champion", "challenger"):
        metrics = res[role]["metrics"]
        assert "lead_time" in metrics
        assert "stability_mean_abs_change" in metrics
        assert metrics["inference_us_per_row"] >= 0
    # an identical challenger brings no significant improvement: never promoted
    twin = _clone(reg, strong)
    same = compare(reg, strong.model_id, twin.model_id, sub, LOADED)
    assert same["brier_difference_challenger_minus_champion"]["difference"] == 0
    assert same["criteria"]["brier_better_with_ci"] is False
    assert same["promote"] is False


def test_promotion_step_logs_comparison_against_existing_champion() -> None:
    ctx = trained()
    reg, champ = _copy()
    at = ctx.trained_at + timedelta(days=1)
    weak = reg.register(
        champ.model_copy(
            update={
                "model_id": new_model_id("constant", champ.horizon_hours, "hour"),
                "model_type": ModelType.CLIMATOLOGY,
                "status": ModelStatus.VALIDATED,
            }
        ),
        {"model": {"kind": "baseline", "rate": 0.02}, "threshold": 0.5},
        None,
    )
    reg.promote(weak.model_id, at=at, reason="weak", evidence={})
    reg.store.set_status(champ.model_id, ModelStatus.VALIDATED, "re-enter as candidate", at)
    decisions = promote_selected(reg, ctx.results, ctx.dataset, LOADED, at=at)
    scope = f"{champ.target_family.value}/hour/{champ.horizon_hours}h/breakout"
    (decision,) = [d for d in decisions if d["scope"] == scope]
    assert decision["comparison"]["comparable"]
    assert decision["promoted"] == champ.model_id
    assert decision["replaced"] == weak.model_id
    statuses = {m.model_id: m.status for m in reg.store.all()}
    assert statuses[champ.model_id] is ModelStatus.CHAMPION
    assert statuses[weak.model_id] is ModelStatus.RETIRED
    in_scope = [
        m
        for m in reg.store.all()
        if m.status is ModelStatus.CHAMPION
        and m.target_type is champ.target_type
        and m.granularity == champ.granularity
        and m.horizon_hours == champ.horizon_hours
        and m.data_origin is champ.data_origin
    ]
    assert len(in_scope) == 1
    assert reg.store.events(champ.model_id)[-1].reason == "challenger beat the champion"


def test_models_on_different_feature_schemas_are_not_compared() -> None:
    reg, champ = _copy()
    other = _clone(reg, champ, feature_names=("x", "y"))
    res = compare(reg, champ.model_id, other.model_id, _scope(champ), LOADED)
    assert res == {
        "comparable": False,
        "reason": "feature schemas differ: models are not comparable",
    }


def test_shared_registry_untouched() -> None:
    ctx = trained()
    champions = [m for m in ctx.registry.store.all() if m.status is ModelStatus.CHAMPION]
    assert champions
    assert {d["promoted"] for d in ctx.decisions if d.get("promoted")} == {
        m.model_id for m in champions
    }
    with pytest.raises(ValueError, match="promote"):
        ctx.registry.mark(champions[0].model_id, ModelStatus.CHAMPION, at=ctx.trained_at,
                          reason="x")  # fmt: skip


def test_rerunning_the_promotion_step_never_demotes_champions() -> None:
    ctx = trained()
    reg, _ = _copy()
    before = {m.model_id: m.status for m in reg.store.all()}
    decisions = promote_selected(reg, ctx.results, ctx.dataset, LOADED, at=ctx.trained_at)
    assert decisions == []  # every selected model already is its scope's champion
    assert {m.model_id: m.status for m in reg.store.all()} == before
