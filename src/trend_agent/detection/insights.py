"""Read models for the ``detect`` CLI: readiness, per-query detail, statistics, thresholds."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np

from trend_agent.db.repositories.detection_runs import (
    DetectionQueueRepository,
    DetectionRunRepository,
)
from trend_agent.db.repositories.detector_state import DetectorStateRepository
from trend_agent.db.repositories.monitoring import MonitoringRepository
from trend_agent.db.repositories.surge_events import SurgeEventRepository
from trend_agent.db.repositories.time_series import SeriesSummary, TimeSeriesRepository
from trend_agent.db.session import transaction
from trend_agent.detection.baseline import BaselineModeler
from trend_agent.detection.engine import EngineState
from trend_agent.detection.preprocessing import knowledge_at, prepare
from trend_agent.detection.service import DetectionService
from trend_agent.discovery.models import Category
from trend_agent.monitoring.replay import AvailabilityMode
from trend_agent.schemas.detection import BaselineReadiness, PreprocessStatus
from trend_agent.schemas.evidence import classify
from trend_agent.schemas.monitoring import MonitoringStatus

ELIGIBLE = frozenset(
    {PreprocessStatus.READY, PreprocessStatus.PARTIALLY_READY, PreprocessStatus.IRREGULAR_SAMPLING}
)


class DetectionInsights:
    def __init__(self, service: DetectionService) -> None:
        self.svc = service
        self.cfg = service.cfg

    def _series_readiness(
        self, ts: TimeSeriesRepository, summary: SeriesSummary, now: datetime
    ) -> dict[str, Any]:
        points = ts.points(series_ids=[summary.series_id], current_only=False, limit=1_000_000)
        series = prepare(
            knowledge_at(points, now, AvailabilityMode.AGENT),
            self.cfg,
            as_of=now,
            mode=AvailabilityMode.AGENT,
            already_known=True,
        )
        out: dict[str, Any] = {
            "series_id": summary.series_id,
            "provider": summary.provider_name,
            "measurement": series.measurement.value,
            "classification": classify(series.measurement, is_proxy=series.is_proxy).value
            if series.n_slots
            else None,
            "proxy": series.is_proxy,
            "synthetic": series.is_synthetic,
            "granularity": summary.granularity,
            "normalization_key": summary.normalization_key[:12],
            "preprocess_status": series.status.value,
            "usable_values": series.n_usable,
            "missing_periods": series.missing_periods,
            "missing_values": series.missing_values,
            "recent_coverage": series.recent_coverage,
            "reasons": series.reasons,
            "baseline_model": None,
            "baseline_readiness": BaselineReadiness.INSUFFICIENT.value,
            "eligible": False,
        }
        if series.family is not None and series.n_slots and series.status in ELIGIBLE:
            modeler = BaselineModeler(self.cfg.baseline, series.granularity, series.family)
            # Readiness for the *next* period: baseline from everything known so far.
            values = [*series.values.tolist(), float("nan")]
            est = modeler.estimates(np.asarray(values), series.first_slot, [len(values) - 1], {})[0]
            out["baseline_model"] = est.model.value
            out["baseline_readiness"] = est.readiness.value
            out["baseline_reason"] = est.selection_reason
            out["eligible"] = est.readiness is not BaselineReadiness.INSUFFICIENT
        if not out["eligible"] and series.status is PreprocessStatus.INSUFFICIENT_DATA:
            out["reasons"] = [*series.reasons, "cold start: handled by the cold-start assessment"]
        return out

    def readiness(
        self, *, category: Category | None = None, include_synthetic: bool = False
    ) -> list[dict[str, Any]]:
        now = self.svc.now()
        rows: list[dict[str, Any]] = []
        with transaction(self.svc.session_factory) as session:
            repo = MonitoringRepository(session)
            ts = TimeSeriesRepository(session)
            targets = repo.targets(
                statuses=[s for s in MonitoringStatus if s is not MonitoringStatus.DISABLED],
                category=category,
                include_synthetic=include_synthetic,
                limit=10_000,
            )
            texts = repo.query_text(list({t.query_id for t in targets}))
            by_target = {t.id: t for t in targets}
            for summary in ts.series_summaries(target_ids=list(by_target)):
                t = by_target[summary.target_id or -1]
                if summary.is_synthetic and not include_synthetic:
                    continue
                rows.append(
                    {
                        "target_id": t.id,
                        "query": texts[t.query_id][0],
                        "category": t.category.value,
                        **self._series_readiness(ts, summary, now),
                    }
                )
        return rows

    def query(self, query: str) -> dict[str, Any]:
        now = self.svc.now()
        with transaction(self.svc.session_factory) as session:
            repo = MonitoringRepository(session)
            ts = TimeSeriesRepository(session)
            states = DetectorStateRepository(session)
            runs = DetectionRunRepository(session)
            targets = repo.targets(query_text=query, include_synthetic=True, limit=100)
            out: dict[str, Any] = {"query": query, "targets": []}
            for t in targets:
                series_out = []
                for summary in ts.series_summaries(target_ids=[t.id]):
                    info = self._series_readiness(ts, summary, now)
                    stored = states.load(summary.series_id)
                    if stored is not None:
                        st = EngineState.from_payloads(stored.payloads)
                        info["state"] = {
                            "version": stored.version,
                            "updated_at": stored.updated_at,
                            "processed_until": st.processed_until_period_end,
                            "evaluations": st.evaluations,
                            "recomputations": st.recomputations,
                            "cusum": round(st.cusum.g, 4),
                            "cusum_alarms": st.cusum.alarms,
                            "change_point_observations": st.bocpd.n if st.bocpd else 0,
                            "seasonal_fit": st.fit.reason if st.fit else None,
                        }
                    info["latest"] = [
                        {
                            "period_start": p.period_start,
                            "value": p.value,
                            "quality": p.sample_quality.value,
                            "revision": p.revision,
                        }
                        for p in ts.points(series_ids=[summary.series_id], limit=1_000_000)[-7:]
                    ]
                    info["results"] = [
                        {
                            "period_start": r.period_start,
                            "detector": r.detector_type.value,
                            "status": r.result_status.value,
                            "score": None if r.anomaly_score is None else round(r.anomaly_score, 3),
                            "threshold": r.threshold,
                            "p_value": r.p_value,
                            "baseline": None
                            if r.baseline_value is None
                            else round(r.baseline_value, 2),
                            "observed": r.observed_value,
                            "support": r.support_status.value,
                            "late": r.is_late,
                        }
                        for r in runs.results(series_id=summary.series_id, limit=24)
                    ]
                    series_out.append(info)
                episodes = SurgeEventRepository(session).events(
                    include_synthetic=True, target_ids=[t.id], limit=20
                )
                out["targets"].append(
                    {
                        "target_id": t.id,
                        "category": t.category.value,
                        "measurement": t.measurement_category.value,
                        "status": t.status.value,
                        "synthetic": t.is_synthetic,
                        "series": series_out,
                        "episodes": [
                            {
                                "event_id": e.id,
                                "status": e.status.value,
                                "classification": e.evidence_classification.value,
                                "severity": round(e.severity, 1),
                                "first_detected_at": e.first_detected_at,
                            }
                            for e in episodes
                        ],
                    }
                )
        return out

    def stats(self, *, include_synthetic: bool = False) -> dict[str, Any]:
        now = self.svc.now()
        with transaction(self.svc.session_factory) as session:
            runs = DetectionRunRepository(session)
            events = SurgeEventRepository(session)
            recent = runs.recent(10, include_synthetic=include_synthetic)
            return {
                "now": now,
                "queue_depth": DetectionQueueRepository(session).depth(),
                "series_with_state": DetectorStateRepository(session).count(),
                "episodes": events.status_counts(include_synthetic=False),
                "synthetic_episodes": events.status_counts(include_synthetic=True)
                if include_synthetic
                else None,
                "notifications_last_24h": events.notifications_since(
                    now - timedelta(days=1), synthetic=False
                ),
                "results_last_24h": runs.result_counts(now - timedelta(days=1)),
                "calibrations": [
                    {
                        "stratum": c.stratum_key,
                        "version": c.calibration_version,
                        "source": c.source,
                        "n": c.parameters.get("n_total"),
                    }
                    for c in runs.active_calibrations()
                ],
                "evaluations": [
                    {
                        "dataset": e.evaluation_dataset,
                        "split": e.dataset_split,
                        "precision": e.precision,
                        "recall": e.recall,
                        "false_alerts_per_day": e.false_alerts_per_day,
                        "synthetic": e.is_synthetic,
                        "completed_at": e.completed_at,
                    }
                    for e in runs.evaluations(5)
                ],
                "recent_runs": [
                    {
                        "run_id": str(r.id),
                        "type": r.run_type.value,
                        "status": r.status.value,
                        "started_at": r.started_at,
                        "series": r.series_evaluated,
                        "observations": r.observations_evaluated,
                        "hypotheses": r.hypotheses_tested,
                        "accepted": r.accepted_after_control,
                        "alerts": r.alerts_created,
                        "errors": r.errors,
                        "seconds": (r.stats or {}).get("seconds"),
                        "synthetic": r.is_synthetic,
                    }
                    for r in recent
                ],
            }

    def thresholds(self) -> dict[str, Any]:
        cfg = self.cfg
        with transaction(self.svc.session_factory) as session:
            cals = DetectionRunRepository(session).active_calibrations()
            calibrations = [
                {
                    "stratum": c.stratum_key,
                    "version": c.calibration_version,
                    "source": c.source,
                    "dataset": c.dataset,
                    "n_total": c.parameters.get("n_total"),
                    "tail_threshold": c.parameters.get("tail_threshold"),
                    "gpd_ok": (c.parameters.get("gpd") or {}).get("ok"),
                }
                for c in cals
            ]
        return {
            "config_version": self.svc.loaded.version_tag,
            "algorithm_version": cfg.algorithm_version,
            "threshold_version": cfg.threshold_version,
            "enabled_detectors": [d.value for d in cfg.enabled_detectors],
            "anomaly": cfg.anomaly.model_dump(mode="json"),
            "cusum": cfg.cusum.model_dump(mode="json"),
            "change_point": cfg.change_point.model_dump(mode="json"),
            "baseline": cfg.baseline.model_dump(mode="json"),
            "multiple_testing": cfg.multiple_testing.model_dump(mode="json"),
            "alert_policy": cfg.alert_policy.model_dump(mode="json"),
            "episodes": cfg.episodes.model_dump(mode="json"),
            "cold_start": cfg.cold_start.model_dump(mode="json"),
            "calibrations": calibrations,
        }
