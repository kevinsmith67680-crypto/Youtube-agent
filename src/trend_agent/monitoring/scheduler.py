"""Cadence-aware scheduling rules and the long-running scheduler process.

The scheduler never polls faster than a source can produce data. For a daily source with
a 36-hour publication delay, the next useful check for a target holding data up to day D
is ``end(D+1) + 36h``; checking earlier can only return "no new data". After a check that
found nothing new, the next check waits the tier's ``recheck_after_no_data`` (never less
than the provider's ``min_poll_interval``). Low tiers wait for several periods and fetch
them in one request.

Tiers come from discovery priority, novelty and time sensitivity - never from search
growth (Phase 4 does not exist yet).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta

from trend_agent.logging import get_logger, log_event
from trend_agent.monitoring.config import MonitoringConfig, TierRule
from trend_agent.schemas.enums import TimeGranularity
from trend_agent.schemas.monitoring import MonitoringTier
from trend_agent.schemas.scheduling import DelayEstimate, NextCheck, ProviderProfile

_log = get_logger(__name__)

_STEPS: dict[TimeGranularity, timedelta] = {
    TimeGranularity.HOUR: timedelta(hours=1),
    TimeGranularity.DAY: timedelta(days=1),
    TimeGranularity.WEEK: timedelta(days=7),
}


def period_step(granularity: TimeGranularity) -> timedelta | None:
    """Fixed-length step; None for calendar months/years (variable length)."""
    return _STEPS.get(granularity)


def period_floor(moment: datetime, granularity: TimeGranularity) -> datetime:
    moment = moment.astimezone(UTC)
    if granularity is TimeGranularity.HOUR:
        return moment.replace(minute=0, second=0, microsecond=0)
    day = datetime.combine(moment.date(), time.min, tzinfo=UTC)
    if granularity is TimeGranularity.WEEK:
        return day - timedelta(days=day.weekday())  # ISO weeks start Monday
    if granularity is TimeGranularity.MONTH:
        return day.replace(day=1)
    if granularity is TimeGranularity.YEAR:
        return day.replace(month=1, day=1)
    return day


def next_check(
    *,
    now: datetime,
    profile: ProviderProfile,
    tier_rule: TierRule,
    delay: DelayEstimate,
    latest_period_end: datetime | None,
    last_attempt_at: datetime | None,
    last_no_data_at: datetime | None,
    force_at: datetime | None = None,
) -> NextCheck:
    """Is a collection worthwhile now? If not, when is the next useful moment?

    The data-availability rule decides *when*; the provider's minimum polling interval can
    only delay that moment, never bring it forward.
    """
    if force_at is not None and force_at <= now:
        return NextCheck(eligible=True, next_at=now, reason="manual override")
    check = _data_check(
        now=now,
        profile=profile,
        tier_rule=tier_rule,
        delay=delay,
        latest_period_end=latest_period_end,
        last_attempt_at=last_attempt_at,
        last_no_data_at=last_no_data_at,
    )
    min_gap_until = (last_attempt_at + profile.min_poll_interval) if last_attempt_at else None
    if min_gap_until is not None and min_gap_until > now:
        if check.eligible:
            return check.model_copy(
                update={
                    "eligible": False,
                    "next_at": min_gap_until,
                    "reason": "provider minimum polling interval",
                }
            )
        if check.next_at < min_gap_until:
            return check.model_copy(update={"next_at": min_gap_until})
    return check


def _data_check(
    *,
    now: datetime,
    profile: ProviderProfile,
    tier_rule: TierRule,
    delay: DelayEstimate,
    latest_period_end: datetime | None,
    last_attempt_at: datetime | None,
    last_no_data_at: datetime | None,
) -> NextCheck:
    if latest_period_end is None:
        if last_no_data_at is not None and last_attempt_at is not None:
            retry_at = last_attempt_at + max(
                tier_rule.recheck_after_no_data, profile.min_poll_interval
            )
            if now < retry_at:
                return NextCheck(
                    eligible=False,
                    next_at=retry_at,
                    reason="initial collection found no data; rechecking later",
                )
        return NextCheck(eligible=True, next_at=now, reason="initial collection")
    step = period_step(profile.collection_granularity)
    expected_delay = delay.expected
    if profile.publication_cadence is None or step is None or expected_delay is None:
        # Irregular or unknown cadence: fall back to the tier's recheck interval.
        due = (last_attempt_at or now) + max(
            tier_rule.recheck_after_no_data, profile.min_poll_interval
        )
        if last_attempt_at is None or now >= due:
            return NextCheck(eligible=True, next_at=now, reason="irregular source; periodic check")
        return NextCheck(eligible=False, next_at=due, reason="irregular source; waiting")
    cadence_steps = max(1, round(profile.publication_cadence / step))
    wanted_end = latest_period_end + step * cadence_steps * tier_rule.collect_every_periods
    available_at = wanted_end + expected_delay
    if now < available_at:
        return NextCheck(
            eligible=False,
            next_at=available_at,
            reason=f"no new data expected before {available_at.isoformat()}",
            expected_period_end=wanted_end,
            expected_available_at=available_at,
        )
    if (
        last_no_data_at is not None
        and last_attempt_at is not None
        and last_no_data_at >= last_attempt_at
        and last_attempt_at >= available_at
    ):
        recheck = last_attempt_at + max(tier_rule.recheck_after_no_data, profile.min_poll_interval)
        if now < recheck:
            return NextCheck(
                eligible=False,
                next_at=recheck,
                reason="checked after expected availability with no new data; rechecking later",
                expected_period_end=wanted_end,
                expected_available_at=available_at,
            )
    return NextCheck(
        eligible=True,
        next_at=now,
        reason="new period expected to be available",
        expected_period_end=wanted_end,
        expected_available_at=available_at,
    )


def tier_rule(config: MonitoringConfig, tier: MonitoringTier) -> TierRule:
    return config.tiers.rule(tier)


# ---------------------------------------------------------------------------- process


class SchedulerLoop:
    """Runs ``tick`` every ``interval`` until stopped (SIGINT/SIGTERM or ``max_ticks``).

    A deliberately small replacement for APScheduler: one periodic job, no extra
    dependency, injectable sleep for tests. Planner and workers are separate processes
    that only share PostgreSQL, so more workers can be added without changing this.
    """

    def __init__(
        self,
        tick: Callable[[], Awaitable[object]],
        *,
        interval: timedelta,
        name: str,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._tick = tick
        self._interval = interval
        self._name = name
        self._sleep = sleep
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):  # non-Unix
                loop.add_signal_handler(sig, self.stop)

    async def run(self, *, max_ticks: int | None = None) -> int:
        ticks = 0
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:  # keep the process alive; the next tick retries
                log_event(
                    _log,
                    f"{self._name}.tick_failed",
                    logging.ERROR,
                    error_type=type(exc).__name__,
                    error=str(exc)[:300],
                )
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            sleep_task = asyncio.ensure_future(self._sleep(self._interval.total_seconds()))
            stop_task = asyncio.ensure_future(self._stop.wait())
            _done, pending = await asyncio.wait(
                {sleep_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
        return ticks
