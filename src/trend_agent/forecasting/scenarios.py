"""Deterministic SYNTHETIC scenarios A-M for the forecasting pipeline (demo and tests).

Every series is fictional and labelled synthetic. Models are trained on the isolated
synthetic corpus (``data_origin = synthetic``); the checks verify behaviour (abstention,
labelling, censoring, leakage, promotion rules), not real-world accuracy.
"""

from __future__ import annotations

import math
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from trend_agent.detection.config import LoadedDetectionConfig
from trend_agent.forecasting import synthetic as syn
from trend_agent.forecasting.calibration import fit as fit_calibration
from trend_agent.forecasting.config import LoadedForecastingConfig
from trend_agent.forecasting.dataset_builder import Dataset, build_dataset, row_features
from trend_agent.forecasting.evaluation import chronological_split
from trend_agent.forecasting.feature_store import PointInTimeSeries, SeriesBundle, hours
from trend_agent.forecasting.model_registry import ArtifactStore, MemoryModelStore, Registry
from trend_agent.forecasting.model_selection import promote_selected, train_and_register
from trend_agent.forecasting.prediction import ForecastEngine
from trend_agent.forecasting.training import train_classifier
from trend_agent.monitoring.replay import AvailabilityMode
from trend_agent.schemas.enums import ScaleType, TimeGranularity
from trend_agent.schemas.forecast import (
    CalibrationStatus,
    ForecastOutput,
    ForecastStatus,
    OpportunityTiming,
    PeakStatus,
)
from trend_agent.schemas.model_metadata import DataOrigin, ModelStatus
from trend_agent.schemas.monitoring import MeasurementCategory

M = MeasurementCategory
H, D = TimeGranularity.HOUR, TimeGranularity.DAY


@dataclass
class ScenarioResult:
    key: str
    name: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    outputs: list[ForecastOutput] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.checks)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, bool(ok), detail))


@dataclass
class Context:
    loaded: LoadedForecastingConfig
    dloaded: LoadedDetectionConfig
    registry: Registry
    engine: ForecastEngine
    dataset: Dataset
    results: list[Any]
    decisions: list[dict[str, Any]]
    trained_at: datetime
    timings: dict[str, float]


def prepare(
    loaded: LoadedForecastingConfig,
    dloaded: LoadedDetectionConfig,
    *,
    scale: float = 0.5,
    workers: int = 1,
    artifact_dir: str | None = None,
    trained_at: datetime | None = None,
) -> Context:
    """Train models on the synthetic corpus and promote explicitly (isolated experiment)."""
    c = loaded.config.corpus
    t0 = time.perf_counter()
    bundles = syn.materialise(
        syn.corpus(
            hourly=max(12, int(c.hourly_series * scale)),
            daily=max(6, int(c.daily_series * scale)),
            hourly_days=c.hourly_days,
            daily_days=c.daily_days,
            seed=c.seed,
            allocation=loaded.config.category_allocation,
        )
    )
    ds = build_dataset(bundles, loaded, dloaded, source="synthetic", audit_rows=6, workers=workers)
    t1 = time.perf_counter()
    registry = Registry(MemoryModelStore(), ArtifactStore(artifact_dir or tempfile.mkdtemp()))
    at = trained_at or syn.START + timedelta(days=c.daily_days)
    results = train_and_register(ds, loaded, registry, origin=DataOrigin.SYNTHETIC, trained_at=at)
    decisions = promote_selected(registry, results, ds, loaded, at=at)
    t2 = time.perf_counter()
    return Context(
        loaded,
        dloaded,
        registry,
        ForecastEngine(loaded, dloaded, registry),
        ds,
        results,
        decisions,
        at,
        {"dataset_seconds": round(t1 - t0, 2), "training_seconds": round(t2 - t1, 2)},
    )


def _bundle(
    spec: syn.Spec, sid: int, support: dict[MeasurementCategory, syn.Spec] | None = None
) -> SeriesBundle:
    sb, _ = syn.Bundle(spec, support or {}).build(sid, sid * 100_000)
    return sb


def _at(spec: syn.Spec, hours_after_start: float) -> datetime:
    return spec.start + timedelta(hours=hours_after_start)


def _by_h(outs: list[ForecastOutput]) -> dict[int, ForecastOutput]:
    return {o.forecast_horizon_hours: o for o in outs}


# ----------------------------------------------------------------------- scenarios
def scenario_a(ctx: Context) -> ScenarioResult:
    r = ScenarioResult("A", "Explosive AI model launch (fictional coding model)")
    onset = 24 * 30
    spec = syn.Spec(
        key="scn_a_launch",
        seed=101,
        periods=24 * 34,
        base=18,
        events=(syn.Event("launch", onset, 3.5, rise_h=36, decay_h=96),),
        category="ai",
        query="fictional ai coding model tutorial",
    )
    sb = _bundle(spec, 9101)
    outs = _by_h(ctx.engine.forecast(sb, _at(spec, onset + 22)))
    r.outputs = list(outs.values())
    o24, o72 = outs[24], outs[72]
    base_rate = _base_rate(ctx, "hour", 24)
    p = o24.breakout.probability if o24.breakout else None
    elevated = o24.breakout is not None and not o24.breakout.applicable
    r.check(
        "strong breakout signal or already elevated",
        elevated or (p is not None and p >= max(0.25, 5 * base_rate)),
        f"p24={p} base_rate={base_rate:.3f} elevated={elevated}",
    )
    r.check(
        "uncertainty reported",
        o72.uncertainty.ood_status != ""
        and bool(o72.trajectory)
        and o72.trajectory[0].lower is not None,
        o72.uncertainty.ood_status,
    )
    r.check(
        "no search counts claimed",
        o24.data_quality.measurement_unit == "relative_search_interest",
        o24.notice[:60],
    )
    r.check("labelled synthetic", all(o.synthetic for o in outs.values()), "")
    return r


def scenario_b(ctx: Context) -> ScenarioResult:
    r = ScenarioResult("B", "Gradual AI tool adoption")
    onset = 24 * 22
    spec = syn.Spec(
        key="scn_b_adoption",
        seed=102,
        periods=24 * 40,
        base=20,
        events=(syn.Event("adoption", onset, 1.2, length_h=300),),
        category="ai",
        query="fictional agent framework tutorial",
    )
    sb = _bundle(spec, 9102)
    outs = _by_h(ctx.engine.forecast(sb, _at(spec, onset + 120)))
    r.outputs = list(outs.values())
    o72 = outs[72]
    f = o72.features
    r.check(
        "growth persistence recognised",
        (f.get("slope_recent_log_per_h") or 0) > 0 or (f.get("change_72h_log") or 0) > 0,
        f"slope={f.get('slope_recent_log_per_h')}, change72={f.get('change_72h_log')}",
    )
    traj = o72.trajectory[0] if o72.trajectory else None
    level = (o72.evidence.get("reference") or {}).get("level")
    r.check(
        "trajectory not forecasting a collapse",
        traj is not None
        and level is not None
        and traj.value is not None
        and traj.value >= 0.8 * level,
        f"value={traj.value if traj else None} level={level}",
    )
    pe = o72.peak_estimate
    r.check(
        "no false immediate-peak claim",
        pe is None or not (pe.status is PeakStatus.ESTIMATED and (pe.latest_hours or 0) < 12),
        pe.status.value if pe else "none",
    )
    return r


def scenario_c(ctx: Context) -> ScenarioResult:
    r = ScenarioResult("C", "Limited-time gaming event (fictional battle-royale season event)")
    onset = 24 * 26
    spec = syn.Spec(
        key="scn_c_event",
        seed=103,
        periods=24 * 34,
        base=22,
        diurnal=0.3,
        events=(syn.Event("limited", onset, 2.0, rise_h=12, length_h=96),),
        category="gaming",
        query="fictional arena event challenges",
    )
    sb = _bundle(spec, 9103)
    outs = _by_h(ctx.engine.forecast(sb, _at(spec, onset + 84)))  # 12 h before the event ends
    r.outputs = list(outs.values())
    o6, o72 = outs[6], outs[72]
    win = {w.production_profile: w for w in o72.opportunity_windows}
    q, d = win.get("QUICK_REACTION"), win.get("DEEP_DIVE")
    pq = q.probability_elevated_at_publication if q else None
    pd = d.probability_elevated_at_publication if d else None
    r.check(
        "opportunity windows estimated",
        q is not None and d is not None,
        f"quick={q.status.value if q else None} deep={d.status.value if d else None}",
    )
    r.check(
        "short event: deep dive no more likely than a quick reaction (±0.05 analog noise)",
        pq is None or pd is None or pd <= pq + 0.05,
        f"quick={pq} deep={pd}",
    )
    r.check(
        "deep dive not a longer window",
        d is None or d.status is not OpportunityTiming.LONGER_WINDOW,
        d.status.value if d else "none",
    )
    w6 = _width(o6)
    w72 = _width(o72)
    r.check(
        "relative uncertainty grows with the horizon",
        w6 is None or w72 is None or w72 >= w6,
        f"width6={w6} width72={w72}",
    )
    return r


def scenario_d(ctx: Context) -> ScenarioResult:
    r = ScenarioResult("D", "Evergreen programming tutorial (weekly seasonality)")
    spec = syn.Spec(
        key="scn_d_evergreen",
        seed=104,
        granularity=D,
        periods=200,
        base=35,
        weekly=0.35,
        noise=0.05,
        category="technology",
        query="fictional programming language tutorial",
    )
    sb = _bundle(spec, 9104)
    ds = build_dataset([sb], ctx.loaded, ctx.dloaded, source="synthetic", audit_rows=0)
    y = ds.labels["trajectory_72"]
    seas = ds.baselines["seasonal_72"]
    ok = np.isfinite(y) & np.isfinite(seas)
    mae_seas = float(np.mean(np.abs(seas[ok] - y[ok]))) if ok.any() else math.nan
    mae_pers = float(np.mean(np.abs(y[ok]))) if ok.any() else math.nan
    r.details["mae"] = {"seasonal_naive": round(mae_seas, 4), "persistence": round(mae_pers, 4)}
    r.check(
        "seasonal baseline competitive",
        mae_seas <= mae_pers * 1.05,
        f"seasonal {mae_seas:.3f} vs persistence {mae_pers:.3f}",
    )
    outs = _by_h(ctx.engine.forecast(sb, _at(spec, 24 * 190)))
    r.outputs = list(outs.values())
    probs = [
        o.breakout.probability
        for o in outs.values()
        if o.breakout and o.breakout.probability is not None
    ]
    r.check(
        "no unjustified breakout forecast", all(p < 0.2 for p in probs), f"probabilities={probs}"
    )
    r.check(
        "daily series: no 6 h forecast",
        outs[6].status is ForecastStatus.UNSUPPORTED_GRANULARITY,
        outs[6].status.value,
    )
    return r


def scenario_e(ctx: Context) -> ScenarioResult:
    r = ScenarioResult("E", "False viral signal (video activity rises, search does not)")
    onset = 24 * 28
    search = syn.Spec(
        key="scn_e_search",
        seed=105,
        periods=24 * 32,
        base=20,
        category="gaming",
        query="fictional viral clip game",
    )
    video = syn.Spec(
        key="scn_e_video",
        seed=205,
        periods=24 * 32,
        base=15,
        noise=0.12,
        events=(syn.Event("spike", onset, 4.0, length_h=30),),
        measurement=M.VIDEO_ACTIVITY,
        scale=ScaleType.ABSOLUTE_COUNT,
        category="gaming",
        query="fictional viral clip game",
    )
    s_b = _bundle(search, 9105, {M.VIDEO_ACTIVITY: video})
    v_b = _bundle(video, 9106)
    at = _at(search, onset + 10)
    so = _by_h(ctx.engine.forecast(s_b, at))
    vo = _by_h(ctx.engine.forecast(v_b, at))
    r.outputs = list(so.values()) + list(vo.values())
    r.check(
        "proxy forecast labelled proxy",
        all(
            o.is_proxy
            and o.target_family == "proxy_attention"
            and o.source_classification.value == "VIDEO_ACTIVITY"
            for o in vo.values()
        ),
        vo[24].target_family,
    )
    r.check(
        "no YouTube Search forecast from video activity",
        all(o.target_family != "youtube_search_interest" for o in vo.values()),
        "",
    )
    p = so[24].breakout.probability if so[24].breakout else None
    r.check("direct search series shows no breakout", p is None or p < 0.3, f"p24={p}")
    r.check(
        "video change visible only as a supporting feature",
        so[24].features.get("video_change_24h_log") is not None,
        "",
    )
    return r


def scenario_f(ctx: Context) -> ScenarioResult:
    r = ScenarioResult("F", "Sparse history (newly discovered query)")
    spec = syn.Spec(
        key="scn_f_sparse",
        seed=106,
        periods=24 * 3,
        base=12,
        category="ai",
        query="fictional brand-new model",
    )
    outs = _by_h(ctx.engine.forecast(_bundle(spec, 9107), _at(spec, 24 * 3)))
    r.outputs = list(outs.values())
    r.check(
        "insufficient history",
        all(o.status is ForecastStatus.INSUFFICIENT_HISTORY for o in outs.values()),
        ", ".join(o.status.value for o in outs.values()),
    )
    r.check("no probability", all(o.breakout is None for o in outs.values()), "")
    return r


def scenario_g(ctx: Context) -> ScenarioResult:
    r = ScenarioResult("G", "Provider delay (Monday's value published on Wednesday)")
    start = syn.START  # a Monday
    monday = 42  # day index of a Monday (6 weeks in)
    spec = syn.Spec(
        key="scn_g_delay",
        seed=107,
        granularity=D,
        periods=60,
        base=30,
        lag_h=10,
        delayed=((monday, 24.0),),
        category="technology",
        start=start,
        query="fictional delayed series",
    )
    sb = _bundle(spec, 9108)
    pit = PointInTimeSeries(sb.points, AvailabilityMode.PUBLICATION)
    monday_end = hours(start + timedelta(days=monday + 1))
    tuesday = start + timedelta(days=monday + 1, hours=23)
    wednesday = start + timedelta(days=monday + 2, hours=12)  # published Wed 10:00
    seen_tue = bool(np.any(np.isclose(pit.at(tuesday).t_end, monday_end, rtol=0.0, atol=1e-6)))
    seen_wed = bool(np.any(np.isclose(pit.at(wednesday).t_end, monday_end, rtol=0.0, atol=1e-6)))
    r.check("Tuesday forecast cannot see Monday's delayed value", not seen_tue, "")
    r.check("visible once published (Wednesday)", seen_wed, "")
    cut = SeriesBundle(
        sb.meta, [p for p in sb.points if p.period_end != start + timedelta(days=monday + 1)]
    )
    a = row_features(sb, pit, None, {}, tuesday, ctx.loaded, ctx.dloaded, strict=True)[2]
    b = row_features(
        cut, PointInTimeSeries(cut.points), None, {}, tuesday, ctx.loaded, ctx.dloaded, strict=True
    )[2]
    r.check(
        "Tuesday features identical with or without the late value",
        np.allclose(a.vector(), b.vector(), equal_nan=True),
        "",
    )
    return r


def scenario_h(ctx: Context) -> ScenarioResult:
    r = ScenarioResult("H", "Normalization mismatch (re-based scale / incompatible series)")
    spec = syn.Spec(
        key="scn_h_norm",
        seed=108,
        periods=24 * 30,
        base=25,
        category="ai",
        normalization_change_at=24 * 28,
        query="fictional rebased series",
    )
    outs = _by_h(ctx.engine.forecast(_bundle(spec, 9109), _at(spec, 24 * 30)))
    r.outputs = list(outs.values())
    r.check(
        "forecast refused after a normalization change",
        all(o.status is ForecastStatus.FORECAST_UNAVAILABLE for o in outs.values()),
        "; ".join(outs[24].status_reasons),
    )
    target = syn.Spec(
        key="scn_h_target",
        seed=109,
        periods=24 * 30,
        base=25,
        category="ai",
        query="fictional comparison",
    )
    ext = syn.Spec(
        key="scn_h_ext",
        seed=110,
        periods=24 * 30,
        base=60,
        category="ai",
        measurement=M.EXTERNAL_SEARCH_INTEREST,
        scale=ScaleType.RELATIVE_PER_REQUEST,
        query="fictional comparison",
    )
    sb = _bundle(target, 9110, {M.EXTERNAL_SEARCH_INTEREST: ext})
    pit = PointInTimeSeries(sb.points)
    sup = {M.EXTERNAL_SEARCH_INTEREST: PointInTimeSeries(sb.supporting[M.EXTERNAL_SEARCH_INTEREST])}
    row = row_features(
        sb, pit, None, sup, _at(target, 24 * 29), ctx.loaded, ctx.dloaded, strict=True
    )[2]
    r.check(
        "cross-series level ratio rejected",
        math.isnan(row.values["cross_level_ratio_log"])
        and any("cross_level_ratio_log rejected" in x for x in row.rejected),
        "; ".join(row.rejected)[:160],
    )
    r.check(
        "within-series external change still available",
        not math.isnan(row.values["ext_change_24h_log"]),
        "",
    )
    return r


def scenario_i(ctx: Context) -> ScenarioResult:
    r = ScenarioResult("I", "Model overfitting (good in training, poor in chronological test)")
    sub = ctx.dataset.subset(ctx.dataset.meta["granularity"] == "hour")
    cfg = ctx.loaded.config
    split = chronological_split(
        sub,
        train_fraction=cfg.splits.train_fraction,
        calibration_fraction=cfg.splits.calibration_fraction,
        embargo_hours=24 + cfg.splits.embargo_extra_hours,
        unseen_group_fraction=0.0,
        seed=cfg.splits.seed,
    )
    y = sub.labels["breakout_24"].copy()
    rng = np.random.default_rng(7)
    m = split.test & np.isfinite(y)
    y[m] = rng.permutation(y[m])  # the test period no longer follows the training relation
    broken = Dataset(
        sub.X,
        sub.feature_names,
        {**sub.labels, "breakout_24": y},
        sub.meta,
        sub.manifest,
        sub.baselines,
    )
    rep = train_classifier(broken, split, task="breakout", horizon=24, cfg=cfg)
    gbm = next((c for c in rep.candidates if c.name == "lightgbm"), None)
    r.details["lightgbm"] = gbm.metrics if gbm else None
    r.check(
        "LightGBM fits the training period",
        gbm is not None
        and (gbm.metrics.get("train_pr_auc") or 0) > (gbm.metrics.get("pr_auc") or 0),
        f"train={gbm.metrics.get('train_pr_auc') if gbm else None} "
        f"test={gbm.metrics.get('pr_auc') if gbm else None}",
    )
    r.check(
        "promotion rejected",
        gbm is not None and not gbm.validated,
        ", ".join(gbm.reasons[:3]) if gbm else "",
    )
    r.check("no model selected (baseline retained)", rep.selected is None, str(rep.selected))
    return r


def scenario_j(ctx: Context) -> ScenarioResult:
    r = ScenarioResult("J", "Distribution shift (new kind of gaming event)")
    onset = 24 * 27
    spec = syn.Spec(
        key="scn_j_shift",
        seed=111,
        periods=24 * 30,
        base=4,
        noise=0.4,
        diurnal=0.0,
        events=(syn.Event("spike", onset, 22.0, length_h=60),),
        category="gaming",
        query="fictional unprecedented event",
    )
    outs = _by_h(ctx.engine.forecast(_bundle(spec, 9111), _at(spec, onset + 30)))
    r.outputs = list(outs.values())
    statuses = {o.uncertainty.ood_status for o in outs.values()}
    r.check(
        "distribution shift flagged",
        statuses & {"out_of_distribution", "limited"} != set(),
        ", ".join(sorted(statuses)),
    )
    withheld = all(
        o.status is not ForecastStatus.OUT_OF_DISTRIBUTION
        or (o.breakout is None or o.breakout.probability is None)
        for o in outs.values()
    )
    r.check("probability withheld when out of distribution", withheld, "")
    return r


def scenario_k(ctx: Context) -> ScenarioResult:
    r = ScenarioResult("K", "Peak beyond the forecast horizon (still growing)")
    onset = 24 * 20
    spec = syn.Spec(
        key="scn_k_growth",
        seed=112,
        periods=24 * 45,
        base=12,
        events=(syn.Event("adoption", onset, 3.0, length_h=480),),
        category="ai",
        query="fictional fast-growing tool",
    )
    sb = _bundle(spec, 9112)
    t = onset + 150
    outs = _by_h(ctx.engine.forecast(sb, _at(spec, t)))
    r.outputs = list(outs.values())
    pe = outs[72].peak_estimate
    final_t, final_v = PointInTimeSeries(sb.points).final()
    now = hours(_at(spec, t))
    win = (final_t > now) & (final_t <= now + 72)
    truth_censored = bool(np.argmax(final_v[win]) >= np.sum(win) - 6) if win.any() else None
    r.details["actual_peak_at_window_end"] = truth_censored
    r.details["peak"] = pe.model_dump(mode="json") if pe else None
    r.check(
        "no confirmed peak inside the window",
        pe is None
        or pe.status is not PeakStatus.ESTIMATED
        or (pe.probability_beyond_horizon or 0) >= 0.3
        or (pe.latest_hours or 0) >= 60,
        pe.status.value if pe else "none",
    )
    r.check(
        "censoring stated",
        pe is None
        or pe.status is not PeakStatus.ESTIMATED
        or pe.probability_beyond_horizon is not None,
        "",
    )
    return r


def scenario_l(ctx: Context) -> ScenarioResult:
    r = ScenarioResult("L", "Insufficient calibration data (too few labelled breakouts)")
    rng = np.random.default_rng(12)
    scores = rng.uniform(0, 1, 300)
    y = np.zeros(300)
    y[:8] = 1  # 8 positives < min_positives
    cal = fit_calibration(scores, y, ctx.loaded.config.calibration)
    r.check(
        "calibration refused",
        cal.status is CalibrationStatus.INSUFFICIENT_CALIBRATION_DATA,
        cal.status.value,
    )
    r.check("no probability mapping", cal.apply(scores) is None, "")
    # a model whose calibration was refused never reports a probability
    breakout_models = [m for m in ctx.registry.store.all() if m.target_type.value == "breakout"]
    uncal = [
        m
        for m in breakout_models
        if (m.calibration or {}).get("status") == "INSUFFICIENT_CALIBRATION_DATA"
    ]
    r.check(
        "uncalibrated models are not validated",
        all(m.status is ModelStatus.EXPERIMENTAL for m in uncal),
        f"{len(uncal)} such models",
    )
    return r


def scenario_m(ctx: Context) -> ScenarioResult:
    r = ScenarioResult("M", "Mixed temporal resolutions (hourly and daily providers)")
    hourly = syn.Spec(
        key="scn_m_hourly",
        seed=113,
        periods=24 * 30,
        base=20,
        category="ai",
        query="fictional mixed resolution",
    )
    daily = syn.Spec(
        key="scn_m_daily",
        seed=114,
        granularity=D,
        periods=60,
        base=20,
        category="ai",
        query="fictional mixed resolution",
    )
    at = _at(hourly, 24 * 30)
    ho = _by_h(ctx.engine.forecast(_bundle(hourly, 9113), at))
    do = _by_h(ctx.engine.forecast(_bundle(daily, 9114), _at(daily, 24 * 59 + 12)))
    r.outputs = list(ho.values()) + list(do.values())
    r.check(
        "6 h forecast available for hourly data",
        ho[6].status is not ForecastStatus.UNSUPPORTED_GRANULARITY,
        ho[6].status.value,
    )
    r.check(
        "6 h forecast refused for daily data",
        do[6].status is ForecastStatus.UNSUPPORTED_GRANULARITY,
        "; ".join(do[6].status_reasons),
    )
    r.check(
        "daily trajectories only at whole days",
        all(p.lead_hours % 24 == 0 for o in do.values() for p in o.trajectory),
        "",
    )
    return r


SCENARIOS: dict[str, Callable[[Context], ScenarioResult]] = {
    "A": scenario_a,
    "B": scenario_b,
    "C": scenario_c,
    "D": scenario_d,
    "E": scenario_e,
    "F": scenario_f,
    "G": scenario_g,
    "H": scenario_h,
    "I": scenario_i,
    "J": scenario_j,
    "K": scenario_k,
    "L": scenario_l,
    "M": scenario_m,
}


def run(ctx: Context, keys: list[str] | None = None) -> list[ScenarioResult]:
    out = []
    for k, fn in SCENARIOS.items():
        if keys and k not in keys:
            continue
        try:
            out.append(fn(ctx))
        except Exception as exc:  # a scenario failing is a result, not a crash of the demo
            res = ScenarioResult(k, fn.__doc__ or k)
            res.check("scenario ran", False, f"{type(exc).__name__}: {exc}")
            out.append(res)
    return out


def _base_rate(ctx: Context, gran: str, h: int) -> float:
    m = ctx.dataset.meta["granularity"] == gran
    y = ctx.dataset.labels[f"breakout_{h}"][m]
    y = y[np.isfinite(y)]
    return float(y.mean()) if y.size else 0.0


def _width(o: ForecastOutput) -> float | None:
    """Relative (log-scale) interval width: comparable across horizons and levels."""
    if not o.trajectory:
        return None
    p = o.trajectory[0]
    if p.lower is None or p.upper is None:
        return None
    return round(math.log((p.upper + 1.0) / (p.lower + 1.0)), 4)
