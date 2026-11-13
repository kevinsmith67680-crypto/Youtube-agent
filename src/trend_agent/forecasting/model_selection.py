"""Training orchestration, registration and champion-challenger promotion.

``train_and_register`` trains every scope (target family x granularity x horizon) that has
enough data, registers each candidate with status VALIDATED (passed all acceptance checks)
or EXPERIMENTAL, and returns the reports. It never promotes. ``promote_selected`` is the
explicit step: a validated model becomes CHAMPION only if there is no champion yet, or if
it beats the current champion on the *same* held-out rows (``compare``) under explicit
criteria; otherwise it is marked CHALLENGER. Every change is recorded as an event.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from trend_agent.forecasting.config import FORECAST_VERSION, LoadedForecastingConfig
from trend_agent.forecasting.dataset_builder import Dataset
from trend_agent.forecasting.evaluation import (
    Split,
    chronological_split,
    classification_metrics,
    paired_bootstrap,
    rolling_origins,
)
from trend_agent.forecasting.feature_engineering import FEATURE_SCHEMA_VERSION, code_version
from trend_agent.forecasting.model_registry import Registry, new_model_id
from trend_agent.forecasting.peak_estimation import AnalogModel, evaluate_peaks
from trend_agent.forecasting.target_definition import definition
from trend_agent.forecasting.training import (
    Candidate,
    LightGBMModel,
    LogisticModel,
    TrainingReport,
    train_classifier,
    train_trajectory,
)
from trend_agent.forecasting.uncertainty import OODReference
from trend_agent.schemas.enums import TimeGranularity
from trend_agent.schemas.model_metadata import (
    DataOrigin,
    ModelMetadata,
    ModelStatus,
    ModelType,
    Period,
    TargetFamily,
    TargetType,
)

_NAMES = {
    ModelType.LOGISTIC_REGRESSION: "logistic_breakout",
    ModelType.LIGHTGBM_CLASSIFIER: "lightgbm_breakout",
    ModelType.LIGHTGBM_REGRESSOR: "lightgbm_trajectory",
    ModelType.PERSISTENCE: "persistence_trajectory",
    ModelType.SEASONAL_NAIVE: "seasonal_naive_trajectory",
    ModelType.DAMPED_TREND: "damped_trend_trajectory",
    ModelType.ANALOG_PEAK: "analog_peak_window",
}


@dataclass
class ScopeResult:
    family: str
    granularity: str
    horizon: int
    task: str
    report: TrainingReport | None
    registered: list[ModelMetadata] = field(default_factory=list)
    split: dict[str, Any] = field(default_factory=dict)
    rolling: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _period(ds: Dataset, mask: np.ndarray) -> Period:
    from trend_agent.forecasting.feature_store import from_hours

    t = ds.meta["t"][mask].astype(float)
    if not t.size:
        return Period(start=None, end=None)
    return Period(start=from_hours(float(t.min())), end=from_hours(float(t.max())))


def feature_reference(x: np.ndarray, names: tuple[str, ...]) -> dict[str, Any]:
    """Decile edges and training proportions per feature (drift reference)."""
    out: dict[str, Any] = {}
    for j, n in enumerate(names):
        col = x[:, j]
        ok = col[np.isfinite(col)]
        missing = float(np.mean(~np.isfinite(col))) if col.size else 1.0
        if ok.size < 20 or np.unique(ok).size < 2:
            out[n] = {"edges": None, "missing_rate": missing}
            continue
        edges = np.unique(np.quantile(ok, np.linspace(0, 1, 11)))
        counts, _ = np.histogram(np.clip(ok, edges[0], edges[-1]), bins=edges)
        out[n] = {
            "edges": [float(e) for e in edges],
            "proportions": [float(c) / ok.size for c in counts],
            "missing_rate": missing,
        }
    return out


def _metadata(
    c: Candidate,
    report: TrainingReport,
    ds: Dataset,
    split: Split,
    loaded: LoadedForecastingConfig,
    *,
    target_type: TargetType,
    origin: DataOrigin,
    trained_at: datetime,
) -> ModelMetadata:
    name = _NAMES.get(c.model_type, c.name)
    status = ModelStatus.VALIDATED if c.validated else ModelStatus.EXPERIMENTAL
    if origin is DataOrigin.SYNTHETIC:
        reasons = ["trained on SYNTHETIC data: valid only for synthetic series", *c.reasons]
    else:
        reasons = list(c.reasons)
    return ModelMetadata(
        model_id=new_model_id(name, report.horizon, report.granularity),
        model_name=name,
        version=f"{FORECAST_VERSION}+{ds.manifest.get('dataset_version', 'unknown')}",
        model_type=c.model_type,
        target_family=TargetFamily(report.family),
        target_type=target_type,
        target_definition=definition(loaded.config.targets, TimeGranularity(report.granularity)),
        horizon_hours=report.horizon,
        granularity=report.granularity,
        category_scope="all",
        provider_scope=tuple(sorted({str(p) for p in ds.meta["provider"]})),
        data_origin=origin,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        feature_names=ds.feature_names,
        dataset_version=str(ds.manifest.get("dataset_version", "unknown")),
        dataset_hash=str(ds.manifest.get("rows_hash", "")),
        training_period=_period(ds, split.train | split.validation),
        calibration_period=_period(ds, split.calibration),
        test_period=_period(ds, split.test),
        hyperparameters=c.hyperparameters,
        metrics=dict(c.metrics.items()),
        calibration=c.calibrator.as_dict() if c.calibrator else {},
        trained_at=trained_at,
        code_version=code_version(),
        status=status,
        status_reasons=tuple(reasons),
    )


def _payload(
    c: Candidate, ood: OODReference, ref: dict[str, Any], kind: str
) -> tuple[dict[str, Any], str | None]:
    text = None
    if isinstance(c.model, LightGBMModel):
        model: dict[str, Any] = c.model.as_dict()
        text = c.model.text
    elif isinstance(c.model, LogisticModel):
        model = c.model.as_dict()
    else:
        model = {"kind": "baseline", **dict(c.model)}
    return (
        {
            "kind": kind,
            "model": model,
            "calibrator": c.calibrator.as_dict() if c.calibrator else None,
            "threshold": c.threshold,
            "interval": c.interval.as_dict() if c.interval else None,
            "ood": ood.as_dict(),
            "feature_reference": ref,
            "metrics": c.metrics,
            "checks": c.checks,
            "comparisons": c.comparisons,
        },
        text,
    )


def train_and_register(
    ds: Dataset,
    loaded: LoadedForecastingConfig,
    registry: Registry,
    *,
    origin: DataOrigin,
    trained_at: datetime,
    horizons: tuple[int, ...] | None = None,
    families: tuple[str, ...] | None = None,
) -> list[ScopeResult]:
    """Train and register every scope with enough data. Never promotes."""
    cfg = loaded.config
    if origin is DataOrigin.REAL and ds.n and bool(np.any(ds.meta["synthetic"])):
        raise ValueError("synthetic rows cannot be mixed into a real (production) training run")
    out: list[ScopeResult] = []
    for fam in sorted({str(f) for f in ds.meta["family"]}) if ds.n else []:
        if families and fam not in families:
            continue
        for gran in sorted({str(g) for g in ds.meta["granularity"][ds.meta["family"] == fam]}):
            sub = ds.subset((ds.meta["family"] == fam) & (ds.meta["granularity"] == gran))
            step = 1.0 if gran == "hour" else 24.0
            for h in horizons or cfg.horizons_hours:
                if h < step or abs(h / step - round(h / step)) > 1e-9:
                    out.append(
                        ScopeResult(
                            fam,
                            gran,
                            h,
                            "all",
                            None,
                            notes=["horizon not supported for this granularity"],
                        )
                    )
                    continue
                split = chronological_split(
                    sub,
                    train_fraction=cfg.splits.train_fraction,
                    calibration_fraction=cfg.splits.calibration_fraction,
                    embargo_hours=h + cfg.splits.embargo_extra_hours,
                    unseen_group_fraction=cfg.splits.unseen_group_fraction,
                    seed=cfg.splits.seed,
                )
                fit_mask = split.train | split.validation
                ood = OODReference.fit(
                    sub.X[fit_mask],
                    sub.feature_names,
                    {str(c) for c in sub.meta["category"][fit_mask]},
                    {gran},
                    cfg.uncertainty,
                )
                ref = feature_reference(sub.X[fit_mask], sub.feature_names)
                for task, tt in (
                    ("breakout", TargetType.BREAKOUT),
                    ("continuation", TargetType.CONTINUATION),
                ):
                    rep = train_classifier(sub, split, task=task, horizon=h, cfg=cfg)
                    res = ScopeResult(fam, gran, h, task, rep, split=split.describe())
                    for c in rep.candidates:
                        if c.model_type is ModelType.CLIMATOLOGY:
                            continue
                        meta = _metadata(
                            c,
                            rep,
                            sub,
                            split,
                            loaded,
                            target_type=tt,
                            origin=origin,
                            trained_at=trained_at,
                        )
                        payload, text = _payload(c, ood, ref, f"{task}_classifier")
                        res.registered.append(registry.register(meta, payload, text))
                    if task == "breakout" and rep.candidates:
                        res.rolling = rolling_evaluation(sub, h, cfg_loaded=loaded)
                    out.append(res)
                trep = train_trajectory(sub, split, horizon=h, cfg=cfg)
                tres = ScopeResult(fam, gran, h, "trajectory", trep, split=split.describe())
                for c in trep.candidates:
                    meta = _metadata(
                        c,
                        trep,
                        sub,
                        split,
                        loaded,
                        target_type=TargetType.TRAJECTORY,
                        origin=origin,
                        trained_at=trained_at,
                    )
                    payload, text = _payload(c, ood, ref, "trajectory")
                    tres.registered.append(registry.register(meta, payload, text))
                out.append(tres)
            # peak / persistence analogs (one per family x granularity)
            out.append(_train_analogs(sub, loaded, registry, origin=origin, trained_at=trained_at))
    return out


def _train_analogs(
    sub: Dataset,
    loaded: LoadedForecastingConfig,
    registry: Registry,
    *,
    origin: DataOrigin,
    trained_at: datetime,
) -> ScopeResult:
    cfg = loaded.config
    fam, gran = str(sub.meta["family"][0]), str(sub.meta["granularity"][0])
    w = cfg.targets.peak_window_hours
    split = chronological_split(
        sub,
        train_fraction=cfg.splits.train_fraction + cfg.splits.calibration_fraction,
        calibration_fraction=1e-9,
        embargo_hours=w + cfg.splits.embargo_extra_hours,
        unseen_group_fraction=cfg.splits.unseen_group_fraction,
        seed=cfg.splits.seed,
    )
    res = ScopeResult(fam, gran, w, "peak", None, split=split.describe())
    model = AnalogModel.fit(
        sub,
        split.train | split.validation,
        window_hours=w,
        k=cfg.windows.analog_k,
        min_analogs=cfg.windows.min_analogs,
        max_rows=cfg.windows.max_analogs_stored,
        seed=cfg.splits.seed,
    )
    metrics = evaluate_peaks(model, sub, split.test)
    if model.states.shape[0] < cfg.windows.min_analogs:
        res.notes.append("too few historical states for peak / window estimates")
        return res
    checks = {
        "enough_identified_peaks": (metrics.get("identified_peaks") or 0) >= 30,
        "interval_coverage_reported": metrics.get("interval_coverage") is not None,
    }
    status = ModelStatus.VALIDATED if all(checks.values()) else ModelStatus.EXPERIMENTAL
    meta = ModelMetadata(
        model_id=new_model_id("analog_peak_window", w, gran),
        model_name="analog_peak_window",
        version=f"{FORECAST_VERSION}+{sub.manifest.get('dataset_version', 'unknown')}",
        model_type=ModelType.ANALOG_PEAK,
        target_family=TargetFamily(fam),
        target_type=TargetType.PEAK,
        target_definition=definition(cfg.targets, TimeGranularity(gran)),
        horizon_hours=w,
        granularity=gran,
        data_origin=origin,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        feature_names=sub.feature_names,
        dataset_version=str(sub.manifest.get("dataset_version", "unknown")),
        dataset_hash=str(sub.manifest.get("rows_hash", "")),
        training_period=_period(sub, split.train | split.validation),
        calibration_period=Period(start=None, end=None),
        test_period=_period(sub, split.test),
        metrics={**metrics, "analogs_stored": int(model.states.shape[0])},
        trained_at=trained_at,
        code_version=code_version(),
        status=status,
        status_reasons=(
            "experimental capability: peak timing is not independently validated",
            *[f"failed: {k}" for k, ok in checks.items() if not ok],
        ),
    )
    payload = {"kind": "analog", "model": model.as_dict(), "metrics": metrics, "checks": checks}
    res.registered.append(registry.register(meta, payload, None))
    return res


def rolling_evaluation(
    ds: Dataset, horizon: int, *, cfg_loaded: LoadedForecastingConfig
) -> list[dict[str, Any]]:
    """Walk-forward stability: logistic baseline refit per fold (expanding window)."""
    cfg = cfg_loaded.config
    y = ds.labels[f"breakout_{horizon}"]
    out = []
    for i, (tr, ev) in enumerate(
        rolling_origins(
            ds,
            folds=cfg.splits.rolling_folds,
            embargo_hours=horizon + cfg.splits.embargo_extra_hours,
        )
    ):
        trm, evm = tr & np.isfinite(y), ev & np.isfinite(y)
        if trm.sum() < 200 or y[trm].sum() < 10 or evm.sum() < 50:
            out.append({"fold": i, "skipped": "too few rows"})
            continue
        lr = LogisticModel.fit(ds.X[trm], y[trm], ds.feature_names, 0.3)
        m = classification_metrics(y[evm], lr.score(ds.X[evm]), threshold=0.5, k=50)
        out.append(
            {
                "fold": i,
                "rows": int(evm.sum()),
                "positives": int(y[evm].sum()),
                "pr_auc": m["pr_auc"],
                "roc_auc": m["roc_auc"],
            }
        )
    return out


# ------------------------------------------------------------- champion-challenger
def score_model(registry: Registry, model_id: str, x: np.ndarray) -> tuple[np.ndarray, float]:
    """Calibrated probabilities (or scores) and microseconds per row."""
    from trend_agent.forecasting.calibration import Calibrator
    from trend_agent.forecasting.prediction import build_model

    _meta, payload, text = registry.load(model_id)
    model = build_model(payload, text)
    t0 = time.perf_counter()
    raw = model.score(x)
    cal = Calibrator.from_dict(payload["calibrator"]) if payload.get("calibrator") else None
    p = cal.apply(raw) if cal is not None else None
    us = (time.perf_counter() - t0) * 1e6 / max(1, x.shape[0])
    return (p if p is not None else raw), us


def lead_time(
    t: np.ndarray, series: np.ndarray, y: np.ndarray, p: np.ndarray, thr: float
) -> dict[str, float | None]:
    """Per breakout episode (consecutive positive rows of a series): was it flagged, and how
    many hours before the episode's last pre-breakout row was the first alert?"""
    leads, flagged, episodes = [], 0, 0
    for s in np.unique(series):
        m = np.flatnonzero(series == s)
        order = m[np.argsort(t[m])]
        run: list[int] = []
        for i in [*order, -1]:
            if i >= 0 and y[i] == 1:
                run.append(i)
                continue
            if run:
                episodes += 1
                hits = [j for j in run if p[j] >= thr]
                if hits:
                    flagged += 1
                    leads.append(float(t[run[-1]] - t[hits[0]]))
                run = []
    return {
        "episodes": episodes,
        "flagged_share": round(flagged / episodes, 4) if episodes else None,
        "mean_lead_hours": round(float(np.mean(leads)), 2) if leads else None,
    }


def stability(t: np.ndarray, series: np.ndarray, p: np.ndarray) -> float | None:
    diffs = []
    for s in np.unique(series):
        m = np.flatnonzero(series == s)
        order = m[np.argsort(t[m])]
        if order.size > 1:
            diffs.append(np.abs(np.diff(p[order])))
    return round(float(np.mean(np.concatenate(diffs))), 5) if diffs else None


def compare(
    registry: Registry,
    champion_id: str,
    challenger_id: str,
    ds: Dataset,
    loaded: LoadedForecastingConfig,
) -> dict[str, Any]:
    """Both models scored on the same chronological test rows of ``ds``."""
    cfg = loaded.config
    champ, _, _ = registry.load(champion_id)
    chall, _, _ = registry.load(challenger_id)
    if champ.feature_names != chall.feature_names or tuple(ds.feature_names) != champ.feature_names:
        return {"comparable": False, "reason": "feature schemas differ: models are not comparable"}
    h = chall.horizon_hours
    split = chronological_split(
        ds,
        train_fraction=cfg.splits.train_fraction,
        calibration_fraction=cfg.splits.calibration_fraction,
        embargo_hours=h + cfg.splits.embargo_extra_hours,
        unseen_group_fraction=cfg.splits.unseen_group_fraction,
        seed=cfg.splits.seed,
    )
    y = ds.labels[f"{chall.target_type.value}_{h}"]
    m = split.test & np.isfinite(y)
    x, yt = ds.X[m], y[m]
    k = cfg.promotion.precision_at_k
    out: dict[str, Any] = {
        "comparable": True,
        "test_rows": int(m.sum()),
        "test_positives": int(yt.sum()),
    }
    probs = {}
    for role, mid in (("champion", champion_id), ("challenger", challenger_id)):
        meta, payload, _ = registry.load(mid)
        p, us = score_model(registry, mid, x)
        probs[role] = p
        thr = float(payload.get("threshold") or 0.5)
        metrics = classification_metrics(yt, p, threshold=thr, k=k)
        metrics.pop("reliability", None)
        metrics["inference_us_per_row"] = round(us, 2)
        metrics["lead_time"] = lead_time(
            ds.meta["t"][m].astype(float), ds.meta["series_id"][m], yt, p, thr
        )
        metrics["stability_mean_abs_change"] = stability(
            ds.meta["t"][m].astype(float), ds.meta["series_id"][m], p
        )
        out[role] = {"model_id": mid, "name": meta.model_name, "metrics": metrics}
    pr = cfg.promotion
    diff = paired_bootstrap(
        yt,
        probs["challenger"],
        probs["champion"],
        ds.meta["series_id"][m],
        metric="brier",
        samples=pr.bootstrap_samples,
        seed=29,
        ci=pr.ci_level,
    )
    out["brier_difference_challenger_minus_champion"] = diff
    cm, hm = out["challenger"]["metrics"], out["champion"]["metrics"]
    criteria = {
        "challenger_validated": chall.status in (ModelStatus.VALIDATED, ModelStatus.CHALLENGER),
        "brier_better_with_ci": diff["upper"] is not None and diff["upper"] < 0,
        "precision_at_k_not_worse": (cm.get(f"precision_at_{k}") or 0)
        >= (hm.get(f"precision_at_{k}") or 0) - pr.max_precision_drop,
        "false_positive_rate_not_worse": (cm.get("false_positive_rate") or 0)
        <= (hm.get("false_positive_rate") or 0) + 0.01,
        "calibration_within_limit": (cm.get("ece") if cm.get("ece") is not None else math.inf)
        <= pr.max_ece,
        "same_data_origin": champ.data_origin is chall.data_origin,
    }
    out["criteria"] = criteria
    out["promote"] = all(criteria.values())
    out["note"] = (
        "Promotion uses held-out Brier score with a series-block bootstrap, precision@K, false "
        "positives and calibration; ROC-AUC alone is never sufficient."
    )
    return out


def promote_selected(
    registry: Registry,
    results: list[ScopeResult],
    ds: Dataset,
    loaded: LoadedForecastingConfig,
    *,
    at: datetime,
) -> list[dict[str, Any]]:
    """Explicit promotion step for the selected candidate of each scope."""
    decisions: list[dict[str, Any]] = []
    for res in results:
        if res.task == "peak":
            for m in res.registered:
                if m.status is not ModelStatus.VALIDATED:
                    continue
                current = registry.champion(
                    family=m.target_family.value,
                    granularity=m.granularity,
                    horizon=m.horizon_hours,
                    target_type="peak",
                    data_origin=m.data_origin,
                )
                if current is None:
                    registry.promote(
                        m.model_id,
                        at=at,
                        reason="initial experimental peak model",
                        evidence=dict(m.metrics),
                    )
                    decisions.append({"scope": _scope(res), "promoted": m.model_id})
                else:
                    registry.mark(
                        m.model_id,
                        ModelStatus.CHALLENGER,
                        at=at,
                        reason="a peak model is already in use",
                    )
            continue
        if res.report is None or res.report.selected is None:
            continue
        chosen = next(
            (
                m
                for m in res.registered
                if m.model_name == _NAMES.get(_type_of(res, m), m.model_name)
                and _candidate_name(res, m) == res.report.selected
            ),
            None,
        )
        if chosen is None or chosen.status is not ModelStatus.VALIDATED:
            continue
        current = registry.champion(
            family=chosen.target_family.value,
            granularity=chosen.granularity,
            horizon=chosen.horizon_hours,
            target_type=chosen.target_type.value,
            data_origin=chosen.data_origin,
        )
        if current is None:
            registry.promote(
                chosen.model_id,
                at=at,
                reason="initial champion: passed the acceptance criteria",
                evidence={"checks": "see model metrics"},
            )
            decisions.append({"scope": _scope(res), "promoted": chosen.model_id, "replaced": None})
            continue
        if chosen.target_type in (TargetType.BREAKOUT, TargetType.CONTINUATION):
            sub = ds.subset(
                (ds.meta["family"] == res.family) & (ds.meta["granularity"] == res.granularity)
            )
            cmp = compare(registry, current.model_id, chosen.model_id, sub, loaded)
        else:
            cur_mae = (current.metrics or {}).get("mae")
            new_mae = (chosen.metrics or {}).get("mae")
            cmp = {
                "comparable": True,
                "promote": cur_mae is not None and new_mae is not None and new_mae < cur_mae,
                "criteria": {"mae_better": (new_mae or 1e9) < (cur_mae or 0)},
            }
        if cmp.get("promote"):
            registry.promote(
                chosen.model_id, at=at, reason="challenger beat the champion", evidence=cmp
            )
            decisions.append(
                {
                    "scope": _scope(res),
                    "promoted": chosen.model_id,
                    "replaced": current.model_id,
                    "comparison": cmp,
                }
            )
        else:
            registry.mark(
                chosen.model_id,
                ModelStatus.CHALLENGER,
                at=at,
                reason="did not beat the champion",
                evidence=cmp,
            )
            decisions.append(
                {
                    "scope": _scope(res),
                    "promoted": None,
                    "kept": current.model_id,
                    "comparison": cmp,
                }
            )
    return decisions


def _scope(res: ScopeResult) -> str:
    return f"{res.family}/{res.granularity}/{res.horizon}h/{res.task}"


def _type_of(res: ScopeResult, m: ModelMetadata) -> ModelType:
    return m.model_type


def _candidate_name(res: ScopeResult, m: ModelMetadata) -> str:
    inverse = {
        ModelType.LOGISTIC_REGRESSION: "logistic_regression",
        ModelType.LIGHTGBM_CLASSIFIER: "lightgbm",
        ModelType.LIGHTGBM_REGRESSOR: "lightgbm_regressor",
        ModelType.PERSISTENCE: "persistence",
        ModelType.SEASONAL_NAIVE: "seasonal_naive",
        ModelType.DAMPED_TREND: "damped_trend",
    }
    return inverse.get(m.model_type, m.model_name)
