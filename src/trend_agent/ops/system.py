"""System view: provider readiness, degradations, validation evidence and the operating mode.

``SystemService.decide()`` is the single place the operating mode is computed. It is used by
the CLI (``system providers|health|readiness``), the API (``/health/providers``,
``/api/system/mode``) and the scheduler (every ops tick, which also records mode changes and
raises an alert on each transition - a mode change is never silent).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from trend_agent.db.repositories.ops import (
    ExperimentRepository,
    HeartbeatRepository,
    LabelRepository,
    ProviderCheckRepository,
    SystemStateRepository,
)
from trend_agent.db.session import transaction
from trend_agent.logging import get_logger, log_event, redact_text
from trend_agent.ops import health
from trend_agent.ops.budgets import BudgetReport
from trend_agent.ops.modes import (
    Degradation,
    DegradationFact,
    ModeDecision,
    OperatingMode,
    ValidationStatus,
    decide_mode,
    transition,
)
from trend_agent.ops.providers import CheckRecord, ProviderReadiness, ReadinessLevel, assess
from trend_agent.ops.signals import SignalClass

if TYPE_CHECKING:
    from trend_agent.container import AppContainer
    from trend_agent.ops.alerts import OpsAlerter

_log = get_logger(__name__)

MODE_STATE_KEY = "operating_mode"
STALE_DATA_AFTER = timedelta(hours=48)
# Minimum real evidence before detection on a signal class counts as validated.
MIN_REAL_SURGE_LABELS = 30
MIN_VALIDATED_PRECISION = 0.7
MIN_VALIDATED_RECALL = 0.5


@dataclass(frozen=True)
class SystemSnapshot:
    decision: ModeDecision
    readiness: list[ProviderReadiness]
    budgets: BudgetReport | None
    validation: ValidationStatus
    notes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "operating_mode": self.decision.as_dict(),
            "providers": [r.as_dict() for r in self.readiness],
            "budgets": self.budgets.as_dict() if self.budgets is not None else None,
            "validation": {
                "direct_detection_validated": self.validation.direct_detection_validated,
                "detail": self.validation.detail,
            },
            "notes": self.notes,
        }


class SystemService:
    def __init__(self, container: AppContainer) -> None:
        self.c = container
        self.settings = container.settings
        self.factory = container.session_factory
        self.clock = container.clock

    # ------------------------------------------------------------------ inputs
    def providers(self, *, with_discovery: bool = True) -> tuple[list[Any], list[str]]:
        notes: list[str] = []
        if with_discovery:
            try:
                self.c.discovery()  # registers discovery providers (no network I/O)
            except Exception as exc:  # configuration problems are reported, not fatal
                notes.append(redact_text(f"discovery providers not loaded: {exc}"))
        return self.c.registry.all(), notes

    def checks(self) -> dict[str, CheckRecord]:
        try:
            with transaction(self.factory) as s:
                repo = ProviderCheckRepository(s)
                latest = repo.latest()
                success = repo.last_success()
        except Exception:
            return {}
        return {
            name: CheckRecord(r.checked_at, r.ok, r.status, r.detail, success.get(name))
            for name, r in latest.items()
        }

    def readiness(
        self, *, with_discovery: bool = True
    ) -> tuple[list[ProviderReadiness], list[str]]:
        provs, notes = self.providers(with_discovery=with_discovery)
        return assess(provs, self.settings, checks=self.checks(), now=self.clock()), notes

    def validation(self) -> ValidationStatus:
        """Direct-search detection counts as validated only with enough real labels and an
        adopted real-data detection experiment on the direct signal class that meets the
        precision and recall floors. Anything else is UNKNOWN, i.e. not validated."""
        try:
            with transaction(self.factory) as s:
                counts = LabelRepository(s).counts()
                exps = ExperimentRepository(s).list("detection", limit=50)
        except Exception:
            return ValidationStatus(False, "validation evidence unavailable (database)")
        real = counts.get("real", {}).get("surge_event", 0)
        if real < MIN_REAL_SURGE_LABELS:
            return ValidationStatus(
                False,
                f"{real} real labelled surge(s) (minimum {MIN_REAL_SURGE_LABELS}): real-world "
                "detection accuracy is UNKNOWN",
            )
        for e in exps:
            if (
                e.data_origin == "real"
                and e.decision == "adopted"
                and e.dataset.get("signal_class") == SignalClass.DIRECT_YOUTUBE_SEARCH_INTEREST
            ):
                p = e.metrics.get("precision")
                r = e.metrics.get("recall")
                if (
                    isinstance(p, (int, float))
                    and isinstance(r, (int, float))
                    and p >= MIN_VALIDATED_PRECISION
                    and r >= MIN_VALIDATED_RECALL
                ):
                    return ValidationStatus(
                        True, f"experiment {e.id}: precision {p:.2f}, recall {r:.2f} on real data"
                    )
        return ValidationStatus(
            False,
            f"{real} real surge labels, but no adopted real-data detection experiment on "
            "DIRECT_YOUTUBE_SEARCH_INTEREST meets precision >= "
            f"{MIN_VALIDATED_PRECISION} and recall >= {MIN_VALIDATED_RECALL}",
        )

    def budgets(self) -> BudgetReport | None:
        try:
            return self.c.budget_guard().report(fresh=True)
        except Exception as exc:
            log_event(_log, "system.budget_read_failed", error=redact_text(str(exc)))
            return None

    def degradations(
        self,
        readiness: Sequence[ProviderReadiness],
        budgets: BudgetReport | None,
        *,
        base_collects: bool,
    ) -> list[DegradationFact]:
        out: list[DegradationFact] = []
        db = health.database(self.c.engine)
        if not db.ok:
            kind = (
                Degradation.DATABASE_UNAVAILABLE
                if "unreachable" in db.detail
                else Degradation.SCHEMA_OUTDATED
            )
            return [DegradationFact(kind, db.detail)]
        if budgets is not None:
            if budgets.llm_exhausted:
                out.append(DegradationFact(Degradation.LLM_BUDGET_EXHAUSTED, "LLM budget reached"))
            if budgets.api_exhausted:
                out.append(DegradationFact(Degradation.API_BUDGET_EXHAUSTED, "API budget reached"))
        failing = [r.name for r in readiness if r.level is ReadinessLevel.DEGRADED]
        if failing:
            out.append(
                DegradationFact(
                    Degradation.PROVIDER_FAILING, "latest live check failed: " + ", ".join(failing)
                )
            )
        if base_collects:
            now = self.clock()
            with transaction(self.factory) as s:
                hb = HeartbeatRepository(s)
                schedulers = hb.live("scheduler", now)
                workers = hb.live("worker", now)
                latest = s.execute(
                    sa.text("SELECT max(collected_at) FROM ts_observations WHERE NOT is_synthetic")
                ).scalar_one_or_none()
            if not schedulers:
                out.append(
                    DegradationFact(
                        Degradation.SCHEDULER_NOT_RUNNING, "no scheduler heartbeat is fresh"
                    )
                )
            if not workers:
                out.append(
                    DegradationFact(Degradation.NO_LIVE_WORKERS, "no worker heartbeat is fresh")
                )
            if latest is None or now - latest > STALE_DATA_AFTER:
                age = "never" if latest is None else f"last at {latest.isoformat()}"
                out.append(
                    DegradationFact(
                        Degradation.DATA_STALE, f"no real observation collected ({age})"
                    )
                )
        return out

    # ------------------------------------------------------------------ decision
    def snapshot(self, *, with_discovery: bool = True) -> SystemSnapshot:
        demo = self.settings.demo_mode
        readiness, notes = self.readiness(with_discovery=with_discovery)
        validation = self.validation() if not demo else ValidationStatus()
        budgets = self.budgets() if not demo else None
        probe = decide_mode(readiness, now=self.clock(), validation=validation, demo=demo)
        degr = (
            []
            if demo
            else self.degradations(
                readiness,
                budgets,
                base_collects=probe.base_mode is not OperatingMode.DEMONSTRATION,
            )
        )
        decision = decide_mode(
            readiness, now=self.clock(), validation=validation, degradations=degr, demo=demo
        )
        return SystemSnapshot(decision, readiness, budgets, validation, notes)

    def decide(self) -> ModeDecision:
        return self.snapshot().decision

    def record(
        self, decision: ModeDecision, alerter: OpsAlerter | None = None
    ) -> dict[str, Any] | None:
        """Persist the mode; on a change append history and raise an alert. Returns the
        transition (None when unchanged)."""
        with transaction(self.factory) as s:
            repo = SystemStateRepository(s)
            prev = repo.get(MODE_STATE_KEY)
            change = transition(prev, decision)
            if change is None:
                return None
            history = [*list((prev or {}).get("history", []))[-49:], change]
            state = decision.as_dict() | {
                "since": decision.decided_at.isoformat(),
                "history": history,
            }
            repo.put(MODE_STATE_KEY, state, decision.decided_at)
        log_event(
            _log, "system.mode_changed", **{k: v for k, v in change.items() if k != "reasons"}
        )
        if alerter is not None:
            alerter.raise_alert(
                "mode_change",
                f"{change['from']}->{change['to']}:{decision.decided_at:%Y%m%d%H%M}",
                severity="warning" if change["downgrade"] else "info",
                title=f"Operating mode {change['from'] or 'unset'} -> {change['to']}",
                detail="; ".join(decision.reasons)
                + (
                    "; direct YouTube Search surge claims are WITHDRAWN"
                    if change["direct_search_claims_withdrawn"]
                    else ""
                ),
                details=change,
            )
        return change

    def mode_state(self) -> dict[str, Any] | None:
        with transaction(self.factory) as s:
            return SystemStateRepository(s).get(MODE_STATE_KEY)


def latest_real_observation(factory: Any) -> datetime | None:
    with transaction(factory) as s:
        return s.execute(
            sa.text("SELECT max(collected_at) FROM ts_observations WHERE NOT is_synthetic")
        ).scalar_one_or_none()
