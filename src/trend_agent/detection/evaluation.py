"""Evaluation of detection against independent ground truth (synthetic labels only here).

Definitions (all fixed before looking at results):

* **alert** = a notification: an episode first reaching CANDIDATE or CONFIRMED (creation,
  escalation or reopening), at its decision time ``as_of``;
* an alert **matches** a label of the same series when its triggering observation period
  lies within [onset - 1, end + ``tolerance`` periods]; the first matching alert of a
  *surge* label is a true positive, further matching alerts are **duplicates**; alerts
  matching only *ambiguous* labels are excluded; all other alerts are **false alerts**;
* **precision** = TP / (TP + FP); **recall** = detected surge labels / surge labels;
* **detection delay** = decision time - time the first observation of the label became
  available (from the onset, and from the "observable" onset when the effect first reached
  surge size); in days;
* **false alerts per day** = FP / evaluation days, also per 1,000 monitored series;
* **duplication rate** = duplicate alerts per detected label;
* **coverage** = share of series-periods in the window evaluated with a READY baseline;
* **severity usefulness** = Spearman correlation between an episode's maximum severity and
  the label's relative magnitude (true positives).

Only alerts and labels inside the evaluation window count (the warm-up builds baselines).
These are SYNTHETIC results; real-world precision and recall remain unknown without
independently labelled real surges.
"""

from __future__ import annotations

import statistics
import time as _time
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from scipy import stats

from trend_agent.detection.calibration import CalibrationResult, calibrate
from trend_agent.detection.config import LoadedDetectionConfig
from trend_agent.detection.deduplication import EpisodeMeta
from trend_agent.detection.replay_adapter import DetectionReplayer, ReplayRun
from trend_agent.detection.significance import NullLookup
from trend_agent.detection.synthetic import (
    GeneratedSeries,
    GroundTruthLabel,
    SyntheticSeriesSpec,
    benchmark_specs,
    generate,
    null_specs,
    stored_points,
)
from trend_agent.schemas.detection import BaselineReadiness
from trend_agent.schemas.evidence import AlertStage, classify
from trend_agent.schemas.monitoring import MeasurementCategory
from trend_agent.schemas.time_series import StoredPoint

DAY = timedelta(days=1)


@dataclass(frozen=True)
class AlertRecord:
    series_key: str
    episode_id: int
    as_of: datetime
    period_index: int
    stage: AlertStage
    max_severity: float
    confirmed_at: datetime | None = None


@dataclass
class EvaluationMetrics:
    dataset: str
    split: str
    series: int
    days: int
    labels: int
    tp: int = 0
    fp: int = 0
    fn: int = 0
    duplicates: int = 0
    ambiguous_alerts: int = 0
    alerts: int = 0
    precision: float | None = None
    recall: float | None = None
    false_alerts_per_day: float | None = None
    false_alerts_per_1000_series_day: float | None = None
    false_alert_rate_per_series_day: float | None = None
    delay_median_days: float | None = None
    delay_p90_days: float | None = None
    delay_from_observable_median_days: float | None = None
    duplication_rate: float | None = None
    coverage: float | None = None
    severity_spearman: float | None = None
    severity_spearman_p: float | None = None
    confirmed_precision: float | None = None
    confirmed_recall: float | None = None
    confirmed_delay_median_days: float | None = None
    recall_by_kind: dict[str, Any] = field(default_factory=dict)
    false_alerts_by_category: dict[str, int] = field(default_factory=dict)
    budget_held_back: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def alerts_from_run(run: ReplayRun, keys: dict[int, str], origin: datetime) -> list[AlertRecord]:
    """One alert per episode activation: its first notification, and each reopening.

    Escalating an open episode (e.g. CANDIDATE -> CONFIRMED) is a stage change of the same
    alert, not a new alert, so it is not counted again.
    """
    records: dict[tuple[int, int], dict[str, Any]] = {}
    activation: dict[int, int] = {}
    for d in run.decisions:
        ch = d.change
        if ch is None or not ch.notification or ch.episode.id is None:
            continue
        eid = ch.episode.id
        if eid not in activation or ch.kind == "reopened":
            activation[eid] = activation.get(eid, -1) + 1
            records[(eid, activation[eid])] = {
                "series_key": keys[d.series_id],
                "episode_id": eid,
                "as_of": d.as_of,
                "period_index": int((d.evaluation.period_start - origin) / DAY),
                "stage": ch.status,
                "max_severity": ch.episode.maximum_severity,
                "confirmed_at": d.as_of if ch.status is AlertStage.CONFIRMED else None,
            }
        elif ch.status is AlertStage.CONFIRMED:
            rec = records[(eid, activation[eid])]
            if rec["confirmed_at"] is None:
                rec["confirmed_at"] = d.as_of
    return [AlertRecord(**r) for r in records.values()]


def _r(x: float | None, digits: int = 4) -> float | None:
    return None if x is None else round(x, digits)


def evaluate(
    alerts: Sequence[AlertRecord],
    generated: Sequence[GeneratedSeries],
    run: ReplayRun,
    *,
    dataset: str,
    split: str,
    eval_start: int,
    tolerance: int = 3,
    keys: dict[int, str],
    final_severity: dict[int, float] | None = None,
) -> EvaluationMetrics:
    by_key = {g.spec.key: g for g in generated}
    days = max(g.spec.days for g in generated) - eval_start
    labels = [lab for g in generated for lab in g.labels if lab.onset_index >= eval_start]
    surge = [lab for lab in labels if lab.label == "surge"]
    m = EvaluationMetrics(dataset, split, len(generated), days, len(surge))
    window_alerts = [a for a in alerts if a.period_index >= eval_start]
    m.alerts = len(window_alerts)
    first: dict[int, AlertRecord] = {}
    dup = 0
    fp_by_cat: Counter[str] = Counter()
    labels_by_series: dict[str, list[tuple[int, GroundTruthLabel]]] = defaultdict(list)
    for i, lab in enumerate(labels):
        labels_by_series[lab.series_key].append((i, lab))
    for a in sorted(window_alerts, key=lambda x: (x.as_of, x.episode_id)):
        hits = [
            (i, lab)
            for i, lab in labels_by_series.get(a.series_key, [])
            if lab.onset_index - 1 <= a.period_index <= lab.end_index + tolerance
        ]
        surge_hits = [(i, lab) for i, lab in hits if lab.label == "surge"]
        if surge_hits:
            i, _ = surge_hits[0]
            if i in first:
                dup += 1
            else:
                first[i] = a
        elif any(lab.label == "ambiguous" for _, lab in hits):
            m.ambiguous_alerts += 1
        else:
            m.fp += 1
            fp_by_cat[by_key[a.series_key].spec.category.value] += 1
    m.tp = len(first)
    m.fn = len(surge) - m.tp
    m.duplicates = dup
    m.false_alerts_by_category = dict(fp_by_cat)
    if m.tp + m.fp:
        m.precision = _r(m.tp / (m.tp + m.fp))
    if surge:
        m.recall = _r(m.tp / len(surge))
    if days > 0:
        m.false_alerts_per_day = _r(m.fp / days)
        m.false_alerts_per_1000_series_day = _r(m.fp / days / (len(generated) / 1000.0))
        m.false_alert_rate_per_series_day = _r(m.fp / (days * len(generated)), 6)
    delays, observable = [], []
    severity_pairs: list[tuple[float, float]] = []
    kinds_total: Counter[str] = Counter(lab.kind for lab in surge)
    kinds_hit: Counter[str] = Counter()
    for i, a in first.items():
        lab = labels[i]
        g = by_key[lab.series_key]
        delays.append((a.as_of - g.available_at(lab.onset_index)) / DAY)
        if lab.observable_index is not None:
            observable.append((a.as_of - g.available_at(lab.observable_index)) / DAY)
        sev = (final_severity or {}).get(a.episode_id, a.max_severity)
        severity_pairs.append((sev, lab.relative_magnitude))
        kinds_hit[lab.kind] += 1
    if delays:
        delays.sort()
        m.delay_median_days = _r(statistics.median(delays), 2)
        m.delay_p90_days = _r(delays[min(len(delays) - 1, int(0.9 * len(delays)))], 2)
    if observable:
        m.delay_from_observable_median_days = _r(statistics.median(observable), 2)
    if m.tp:
        m.duplication_rate = _r(dup / m.tp)
    if len(severity_pairs) >= 5:
        rho = stats.spearmanr([s for s, _ in severity_pairs], [r for _, r in severity_pairs])
        m.severity_spearman = _r(float(rho.statistic))
        m.severity_spearman_p = float(f"{float(rho.pvalue):.3g}")
    m.recall_by_kind = {
        k: {"labels": n, "detected": kinds_hit[k], "recall": _r(kinds_hit[k] / n)}
        for k, n in sorted(kinds_total.items())
    }
    confirmed = [a for a in window_alerts if a.confirmed_at is not None]
    conf_tp = {
        i
        for a in confirmed
        for i, lab in labels_by_series.get(a.series_key, [])
        if lab.label == "surge"
        and lab.onset_index - 1 <= a.period_index <= lab.end_index + tolerance
    }
    conf_fp = sum(
        1
        for a in confirmed
        if not any(
            lab.onset_index - 1 <= a.period_index <= lab.end_index + tolerance
            for _, lab in labels_by_series.get(a.series_key, [])
        )
    )
    if confirmed:
        m.confirmed_precision = _r(len(conf_tp) / max(1, len(conf_tp) + conf_fp))
    if surge:
        m.confirmed_recall = _r(len(conf_tp) / len(surge))
    conf_delays = []
    for i, a in first.items():
        if a.confirmed_at is not None:
            lab = labels[i]
            conf_delays.append(
                (a.confirmed_at - by_key[lab.series_key].available_at(lab.onset_index)) / DAY
            )
    if conf_delays:
        m.confirmed_delay_median_days = _r(statistics.median(conf_delays), 2)
    ready = total = 0
    for d in run.decisions:
        found = by_key.get(keys[d.series_id])
        if found is None:
            continue
        idx = int((d.evaluation.period_start - found.spec.start) / DAY)
        if idx < eval_start:
            continue
        total += 1
        ready += d.evaluation.baseline.readiness is BaselineReadiness.READY
    if total:
        m.coverage = _r(ready / total)
    return m


# ----------------------------------------------------------------------------- benchmark
@dataclass
class SyntheticWorld:
    generated: list[GeneratedSeries]
    points: dict[int, list[StoredPoint]]
    metas: dict[int, EpisodeMeta]
    keys: dict[int, str]
    categories: dict[int, str]


def build_world(
    specs: Sequence[SyntheticSeriesSpec], *, first_series_id: int = 1
) -> SyntheticWorld:
    generated = [generate(s) for s in specs]
    points: dict[int, list[StoredPoint]] = {}
    metas: dict[int, EpisodeMeta] = {}
    keys: dict[int, str] = {}
    categories: dict[int, str] = {}
    next_id = 1
    for offset, g in enumerate(generated):
        sid = first_series_id + offset
        points[sid] = stored_points(g, series_id=sid, target_id=sid, first_id=next_id)
        next_id += len(g.values)
        spec = g.spec
        proxy = spec.measurement not in (
            MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST,
            MeasurementCategory.EXTERNAL_SEARCH_INTEREST,
        )
        metas[sid] = EpisodeMeta(
            target_id=sid,
            query=spec.key,
            query_id=sid,
            keyword_id=None,
            topic_cluster_id=None,
            category=spec.category,
            region="US",
            language="en",
            provider=spec.provider,
            measurement=spec.measurement,
            is_proxy=proxy,
            classification=classify(spec.measurement, is_proxy=proxy),
            is_synthetic=True,
            step=DAY,
        )
        keys[sid] = spec.key
        categories[sid] = spec.category.value
    return SyntheticWorld(generated, points, metas, keys, categories)


def replay_world(
    world: SyntheticWorld, loaded: LoadedDetectionConfig, nulls: NullLookup | None
) -> tuple[ReplayRun, DetectionReplayer]:
    replayer = DetectionReplayer(loaded, world.metas, nulls=nulls, categories=world.categories)
    origin = min(p[0].period_start for p in world.points.values())
    run = replayer.evaluate_batch(world.points, DAY, origin)
    return run, replayer


def evaluate_world(
    world: SyntheticWorld,
    loaded: LoadedDetectionConfig,
    nulls: NullLookup | None,
    *,
    dataset: str,
    split: str,
    eval_start: int,
) -> EvaluationMetrics:
    run, replayer = replay_world(world, loaded, nulls)
    origin = min(p[0].period_start for p in world.points.values())
    alerts = alerts_from_run(run, world.keys, origin)
    final = {eid: ep.maximum_severity for eid, ep in replayer.store.episodes.items()}
    m = evaluate(
        alerts,
        world.generated,
        run,
        dataset=dataset,
        split=split,
        eval_start=eval_start,
        keys=world.keys,
        final_severity=final,
    )
    m.budget_held_back = replayer.budget.held_back
    return m


@dataclass
class BenchmarkResult:
    calibration: dict[str, Any]
    test: EvaluationMetrics
    test_uncalibrated: EvaluationMetrics
    null_scale: EvaluationMetrics
    seconds: dict[str, float]
    sizes: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": "SYNTHETIC BENCHMARK - not real-world performance",
            "sizes": self.sizes,
            "calibration": self.calibration,
            "test_calibrated": self.test.as_dict(),
            "test_uncalibrated": self.test_uncalibrated.as_dict(),
            "null_scale": self.null_scale.as_dict(),
            "seconds": self.seconds,
        }


def run_synthetic_benchmark(
    loaded: LoadedDetectionConfig,
    *,
    train_count: int = 300,
    test_count: int = 400,
    null_count: int = 1000,
    days: int = 150,
    eval_start: int = 90,
    null_days: int = 120,
    now: datetime,
    split: str = "test",
) -> tuple[BenchmarkResult, CalibrationResult]:
    """Calibrate on the training split, then evaluate once on the held-out split.

    ``split='train'`` evaluates on the training split instead (for tuning); the reported
    benchmark always uses ``split='test'``.
    """
    timings: dict[str, float] = {}
    t0 = _time.perf_counter()
    train_specs = [s for s in benchmark_specs("train", train_count, days=days) if not s.events]
    train = build_world(train_specs)
    calibration = calibrate(
        train.points,
        loaded,
        version=f"synthetic-{loaded.config.threshold_version}",
        source="synthetic",
        dataset=f"synthetic-train-null-{len(train_specs)}x{days}d",
        now=now,
    )
    nulls = calibration.lookup()
    timings["calibration"] = round(_time.perf_counter() - t0, 2)
    t0 = _time.perf_counter()
    eval_specs = benchmark_specs(split, test_count, days=days, eval_start=eval_start)
    world = build_world(eval_specs)
    dataset = f"synthetic-{split}-{test_count}x{days}d"
    test = evaluate_world(world, loaded, nulls, dataset=dataset, split=split, eval_start=eval_start)
    timings["test_calibrated"] = round(_time.perf_counter() - t0, 2)
    t0 = _time.perf_counter()
    test_unc = evaluate_world(
        world, loaded, None, dataset=dataset, split=split, eval_start=eval_start
    )
    test_unc.notes.append("no calibration: thresholds + alert budget only")
    timings["test_uncalibrated"] = round(_time.perf_counter() - t0, 2)
    t0 = _time.perf_counter()
    null_world = build_world(null_specs(null_count, days=null_days))
    null_eval_start = null_days - 60
    null = evaluate_world(
        null_world,
        loaded,
        nulls,
        dataset=f"synthetic-null-{null_count}x{null_days}d-50pct-correlated",
        split="null",
        eval_start=null_eval_start,
    )
    null.notes.append("no injected events: every alert is a false alert")
    timings["null_scale"] = round(_time.perf_counter() - t0, 2)
    result = BenchmarkResult(
        calibration=calibration.summary(),
        test=test,
        test_uncalibrated=test_unc,
        null_scale=null,
        seconds=timings,
        sizes={
            "train_null_series": len(train_specs),
            "test_series": test_count,
            "null_series": null_count,
            "days": days,
            "eval_window_days": days - eval_start,
            "null_eval_window_days": null_days - null_eval_start,
        },
    )
    return result, calibration
