"""Cost budgets: measured spend against the configured limits.

Spend is read from the records each stage already writes, so nothing is counted twice and
no separate ledger can drift from reality:

* LLM USD  - ``recommendation_generation_runs.estimated_cost_usd`` and
  ``content_analysis_runs.cost->>'llm_usd'`` (both are estimates from token counts and the
  configured per-token prices; the provider's invoice is authoritative).
* API units - ``provider_rate_limits.units_used`` for the quota windows that are open now
  (YouTube quota units; one unit per request for request-counted providers).

Limits come from settings (``TREND_AGENT_DAILY_API_BUDGET`` ...). A budget at or above its
limit is EXHAUSTED; the scheduler then skips the affected stage and the operating mode
becomes DEGRADED. LLM exhaustion makes generators fall back to deterministic templates.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import sqlalchemy as sa

from trend_agent.config import Settings
from trend_agent.db.session import SessionFactory, transaction


class BudgetState(StrEnum):
    OK = "OK"
    WARNING = "WARNING"
    EXHAUSTED = "EXHAUSTED"
    UNLIMITED = "UNLIMITED"  # a limit of 0 means "no spending allowed"; never unlimited


@dataclass(frozen=True)
class BudgetLine:
    name: str
    used: float
    limit: float
    unit: str
    state: BudgetState
    window: str

    @property
    def remaining(self) -> float:
        return max(0.0, self.limit - self.used)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        d["remaining"] = round(self.remaining, 6)
        d["used"] = round(self.used, 6)
        return d


def _state(used: float, limit: float, warn: float) -> BudgetState:
    if limit <= 0 or used >= limit:
        return BudgetState.EXHAUSTED
    if used >= warn * limit:
        return BudgetState.WARNING
    return BudgetState.OK


def day_start(now: datetime) -> datetime:
    n = now.astimezone(UTC)
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


def month_start(now: datetime) -> datetime:
    return day_start(now).replace(day=1)


@dataclass(frozen=True)
class SpendReport:
    at: datetime
    llm_usd_today: float
    llm_usd_month: float
    llm_breakdown_today: dict[str, float]
    api_units_today: float
    api_breakdown_today: dict[str, float]


def measure_spend(factory: SessionFactory, now: datetime) -> SpendReport:
    d0, m0 = day_start(now), month_start(now)
    with transaction(factory) as s:
        rec_day, rec_month = s.execute(
            sa.text(
                "SELECT COALESCE(SUM(estimated_cost_usd) FILTER (WHERE started_at >= :d), 0), "
                "COALESCE(SUM(estimated_cost_usd), 0) "
                "FROM recommendation_generation_runs WHERE started_at >= :m"
            ),
            {"d": d0, "m": m0},
        ).one()
        ca_day, ca_month = s.execute(
            sa.text(
                "SELECT COALESCE(SUM((cost->>'llm_usd')::float) FILTER (WHERE started_at >= :d), "
                "0), COALESCE(SUM((cost->>'llm_usd')::float), 0) "
                "FROM content_analysis_runs WHERE started_at >= :m AND cost ? 'llm_usd'"
            ),
            {"d": d0, "m": m0},
        ).one()
        api_rows = s.execute(
            sa.text(
                "SELECT provider_name, SUM(units_used) FROM provider_rate_limits "
                "WHERE window_start <= :n AND window_end > :n GROUP BY provider_name"
            ),
            {"n": now},
        ).all()
    api = {str(p): float(u or 0) for p, u in api_rows}
    return SpendReport(
        at=now,
        llm_usd_today=float(rec_day) + float(ca_day),
        llm_usd_month=float(rec_month) + float(ca_month),
        llm_breakdown_today={
            "recommendations": round(float(rec_day), 6),
            "content_analysis": round(float(ca_day), 6),
        },
        api_units_today=sum(api.values()),
        api_breakdown_today=api,
    )


@dataclass(frozen=True)
class BudgetReport:
    spend: SpendReport
    lines: tuple[BudgetLine, ...]
    limits: dict[str, Any]

    def line(self, name: str) -> BudgetLine:
        for ln in self.lines:
            if ln.name == name:
                return ln
        raise KeyError(name)

    @property
    def llm_exhausted(self) -> bool:
        return any(
            ln.state is BudgetState.EXHAUSTED
            for ln in self.lines
            if ln.name in ("daily_llm_budget", "monthly_llm_budget")
        )

    @property
    def api_exhausted(self) -> bool:
        return self.line("daily_api_budget").state is BudgetState.EXHAUSTED

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.spend.at.isoformat(),
            "budgets": [ln.as_dict() for ln in self.lines],
            "llm_breakdown_today_usd": self.spend.llm_breakdown_today,
            "api_breakdown_today_units": self.spend.api_breakdown_today,
            "limits": self.limits,
            "notes": [
                "LLM spend is estimated from recorded token counts and configured prices; the "
                "provider invoice is authoritative.",
                "API units count YouTube quota units plus one per request for request-counted "
                "providers, over the quota windows open now.",
            ],
        }


def evaluate(settings: Settings, spend: SpendReport) -> BudgetReport:
    warn = settings.budget_warning_ratio
    lines = (
        BudgetLine(
            "daily_api_budget",
            spend.api_units_today,
            float(settings.daily_api_budget),
            "units",
            _state(spend.api_units_today, settings.daily_api_budget, warn),
            "open quota windows",
        ),
        BudgetLine(
            "daily_llm_budget",
            spend.llm_usd_today,
            settings.daily_llm_budget,
            "USD",
            _state(spend.llm_usd_today, settings.daily_llm_budget, warn),
            "UTC day",
        ),
        BudgetLine(
            "monthly_llm_budget",
            spend.llm_usd_month,
            settings.monthly_llm_budget,
            "USD",
            _state(spend.llm_usd_month, settings.monthly_llm_budget, warn),
            "calendar month (UTC)",
        ),
    )
    return BudgetReport(
        spend=spend,
        lines=lines,
        limits={
            "max_recommendations_per_run": settings.max_recommendations_per_run,
            "max_analysis_requests_per_topic": settings.max_analysis_requests_per_topic,
            "max_retries": settings.max_retries,
            "max_concurrent_workers": settings.max_concurrent_workers,
        },
    )


class BudgetGuard:
    """Checks budgets before spending. Cached briefly so hot paths do not hit the DB."""

    def __init__(
        self,
        settings: Settings,
        factory: SessionFactory,
        *,
        clock: Any,
        cache_seconds: float = 30.0,
    ) -> None:
        self.settings = settings
        self.factory = factory
        self.clock = clock
        self.cache = timedelta(seconds=cache_seconds)
        self._cached: tuple[datetime, BudgetReport] | None = None

    def report(self, *, fresh: bool = False) -> BudgetReport:
        now = self.clock()
        if not fresh and self._cached is not None and now - self._cached[0] < self.cache:
            return self._cached[1]
        rep = evaluate(self.settings, measure_spend(self.factory, now))
        self._cached = (now, rep)
        return rep

    def llm_allowed(self) -> tuple[bool, str]:
        rep = self.report()
        for name in ("daily_llm_budget", "monthly_llm_budget"):
            ln = rep.line(name)
            if ln.state is BudgetState.EXHAUSTED:
                return False, (
                    f"{name} exhausted ({ln.used:.4f}/{ln.limit:.2f} {ln.unit}); used the "
                    "deterministic template generator"
                )
        return True, ""

    def api_allowed(self) -> tuple[bool, str]:
        ln = self.report().line("daily_api_budget")
        if ln.state is BudgetState.EXHAUSTED:
            return False, f"daily_api_budget exhausted ({ln.used:.0f}/{ln.limit:.0f} units)"
        return True, ""

    def topics_over_analysis_limit(self, now: datetime) -> set[int]:
        """Monitoring targets analysed MAX_ANALYSIS_REQUESTS_PER_TOPIC times in 24 h."""
        with transaction(self.factory) as s:
            rows = s.execute(
                sa.text(
                    "SELECT target_id FROM content_analysis_runs WHERE target_id IS NOT NULL "
                    "AND started_at >= :since GROUP BY target_id HAVING count(*) >= :n"
                ),
                {
                    "since": now - timedelta(hours=24),
                    "n": self.settings.max_analysis_requests_per_topic,
                },
            ).all()
        return {int(r[0]) for r in rows}
