"""Prometheus text-format metrics without a client-library dependency.

In-process instruments (request counts, latency histograms) live in :data:`REGISTRY`.
Database-derived gauges (queue depth, open alerts, dead letters, heartbeat ages, budget
use, operating mode) are computed at scrape time by :func:`database_gauges`, so a scrape
reflects the shared state of every process, not just the one answering.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import sqlalchemy as sa

from trend_agent.db.session import SessionFactory, transaction

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _labels(names: Sequence[str], values: Sequence[str]) -> str:
    if not names:
        return ""
    esc = [v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") for v in values]
    return "{" + ",".join(f'{n}="{v}"' for n, v in zip(names, esc, strict=True)) + "}"


@dataclass
class Counter:
    name: str
    help: str
    labelnames: tuple[str, ...] = ()
    _values: dict[tuple[str, ...], float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def inc(self, *labels: str, amount: float = 1.0) -> None:
        with self._lock:
            self._values[labels] = self._values.get(labels, 0.0) + amount

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} counter"
        with self._lock:
            for labels, v in sorted(self._values.items()):
                yield f"{self.name}{_labels(self.labelnames, labels)} {v}"


@dataclass
class Histogram:
    name: str
    help: str
    labelnames: tuple[str, ...] = ()
    buckets: tuple[float, ...] = LATENCY_BUCKETS
    _data: dict[tuple[str, ...], list[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def observe(self, value: float, *labels: str) -> None:
        with self._lock:
            d = self._data.setdefault(labels, [0.0] * (len(self.buckets) + 2))
            for i, b in enumerate(self.buckets):
                if value <= b:
                    d[i] += 1
            d[-2] += 1  # count
            d[-1] += value  # sum

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} histogram"
        with self._lock:
            for labels, d in sorted(self._data.items()):
                names = (*self.labelnames, "le")
                for i, b in enumerate(self.buckets):
                    yield f"{self.name}_bucket{_labels(names, (*labels, repr(b)))} {d[i]}"
                yield f"{self.name}_bucket{_labels(names, (*labels, '+Inf'))} {d[-2]}"
                yield f"{self.name}_count{_labels(self.labelnames, labels)} {d[-2]}"
                yield f"{self.name}_sum{_labels(self.labelnames, labels)} {d[-1]}"


class Registry:
    def __init__(self) -> None:
        self.http_requests = Counter(
            "trend_agent_http_requests_total", "API requests", ("method", "route", "status")
        )
        self.http_latency = Histogram(
            "trend_agent_http_request_seconds", "API request latency", ("method", "route")
        )

    def render(self) -> list[str]:
        return [*self.http_requests.render(), *self.http_latency.render()]


REGISTRY = Registry()


def _gauge(name: str, help_: str, rows: Iterable[tuple[dict[str, str], float]]) -> list[str]:
    out = [f"# HELP {name} {help_}", f"# TYPE {name} gauge"]
    for labels, value in rows:
        out.append(f"{name}{_labels(tuple(labels), tuple(labels.values()))} {value}")
    return out


def database_gauges(factory: SessionFactory, now: datetime, mode: Any | None) -> list[str]:
    lines: list[str] = []
    with transaction(factory) as s:
        q = s.execute(sa.text("SELECT status, count(*) FROM collection_jobs GROUP BY status")).all()
        lines += _gauge(
            "trend_agent_collection_jobs", "Collection jobs by status",
            (({"status": str(st)}, float(n)) for st, n in q),
        )  # fmt: skip
        dq = s.execute(sa.text("SELECT count(*) FROM detection_queue")).scalar_one()
        lines += _gauge("trend_agent_detection_queue", "Series waiting for detection",
                        [({}, float(dq))])  # fmt: skip
        alerts = s.execute(
            sa.text("SELECT severity, count(*) FROM ops_alerts WHERE status='open' GROUP BY 1")
        ).all()
        lines += _gauge(
            "trend_agent_ops_alerts_open", "Open operational alerts",
            (({"severity": str(sv)}, float(n)) for sv, n in alerts),
        )  # fmt: skip
        dl = s.execute(
            sa.text(
                "SELECT source, count(*) FROM dead_letters WHERE resolved_at IS NULL GROUP BY 1"
            )
        ).all()
        lines += _gauge(
            "trend_agent_dead_letters_open", "Unresolved dead letters",
            (({"source": str(src)}, float(n)) for src, n in dl),
        )  # fmt: skip
        hb = s.execute(
            sa.text("SELECT service_id, role, last_seen_at FROM service_heartbeats "
                    "WHERE status = 'running'")
        ).all()  # fmt: skip
        lines += _gauge(
            "trend_agent_heartbeat_age_seconds", "Seconds since a running service's last beat",
            (({"service": str(sid), "role": str(role)}, round((now - ts).total_seconds(), 1))
             for sid, role, ts in hb),
        )  # fmt: skip
        runs = s.execute(
            sa.text(
                "SELECT DISTINCT ON (task) task, status, extract(epoch FROM started_at) "
                "FROM scheduled_runs WHERE status <> 'running' ORDER BY task, started_at DESC"
            )
        ).all()
        lines += _gauge(
            "trend_agent_stage_last_run_timestamp", "Start time of each stage's last run",
            (({"task": str(t), "status": str(st)}, float(ts)) for t, st, ts in runs),
        )  # fmt: skip
    if mode is not None:
        lines += _gauge(
            "trend_agent_operating_mode", "1 for the current operating mode",
            [({"mode": mode.mode.value, "base_mode": mode.base_mode.value}, 1.0)],
        )  # fmt: skip
    return lines


def budget_gauges(report: Any | None) -> list[str]:
    if report is None:
        return []
    rows = []
    for ln in report.lines:
        rows.append(({"budget": ln.name, "unit": ln.unit, "kind": "used"}, round(ln.used, 6)))
        rows.append(({"budget": ln.name, "unit": ln.unit, "kind": "limit"}, ln.limit))
    return _gauge("trend_agent_budget", "Budget use and limits", rows)
