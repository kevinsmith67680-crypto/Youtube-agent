"""Phase 6 application service: database data access, training runs, forecasts, drift.

Training (scheduled, heavy) and inference (frequent, light) are separate operations:
``train`` builds a causal dataset, trains and registers models (never promoting unless
asked explicitly); ``predict`` only loads registered champions and scores. Models trained
on synthetic data are kept apart (``data_origin = synthetic``) and serve only synthetic
series; real series without a real validated model get baseline-only forecasts.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import sqlalchemy as sa

from trend_agent.db.content_models import AnalysisRunStatus, ContentAnalysisRunRow, ContentGapRow
from trend_agent.db.discovery_models import DiscoveredEntityRow, DiscoveredKeywordRow
from trend_agent.db.forecast_models import ForecastPredictionRow
from trend_agent.db.models import SearchQueryRow
from trend_agent.db.monitoring_models import MonitoringTargetRow, SeriesRow
from trend_agent.db.repositories.forecast_registry import (
    EvaluationRepository,
    SqlModelStore,
    TrainingRunRepository,
)
from trend_agent.db.repositories.forecasts import ForecastRepository
from trend_agent.db.repositories.time_series import TimeSeriesRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.detection.config import LoadedDetectionConfig
from trend_agent.forecasting import drift as drift_mod
from trend_agent.forecasting import eligibility
from trend_agent.forecasting import synthetic as syn
from trend_agent.forecasting.config import LoadedForecastingConfig
from trend_agent.forecasting.dataset_builder import (
    Dataset,
    _support_views,
    build_dataset,
    row_features,
)
from trend_agent.forecasting.feature_engineering import FEATURE_NAMES, FEATURE_SCHEMA_VERSION, SPECS
from trend_agent.forecasting.feature_store import (
    ContentEvent,
    PointInTimeSeries,
    SeriesBundle,
    SeriesMeta,
)
from trend_agent.forecasting.model_registry import ArtifactError, ArtifactStore, Registry
from trend_agent.forecasting.model_selection import compare, promote_selected, train_and_register
from trend_agent.forecasting.prediction import ForecastEngine
from trend_agent.monitoring.replay import AvailabilityMode
from trend_agent.schemas.forecast import ForecastOutput
from trend_agent.schemas.model_metadata import DataOrigin, ModelMetadata, ModelStatus
from trend_agent.schemas.monitoring import MeasurementCategory
from trend_agent.utils.time import Clock, utc_now
from trend_agent.verification.permissions import read_only

_OPPORTUNITY = {"weak": 1, "moderate": 2, "strong": 3}


class ForecastingService:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        loaded: LoadedForecastingConfig,
        dloaded: LoadedDetectionConfig,
        artifact_dir: str | Path,
        clock: Clock = utc_now,
    ) -> None:
        self._factory = session_factory
        self.loaded = loaded
        self.cfg = loaded.config
        self.dloaded = dloaded
        self._clock = clock
        self.artifact_dir = Path(artifact_dir)
        self.registry = Registry(SqlModelStore(session_factory), ArtifactStore(self.artifact_dir))
        self.engine = ForecastEngine(loaded, dloaded, self.registry)

    # ------------------------------------------------------------------ data access
    def bundles(
        self,
        *,
        query: str | None = None,
        category: str | None = None,
        include_synthetic: bool = False,
        limit: int = 2000,
    ) -> list[SeriesBundle]:
        """Monitored series (one bundle per forecastable series), read-only."""
        out: list[SeriesBundle] = []
        with transaction(self._factory) as s:
            read_only(s)
            stmt = (
                sa.select(SeriesRow, MonitoringTargetRow, SearchQueryRow)
                .join(MonitoringTargetRow, MonitoringTargetRow.id == SeriesRow.target_id)
                .join(SearchQueryRow, SearchQueryRow.id == SeriesRow.query_id)
            )
            if not include_synthetic:
                stmt = stmt.where(sa.not_(SeriesRow.is_synthetic))
            if category:
                stmt = stmt.where(MonitoringTargetRow.category == category)
            if query:
                stmt = stmt.where(sa.func.lower(SearchQueryRow.original_query) == query.lower())
            rows = s.execute(stmt.order_by(SeriesRow.id).limit(limit)).all()
            by_query: dict[int, list[Any]] = {}
            for series, target, q in rows:
                by_query.setdefault(series.query_id, []).append((series, target, q))
            repo = TimeSeriesRepository(s)
            for _qid, members in by_query.items():
                pts = {
                    sr.id: repo.points(series_ids=[sr.id], current_only=False, limit=200_000)
                    for sr, _, _ in members
                }
                for series, target, q in members:
                    measurement = series.simulated_category or series.measurement_category
                    if eligibility.target_family(measurement) is None:
                        continue
                    keyword = (
                        s.get(DiscoveredKeywordRow, target.keyword_id)
                        if target.keyword_id
                        else None
                    )
                    entity = (
                        s.get(DiscoveredEntityRow, keyword.entity_id)
                        if keyword is not None and keyword.entity_id
                        else None
                    )
                    meta = SeriesMeta(
                        series_id=series.id,
                        query=q.original_query,
                        provider=series.provider_name,
                        measurement=measurement,
                        granularity=series.granularity,
                        scale_type=series.scale_type,
                        unit=series.unit,
                        region=series.region,
                        is_proxy=series.is_proxy,
                        is_synthetic=series.is_synthetic,
                        category=target.category.value,
                        target_id=target.id,
                        query_id=series.query_id,
                        entity_id=keyword.entity_id if keyword else None,
                        topic_cluster_id=target.topic_cluster_id,
                        group_key=f"topic:{target.topic_cluster_id}"
                        if target.topic_cluster_id
                        else f"query:{series.query_id}",
                        language=series.language,
                        keyword_first_seen=keyword.first_discovered_at if keyword else None,
                        entity_first_seen=entity.first_seen_at if entity else None,
                    )
                    supporting: dict[MeasurementCategory, list[Any]] = {}
                    for other, _, _ in members:
                        if other.id == series.id:
                            continue
                        cat = other.simulated_category or other.measurement_category
                        supporting.setdefault(cat, pts[other.id])
                    out.append(
                        SeriesBundle(
                            meta, pts[series.id], supporting, self._content(s, series.query_id)
                        )
                    )
        return out

    def _content(self, s: Any, query_id: int) -> list[ContentEvent]:
        runs = s.execute(
            sa.select(ContentAnalysisRunRow).where(
                ContentAnalysisRunRow.query_id == query_id,
                ContentAnalysisRunRow.status == AnalysisRunStatus.COMPLETED,
            )
        ).scalars()
        out = []
        for run in runs:
            gaps = list(
                s.execute(
                    sa.select(ContentGapRow).where(ContentGapRow.analysis_run_id == run.id)
                ).scalars()
            )
            opp = [
                g for g in gaps if g.gap_type not in ("no_verifiable_gap", "insufficient_evidence")
            ]
            strength = max(
                (g.evidence_strength for g in opp),
                key=lambda x: _OPPORTUNITY.get(x, 0),
                default=None,
            )
            if run.completed_at is not None:
                out.append(
                    ContentEvent(
                        run.completed_at,
                        opp[0].gap_type if opp else None,
                        strength,
                        run.opportunity_status == "candidate",
                    )
                )
        return out

    # ------------------------------------------------------------------ eligibility / features
    def eligibility(self, **kw: Any) -> list[dict[str, Any]]:
        now = self._clock()
        mode = AvailabilityMode(self.cfg.availability_mode)
        out = []
        for b in self.bundles(**kw):
            vis = PointInTimeSeries(b.points, mode).at(now)
            per = {h: eligibility.check(vis, b.meta, self.cfg, h) for h in self.cfg.horizons_hours}
            out.append(
                {
                    "series_id": b.meta.series_id,
                    "query": b.meta.query,
                    "category": b.meta.category,
                    "provider": b.meta.provider,
                    "measurement": b.meta.measurement.value,
                    "granularity": b.meta.granularity.value,
                    "synthetic": b.meta.is_synthetic,
                    "horizons": {
                        str(h): {"status": r.status.value, "reasons": list(r.reasons)}
                        for h, r in per.items()
                    },
                    "history_hours": round(next(iter(per.values())).history_hours, 1),
                }
            )
        return out

    def features(
        self, query: str, *, include_synthetic: bool = False, at: datetime | None = None
    ) -> list[dict[str, Any]]:
        now = at or self._clock()
        mode = AvailabilityMode(self.cfg.availability_mode)
        out = []
        for b in self.bundles(query=query, include_synthetic=include_synthetic):
            pit = PointInTimeSeries(b.points, mode)
            _vis, ref, row = row_features(
                b, pit, None, _support_views(b, mode), now, self.loaded, self.dloaded, strict=True
            )
            out.append(
                {
                    "series_id": b.meta.series_id,
                    "provider": b.meta.provider,
                    "measurement": b.meta.measurement.value,
                    "granularity": b.meta.granularity.value,
                    "as_of": now.isoformat(),
                    "feature_schema_version": FEATURE_SCHEMA_VERSION,
                    "reference": {
                        "baseline": ref.baseline,
                        "threshold": ref.threshold,
                        "level": ref.level,
                        "elevated": ref.elevated,
                    },
                    "features": [
                        {
                            "name": n,
                            "value": None if math.isnan(v) else round(v, 6),
                            "unit": SPECS[n].unit,
                            "source": SPECS[n].source,
                            "availability": SPECS[n].availability,
                        }
                        for n, v in ((n, row.values[n]) for n in FEATURE_NAMES)
                    ],
                    "provenance": row.provenance,
                    "rejected": row.rejected,
                }
            )
        return out

    # ------------------------------------------------------------------ training
    def train(
        self,
        *,
        dataset: str = "database",
        horizons: Sequence[int] | None = None,
        include_synthetic: bool = False,
        promote: bool = False,
        workers: int = 1,
        scale: float = 1.0,
    ) -> dict[str, Any]:
        now = self._clock()
        if dataset == "synthetic":
            c = self.cfg.corpus
            bundles = syn.materialise(
                syn.corpus(
                    hourly=max(12, int(c.hourly_series * scale)),
                    daily=max(6, int(c.daily_series * scale)),
                    hourly_days=c.hourly_days,
                    daily_days=c.daily_days,
                    seed=c.seed,
                    allocation=self.cfg.category_allocation,
                )
            )
            origin = DataOrigin.SYNTHETIC
        else:
            bundles = self.bundles(include_synthetic=include_synthetic)
            kinds = {b.meta.is_synthetic for b in bundles}
            if len(kinds) > 1:
                bundles = [b for b in bundles if b.meta.is_synthetic is include_synthetic]
            origin = DataOrigin.SYNTHETIC if include_synthetic else DataOrigin.REAL
        runs = TrainingRunRepository(self._factory)
        run_id = runs.start(
            started_at=now,
            source=dataset,
            origin=origin.value,
            horizons=list(horizons or self.cfg.horizons_hours),
            configuration_version=self.loaded.version_tag,
            feature_schema_version=FEATURE_SCHEMA_VERSION,
        )
        if not bundles:
            runs.finish(
                run_id,
                completed_at=self._clock(),
                status="failed",
                error="no eligible series for this dataset",
            )
            return {
                "run_id": str(run_id),
                "status": "failed",
                "message": "no eligible series: nothing was trained (use --dataset synthetic "
                "for an isolated experimental run)",
            }
        try:
            ds = build_dataset(bundles, self.loaded, self.dloaded, source=dataset, workers=workers)
            registry = Registry(
                SqlModelStore(self._factory, run_id), ArtifactStore(self.artifact_dir)
            )
            if ds.n == 0:
                runs.finish(
                    run_id,
                    completed_at=self._clock(),
                    status="failed",
                    manifest=ds.manifest,
                    error="no eligible training rows",
                )
                return {
                    "run_id": str(run_id),
                    "status": "failed",
                    "manifest": ds.manifest,
                    "message": "no eligible training rows (insufficient history?)",
                }
            results = train_and_register(
                ds,
                self.loaded,
                registry,
                origin=origin,
                trained_at=now,
                horizons=tuple(horizons) if horizons else None,
            )
            evals = EvaluationRepository(self._factory)
            for res in results:
                for m in res.registered:
                    metrics = dict(m.metrics or {})
                    sl = metrics.pop("slices", {})
                    metrics.pop("reliability", None)
                    evals.add(
                        model_id=m.model_id,
                        kind="test",
                        metrics=metrics,
                        slices=sl,
                        at=now,
                        dataset_version=m.dataset_version,
                        period=(m.test_period.start, m.test_period.end),
                        synthetic=origin is DataOrigin.SYNTHETIC,
                    )
                if res.rolling:
                    evals.add(
                        model_id=None,
                        kind="rolling",
                        at=now,
                        metrics={
                            "scope": f"{res.family}/{res.granularity}/{res.horizon}h",
                            "folds": res.rolling,
                        },
                        dataset_version=ds.manifest.get("dataset_version"),
                        synthetic=origin is DataOrigin.SYNTHETIC,
                    )
            decisions = (
                promote_selected(registry, results, ds, self.loaded, at=now) if promote else []
            )
            summary = [
                r.report.summary()
                if r.report
                else {
                    "task": r.task,
                    "horizon_hours": r.horizon,
                    "granularity": r.granularity,
                    "family": r.family,
                    "notes": r.notes,
                    "registered": [m.model_id for m in r.registered],
                }
                for r in results
            ]
            runs.finish(
                run_id,
                completed_at=self._clock(),
                status="completed",
                manifest=ds.manifest,
                metrics={"scopes": _compact(summary)},
            )
            return {
                "run_id": str(run_id),
                "status": "completed",
                "data_origin": origin.value,
                "manifest": ds.manifest,
                "scopes": summary,
                "promotions": decisions,
                "registered": sum(len(r.registered) for r in results),
            }
        except Exception as exc:
            runs.finish(run_id, completed_at=self._clock(), status="failed", error=str(exc)[:2000])
            raise

    # ------------------------------------------------------------------ evaluation
    def evaluate(
        self,
        *,
        horizon: int | None = None,
        dataset: str = "synthetic",
        include_synthetic: bool = False,
        workers: int = 1,
        scale: float = 0.5,
    ) -> list[dict[str, Any]]:
        """Champions re-scored on a freshly built dataset's untouched test period."""
        ds = self._dataset(
            dataset, include_synthetic=include_synthetic, workers=workers, scale=scale
        )
        now = self._clock()
        out = []
        evals = EvaluationRepository(self._factory)
        for m in self.registry.store.all():
            if m.status is not ModelStatus.CHAMPION or m.target_type.value not in (
                "breakout",
                "continuation",
            ):
                continue
            if horizon is not None and m.horizon_hours != horizon:
                continue
            sub = ds.subset(
                (ds.meta["family"] == m.target_family.value)
                & (ds.meta["granularity"] == m.granularity)
            )
            if sub.n == 0:
                continue
            res = compare(self.registry, m.model_id, m.model_id, sub, self.loaded)
            metrics = res.get("champion", {}).get("metrics", {})
            evals.add(
                model_id=m.model_id,
                kind="reevaluation",
                metrics=metrics,
                at=now,
                dataset_version=ds.manifest.get("dataset_version"),
                synthetic=bool(ds.manifest.get("any_synthetic")),
            )
            out.append(
                {
                    "model_id": m.model_id,
                    "scope": f"{m.target_family.value}/{m.granularity}/"
                    f"{m.horizon_hours}h/{m.target_type.value}",
                    "test_rows": res.get("test_rows"),
                    "metrics": metrics,
                }
            )
        return out

    def _dataset(
        self, dataset: str, *, include_synthetic: bool, workers: int, scale: float
    ) -> Dataset:
        if dataset == "synthetic":
            c = self.cfg.corpus
            bundles = syn.materialise(
                syn.corpus(
                    hourly=max(12, int(c.hourly_series * scale)),
                    daily=max(6, int(c.daily_series * scale)),
                    hourly_days=c.hourly_days,
                    daily_days=c.daily_days,
                    seed=c.seed + 1,
                    allocation=self.cfg.category_allocation,
                )
            )
        else:
            bundles = self.bundles(include_synthetic=include_synthetic)
        return build_dataset(
            bundles, self.loaded, self.dloaded, source=dataset, workers=workers, audit_rows=2
        )

    def compare(
        self,
        *,
        dataset: str = "synthetic",
        include_synthetic: bool = False,
        workers: int = 1,
        scale: float = 0.5,
    ) -> list[dict[str, Any]]:
        ds = self._dataset(
            dataset, include_synthetic=include_synthetic, workers=workers, scale=scale
        )
        models = self.registry.store.all()
        out = []
        for champ in [
            m
            for m in models
            if m.status is ModelStatus.CHAMPION
            and m.target_type.value in ("breakout", "continuation")
        ]:
            sub = ds.subset(
                (ds.meta["family"] == champ.target_family.value)
                & (ds.meta["granularity"] == champ.granularity)
            )
            for chall in models:
                if (
                    chall.status in (ModelStatus.CHALLENGER, ModelStatus.VALIDATED)
                    and chall.model_id != champ.model_id
                    and (
                        chall.target_family,
                        chall.granularity,
                        chall.horizon_hours,
                        chall.target_type,
                        chall.data_origin,
                    )
                    == (
                        champ.target_family,
                        champ.granularity,
                        champ.horizon_hours,
                        champ.target_type,
                        champ.data_origin,
                    )
                ):
                    out.append(
                        compare(self.registry, champ.model_id, chall.model_id, sub, self.loaded)
                    )
        return out

    def promote(self, model_id: str, reason: str = "operator promotion") -> ModelMetadata:
        found = self.registry.store.get(model_id)
        if found is None:
            raise LookupError(model_id)
        return self.registry.promote(model_id, at=self._clock(), reason=reason, evidence={})

    def rollback(
        self,
        model_id: str,
        reason: str = "operator rollback",
        *,
        reject: bool = False,
        restore_previous: bool = False,
    ) -> str | None:
        """Take a champion out of service. Default: RETIRED, and the scope falls back to
        baseline-only forecasts. ``reject`` marks it REJECTED instead; ``restore_previous``
        reinstates the champion it replaced (after an artifact checksum check)."""
        if not reject and not restore_previous:
            self.registry.retire(model_id, at=self._clock(), reason=reason)
            return None
        try:
            return self.registry.rollback(
                model_id,
                at=self._clock(),
                reason=reason,
                reject=reject,
                restore_previous=restore_previous,
            )
        except (ValueError, ArtifactError) as exc:
            raise LookupError(str(exc)) from exc

    def reject(self, model_id: str, reason: str) -> None:
        try:
            self.registry.reject(model_id, at=self._clock(), reason=reason)
        except (ValueError, ArtifactError) as exc:
            raise LookupError(str(exc)) from exc

    # ------------------------------------------------------------------ inference
    def predict(
        self,
        *,
        query: str | None = None,
        category: str | None = None,
        include_synthetic: bool = False,
        persist: bool = True,
        at: datetime | None = None,
    ) -> list[tuple[ForecastOutput, bool]]:
        now = at or self._clock()
        results = []
        for b in self.bundles(query=query, category=category, include_synthetic=include_synthetic):
            outs = self.engine.forecast(b, now)
            expires = _expiry(
                b, now, self.cfg.targets.baseline_window_hours.get(b.meta.granularity, 168)
            )
            for o in outs:
                created = False
                if persist:
                    with transaction(self._factory) as s:
                        created = ForecastRepository(s).save(o, expires_at=expires)
                results.append((o, created))
        return results

    def models(self) -> list[ModelMetadata]:
        return self.registry.store.all()

    def explain(self, prediction_id: str) -> dict[str, Any] | None:
        with transaction(self._factory) as s:
            read_only(s)
            row = ForecastRepository(s).get(prediction_id)
            return dict(row.output) if row is not None else None

    def peaks(self, *, include_synthetic: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        with transaction(self._factory) as s:
            read_only(s)
            return [
                {
                    "prediction_id": str(p.id),
                    "query": p.output.get("query"),
                    "horizon_hours": p.horizon_hours,
                    "status": pe.status,
                    "earliest_hours": pe.earliest_hours,
                    "latest_hours": pe.latest_hours,
                    "probability_beyond_horizon": pe.probability_beyond_horizon,
                    "censoring": pe.censoring_status,
                    "confidence": pe.confidence_status,
                }
                for pe, p in ForecastRepository(s).peaks(
                    include_synthetic=include_synthetic, limit=limit
                )
            ]

    def windows(
        self, *, include_synthetic: bool = False, limit: int = 50, status: str | None = None
    ) -> list[dict[str, Any]]:
        with transaction(self._factory) as s:
            read_only(s)
            return [
                {
                    "prediction_id": str(p.id),
                    "query": p.output.get("query"),
                    "horizon_hours": p.horizon_hours,
                    "profile": w.production_profile,
                    "publication": w.estimated_publication_time.isoformat(),
                    "status": w.opportunity_status,
                    "probability_elevated": w.probability_elevated,
                    "uncertainty": w.estimated_persistence,
                }
                for w, p in ForecastRepository(s).windows(
                    include_synthetic=include_synthetic, limit=limit, status=status
                )
            ]

    def drift(self, *, since_days: int = 7) -> list[dict[str, Any]]:
        now = self._clock()
        out = []
        for m in self.registry.store.all():
            if m.status is not ModelStatus.CHAMPION or m.target_type.value == "peak":
                continue
            _, payload, _ = self.registry.load(m.model_id)
            with transaction(self._factory) as s:
                read_only(s)
                feats = ForecastRepository(s).recent_features(
                    m.model_id, now - timedelta(days=since_days)
                )
            x = np.array(
                [
                    [np.nan if f.get(n) is None else float(f[n]) for n in m.feature_names]
                    for f in feats
                ],
                dtype=float,
            ).reshape(-1, len(m.feature_names))
            data = drift_mod.data_drift(
                payload.get("feature_reference", {}), x, m.feature_names, self.cfg.drift
            )
            concept = self._matured(m, payload, now)
            rec = drift_mod.recommendation(data, concept)
            EvaluationRepository(self._factory).add(
                model_id=m.model_id,
                kind="drift",
                at=now,
                metrics={"data": data, "concept": concept, "recommendation": rec},
                synthetic=m.data_origin is DataOrigin.SYNTHETIC,
            )
            out.append(
                {
                    "model_id": m.model_id,
                    "scope": f"{m.target_family.value}/{m.granularity}/"
                    f"{m.horizon_hours}h/{m.target_type.value}",
                    "data": data,
                    "concept": concept,
                    "recommendation": rec,
                }
            )
        return out

    def _matured(self, m: ModelMetadata, payload: dict[str, Any], now: datetime) -> dict[str, Any]:
        """Labels of past predictions whose horizon (+ publication lag) has ended."""
        from trend_agent.forecasting.feature_store import hours
        from trend_agent.forecasting.target_definition import outcome_labels, reference

        if m.target_type.value != "breakout":
            return {"status": "not_applicable"}
        with transaction(self._factory) as s:
            read_only(s)
            rows = ForecastRepository(s).for_maturity(
                m.model_id, now - timedelta(hours=self.cfg.splits.embargo_extra_hours)
            )
            # The forecast predicts (T, T + H] from its issue time T (prediction_timestamp),
            # not from the data cutoff, which is earlier by the publication lag.
            pairs = [
                (r.series_id, r.prediction_timestamp, r.horizon_hours, r.breakout_probability)
                for r in rows
            ]
        if not pairs:
            return drift_mod.concept_drift(
                np.array([]), np.array([]), model_metrics=m.metrics, cfg=self.cfg.drift
            )
        bundles = {b.meta.series_id: b for b in self.bundles(include_synthetic=True)}
        y, p = [], []
        for sid, issued, h, prob in pairs:
            b = bundles.get(sid) if sid is not None else None
            if b is None or prob is None:
                continue
            pit = PointInTimeSeries(b.points)
            ref = reference(pit.at(issued), b.meta, self.cfg.targets)
            t, v = pit.final()
            lab = outcome_labels(ref, t, v, hours(issued), b.meta, (h,), self.cfg.targets)
            if math.isfinite(lab[f"breakout_{h}"]):
                y.append(lab[f"breakout_{h}"])
                p.append(prob)
        return drift_mod.concept_drift(
            np.array(y), np.array(p), model_metrics=m.metrics, cfg=self.cfg.drift
        )

    def status(self) -> dict[str, Any]:
        models = self.registry.store.all()
        by_status: dict[str, int] = {}
        for m in models:
            by_status[m.status.value] = by_status.get(m.status.value, 0) + 1
        with transaction(self._factory) as s:
            read_only(s)
            n_pred = s.execute(
                sa.select(sa.func.count()).select_from(ForecastPredictionRow)
            ).scalar_one()
        return {
            "models": by_status,
            "predictions": int(n_pred),
            "real_champions": sum(
                1
                for m in models
                if m.status is ModelStatus.CHAMPION and m.data_origin is DataOrigin.REAL
            ),
            "synthetic_champions": sum(
                1
                for m in models
                if m.status is ModelStatus.CHAMPION and m.data_origin is DataOrigin.SYNTHETIC
            ),
            "artifact_dir": str(self.artifact_dir),
        }


def _expiry(b: SeriesBundle, now: datetime, window_hours: int) -> datetime | None:
    """Forecasts expire with the earliest-expiring observation of the baseline window."""
    lo = now - timedelta(hours=window_hours)
    exp = [
        p.expires_at
        for p in b.points
        if p.expires_at is not None and p.period_end > lo and p.period_end <= now
    ]
    return min(exp) if exp else None


def _compact(summary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for s in summary:
        out.append(
            {k: v for k, v in s.items() if k != "candidates"}
            | {
                "candidates": [
                    {
                        "name": c["name"],
                        "validated": c["validated"],
                        "brier": c["metrics"].get("brier"),
                        "mae": c["metrics"].get("mae"),
                        "pr_auc": c["metrics"].get("pr_auc"),
                    }
                    for c in s.get("candidates", [])
                ]
            }
        )
    return out
