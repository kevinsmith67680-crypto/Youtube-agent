"""Storage permission decisions and retention enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from trend_agent.db.repositories import ObservationRepository, VideoRepository
from trend_agent.db.repositories.detection_runs import DetectionRunRepository
from trend_agent.db.repositories.discovery import DiscoveryRepository
from trend_agent.db.repositories.surge_events import SurgeEventRepository
from trend_agent.db.repositories.time_series import TimeSeriesRepository
from trend_agent.db.repositories.verification import VerificationRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.logging import get_logger, log_event
from trend_agent.schemas.enums import RecordKind
from trend_agent.schemas.provider import ProviderDescriptor
from trend_agent.utils.time import Clock, utc_now

_log = get_logger(__name__)


@dataclass(frozen=True)
class StorageDecision:
    allowed: bool
    expires_at: datetime | None
    reason: str


def storage_decision(
    descriptor: ProviderDescriptor, kind: RecordKind, collected_at: datetime
) -> StorageDecision:
    """Whether a record of ``kind`` from this provider may be stored, and until when.

    Retention periods run from the time the data was *collected* from the provider.
    """
    rule = descriptor.retention_for(kind)
    if not rule.persist_allowed:
        return StorageDecision(False, None, rule.notes or "persistence not permitted")
    expires = collected_at + rule.max_retention if rule.max_retention is not None else None
    return StorageDecision(True, expires, rule.notes)


@dataclass(frozen=True)
class RetentionReport:
    checked_at: datetime
    expired: dict[str, int]
    expiring_videos_within_horizon: int
    horizon: timedelta


class RetentionService:
    def __init__(self, factory: SessionFactory, clock: Clock = utc_now) -> None:
        self._factory = factory
        self._clock = clock

    def report(self, horizon: timedelta = timedelta(days=3)) -> RetentionReport:
        now = self._clock()
        with transaction(self._factory) as session:
            expired = VideoRepository(session).count_expired(now)
            expired["search_interest_observations"] = ObservationRepository(session).count_expired(
                now
            )
            expired["discovery_items"] = DiscoveryRepository(session).count_expired_items(now)
            expired["ts_observations"] = TimeSeriesRepository(session).count_expired(now)
            # Phase 4: derived detection records carry the expiry of their sources.
            expired["anomaly_results"] = DetectionRunRepository(session).count_expired(now)
            expired["surge_events"] = SurgeEventRepository(session).count_expired(now)
            # Verification layer: evidence records and gate runs expire with their sources.
            expired.update(VerificationRepository(session).count_expired(now))
            due = len(VideoRepository(session).videos_due_for_refresh(now, horizon, 100_000))
        return RetentionReport(
            checked_at=now, expired=expired, expiring_videos_within_horizon=due, horizon=horizon
        )

    def purge_expired(self) -> dict[str, int]:
        """Delete every record whose retention period has ended, in one transaction."""
        now = self._clock()
        with transaction(self._factory) as session:
            deleted = VideoRepository(session).delete_expired(now)
            deleted["search_interest_observations"] = ObservationRepository(session).delete_expired(
                now
            )
            # Phase 2: discovery source material (e.g. YouTube titles) expires too; its
            # evidence links and entity mentions cascade.
            deleted["discovery_items"] = DiscoveryRepository(session).purge_expired_items(now)
            # Phase 3: monitoring observations expire per their data policy (derived values
            # never outlive their sources: their expiry is the earliest source expiry).
            # Phase 4: detection results and episodes derived from expiring observations go
            # first (also enforced by cascades when the source observations are deleted).
            deleted["anomaly_results"] = DetectionRunRepository(session).purge_expired(now)
            deleted["surge_events"] = SurgeEventRepository(session).purge_expired(now)
            # Verification runs (expiry = earliest expiry of the evidence they were shown),
            # then unused expired bundles, then evidence records copied from expiring sources.
            deleted.update(VerificationRepository(session).purge_expired(now))
            deleted["ts_observations"] = TimeSeriesRepository(session).purge_expired(now)
        log_event(_log, "retention.purged", deleted=deleted, as_of=now.isoformat())
        return deleted
