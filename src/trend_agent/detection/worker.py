"""Event-driven detection worker (PostgreSQL LISTEN/NOTIFY + transactional outbox).

The Phase 3 executor writes new observations and, in the same transaction, upserts the
series into ``detection_queue`` and issues ``NOTIFY <channel>``. This worker ``LISTEN``s on
that channel and processes the queue as soon as a notification arrives; it also re-checks
the queue every ``fallback_seconds`` so nothing is lost if a notification is missed (e.g.
the worker was restarted: the queue row is durable, the notification is not). Several
workers can run: queue rows are claimed with ``FOR UPDATE SKIP LOCKED`` and leases, and
each series is processed under an advisory lock with optimistic state versions.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from sqlalchemy import Engine

from trend_agent.detection.service import DetectionReport, DetectionService
from trend_agent.logging import get_logger, log_event
from trend_agent.schemas.detection import RunType

_log = get_logger(__name__)


def run_worker(
    service: DetectionService,
    engine: Engine,
    *,
    worker_id: str,
    channel: str,
    fallback_seconds: float,
    once: bool = False,
    max_cycles: int | None = None,
    on_report: Callable[[DetectionReport], None] | None = None,
) -> int:
    """Process the queue now, then on every notification (or fallback timeout)."""
    raw = engine.raw_connection()
    cycles = 0
    try:
        conn = raw.driver_connection
        conn.autocommit = True  # type: ignore[union-attr]
        conn.execute(f"LISTEN {channel}")  # type: ignore[union-attr]  # channel validated by config
        while True:
            started = time.perf_counter()
            report = service.run(run_type=RunType.WORKER, from_queue=True, worker=worker_id)
            cycles += 1
            if on_report is not None:
                on_report(report)
            log_event(
                _log,
                "detect.worker_cycle",
                worker=worker_id,
                series=report.series_evaluated,
                notifications=report.notifications,
                seconds=round(time.perf_counter() - started, 3),
            )
            if once or (max_cycles is not None and cycles >= max_cycles):
                return cycles
            woke = False
            for _ in conn.notifies(timeout=fallback_seconds, stop_after=1):  # type: ignore[union-attr]
                woke = True
            log_event(
                _log,
                "detect.worker_wake",
                logging.DEBUG,
                worker=worker_id,
                reason="notification" if woke else "fallback",
            )
    finally:
        raw.close()
