"""Health checks backing ``/health/*`` and ``trend-agent system health``.

Every check performs the real operation it reports on (a query, a heartbeat read); none
returns a constant. Status values: ``ok``, ``degraded`` (working with reduced capability)
and ``fail`` (cannot serve its purpose).

* live       the process answers (no I/O): restart only if this fails.
* database   connect, ``SELECT 1`` latency, schema revision == the code's expected head.
* ready      can this API instance serve requests now (database ok, or demo mode).
* workers    scheduler and worker heartbeats are fresh; one scheduler lease holder.
* providers  provider readiness and the operating mode derived from it.
"""

from __future__ import annotations

import os
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine

from trend_agent import __version__
from trend_agent.db import EXPECTED_SCHEMA_REVISION
from trend_agent.db.repositories.ops import HeartbeatRepository, LeaseRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.logging import get_logger, log_event, redact_text

_log = get_logger(__name__)

OK, DEGRADED, FAIL = "ok", "degraded", "fail"
SCHEDULER_LEASE = "pipeline"


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)
    latency_ms: float | None = None

    @property
    def ok(self) -> bool:
        return self.status == OK

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"check": self.name, "status": self.status, "detail": self.detail}
        if self.latency_ms is not None:
            out["latency_ms"] = round(self.latency_ms, 2)
        out.update(self.data)
        return out


def live() -> CheckResult:
    return CheckResult(
        "live", OK, "process is running", {"version": __version__, "pid": os.getpid()}
    )


def database(engine: Engine) -> CheckResult:
    started = time.perf_counter()
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
            revision = None
            if conn.execute(
                sa.text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
            ).scalar_one():
                revision = conn.execute(
                    sa.text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
    except Exception as exc:
        detail = redact_text(
            f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
        )
        log_event(_log, "health.database_unreachable", error=detail)
        return CheckResult(
            "database", FAIL, f"database unreachable: {detail}",
            latency_ms=(time.perf_counter() - started) * 1000,
        )  # fmt: skip
    latency = (time.perf_counter() - started) * 1000
    data = {"schema_revision": revision, "expected_revision": EXPECTED_SCHEMA_REVISION}
    if revision != EXPECTED_SCHEMA_REVISION:
        return CheckResult(
            "database",
            FAIL,
            f"schema revision {revision or 'none'} != expected {EXPECTED_SCHEMA_REVISION}; "
            "run the migration step (trend-agent db upgrade)",
            data,
            latency,
        )
    return CheckResult("database", OK, "connected; schema current", data, latency)


def ready(engine: Engine | None, *, demo: bool) -> CheckResult:
    if demo:
        return CheckResult(
            "ready", OK, "demo mode: serving the isolated synthetic dataset (no database)",
            {"demo": True},
        )  # fmt: skip
    assert engine is not None
    db = database(engine)
    if not db.ok:
        return CheckResult("ready", FAIL, db.detail, {"database": db.as_dict()})
    return CheckResult("ready", OK, "database reachable and schema current", {"demo": False})


def workers(factory: SessionFactory, now: datetime) -> CheckResult:
    try:
        with transaction(factory) as s:
            beats = HeartbeatRepository(s).all()
            lease = LeaseRepository(s).get(SCHEDULER_LEASE)
            db_now = LeaseRepository(s).db_now()
    except Exception as exc:
        return CheckResult("workers", FAIL, redact_text(f"cannot read heartbeats: {exc}"))
    services = []
    live_by_role: dict[str, int] = {}
    for b in beats:
        age = (now - b.last_seen_at).total_seconds()
        fresh = b.status == "running" and age <= 3 * b.interval_seconds
        if fresh:
            live_by_role[b.role] = live_by_role.get(b.role, 0) + 1
        services.append(
            {
                "service_id": b.service_id,
                "role": b.role,
                "host": b.hostname,
                "status": b.status if fresh or b.status != "running" else "stale",
                "last_seen_seconds_ago": round(age, 1),
                "version": b.version,
                "details": b.details,
            }
        )
    lease_info: dict[str, Any] | None = None
    lease_live = False
    if lease is not None:
        expires_in = (lease.expires_at - db_now).total_seconds()
        lease_live = expires_in >= 0
        lease_info = {
            "owner": lease.owner,
            "fencing_token": lease.fencing_token,
            "expires_in_seconds": round(expires_in, 1),
        }
    problems = []
    if not live_by_role.get("scheduler"):
        problems.append("no live scheduler (scheduled stages are not running)")
    if not live_by_role.get("worker"):
        problems.append("no live worker (collection jobs and detection are not processed)")
    if not lease_live:
        problems.append("no scheduler holds the pipeline lease")
    status = OK if not problems else (DEGRADED if live_by_role else FAIL)
    return CheckResult(
        "workers",
        status,
        "; ".join(problems) or "scheduler and workers are live",
        {"live": live_by_role, "services": services, "lease": lease_info},
    )


# ---------------------------------------------------------------------- heartbeats
class Heartbeat:
    """Writes this process's heartbeat; call :meth:`beat` from the main loop."""

    def __init__(
        self,
        factory: SessionFactory,
        *,
        role: str,
        interval: timedelta,
        clock: Callable[[], datetime],
        service_id: str | None = None,
    ) -> None:
        self.factory = factory
        self.role = role
        self.interval = interval
        self.clock = clock
        self.hostname = socket.gethostname()
        self.pid = os.getpid()
        self.service_id = service_id or f"{role}:{self.hostname}:{self.pid}:{uuid.uuid4().hex[:6]}"
        self.started_at = clock()
        self.failures = 0

    def beat(self, status: str = "running", details: dict[str, Any] | None = None) -> bool:
        """Record a beat; never raises (a heartbeat failure must not stop the work)."""
        try:
            with transaction(self.factory) as s:
                HeartbeatRepository(s).beat(
                    service_id=self.service_id,
                    role=self.role,
                    hostname=self.hostname,
                    pid=self.pid,
                    version=__version__,
                    started_at=self.started_at,
                    now=self.clock(),
                    interval_seconds=self.interval.total_seconds(),
                    status=status,
                    details=details or {},
                )
            self.failures = 0
            return True
        except Exception as exc:
            self.failures += 1
            log_event(
                _log, "heartbeat.failed", service=self.service_id, error=redact_text(str(exc))
            )
            return False

    def stop(self) -> None:
        self.beat("stopped")
