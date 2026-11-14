"""Alerts: meaningful events only, deduplicated, rate-limited, delivery-tracked.

Events: a new high-quality opportunity, a significant upgrade of an opportunity's score, a
strong new recommendation, a recommendation becoming time-sensitive, and a verification
status change (evidence change). Each has a deduplication key (the same event is never
alerted twice) and a cooldown per subject. Message bodies are built from structured,
already-verified fields; no generated claim is sent as a confirmed fact.

Channels: in-app (always available), email (SMTP) and Telegram, both optional. A channel
without credentials reports ``unconfigured``; nothing is assumed to exist. Temporary
delivery failures are retried up to the configured number of attempts. Synthetic data is
alerted in-app only (labelled) and never sent to external channels unless explicitly
configured for testing.
"""

from __future__ import annotations

import smtplib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any, Protocol

import httpx

from trend_agent.recommendations.config import LoadedRecommendationConfig
from trend_agent.schemas.content_opportunity import OpportunityReport
from trend_agent.schemas.dashboard import AlertView, ProviderView
from trend_agent.schemas.recommendation import (
    CreatorProfile,
    PublishingUrgency,
    RecommendationOutput,
    RecommendationStatus,
)

_OK = (RecommendationStatus.APPROVED, RecommendationStatus.APPROVED_WITH_UNCERTAINTY)


@dataclass(frozen=True)
class AlertEvent:
    dedup_key: str
    subject_key: str  # cooldown scope
    event_type: str
    severity: str
    title: str
    body: str
    recommendation_id: str | None = None
    opportunity_id: str | None = None
    synthetic: bool = False


@dataclass
class StoredAlert:
    id: str
    event: AlertEvent
    created_at: datetime
    read: bool = False
    deliveries: dict[str, dict[str, Any]] = field(default_factory=dict)

    def view(self) -> AlertView:
        e = self.event
        return AlertView(
            id=self.id,
            event_type=e.event_type,
            severity=e.severity,
            title=e.title,
            body=e.body,
            created_at=self.created_at,
            read=self.read,
            recommendation_id=e.recommendation_id,
            opportunity_id=e.opportunity_id,
            synthetic=e.synthetic,
            deliveries=[{"channel": k, **v} for k, v in self.deliveries.items()],
        )


class AlertStore(Protocol):
    def exists(self, dedup_key: str) -> bool: ...
    def last_for_subject(self, subject_key: str) -> datetime | None: ...
    def add(self, alert: StoredAlert) -> None: ...
    def update_delivery(self, alert_id: str, channel: str, status: dict[str, Any]) -> None: ...


class MemoryAlertStore:
    def __init__(self) -> None:
        self.alerts: list[StoredAlert] = []

    def exists(self, dedup_key: str) -> bool:
        return any(a.event.dedup_key == dedup_key for a in self.alerts)

    def last_for_subject(self, subject_key: str) -> datetime | None:
        times = [a.created_at for a in self.alerts if a.event.subject_key == subject_key]
        return max(times) if times else None

    def add(self, alert: StoredAlert) -> None:
        self.alerts.append(alert)

    def update_delivery(self, alert_id: str, channel: str, status: dict[str, Any]) -> None:
        for a in self.alerts:
            if a.id == alert_id:
                a.deliveries[channel] = status


# ------------------------------------------------------------------- channels
class DeliveryError(Exception):
    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


class Channel(Protocol):
    name: str
    configured: bool
    external: bool

    def send(self, event: AlertEvent) -> None: ...
    def status(self) -> ProviderView: ...


class InAppChannel:
    name = "in_app"
    configured = True
    external = False

    def send(self, event: AlertEvent) -> None:  # stored by the dispatcher
        return None

    def status(self) -> ProviderView:
        return ProviderView(
            name="in_app",
            kind="notification",
            status="ok",
            configured=True,
            detail="alerts are stored and shown in the dashboard",
        )


class EmailChannel:
    name = "email"
    external = True

    def __init__(
        self,
        *,
        host: str | None,
        port: int,
        username: str | None,
        password: str | None,
        sender: str | None,
        recipient: str | None,
        timeout: float = 10.0,
    ) -> None:
        self.host, self.port, self.username, self.password = host, port, username, password
        self.sender, self.recipient, self.timeout = sender, recipient, timeout
        self.configured = bool(host and sender and recipient)

    def send(self, event: AlertEvent) -> None:
        if not self.configured:
            raise DeliveryError("email is not configured", transient=False)
        msg = EmailMessage()
        msg["Subject"] = event.title[:150]
        msg["From"], msg["To"] = self.sender or "", self.recipient or ""
        msg.set_content(event.body)
        try:
            with smtplib.SMTP(self.host or "", self.port, timeout=self.timeout) as smtp:
                smtp.starttls()
                if self.username and self.password:
                    smtp.login(self.username, self.password)
                smtp.send_message(msg)
        except (
            smtplib.SMTPServerDisconnected,
            smtplib.SMTPConnectError,
            TimeoutError,
            OSError,
        ) as exc:
            raise DeliveryError(type(exc).__name__, transient=True) from None
        except smtplib.SMTPException as exc:
            raise DeliveryError(type(exc).__name__, transient=False) from None

    def status(self) -> ProviderView:
        return ProviderView(
            name="email",
            kind="notification",
            status="ok" if self.configured else "unconfigured",
            configured=self.configured,
            detail="SMTP configured"
            if self.configured
            else "set TREND_AGENT_SMTP_HOST, TREND_AGENT_ALERT_EMAIL_FROM and _TO to enable",
        )


class TelegramChannel:
    name = "telegram"
    external = True

    def __init__(
        self, *, token: str | None, chat_id: str | None, client: httpx.Client | None = None
    ) -> None:
        self.token, self.chat_id = token, chat_id
        self.configured = bool(token and chat_id)
        self._client = client

    def send(self, event: AlertEvent) -> None:
        if not self.configured:
            raise DeliveryError("telegram is not configured", transient=False)
        client = self._client or httpx.Client(timeout=10.0)
        try:
            resp = client.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": f"{event.title}\n\n{event.body}"[:4000],
                    "disable_web_page_preview": True,
                },
            )
        except httpx.HTTPError as exc:
            raise DeliveryError(type(exc).__name__, transient=True) from None
        finally:
            if self._client is None:
                client.close()
        if resp.status_code >= 500 or resp.status_code == 429:
            raise DeliveryError(f"HTTP {resp.status_code}", transient=True)
        if resp.status_code >= 400:
            raise DeliveryError(f"HTTP {resp.status_code}", transient=False)

    def status(self) -> ProviderView:
        return ProviderView(
            name="telegram",
            kind="notification",
            status="ok" if self.configured else "unconfigured",
            configured=self.configured,
            detail="bot token and chat configured"
            if self.configured
            else "set TREND_AGENT_TELEGRAM_BOT_TOKEN and TREND_AGENT_TELEGRAM_CHAT_ID to enable",
        )


# --------------------------------------------------------------------- events
def detect_events(
    recs: Sequence[RecommendationOutput],
    reports: Sequence[OpportunityReport],
    loaded: LoadedRecommendationConfig,
    *,
    previous_scores: dict[str, float] | None = None,
    previous_status: dict[str, str] | None = None,
) -> list[AlertEvent]:
    cfg = loaded.config.alerts
    prev = previous_scores or {}
    prev_status = previous_status or {}
    out: list[AlertEvent] = []
    for rep in reports:
        s = rep.score.score
        if rep.status.value != "candidate" or s is None:
            continue
        tag = "[SYNTHETIC] " if rep.synthetic else ""
        if s >= cfg.min_opportunity_score and rep.opportunity_id not in prev:
            out.append(
                AlertEvent(
                    f"opp:{rep.opportunity_id}:new",
                    f"opp:{rep.opportunity_id}",
                    "NEW_OPPORTUNITY",
                    "info",
                    f"{tag}New research opportunity: {rep.canonical_query}"[:200],
                    f"Opportunity score {s:.0f} (a research-priority index, not a probability). "
                    f"Demand basis: {rep.demand.basis}. Gap: "
                    f"{rep.primary_gap.gap_type.value if rep.primary_gap else 'none'}.",
                    opportunity_id=rep.opportunity_id,
                    synthetic=rep.synthetic,
                )
            )
        elif rep.opportunity_id in prev and s - prev[rep.opportunity_id] >= cfg.score_upgrade_delta:
            out.append(
                AlertEvent(
                    f"opp:{rep.opportunity_id}:upgrade:{round(s)}",
                    f"opp:{rep.opportunity_id}",
                    "OPPORTUNITY_UPGRADE",
                    "info",
                    f"{tag}Opportunity strengthened: {rep.canonical_query}"[:200],
                    f"Opportunity score {prev[rep.opportunity_id]:.0f} -> {s:.0f} after new "
                    "evidence.",
                    opportunity_id=rep.opportunity_id,
                    synthetic=rep.synthetic,
                )
            )
    for r in recs:
        if r.suppression.suppressed:
            continue
        tag = "[SYNTHETIC] " if r.synthetic else ""
        st = r.verification.status
        old = prev_status.get(r.recommendation_id)
        if old is not None and old != st.value:
            out.append(
                AlertEvent(
                    f"rec:{r.recommendation_id}:status:{st.value}",
                    f"rec:{r.recommendation_id}",
                    "EVIDENCE_CHANGE",
                    "warning" if st not in _OK else "info",
                    f"{tag}Verification changed: {r.title}"[:200],
                    f"Verification status {old} -> {st.value}. Review the evidence before "
                    "producing.",
                    recommendation_id=r.recommendation_id,
                    opportunity_id=r.opportunity_id,
                    synthetic=r.synthetic,
                )
            )
        score = r.ranking.composite_score
        if (
            st in _OK
            and r.ranking.eligible
            and score is not None
            and (score >= cfg.min_score_new_recommendation)
        ):
            out.append(
                AlertEvent(
                    f"rec:{r.recommendation_id}:strong",
                    f"rec:{r.recommendation_id}",
                    "STRONG_RECOMMENDATION",
                    "info",
                    f"{tag}Strong video idea: {r.title}"[:200],
                    f"{st.value}; ranking index {score:.0f}/100 (not a probability of success). "
                    f"Format: {r.format_label}. Estimated {r.production.estimated_hours_low:g}-"
                    f"{r.production.estimated_hours_high:g} h (planning assumption).",
                    recommendation_id=r.recommendation_id,
                    opportunity_id=r.opportunity_id,
                    synthetic=r.synthetic,
                )
            )
        if st in _OK and r.production.urgency is PublishingUrgency.PUBLISH_SOON:
            out.append(
                AlertEvent(
                    f"rec:{r.recommendation_id}:urgent",
                    f"rec:{r.recommendation_id}:urgent",
                    "TIME_SENSITIVE",
                    "warning",
                    f"{tag}Time-sensitive: {r.title}"[:200],
                    "The Phase 6 opportunity window for this production profile is URGENT: "
                    + "; ".join(r.production.urgency_reasons[:1]),
                    recommendation_id=r.recommendation_id,
                    opportunity_id=r.opportunity_id,
                    synthetic=r.synthetic,
                )
            )
    return out


@dataclass
class DispatchReport:
    created: list[StoredAlert] = field(default_factory=list)
    duplicates: int = 0
    cooled_down: int = 0
    suppressed_synthetic: int = 0


class NotificationCenter:
    def __init__(
        self,
        channels: Sequence[Channel],
        store: AlertStore,
        loaded: LoadedRecommendationConfig,
        *,
        demo_mode: bool = False,
    ) -> None:
        self.channels = list(channels)
        self.store = store
        self.cfg = loaded.config.alerts
        self.demo_mode = demo_mode

    def status(self) -> list[ProviderView]:
        return [c.status() for c in self.channels]

    def dispatch(
        self, events: Sequence[AlertEvent], *, profile: CreatorProfile, now: datetime
    ) -> DispatchReport:
        rep = DispatchReport()
        for e in events:
            if e.synthetic and not (self.demo_mode or self.cfg.alert_on_synthetic):
                rep.suppressed_synthetic += 1
                continue
            if self.store.exists(e.dedup_key):
                rep.duplicates += 1
                continue
            last = self.store.last_for_subject(e.subject_key)
            if last is not None and now - last < timedelta(hours=self.cfg.cooldown_hours):
                rep.cooled_down += 1
                continue
            alert = StoredAlert(str(uuid.uuid4()), e, now)
            self.store.add(alert)
            rep.created.append(alert)
            for ch in self.channels:
                if ch.external and (e.synthetic and not self.cfg.alert_on_synthetic):
                    continue  # synthetic data never leaves the dashboard
                if not profile.notification_preferences.get(ch.name, ch.name == "in_app"):
                    continue
                self._deliver(alert, ch, now)
        return rep

    def _deliver(self, alert: StoredAlert, ch: Channel, now: datetime) -> None:
        if not ch.configured:
            self.store.update_delivery(
                alert.id,
                ch.name,
                {
                    "status": "skipped_unconfigured",
                    "attempts": 0,
                    "last_error": None,
                    "at": now.isoformat(),
                },
            )
            return
        attempts, error = 0, None
        for attempt in range(1, self.cfg.max_delivery_attempts + 1):
            attempts = attempt
            try:
                ch.send(alert.event)
                error = None
                break
            except DeliveryError as exc:
                error = str(exc)
                if not exc.transient:
                    break
        self.store.update_delivery(
            alert.id,
            ch.name,
            {
                "status": "delivered" if error is None else "failed",
                "attempts": attempts,
                "last_error": error,
                "at": now.isoformat(),
            },
        )
