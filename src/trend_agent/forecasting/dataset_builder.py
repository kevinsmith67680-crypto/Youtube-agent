"""Causal training-dataset construction with point-in-time joins and a leakage audit.

For every eligible series and every prediction time T on the cadence grid:

* features come only from what was *available* at T (``PointInTimeSeries.at``; supporting
  series, catalog facts and Phase 5 analyses are cut at T the same way);
* the reference state (baseline, threshold, level) is computed from the same view;
* labels come from the final known values of the periods that end after T
  (``target_definition.outcome_labels``) — never from detector alerts;
* rows whose label windows lack enough observed periods carry NaN labels (excluded per
  target), and the coverage is recorded.

A sample of rows is re-computed from a copy of the data truncated to what was available
at T (every later observation and revision removed); any difference fails the audit and
is recorded in the manifest. The manifest stores configuration, schema and code versions,
the source series and hashes of the rows, so a dataset can be reproduced and verified.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from trend_agent.detection.config import LoadedDetectionConfig
from trend_agent.forecasting import eligibility
from trend_agent.forecasting.baselines import seasonal_naive
from trend_agent.forecasting.config import (
    FORECAST_VERSION,
    LoadedForecastingConfig,
    granularity_hours,
)
from trend_agent.forecasting.feature_engineering import (
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    code_version,
    compute,
)
from trend_agent.forecasting.feature_store import (
    DetectorFeed,
    PointInTimeSeries,
    SeriesBundle,
    from_hours,
    hours,
    strict_history,
)
from trend_agent.forecasting.target_definition import (
    TARGET_VERSION,
    label_names,
    outcome_labels,
    reference,
)
from trend_agent.logging import get_logger, log_event
from trend_agent.monitoring.replay import AvailabilityMode
from trend_agent.schemas.forecast import ForecastStatus
from trend_agent.schemas.monitoring import MeasurementCategory

_log = get_logger(__name__)


@dataclass
class Dataset:
    X: np.ndarray
    feature_names: tuple[str, ...]
    labels: dict[str, np.ndarray]
    meta: dict[str, np.ndarray]
    manifest: dict[str, Any] = field(default_factory=dict)
    baselines: dict[str, np.ndarray] = field(default_factory=dict)  # predictions known at T

    @property
    def n(self) -> int:
        return int(self.X.shape[0])

    def subset(self, mask: np.ndarray) -> Dataset:
        return Dataset(
            self.X[mask],
            self.feature_names,
            {k: v[mask] for k, v in self.labels.items()},
            {k: v[mask] for k, v in self.meta.items()},
            dict(self.manifest),
            {k: v[mask] for k, v in self.baselines.items()},
        )

    def column(self, name: str) -> np.ndarray:
        return self.X[:, self.feature_names.index(name)]


def _grid(first: float, last: float, cadence: float) -> list[float]:
    start = math.ceil(first / cadence) * cadence
    return [float(t) for t in np.arange(start, last + 1e-9, cadence)]


def _support_views(
    bundle: SeriesBundle, mode: AvailabilityMode
) -> dict[MeasurementCategory, PointInTimeSeries]:
    return {cat: PointInTimeSeries(pts, mode) for cat, pts in bundle.supporting.items() if pts}


def row_features(
    bundle: SeriesBundle,
    pit: PointInTimeSeries,
    feed: DetectorFeed | None,
    supports: dict[MeasurementCategory, PointInTimeSeries],
    as_of: datetime,
    loaded: LoadedForecastingConfig,
    dloaded: LoadedDetectionConfig,
    *,
    strict: bool = False,
) -> tuple[Any, Any, Any]:
    """(Visible, Reference, FeatureRow) for one series at ``as_of``."""
    cfg = loaded.config
    vis = pit.at(as_of)
    ref = reference(vis, bundle.meta, cfg.targets)
    if strict or feed is None:
        det = strict_history(bundle.points, dloaded, as_of, bundle.meta.category)
    else:
        det = feed.history(as_of)
    sup = {cat: (sp.at(as_of), sp) for cat, sp in supports.items()}
    row = compute(
        vis,
        ref,
        bundle.meta,
        cfg.targets,
        detector=det,
        supporting=sup,
        target_pit=pit,
        content=bundle.content,
    )
    return vis, ref, row


_META_COLS = (
    "series_id",
    "group",
    "category",
    "t",
    "granularity",
    "family",
    "synthetic",
    "query",
    "query_id",
    "entity_id",
    "topic_cluster_id",
    "region",
    "measurement",
    "provider",
    "data_cutoff",
    "baseline",
    "threshold",
    "level",
    "elevated",
)


_OBJECT_COLS = frozenset(
    (
        "group",
        "category",
        "granularity",
        "family",
        "query",
        "query_id",
        "entity_id",
        "topic_cluster_id",
        "region",
        "measurement",
        "provider",
    )
)


def _cutoff(
    pit: PointInTimeSeries, supports: dict[MeasurementCategory, PointInTimeSeries], t: float
) -> float:
    """Data availability cutoff of an example: the newest availability time <= T among
    the target and supporting observations it could use (hours since epoch)."""
    times = [
        x
        for x in (pit.latest_available(t), *(sp.latest_available(t) for sp in supports.values()))
        if x is not None
    ]
    return max(times) if times else t


@dataclass
class _SeriesRows:
    rows: list[np.ndarray] = field(default_factory=list)
    labels: dict[str, list[float]] = field(default_factory=dict)
    meta: dict[str, list[Any]] = field(default_factory=dict)
    baselines: dict[str, list[float]] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    source: dict[str, Any] | None = None
    times: list[float] = field(default_factory=list)


def _series_rows(
    args: tuple[SeriesBundle, LoadedForecastingConfig, LoadedDetectionConfig, datetime | None],
) -> _SeriesRows:
    """All examples of one series (top-level so a process pool can run it)."""
    b, loaded, dloaded, end = args
    cfg = loaded.config
    mode = AvailabilityMode(cfg.availability_mode)
    if end is not None:
        # A dataset "as of end" knows only what was available then: labels whose window
        # has not matured by ``end`` lack coverage and stay unknown (never look-ahead).
        b = _truncate(b, end, mode)
    horizons = cfg.horizons_hours
    lnames = label_names(horizons)
    bnames = [f"seasonal_{h}" for h in horizons]
    out = _SeriesRows(
        labels={k: [] for k in lnames},
        meta={k: [] for k in _META_COLS},
        baselines={k: [] for k in bnames},
    )

    def skip(key: str) -> None:
        out.skipped[key] = out.skipped.get(key, 0) + 1

    family = eligibility.target_family(b.meta.measurement)
    if family is None:
        skip("not_a_forecast_target")
        return out
    step = granularity_hours(b.meta.granularity)
    cadence = cfg.cadence_hours.get(b.meta.granularity)
    if step is None or cadence is None:
        skip("unsupported_granularity")
        return out
    pit = PointInTimeSeries(b.points, mode)
    first = pit.first_available()
    if first is None:
        skip("no_observations")
        return out
    final_t, final_v = pit.final()
    feed = DetectorFeed(b.points, dloaded, b.meta.category, pit=pit, mode=mode)
    supports = _support_views(b, mode)
    last = float(final_t.max()) if final_t.size else hours(first)
    if end is not None:
        last = min(last, hours(end))
    out.source = {
        "series_id": b.meta.series_id,
        "group": b.meta.group_key,
        "points": len(b.points),
        "digest": hashlib.sha256(
            json.dumps(
                [(p.id, p.period_start.isoformat(), p.value, p.revision) for p in b.points]
            ).encode()
        ).hexdigest()[:16],
    }
    for t in _grid(hours(first), last, float(cadence)):
        as_of = from_hours(t)
        vis, ref, row = row_features(b, pit, feed, supports, as_of, loaded, dloaded)
        elig = eligibility.check(vis, b.meta, cfg)
        if elig.status is not ForecastStatus.READY:
            skip(elig.status.value)
            continue
        if ref.level is None:
            skip(ref.reason or "no_reference")
            continue
        lab = outcome_labels(ref, final_t, final_v, t, b.meta, horizons, cfg.targets)
        if all(
            math.isnan(lab[f"breakout_{h}"])
            and math.isnan(lab[f"continuation_{h}"])
            and math.isnan(lab[f"trajectory_{h}"])
            for h in horizons
        ):
            skip("label_window_not_observed")
            continue
        out.rows.append(row.vector())
        for k in lnames:
            out.labels[k].append(lab[k])
        for h in horizons:
            out.baselines[f"seasonal_{h}"].append(seasonal_naive(vis, b.meta.granularity, float(h)))
        for k, val in (
            ("series_id", b.meta.series_id),
            ("group", b.meta.group_key),
            ("category", b.meta.category or "other"),
            ("t", t),
            ("granularity", b.meta.granularity.value),
            ("family", family.value),
            ("synthetic", b.meta.is_synthetic),
            ("query", b.meta.query),
            ("query_id", b.meta.query_id),
            ("entity_id", b.meta.entity_id),
            ("topic_cluster_id", b.meta.topic_cluster_id),
            ("region", b.meta.region),
            ("measurement", b.meta.measurement.value),
            ("provider", b.meta.provider),
            ("data_cutoff", _cutoff(pit, supports, t)),
            ("baseline", ref.baseline),
            ("threshold", ref.threshold),
            ("level", ref.level),
            ("elevated", bool(ref.elevated)),
        ):
            out.meta[k].append(val)
        out.times.append(t)
    return out


def build_dataset(
    bundles: Sequence[SeriesBundle],
    loaded: LoadedForecastingConfig,
    dloaded: LoadedDetectionConfig,
    *,
    source: str,
    audit_rows: int = 6,
    end: datetime | None = None,
    workers: int = 1,
) -> Dataset:
    """Build the dataset; ``workers > 1`` processes series in parallel (same result)."""
    lnames = label_names(loaded.config.horizons_hours)
    jobs = [(b, loaded, dloaded, end) for b in bundles]
    parts: list[_SeriesRows] | None = None
    if workers > 1 and len(jobs) > 1:
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor
        from concurrent.futures.process import BrokenProcessPool

        # Never fork a multi-threaded process (database pools, BLAS threads): fork can
        # deadlock. forkserver/spawn start clean interpreters; results are identical.
        method = "forkserver" if "forkserver" in mp.get_all_start_methods() else "spawn"
        try:
            with ProcessPoolExecutor(
                max_workers=workers, mp_context=mp.get_context(method)
            ) as pool:
                parts = list(
                    pool.map(_series_rows, jobs, chunksize=max(1, len(jobs) // (4 * workers)))
                )
        except BrokenProcessPool:
            # e.g. no importable __main__ (stdin, notebooks): same result, one process
            log_event(_log, "forecast.dataset.sequential_fallback", logging.WARNING,
                      reason="worker processes could not start")  # fmt: skip
            parts = None
    if parts is None:
        parts = [_series_rows(j) for j in jobs]
    rows: list[np.ndarray] = []
    labels: dict[str, list[float]] = {k: [] for k in lnames}
    meta: dict[str, list[Any]] = {k: [] for k in _META_COLS}
    bl: dict[str, list[float]] = {f"seasonal_{h}": [] for h in loaded.config.horizons_hours}
    skipped: dict[str, int] = {}
    sources: list[dict[str, Any]] = []
    audit_candidates: list[tuple[int, float]] = []
    for bi, part in enumerate(parts):
        rows += part.rows
        for k in lnames:
            labels[k] += part.labels.get(k, [])
        for k in _META_COLS:
            meta[k] += part.meta.get(k, [])
        for k in bl:
            bl[k] += part.baselines.get(k, [])
        for k, n in part.skipped.items():
            skipped[k] = skipped.get(k, 0) + n
        if part.source is not None:
            sources.append(part.source)
        audit_candidates += [(bi, t) for t in part.times]
    arr = np.vstack(rows) if rows else np.empty((0, len(FEATURE_NAMES)))
    ds = Dataset(
        arr,
        FEATURE_NAMES,
        {k: np.array(v, dtype=float) for k, v in labels.items()},
        {
            k: np.array(
                v,
                dtype=object if k in _OBJECT_COLS else None,
            )
            for k, v in meta.items()
        },
    )
    ds.baselines = {k: np.array(v, dtype=float) for k, v in bl.items()}
    audit = leakage_audit(bundles, ds, audit_candidates, loaded, dloaded, rows=audit_rows)
    ds.manifest = manifest(ds, loaded, source=source, sources=sources, skipped=skipped, audit=audit)
    return ds


def _truncate(bundle: SeriesBundle, as_of: datetime, mode: AvailabilityMode) -> SeriesBundle:
    from trend_agent.monitoring.replay import availability

    def cut(pts: list[Any]) -> list[Any]:
        return [p for p in pts if availability(p, mode)[0] <= as_of]

    return SeriesBundle(
        bundle.meta,
        cut(bundle.points),
        {k: cut(v) for k, v in bundle.supporting.items()},
        [c for c in bundle.content if c.completed_at <= as_of],
    )


def leakage_audit(
    bundles: Sequence[SeriesBundle],
    ds: Dataset,
    candidates: list[tuple[int, float]],
    loaded: LoadedForecastingConfig,
    dloaded: LoadedDetectionConfig,
    *,
    rows: int,
) -> dict[str, Any]:
    """Recompute sampled rows from data truncated at T; every feature must be identical."""
    if not candidates or rows <= 0:
        return {"checked": 0, "mismatches": 0, "details": []}
    mode = AvailabilityMode(loaded.config.availability_mode)
    rng = np.random.default_rng(loaded.config.splits.seed)
    picks = rng.choice(len(candidates), size=min(rows, len(candidates)), replace=False)
    mismatches: list[str] = []
    for k in sorted(int(i) for i in picks):
        bi, t = candidates[k]
        as_of = from_hours(t)
        cut = _truncate(bundles[bi], as_of, mode)
        pit = PointInTimeSeries(cut.points, mode)
        _, _, row = row_features(
            cut, pit, None, _support_views(cut, mode), as_of, loaded, dloaded, strict=True
        )
        got = row.vector()
        want = ds.X[k]
        same = np.allclose(got, want, equal_nan=True, rtol=1e-9, atol=1e-9)
        if not same:
            diff = [
                FEATURE_NAMES[j]
                for j in range(len(FEATURE_NAMES))
                if not (
                    (math.isnan(got[j]) and math.isnan(want[j]))
                    or abs(got[j] - want[j]) <= 1e-9 + 1e-9 * abs(want[j])
                )
            ]
            mismatches.append(f"series {bundles[bi].meta.series_id} at {as_of.isoformat()}: {diff}")
    return {"checked": len(picks), "mismatches": len(mismatches), "details": mismatches}


def manifest(
    ds: Dataset,
    loaded: LoadedForecastingConfig,
    *,
    source: str,
    sources: list[dict[str, Any]],
    skipped: dict[str, int],
    audit: dict[str, Any],
) -> dict[str, Any]:
    h = hashlib.sha256()
    h.update(np.round(np.nan_to_num(ds.X, nan=-9.87654321e9), 9).tobytes())
    for k in sorted(ds.labels):
        h.update(np.round(np.nan_to_num(ds.labels[k], nan=-9.87654321e9), 9).tobytes())
    h.update(json.dumps([str(x) for x in ds.meta.get("t", [])]).encode())
    rows_hash = h.hexdigest()
    identity = {
        "config": loaded.version_tag,
        "features": FEATURE_SCHEMA_VERSION,
        "targets": TARGET_VERSION,
        "code": code_version(),
        "forecast": FORECAST_VERSION,
        "source": source,
        "sources": sources,
    }
    version = "ds-" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    t = ds.meta.get("t", np.array([]))
    return {
        "dataset_version": version,
        "rows_hash": rows_hash,
        "rows": ds.n,
        "source": source,
        "synthetic": bool(np.all(ds.meta["synthetic"])) if ds.n else None,
        "any_synthetic": bool(np.any(ds.meta["synthetic"])) if ds.n else None,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "target_version": TARGET_VERSION,
        "code_version": identity["code"],
        "configuration_version": loaded.version_tag,
        "series": len(sources),
        "period": [from_hours(float(t.min())).isoformat(), from_hours(float(t.max())).isoformat()]
        if t.size
        else None,
        "skipped_prediction_times": skipped,
        "leakage_audit": audit,
        "label_counts": {
            k: {"observed": int(np.sum(np.isfinite(v))), "positive": int(np.nansum(v))}
            for k, v in ds.labels.items()
            if k.startswith(("breakout", "continuation"))
        },
    }


def time_to(t: float) -> datetime:
    return from_hours(t)


__all__ = ["Dataset", "build_dataset", "leakage_audit", "row_features", "timedelta"]
