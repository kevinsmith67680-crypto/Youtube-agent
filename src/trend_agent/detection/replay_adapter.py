"""Causal detection replay on top of the Phase 3 replay engine (no look-ahead).

``DetectionReplayer`` runs the *same* engine, alert policy and episode rules as live
detection, against in-memory state, on what was available at each simulated time:

* **step-wise** (``consumer()``): a Phase 3 ``ReplayConsumer``. At each step it receives the
  snapshot of knowledge at T (provider publication time, else conservative
  collection/ingestion time; revisions only from their own availability) and processes
  the newly available observations. An observation covering Monday that became available
  Wednesday is invisible on Tuesday; a later revision changes nothing before it appeared.
  Late or revised values trigger the engine's deterministic recomputation.
* **fast causal evaluation** (``evaluate_batch``): for data without revisions whose values
  arrive in period order (synthetic benchmarks), each series is processed once and every
  evaluation is assigned to the replay step at which its observation became available.
  Because every engine quantity at period t depends only on values up to t
  (incremental == recomputed, tested), the decisions, multiple-testing families (one per
  step) and episode updates are identical to step-wise replay - just faster.

The decision log distinguishes event time (observation window), availability time and
decision time (``as_of``); later revisions never rewrite earlier decisions.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from trend_agent.detection.alert_policy import AlertDecision, DecisionContext, decide
from trend_agent.detection.cold_start import CorroborationContext
from trend_agent.detection.config import LoadedDetectionConfig, ScaleFamily
from trend_agent.detection.contract import make_renderer
from trend_agent.detection.deduplication import EpisodeMeta
from trend_agent.detection.engine import EngineState, PointEvaluation, SeriesEngine
from trend_agent.detection.events import EpisodeChange, EpisodeManager, MemoryEpisodeStore
from trend_agent.detection.preprocessing import AnalysisSeries, prepare
from trend_agent.detection.significance import (
    AlertBudget,
    FamilyResult,
    Hypothesis,
    NullLookup,
    control,
)
from trend_agent.monitoring.replay import AvailabilityMode, ReplaySnapshot
from trend_agent.schemas.time_series import StoredPoint

REPLAY_VERSION = "detection-replay-v1"


@dataclass(frozen=True)
class ReplayDecision:
    as_of: datetime
    series_id: int
    evaluation: PointEvaluation
    decision: AlertDecision
    change: EpisodeChange | None

    def as_dict(self) -> dict[str, Any]:
        ev = self.evaluation
        return {
            "as_of": self.as_of.isoformat(),
            "series_id": self.series_id,
            "period_start": ev.period_start.isoformat(),
            "value": ev.value,
            "available_at": ev.available_at.isoformat() if ev.available_at else None,
            "baseline_expected": ev.baseline.expected,
            "z": None if ev.anomaly.z is None else round(ev.anomaly.z, 3),
            "stage": self.decision.stage.value if self.decision.stage else None,
            "confidence": self.decision.confidence.value,
            "triggered": [d.value for d in self.decision.triggered],
            "change": self.change.kind if self.change else None,
            "event_id": self.change.episode.id if self.change else None,
        }


@dataclass
class ReplayRun:
    decisions: list[ReplayDecision] = field(default_factory=list)
    steps: int = 0
    families: list[dict[str, Any]] = field(default_factory=list)
    recomputations: dict[str, int] = field(default_factory=dict)

    def digest(self) -> str:
        rows = [
            (
                d.as_of.isoformat(),
                d.series_id,
                d.evaluation.period_start.isoformat(),
                d.evaluation.value,
                None if d.evaluation.anomaly.z is None else round(d.evaluation.anomaly.z, 9),
                d.decision.stage.value if d.decision.stage else None,
                d.change.kind if d.change else None,
            )
            for d in self.decisions
        ]
        return hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()[:16]


class DetectionReplayer:
    def __init__(
        self,
        loaded: LoadedDetectionConfig,
        metas: dict[int, EpisodeMeta],
        *,
        nulls: NullLookup | None = None,
        categories: dict[int, str] | None = None,
        cold_contexts: dict[int, CorroborationContext] | None = None,
        mode: AvailabilityMode = AvailabilityMode.PUBLICATION,
        render: bool = False,
    ) -> None:
        self.loaded = loaded
        self.cfg = loaded.config
        self.engine = SeriesEngine(loaded, nulls)
        self.metas = metas
        self.categories = categories or {}
        self.cold = cold_contexts or {}
        self.mode = mode
        self.store = MemoryEpisodeStore()
        self.states: dict[int, EngineState] = {}
        self.series: dict[int, AnalysisSeries] = {}
        self.run = ReplayRun()
        self.render = render
        mt = self.cfg.multiple_testing
        self.budget = AlertBudget(
            limit_per_day=mt.max_new_alerts_per_day,
            target_false_alerts_per_day=mt.target_false_alerts_per_day,
        )

    # ------------------------------------------------------------------ step-wise
    def consumer(self) -> _Consumer:
        return _Consumer(self)

    def step(
        self,
        as_of: datetime,
        known: Mapping[int, Sequence[StoredPoint]],
        new_ids: dict[int, set[int]],
    ) -> list[ReplayDecision]:
        produced: list[tuple[int, PointEvaluation]] = []
        for sid in sorted(new_ids):
            if not new_ids[sid] or sid not in self.metas:
                continue
            series = prepare(
                list(known.get(sid, ())), self.cfg, as_of=as_of, mode=self.mode, already_known=True
            )
            self.series[sid] = series
            out = self.engine.run(
                series,
                self.states.get(sid),
                new_obs_ids=new_ids[sid],
                category=self.categories.get(sid),
                cold_context=self.cold.get(sid),
            )
            self.states[sid] = out.state
            if out.recompute_reason and out.recompute_reason != "initial":
                self.run.recomputations[out.recompute_reason] = (
                    self.run.recomputations.get(out.recompute_reason, 0) + 1
                )
            produced.extend((sid, ev) for ev in out.evaluations)
        return self._decide(as_of, produced)

    # ------------------------------------------------------------------ fast path
    def evaluate_batch(
        self, points: Mapping[int, Sequence[StoredPoint]], step: timedelta, origin: datetime
    ) -> ReplayRun:
        """Process each series once; decisions grouped by the step their data appeared."""
        buckets: dict[datetime, list[tuple[int, PointEvaluation]]] = defaultdict(list)
        for sid in sorted(points):
            if sid not in self.metas:
                continue
            series = prepare(list(points[sid]), self.cfg, mode=self.mode, already_known=True)
            self.series[sid] = series
            out = self.engine.run(
                series,
                None,
                category=self.categories.get(sid),
                cold_context=self.cold.get(sid),
            )
            self.states[sid] = out.state
            for ev in out.evaluations:
                available = ev.available_at or ev.period_end
                n = max(0, math.ceil((available - origin) / step))
                buckets[origin + step * n].append((sid, replace(ev, is_late=False)))
        for as_of in sorted(buckets):
            self._decide(as_of, buckets[as_of])
        return self.run

    # ------------------------------------------------------------------ internals
    def _decide(
        self, as_of: datetime, produced: list[tuple[int, PointEvaluation]]
    ) -> list[ReplayDecision]:
        self.run.steps += 1
        hyps = [
            Hypothesis(h.key, h.p_value, h.exact)
            for _, ev in produced
            if not ev.is_late
            for h in ev.hypotheses
        ]
        family: FamilyResult | None = None
        if hyps:
            mt = self.cfg.multiple_testing
            family = control(hyps, mt.method, mt.fdr_q)
            self.run.families.append({"as_of": as_of.isoformat(), **family.as_dict()})
        self.budget.used_last_24h = self.store.notifications_since(as_of - timedelta(days=1))
        out: list[ReplayDecision] = []
        for sid, ev in produced:
            meta = self.metas[sid]
            series = self.series[sid]
            fam: ScaleFamily | None = series.family
            if fam is None:
                continue
            ctx = DecisionContext(
                classification=meta.classification,
                measurement=meta.measurement,
                family=fam,
                multiple_testing=family,
                comparability=series.comparability.status if series.comparability else None,
            )
            decision = decide(ev, ctx, self.cfg)
            manager = EpisodeManager(
                self.store,
                self.cfg,
                config_version=self.loaded.version_tag,
                budget=self.budget,
                renderer=make_renderer(
                    loaded=self.loaded,
                    family=fam,
                    comparability=series.comparability,
                    multiple_testing=family,
                    freshness=None,
                    now=as_of,
                )
                if self.render
                else None,
            )
            change = manager.apply(ev, decision, meta, as_of, family=fam)
            record = ReplayDecision(as_of, sid, ev, decision, change)
            self.run.decisions.append(record)
            out.append(record)
        manager = EpisodeManager(
            self.store, self.cfg, config_version=self.loaded.version_tag, budget=self.budget
        )
        manager.resolve_stale(
            as_of, lambda ep: timedelta(seconds=ep.counters.get("step_seconds", 86400))
        )
        return out


class _Consumer:
    """Adapter: Phase 3 ``replay(..., consumers=[...])`` -> detection steps."""

    def __init__(self, replayer: DetectionReplayer) -> None:
        self.replayer = replayer

    def on_step(self, snapshot: ReplaySnapshot) -> None:
        new_ids: dict[int, set[int]] = defaultdict(set)
        for p in (*snapshot.newly_available, *snapshot.revisions_applied):
            new_ids[p.series_id].add(p.id)
        self.replayer.step(snapshot.as_of, snapshot.series, dict(new_ids))
