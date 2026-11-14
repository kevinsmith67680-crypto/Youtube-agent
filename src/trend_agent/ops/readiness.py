"""Production readiness scorecard.

Every item is PASS, FAIL, BLOCKED or NOT_APPLICABLE, with the evidence it was decided on.
BLOCKED means the item cannot pass for a reason outside the code (a credential, an access
grant, a legal review, a deployment that has not happened); it is **never** counted as
PASS.

Items have a scope:

* ``build``    must hold for the code base itself (tests, security scans, artifacts).
* ``runtime``  can only hold in a running deployment (scheduler, workers, backups, data).
* ``external`` depends on third parties (search-interest access, policy approvals, labels).

Release state (first match):

==========================  ==============================================================
NOT_READY                    any ``build`` item FAILS.
BLOCKED                      any ``build`` item is BLOCKED.
PRODUCTION_READY             every item PASSES or is NOT_APPLICABLE.
LIMITED_PRODUCTION_READY     every ``build`` and ``runtime`` item passes, a real proxy
                             provider is live, the compliance review passed; only
                             search-interest access and real-world accuracy remain BLOCKED.
STAGING_READY                every ``build`` item passes; runtime/external items do not.
==========================  ==============================================================
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trend_agent.db.repositories.ops import (
    ExperimentRepository,
    LabelRepository,
    SystemStateRepository,
)
from trend_agent.db.session import transaction
from trend_agent.ops import health, production
from trend_agent.ops.providers import ReadinessLevel
from trend_agent.ops.signals import PROXY_CLASSES, SignalClass

if TYPE_CHECKING:
    from trend_agent.container import AppContainer
    from trend_agent.ops.system import SystemSnapshot

EVIDENCE_FILE = Path("docs/benchmarks/phase8_verification.json")
RESTORE_DRILL_KEY = "restore_drill"
RESTORE_DRILL_MAX_AGE = timedelta(days=30)
MIN_REAL_LABELS = {
    "surge_event": 30,
    "content_gap": 50,
    "forecast_outcome": 100,
}
# Offline (synthetic / fixture) quality targets per component; see the evaluation docs.
OFFLINE_TARGETS: dict[str, dict[str, tuple[str, float]]] = {
    "detection": {"precision": (">=", 0.6), "recall": (">=", 0.6)},
    "forecasting": {"beats_best_baseline_share": (">=", 0.5)},
    "content_gaps": {"false_gap_rate": ("<=", 0.25), "verified_gap_precision": (">=", 0.7)},
    "recommendations": {"grounding_violation_rate": ("<=", 0.0), "duplicate_f1": (">=", 0.8)},
}


class ItemStatus(StrEnum):
    PASS = "PASS"  # noqa: S105 - a status name, not a password
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ReleaseState(StrEnum):
    PRODUCTION_READY = "PRODUCTION_READY"
    LIMITED_PRODUCTION_READY = "LIMITED_PRODUCTION_READY"
    STAGING_READY = "STAGING_READY"
    BLOCKED = "BLOCKED"
    NOT_READY = "NOT_READY"


@dataclass(frozen=True)
class Item:
    key: str
    area: str
    scope: str
    status: ItemStatus
    evidence: str
    action: str = ""

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass(frozen=True)
class Scorecard:
    generated_at: datetime
    state: ReleaseState
    items: tuple[Item, ...]
    reasons: tuple[str, ...]

    def counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in ItemStatus}
        for i in self.items:
            out[i.status.value] += 1
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "release_state": self.state.value,
            "counts": self.counts(),
            "reasons": list(self.reasons),
            "items": [i.as_dict() for i in self.items],
        }


def release_state(items: Sequence[Item]) -> tuple[ReleaseState, list[str]]:
    build = [i for i in items if i.scope == "build"]
    runtime = [i for i in items if i.scope == "runtime"]
    external = [i for i in items if i.scope == "external"]
    ok = {ItemStatus.PASS, ItemStatus.NOT_APPLICABLE}
    if fails := [i.key for i in build if i.status is ItemStatus.FAIL]:
        return ReleaseState.NOT_READY, [f"build item(s) failing: {', '.join(fails)}"]
    if blocked := [i.key for i in build if i.status is ItemStatus.BLOCKED]:
        return ReleaseState.BLOCKED, [f"build item(s) blocked: {', '.join(blocked)}"]
    if all(i.status in ok for i in items):
        return ReleaseState.PRODUCTION_READY, ["every item passes"]
    runtime_ok = all(i.status in ok for i in runtime)
    tolerated = {
        "direct_search_provider",
        "external_search_provider",
        "detection_real_accuracy",
        "forecast_real_accuracy",
        "content_gap_real_accuracy",
    }
    ext_bad = [i for i in external if i.status not in ok]
    proxy_live = any(i.key == "proxy_provider_live" and i.status is ItemStatus.PASS for i in items)
    if (
        runtime_ok
        and proxy_live
        and all(i.key in tolerated and i.status is ItemStatus.BLOCKED for i in ext_bad)
    ):
        return ReleaseState.LIMITED_PRODUCTION_READY, [
            "build and runtime items pass; operating as proxy opportunity research only",
            "blocked: " + ", ".join(i.key for i in ext_bad),
        ]
    reasons = ["all build items pass"]
    if bad := [i.key for i in runtime if i.status not in ok]:
        reasons.append(
            "runtime items not passing (no running deployment verified): " + ", ".join(bad)
        )
    if ext_bad:
        reasons.append("external items not passing: " + ", ".join(i.key for i in ext_bad))
    return ReleaseState.STAGING_READY, reasons


def _evidence(root: Path) -> dict[str, Any] | None:
    path = root / EVIDENCE_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _build_items(ev: dict[str, Any] | None) -> list[Item]:
    def item(key: str, area: str, check: str, action: str) -> Item:
        if ev is None:
            return Item(
                key,
                area,
                "build",
                ItemStatus.FAIL,
                f"no recorded verification ({EVIDENCE_FILE})",
                action,
            )
        rec = ev.get(check)
        if not isinstance(rec, dict) or "passed" not in rec:
            return Item(key, area, "build", ItemStatus.FAIL, f"'{check}' not recorded", action)
        st = ItemStatus.PASS if rec["passed"] else ItemStatus.FAIL
        where = f" (recorded {ev.get('recorded_at', '?')} at {ev.get('commit', '?')})"
        return Item(key, area, "build", st, str(rec.get("summary", "")) + where, action)

    return [
        item("backend_tests", "quality", "backend_tests", "run pytest on Python 3.12 and 3.13"),
        item("e2e_scenarios", "quality", "e2e_scenarios", "run tests/e2e (40 scenarios)"),
        item("static_checks", "quality", "static_checks", "ruff, mypy (src, tests), tsc"),
        item("frontend_tests", "quality", "frontend_tests", "vitest, Playwright, next build"),
        item("dependency_audit", "security", "dependency_audit", "pip-audit, npm audit --omit=dev"),
        item("code_security_scan", "security", "code_security_scan", "bandit, detect-secrets"),
        item(
            "deployment_artifacts",
            "deployment",
            "deployment_artifacts",
            "docker build the api/scheduler/worker and frontend images; compose config",
        ),
        item(
            "migration_validation",
            "deployment",
            "migration_validation",
            "alembic upgrade/downgrade/upgrade on an empty database; alembic check",
        ),
        item(
            "offline_evaluation",
            "accuracy",
            "offline_evaluation",
            "trend-agent evaluate pipeline (synthetic held-out evaluation)",
        ),
    ]


def build_scorecard(
    container: AppContainer, snapshot: SystemSnapshot, *, root: Path | None = None
) -> Scorecard:
    root = root or Path.cwd()
    now = container.clock()
    items = _build_items(_evidence(root))
    s = container.settings
    probs = [p for p in production.check(s) if p.severity == "error"]
    items.append(
        Item(
            "production_configuration",
            "security",
            "runtime",
            ItemStatus.PASS if not probs else ItemStatus.FAIL,
            "no production configuration errors"
            if not probs
            else "; ".join(f"{p.code}" for p in probs),
            "set the production secrets listed by `trend-agent system audit`",
        )
    )
    db = health.database(container.engine)
    items.append(
        Item(
            "database_schema",
            "operations",
            "runtime",
            ItemStatus.PASS if db.ok else ItemStatus.FAIL,
            db.detail,
            "trend-agent db upgrade",
        )
    )
    w = health.workers(container.session_factory, now)
    live = w.data.get("live", {})
    items.append(
        Item(
            "scheduler_running",
            "operations",
            "runtime",
            ItemStatus.PASS if live.get("scheduler") else ItemStatus.FAIL,
            f"live schedulers: {live.get('scheduler', 0)}",
            "run exactly one `trend-agent system scheduler` (or more as hot standby)",
        )
    )
    items.append(
        Item(
            "workers_running",
            "operations",
            "runtime",
            ItemStatus.PASS if live.get("worker") else ItemStatus.FAIL,
            f"live workers: {live.get('worker', 0)}",
            "run `trend-agent system worker`",
        )
    )
    with transaction(container.session_factory) as ses:
        drill = SystemStateRepository(ses).get(RESTORE_DRILL_KEY)
        labels = LabelRepository(ses).counts()
        exps = ExperimentRepository(ses).list(limit=500)
    drill_ok = False
    drill_txt = "no restore drill recorded"
    if drill:
        at = datetime.fromisoformat(drill["at"])
        drill_ok = bool(drill.get("ok")) and now - at <= RESTORE_DRILL_MAX_AGE
        drill_txt = f"last drill {drill['at']}: {'ok' if drill.get('ok') else 'FAILED'}"
        if drill.get("ok") and not drill_ok:
            drill_txt += " (older than 30 days)"
    items.append(
        Item(
            "backup_restore_drill",
            "operations",
            "runtime",
            ItemStatus.PASS if drill_ok else ItemStatus.FAIL,
            drill_txt,
            "scripts/backup.sh then `trend-agent system restore-drill`",
        )
    )
    external = any(getattr(s, a) for a in ("smtp_host", "telegram_bot_token")) and bool(
        s.alert_email_to or s.telegram_chat_id
    )
    items.append(
        Item(
            "operational_alert_channel",
            "operations",
            "runtime",
            ItemStatus.PASS if external else ItemStatus.FAIL,
            "external alert channel configured"
            if external
            else "alerts reach the logs only (no SMTP / Telegram configured)",
            "configure SMTP or Telegram so ops alerts reach a person",
        )
    )
    b = snapshot.budgets
    budget_ok = b is not None and not any(ln.state.value == "EXHAUSTED" for ln in b.lines)
    items.append(
        Item(
            "budgets",
            "cost",
            "runtime",
            ItemStatus.PASS if budget_ok else ItemStatus.FAIL,
            "budgets configured and not exhausted"
            if budget_ok
            else "a budget is exhausted or unreadable",
            "see `trend-agent system costs`",
        )
    )
    # ---- data sources (external)
    rd = snapshot.readiness

    def best(classes: frozenset[SignalClass]) -> tuple[ItemStatus, str]:
        cand = [r for r in rd if set(r.signal_classes) & classes and not r.synthetic]
        if any(r.level is ReadinessLevel.READY for r in cand):
            return ItemStatus.PASS, "live-verified: " + ", ".join(
                r.name for r in cand if r.level is ReadinessLevel.READY
            )
        if any(r.level is ReadinessLevel.DEGRADED for r in cand):
            return ItemStatus.FAIL, "latest live check failed: " + ", ".join(
                r.name for r in cand if r.level is ReadinessLevel.DEGRADED
            )
        if any(r.level is ReadinessLevel.CONFIGURED_UNVERIFIED for r in cand):
            return ItemStatus.FAIL, "configured but never verified live: " + ", ".join(
                r.name for r in cand if r.level is ReadinessLevel.CONFIGURED_UNVERIFIED
            )
        return ItemStatus.BLOCKED, "no provider configured or access granted: " + (
            ", ".join(f"{r.name} ({r.level.value})" for r in cand) or "none exists"
        )

    st, ev = best(frozenset({SignalClass.DIRECT_YOUTUBE_SEARCH_INTEREST}))
    if st is ItemStatus.BLOCKED:
        ev = (
            "no legitimate DIRECT_YOUTUBE_SEARCH_INTEREST provider exists in this deployment; "
            "FULL_SEARCH_INTELLIGENCE is unavailable"
        )
    items.append(
        Item(
            "direct_search_provider",
            "data",
            "external",
            st,
            ev,
            "obtain a legitimate direct YouTube Search-interest source",
        )
    )
    st, ev = best(frozenset({SignalClass.EXTERNAL_SEARCH_INTEREST}))
    items.append(
        Item(
            "external_search_provider",
            "data",
            "external",
            st,
            ev,
            "Google Trends API alpha admission and a verified transport",
        )
    )
    st, ev = best(PROXY_CLASSES)
    if st is ItemStatus.FAIL and "never verified" in ev:
        ev += " (run `trend-agent system smoke-test`)"
    items.append(
        Item(
            "proxy_provider_live",
            "data",
            "external",
            st,
            ev,
            "configure TREND_AGENT_YOUTUBE_API_KEY and run a smoke test",
        )
    )
    from trend_agent.ops.system import STALE_DATA_AFTER, latest_real_observation

    last = latest_real_observation(container.session_factory)
    usable_real = any(r.usable_for_real_data for r in rd)
    if last is not None and now - last <= STALE_DATA_AFTER:
        items.append(
            Item(
                "real_data_flowing",
                "data",
                "runtime",
                ItemStatus.PASS,
                f"last real observation {last.isoformat()}",
            )
        )
    else:
        items.append(
            Item(
                "real_data_flowing",
                "data",
                "runtime",
                ItemStatus.FAIL if usable_real else ItemStatus.BLOCKED,
                "no real observation in 48 h"
                + (f" (last {last.isoformat()})" if last else " (none ever collected)"),
                "run the scheduler and a worker with a configured real provider",
            )
        )
    # ---- real-world accuracy (external: needs labelled reality)
    real = labels.get("real", {})
    for key, kind in (
        ("detection_real_accuracy", "surge_event"),
        ("content_gap_real_accuracy", "content_gap"),
        ("forecast_real_accuracy", "forecast_outcome"),
    ):
        n, need = real.get(kind, 0), MIN_REAL_LABELS[kind]
        adopted = [
            e
            for e in exps
            if e.data_origin == "real"
            and e.decision == "adopted"
            and e.component
            == {
                "surge_event": "detection",
                "content_gap": "content_gaps",
                "forecast_outcome": "forecasting",
            }[kind]
        ]
        if n >= need and adopted:
            items.append(
                Item(
                    key,
                    "accuracy",
                    "external",
                    ItemStatus.PASS,
                    f"{n} real labels; experiment {adopted[0].id}",
                )
            )
        else:
            items.append(
                Item(
                    key,
                    "accuracy",
                    "external",
                    ItemStatus.BLOCKED,
                    f"{n} real '{kind}' labels (minimum {need}): real-world accuracy is UNKNOWN",
                    "collect and label real outcomes (docs/end_to_end_evaluation.md)",
                )
            )
    outcomes = real.get("recommendation_outcome", 0)
    items.append(
        Item(
            "recommendation_outcomes",
            "accuracy",
            "external",
            ItemStatus.NOT_APPLICABLE if outcomes == 0 else ItemStatus.PASS,
            "measured after launch from creator-entered outcomes; no causal claim "
            f"is made ({outcomes} recorded)",
        )
    )
    # ---- compliance and deployment (external)
    review = None
    with transaction(container.session_factory) as ses:
        review = SystemStateRepository(ses).get("compliance_review")
    items.append(
        Item(
            "youtube_api_compliance_review",
            "compliance",
            "external",
            ItemStatus.PASS if review and review.get("approved") else ItemStatus.BLOCKED,
            f"recorded review: {review}"
            if review
            else "requires a human review of the YouTube API Services policies (not verifiable by "
            "automated tests); see docs/api_compliance_audit.md",
            "complete the review checklist and record it",
        )
    )
    deployed = None
    with transaction(container.session_factory) as ses:
        deployed = SystemStateRepository(ses).get("production_deployment")
    items.append(
        Item(
            "production_deployment",
            "deployment",
            "external",
            ItemStatus.PASS if deployed else ItemStatus.BLOCKED,
            f"recorded: {deployed}"
            if deployed
            else "no deployment target or credentials were available; artifacts are prepared, "
            "nothing was deployed",
            "deploy with docs/production_deployment.md",
        )
    )
    state, reasons = release_state(items)
    return Scorecard(now, state, tuple(items), tuple(reasons))
