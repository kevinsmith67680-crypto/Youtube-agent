"""Persistent surge episodes: creation, update, escalation, downgrade, resolution, reopening.

One open episode exists per episode key (``deduplication.py``); further evidence for the
same continuing event updates it instead of creating another alert. A *notification* is
recorded only when an episode first reaches CANDIDATE or CONFIRMED (creation, escalation
or reopening) - these are what the alert budget counts and what evaluation treats as
alerts. Lifecycle rules (``episodes`` section of the configuration):

* **escalation** is immediate; **downgrade** needs ``downgrade_hold_periods`` consecutive
  periods of lower-stage evidence (hysteresis) and drops one stage at a time;
* **resolution**: back within ``resolve_z`` sd of the *pre-episode* baseline for
  ``resolve_periods`` periods; or no detector support for ``max_quiet_periods`` (the new
  level is treated as normal); or no new observation for ``stale_after_periods`` (stale);
* **rejection**: the only supporting value was revised and no longer triggers;
* **reopening**: a CANDIDATE+ trigger within ``reopen_window`` of resolution reopens the
  same episode (same event, second wave); WATCH-level triggers within
  ``cooldown_periods`` of resolution are ignored (no flapping); later triggers start a new
  episode.

The manager works against an ``EpisodeStore``: PostgreSQL in production
(``SqlEpisodeStore``), memory in replay and evaluation (``MemoryEpisodeStore``), so both
paths share exactly the same rules.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Protocol

from trend_agent.detection.alert_policy import AlertDecision
from trend_agent.detection.config import DetectionConfig, ScaleFamily
from trend_agent.detection.deduplication import EpisodeMeta
from trend_agent.detection.engine import PointEvaluation
from trend_agent.detection.severity import Severity
from trend_agent.detection.severity import score as severity_score
from trend_agent.detection.significance import AlertBudget
from trend_agent.discovery.models import Category
from trend_agent.schemas.detection import DetectorType, Reason, SupportStatus
from trend_agent.schemas.evidence import STAGE_RANK, AlertStage, SurgeClassification
from trend_agent.schemas.monitoring import MeasurementCategory

ORDER = [AlertStage.WATCH, AlertStage.CANDIDATE, AlertStage.CONFIRMED]


@dataclass
class Episode:
    key: str
    target_id: int
    series_id: int | None
    query_id: int
    keyword_id: int | None
    topic_cluster_id: int | None
    category: Category
    provider: str
    measurement: MeasurementCategory
    is_proxy: bool
    classification: SurgeClassification
    region: str
    language: str | None
    status: AlertStage
    confidence: SupportStatus
    episode_start: datetime
    first_detected_at: datetime
    last_updated_at: datetime
    last_observation_period_end: datetime
    severity: float
    maximum_severity: float
    peak_value: float | None
    peak_period_start: datetime | None
    peak_observation_id: int | None
    baseline_expected: float | None
    baseline_dispersion: float | None
    algorithm_version: str
    configuration_version: str
    is_synthetic: bool
    resolved_at: datetime | None = None
    reopen_count: int = 0
    notifications: int = 0
    counters: dict[str, Any] = field(default_factory=dict)
    explanation: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    expires_at: datetime | None = None
    id: int | None = None

    @property
    def is_open(self) -> bool:
        return self.status in ORDER


@dataclass(frozen=True)
class EvidenceItem:
    observation_id: int
    evidence_type: str
    source_reference: str
    contribution: float
    detector_result_id: int | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TransitionRecord:
    kind: str  # created | escalated | downgraded | updated | resolved | reopened | rejected
    from_status: str | None
    to_status: str
    severity: float
    notification: bool
    reason: str
    at: datetime


@dataclass
class EpisodeChange:
    kind: str
    episode: Episode  # live object (may change later)
    notification: bool
    reason: str
    status: AlertStage | None = None  # snapshot of the episode status after this change
    severity: float = 0.0  # snapshot of the severity after this change

    def __post_init__(self) -> None:
        if self.status is None:
            self.status = self.episode.status
        self.severity = self.episode.severity


class EpisodeStore(Protocol):
    def open_episode(self, key: str) -> Episode | None: ...
    def last_closed(self, key: str) -> Episode | None: ...
    def open_episodes(self) -> list[Episode]: ...
    def insert(self, episode: Episode) -> Episode: ...
    def update(self, episode: Episode) -> None: ...
    def transition(self, episode: Episode, record: TransitionRecord) -> None: ...
    def evidence(self, episode: Episode, items: list[EvidenceItem]) -> None: ...


class MemoryEpisodeStore:
    """In-memory store (replay, evaluation, unit tests)."""

    def __init__(self) -> None:
        self.episodes: dict[int, Episode] = {}
        self.transitions: list[tuple[int, TransitionRecord]] = []
        self.evidence_items: list[tuple[int, EvidenceItem]] = []
        self._ids = itertools.count(1)

    def open_episode(self, key: str) -> Episode | None:
        for ep in self.episodes.values():
            if ep.key == key and ep.is_open:
                return ep
        return None

    def last_closed(self, key: str) -> Episode | None:
        closed = [e for e in self.episodes.values() if e.key == key and not e.is_open]
        return max(
            closed, key=lambda e: (e.resolved_at or e.last_updated_at, e.id or 0), default=None
        )

    def open_episodes(self) -> list[Episode]:
        return [e for e in self.episodes.values() if e.is_open]

    def insert(self, episode: Episode) -> Episode:
        episode.id = next(self._ids)
        self.episodes[episode.id] = episode
        return episode

    def update(self, episode: Episode) -> None:
        assert episode.id is not None
        self.episodes[episode.id] = episode

    def transition(self, episode: Episode, record: TransitionRecord) -> None:
        assert episode.id is not None
        self.transitions.append((episode.id, record))

    def evidence(self, episode: Episode, items: list[EvidenceItem]) -> None:
        assert episode.id is not None
        self.evidence_items.extend((episode.id, i) for i in items)

    def notifications_since(self, since: datetime) -> int:
        return sum(1 for _, t in self.transitions if t.notification and t.at > since)


Renderer = Callable[
    [Episode, PointEvaluation | None, AlertDecision | None, EpisodeMeta], tuple[str, dict[str, Any]]
]


class EpisodeManager:
    def __init__(
        self,
        store: EpisodeStore,
        cfg: DetectionConfig,
        *,
        config_version: str,
        budget: AlertBudget,
        renderer: Renderer | None = None,
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.config_version = config_version
        self.budget = budget
        self.renderer = renderer

    # ------------------------------------------------------------------ public
    def apply(
        self,
        ev: PointEvaluation,
        decision: AlertDecision,
        meta: EpisodeMeta,
        now: datetime,
        *,
        family: ScaleFamily,
        result_ids: dict[DetectorType, int] | None = None,
    ) -> EpisodeChange | None:
        key = meta.key
        revised = ev.observation is not None and ev.observation.revision > 1
        if ev.is_late and not revised:
            # Late history (e.g. re-evaluation after lost state) never drives the lifecycle.
            return None
        ep = self.store.open_episode(key)
        stage = decision.stage if decision.opens else None
        obs_id = ev.observation_id
        # Idempotency: an observation revision already applied to this episode key (e.g.
        # reprocessing after lost detector state) changes nothing. Revisions and late
        # arrivals carry new observation ids and are applied.
        if ep is not None and obs_id is not None and obs_id in ep.counters.get("applied", []):
            return None
        if ep is None:
            last = self.store.last_closed(key)
            if (
                last is not None
                and obs_id is not None
                and obs_id in last.counters.get("applied", [])
            ):
                return None
            if stage is None:
                return None
            if last is not None and last.status is AlertStage.RESOLVED and last.resolved_at:
                within = now - last.resolved_at <= self.cfg.episodes.reopen_window
                if within and STAGE_RANK[stage] >= STAGE_RANK[AlertStage.CANDIDATE]:
                    return self._reopen(last, ev, decision, meta, now, family, result_ids)
                gap = (ev.period_start - last.last_observation_period_end) / meta.step
                if stage is AlertStage.WATCH and gap < self.cfg.episodes.cooldown_periods:
                    return EpisodeChange("suppressed", last, False, "cooldown after resolution")
            return self._create(ev, decision, meta, now, stage, result_ids)
        return self._update(ep, ev, decision, meta, now, stage, family, result_ids)

    def resolve_stale(
        self, now: datetime, step_of: Callable[[Episode], timedelta]
    ) -> list[Episode]:
        """Resolve open episodes whose series produced no observation for too long."""
        out: list[Episode] = []
        limit = self.cfg.episodes.stale_after_periods
        for ep in self.store.open_episodes():
            step = step_of(ep)
            if (now - ep.last_observation_period_end) / step > limit:
                self._close(
                    ep,
                    AlertStage.RESOLVED,
                    now,
                    f"no new observation for more than {limit} periods (source stale)",
                )
                out.append(ep)
        return out

    # ------------------------------------------------------------------ internals
    def _notify(self, stage: AlertStage, ep: Episode | None) -> tuple[bool, bool]:
        """(is a notification, allowed by the budget)."""
        if STAGE_RANK[stage] < STAGE_RANK[AlertStage.CANDIDATE]:
            return False, True
        notified = set(ep.counters.get("notified", [])) if ep else set()
        if stage.value in notified:
            return False, True
        if not self.budget.allow():
            self.budget.held_back += 1
            return True, False
        return True, True

    def _onset(self, ev: PointEvaluation, meta: EpisodeMeta) -> datetime:
        """Episode start: the triggering period, or the onset located by an *elevated*
        sustained-change detector (CUSUM run start / change point), whichever is earlier.
        A quietly accumulating CUSUM run (noise) does not move the start."""
        start = ev.period_start
        elevated = ("triggered", "watch")
        candidates = []
        if ev.cusum.status.value in elevated:
            candidates.append(ev.cusum.onset_slot)
        if ev.change_point.status.value in elevated:
            candidates.append(ev.change_point.change_slot)
        for slot in candidates:
            if slot is not None and slot <= ev.slot:
                start = min(start, ev.period_start - meta.step * (ev.slot - slot))
        return start

    def _evidence(
        self,
        ep: Episode,
        ev: PointEvaluation,
        decision: AlertDecision,
        result_ids: dict[DetectorType, int] | None,
    ) -> None:
        if ev.observation is None:
            return
        comp = {c.name: c.normalized for c in decision.severity.components}
        items = [
            EvidenceItem(
                ev.observation.id,
                "observation",
                ev.observation.source_reference,
                round(comp.get("magnitude", 0.0), 4),
                None,
                {"value": ev.value, "revision": ev.observation.revision},
            )
        ]
        detail = {
            DetectorType.ROBUST_ANOMALY: (ev.anomaly.elevated, comp.get("deviation", 0.0)),
            DetectorType.CUSUM: (
                ev.cusum.status.value in ("triggered", "watch"),
                comp.get("persistence", 0.0),
            ),
            DetectorType.CHANGE_POINT: (
                ev.change_point.status.value in ("triggered", "watch"),
                comp.get("change_point", 0.0),
            ),
        }
        for det, (hit, contribution) in detail.items():
            if hit:
                items.append(
                    EvidenceItem(
                        ev.observation.id,
                        det.value,
                        ev.observation.source_reference,
                        round(contribution, 4),
                        (result_ids or {}).get(det),
                    )
                )
        if ev.cold_start is not None and ev.cold_start.stage is not None:
            items.append(
                EvidenceItem(
                    ev.observation.id,
                    DetectorType.COLD_START.value,
                    ev.observation.source_reference,
                    0.0,
                    (result_ids or {}).get(DetectorType.COLD_START),
                    {"state": ev.cold_start.state.value},
                )
            )
        self.store.evidence(ep, items)

    def _render(
        self,
        ep: Episode,
        ev: PointEvaluation | None,
        decision: AlertDecision | None,
        meta: EpisodeMeta | None,
    ) -> None:
        if self.renderer is not None and meta is not None:
            ep.explanation, ep.payload = self.renderer(ep, ev, decision, meta)

    def _create(
        self,
        ev: PointEvaluation,
        decision: AlertDecision,
        meta: EpisodeMeta,
        now: datetime,
        stage: AlertStage,
        result_ids: dict[DetectorType, int] | None,
    ) -> EpisodeChange:
        notify, allowed = self._notify(stage, None)
        reason = "; ".join(decision.reasons[:4]) or f"{stage.value} evidence"
        if notify and not allowed:
            stage, notify = AlertStage.WATCH, False
            reason = f"{Reason.BUDGET_EXHAUSTED.value}; {reason}"
        if notify:
            self.budget.consume()
        expected = ev.baseline.expected
        dispersion = ev.baseline.dispersion
        if expected is None and ev.cold_start is not None:
            expected = ev.cold_start.first_value
        expires = ev.observation.expires_at if ev.observation is not None else None
        ep = Episode(
            key=meta.key,
            target_id=meta.target_id,
            series_id=ev.series_id,
            query_id=meta.query_id,
            keyword_id=meta.keyword_id,
            topic_cluster_id=meta.topic_cluster_id,
            category=meta.category,
            provider=meta.provider,
            measurement=meta.measurement,
            is_proxy=meta.is_proxy,
            classification=meta.classification,
            region=meta.region,
            language=meta.language,
            status=stage,
            confidence=decision.confidence,
            episode_start=self._onset(ev, meta),
            first_detected_at=now,
            last_updated_at=now,
            last_observation_period_end=ev.period_end,
            severity=decision.severity.score,
            maximum_severity=decision.severity.score,
            peak_value=ev.value,
            peak_period_start=ev.period_start,
            peak_observation_id=ev.observation_id,
            baseline_expected=expected,
            baseline_dispersion=dispersion,
            algorithm_version=self.cfg.algorithm_version,
            configuration_version=self.config_version,
            is_synthetic=meta.is_synthetic,
            notifications=1 if notify else 0,
            counters={
                "calm": 0,
                "quiet": 0,
                "downgrade": 0,
                "support_periods": 1,
                "notified": [stage.value] if notify else [],
                "step_seconds": int(meta.step.total_seconds()),
                "applied": [ev.observation_id] if ev.observation_id is not None else [],
            },
            expires_at=expires,
        )
        self._render(ep, ev, decision, meta)
        ep = self.store.insert(ep)
        self.store.transition(
            ep,
            TransitionRecord("created", None, stage.value, ep.severity, notify, reason, now),
        )
        self._evidence(ep, ev, decision, result_ids)
        return EpisodeChange("created", ep, notify, reason)

    def _reopen(
        self,
        ep: Episode,
        ev: PointEvaluation,
        decision: AlertDecision,
        meta: EpisodeMeta,
        now: datetime,
        family: ScaleFamily,
        result_ids: dict[DetectorType, int] | None,
    ) -> EpisodeChange:
        assert decision.stage is not None
        stage = decision.stage
        notify, allowed = self._notify(stage, None)
        reason = f"renewed {stage.value} evidence within the reopen window"
        if notify and not allowed:
            stage, notify = AlertStage.WATCH, False
            reason = f"{Reason.BUDGET_EXHAUSTED.value}; {reason}"
        if notify:
            self.budget.consume()
            ep.notifications += 1
        previous = ep.status
        ep.status = stage
        ep.resolved_at = None
        ep.reopen_count += 1
        ep.confidence = decision.confidence
        ep.counters.update({"calm": 0, "quiet": 0, "downgrade": 0})
        ep.counters["notified"] = [stage.value] if notify else []
        self._touch(ep, ev, meta, now, family, decision)
        self._render(ep, ev, decision, meta)
        self.store.update(ep)
        self.store.transition(
            ep,
            TransitionRecord(
                "reopened", previous.value, stage.value, ep.severity, notify, reason, now
            ),
        )
        self._evidence(ep, ev, decision, result_ids)
        return EpisodeChange("reopened", ep, notify, reason)

    def _touch(
        self,
        ep: Episode,
        ev: PointEvaluation,
        meta: EpisodeMeta,
        now: datetime,
        family: ScaleFamily,
        decision: AlertDecision,
    ) -> Severity:
        ep.last_updated_at = now
        ep.series_id = ev.series_id
        _remember(ep, ev)
        ep.last_observation_period_end = max(ep.last_observation_period_end, ev.period_end)
        sev = decision.severity
        if (
            ep.baseline_expected is not None
            and ep.baseline_dispersion is not None
            and ev.value is not None
        ):
            sev = severity_score(
                ev,
                family,
                self.cfg.severity,
                cusum_h=self.cfg.cusum.h,
                min_expected_for_relative=self.cfg.anomaly.min_expected_for_relative.get(
                    family, 5.0
                ),
                reference=(ep.baseline_expected, ep.baseline_dispersion),
            )
            sev = replace(sev, score=max(sev.score, decision.severity.score))
        if ev.value is not None:
            ep.severity = sev.score
            ep.maximum_severity = max(ep.maximum_severity, sev.score)
        if ev.value is not None and (ep.peak_value is None or ev.value > ep.peak_value):
            ep.peak_value = ev.value
            ep.peak_period_start = ev.period_start
            ep.peak_observation_id = ev.observation_id
        if ev.observation is not None:
            exp = ev.observation.expires_at
            # derived values never outlive any of their sources: earliest expiry wins
            if exp is not None and (ep.expires_at is None or exp < ep.expires_at):
                ep.expires_at = exp
        return sev

    def _close(self, ep: Episode, status: AlertStage, now: datetime, reason: str) -> None:
        previous = ep.status
        ep.status = status
        ep.resolved_at = now
        ep.last_updated_at = now
        ep.payload = {
            **ep.payload,
            "alert_status": status.value,
            "resolved_at": now.isoformat(),
            "last_updated_at": now.isoformat(),
        }
        self.store.update(ep)
        self.store.transition(
            ep,
            TransitionRecord(
                "resolved" if status is AlertStage.RESOLVED else "rejected",
                previous.value,
                status.value,
                ep.severity,
                False,
                reason,
                now,
            ),
        )

    def _update(
        self,
        ep: Episode,
        ev: PointEvaluation,
        decision: AlertDecision,
        meta: EpisodeMeta,
        now: datetime,
        stage: AlertStage | None,
        family: ScaleFamily,
        result_ids: dict[DetectorType, int] | None,
    ) -> EpisodeChange:
        c = ep.counters
        cfg = self.cfg.episodes
        revised = ev.observation is not None and ev.observation.revision > 1
        if not revised and (
            ev.period_end <= ep.episode_start or ev.period_end <= ep.last_observation_period_end
        ):
            # A late value for a period the episode already moved past: keep it as evidence
            # (peak, provenance) but never let it drive escalation or resolution counters.
            self._touch(ep, ev, meta, now, family, decision)
            if stage is not None:
                self._evidence(ep, ev, decision, result_ids)
            self.store.update(ep)
            return EpisodeChange("updated", ep, False, "late value for an earlier period")
        self._touch(ep, ev, meta, now, family, decision)
        if (
            stage is None
            and revised
            and ep.peak_period_start == ev.period_start
            and int(c.get("support_periods", 1)) <= 1
        ):
            self._render(ep, ev, decision, meta)
            self._close(ep, AlertStage.REJECTED, now, Reason.REVISED_AWAY.value)
            return EpisodeChange("rejected", ep, False, Reason.REVISED_AWAY.value)
        kind, notify, reason = "updated", False, ""
        if stage is not None:
            c["quiet"] = 0
            c["calm"] = 0
            c["support_periods"] = int(c.get("support_periods", 1)) + 1
            if STAGE_RANK[stage] > STAGE_RANK[ep.status]:
                notify, allowed = self._notify(stage, ep)
                if notify and not allowed:
                    notify = False
                    reason = f"{Reason.BUDGET_EXHAUSTED.value}: escalation to {stage.value} held"
                else:
                    if notify:
                        self.budget.consume()
                        ep.notifications += 1
                        c["notified"] = [*c.get("notified", []), stage.value]
                    previous = ep.status
                    ep.status = stage
                    kind = "escalated"
                    reason = "; ".join(decision.reasons[:4]) or f"{stage.value} evidence"
                    self.store.transition(
                        ep,
                        TransitionRecord(
                            kind, previous.value, stage.value, ep.severity, notify, reason, now
                        ),
                    )
                c["downgrade"] = 0
            elif STAGE_RANK[stage] < STAGE_RANK[ep.status] and _sustained(ep, ev, cfg.sustain_z):
                # Detectors weaken as rolling baselines adapt, but the value is still far
                # above the pre-episode baseline: the surge persists, the stage is held.
                c["downgrade"] = 0
            elif STAGE_RANK[stage] < STAGE_RANK[ep.status]:
                c["downgrade"] = int(c.get("downgrade", 0)) + 1
                if c["downgrade"] >= cfg.downgrade_hold_periods:
                    previous = ep.status
                    ep.status = ORDER[ORDER.index(ep.status) - 1]
                    c["downgrade"] = 0
                    kind = "downgraded"
                    reason = (
                        f"evidence at {stage.value} level for {cfg.downgrade_hold_periods} periods"
                    )
                    self.store.transition(
                        ep,
                        TransitionRecord(
                            kind, previous.value, ep.status.value, ep.severity, False, reason, now
                        ),
                    )
            else:
                c["downgrade"] = 0
            if decision.confidence is SupportStatus.STATISTICALLY_SUPPORTED or (
                ep.confidence is not SupportStatus.STATISTICALLY_SUPPORTED
            ):
                ep.confidence = decision.confidence
            self._evidence(ep, ev, decision, result_ids)
        elif ev.value is not None:
            c["quiet"] = int(c.get("quiet", 0)) + 1
            ref, sd = ep.baseline_expected, ep.baseline_dispersion
            calm = (
                ref is not None
                and sd is not None
                and (ev.value - ref) / max(sd, 1e-9) <= cfg.resolve_z
            )
            c["calm"] = int(c.get("calm", 0)) + 1 if calm else 0
            if c["calm"] >= cfg.resolve_periods:
                why = (
                    f"back within {cfg.resolve_z:g} sd of the pre-episode baseline for "
                    f"{cfg.resolve_periods} periods"
                )
                self._render(ep, ev, decision, meta)
                self._close(ep, AlertStage.RESOLVED, now, why)
                return EpisodeChange("resolved", ep, False, why)
            if c["quiet"] >= cfg.max_quiet_periods:
                why = (
                    f"no detector support for {cfg.max_quiet_periods} periods; the elevated "
                    "level is treated as the new normal"
                )
                self._render(ep, ev, decision, meta)
                self._close(ep, AlertStage.RESOLVED, now, why)
                return EpisodeChange("resolved", ep, False, why)
        self._render(ep, ev, decision, meta)
        self.store.update(ep)
        return EpisodeChange(kind, ep, notify, reason)


APPLIED_MEMORY = 128


def _remember(ep: Episode, ev: PointEvaluation) -> None:
    if ev.observation_id is None:
        return
    applied = [i for i in ep.counters.get("applied", []) if i != ev.observation_id]
    applied.append(ev.observation_id)
    ep.counters["applied"] = applied[-APPLIED_MEMORY:]


def _sustained(ep: Episode, ev: PointEvaluation, sustain_z: float) -> bool:
    ref, sd = ep.baseline_expected, ep.baseline_dispersion
    if ref is None or sd is None or ev.value is None:
        return False
    return (ev.value - ref) / max(sd, 1e-9) >= sustain_z
