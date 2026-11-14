"""Operational alerts with fingerprint deduplication and a notification cooldown.

An alert is identified by a fingerprint (``kind:key``). Each occurrence is recorded (count,
last seen), but a notification is sent only when the alert is first seen, when it reopens
after being resolved, or once per cooldown while it keeps recurring - so a repeating error
produces one message per cooldown, not one per occurrence. Conditions that clear call
:meth:`OpsAlerter.resolve`.

Notifications always go to the structured log; external channels (email / Telegram, the
Phase 7 channels) are used only when configured. Delivery failures are logged and never
raised: alerting must not take the scheduler down.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from trend_agent.db.repositories.ops import OpsAlertRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.logging import get_logger, log_event, redact_text
from trend_agent.recommendations.notifications import AlertEvent, Channel

_log = get_logger("trend_agent.ops.alerts")
_LEVEL = {"info": logging.INFO, "warning": logging.WARNING, "critical": logging.CRITICAL}


@dataclass(frozen=True)
class RaisedAlert:
    fingerprint: str
    notified: bool
    new: bool
    occurrences: int
    channels: tuple[str, ...]


class OpsAlerter:
    def __init__(
        self,
        factory: SessionFactory,
        *,
        clock: Callable[[], datetime],
        cooldown: timedelta,
        channels: Sequence[Channel] = (),
    ) -> None:
        self.factory = factory
        self.clock = clock
        self.cooldown = cooldown
        self.channels = [c for c in channels if c.external and c.configured]

    def raise_alert(
        self,
        kind: str,
        key: str,
        *,
        severity: str,
        title: str,
        detail: str,
        details: dict[str, Any] | None = None,
    ) -> RaisedAlert:
        fingerprint = f"{kind}:{key}"[:200]
        now = self.clock()
        with transaction(self.factory) as s:
            d = OpsAlertRepository(s).raise_alert(
                fingerprint=fingerprint,
                kind=kind,
                severity=severity,
                title=title,
                detail=detail,
                now=now,
                cooldown=self.cooldown,
                details=details,
            )
        sent: list[str] = []
        if d.notify:
            log_event(
                _log,
                "ops.alert",
                _LEVEL.get(severity, logging.WARNING),
                fingerprint=fingerprint,
                severity=severity,
                title=title,
                detail=redact_text(detail),
                occurrences=d.occurrences,
            )
            event = AlertEvent(
                dedup_key=f"ops:{fingerprint}:{now.isoformat()}",
                subject_key=f"ops:{fingerprint}",
                event_type="ops_alert",
                severity=severity,
                title=f"[{severity.upper()}] {title}",
                body=f"{redact_text(detail)}\n\noccurrences: {d.occurrences}",
            )
            for ch in self.channels:
                try:
                    ch.send(event)
                    sent.append(ch.name)
                except Exception as exc:  # never let a channel failure stop the caller
                    log_event(
                        _log, "ops.alert_delivery_failed", logging.WARNING,
                        channel=ch.name, error=redact_text(str(exc)),
                    )  # fmt: skip
        return RaisedAlert(fingerprint, d.notify, d.new, d.occurrences, tuple(sent))

    def resolve(self, kind: str, key: str) -> bool:
        fingerprint = f"{kind}:{key}"[:200]
        with transaction(self.factory) as s:
            done = OpsAlertRepository(s).resolve(fingerprint, self.clock())
        if done:
            log_event(_log, "ops.alert_resolved", fingerprint=fingerprint)
        return done

    def resolve_missing(self, kind: str, active_keys: set[str]) -> int:
        """Resolve open alerts of ``kind`` whose condition is no longer present."""
        with transaction(self.factory) as s:
            open_fps = OpsAlertRepository(s).open_fingerprints(f"{kind}:")
        n = 0
        for fp in open_fps:
            if fp.split(":", 1)[1] not in active_keys:
                n += self.resolve(kind, fp.split(":", 1)[1])
        return n
