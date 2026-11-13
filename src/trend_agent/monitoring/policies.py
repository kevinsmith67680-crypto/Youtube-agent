"""Data policies: may an observation be stored, until when, and under which policy version.

Each stored observation references a versioned policy (``data_policies``). Expiry runs from
the collection time, or - for values derived from other stored records - from the
earliest expiry of those sources, so derived values never outlive their inputs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from trend_agent.config import Settings
from trend_agent.monitoring.config import DataPolicySpec, MonitoringConfig


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    policy_id: str
    expires_at: datetime | None
    reason: str


@dataclass(frozen=True)
class ResolvedPolicy:
    spec: DataPolicySpec
    retention: timedelta | None
    storage_allowed: bool
    authorized: bool
    reason: str

    @property
    def policy_id(self) -> str:
        return self.spec.policy_id

    def row(self) -> dict[str, object]:
        payload = self.spec.model_dump(mode="json")
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return {
            "id": self.policy_id,
            "policy_key": self.spec.id,
            "version": self.spec.version,
            "description": self.spec.description,
            "retention_days": self.retention.days if self.retention else None,
            "storage_allowed": self.storage_allowed,
            "requires_authorization": self.spec.requires_authorization,
            "authorized": self.authorized,
            "permissions": dict(self.spec.permissions),
            "config_hash": digest,
        }


class PolicyRegistry:
    def __init__(self, config: MonitoringConfig, settings: Settings | None = None) -> None:
        self._resolved: dict[str, ResolvedPolicy] = {}
        for spec in config.policies:
            self._resolved[spec.id] = _resolve(spec, settings)

    def resolved(self, key: str) -> ResolvedPolicy:
        try:
            return self._resolved[key]
        except KeyError:
            raise KeyError(f"unknown data policy {key!r}") from None

    def all(self) -> list[ResolvedPolicy]:
        return list(self._resolved.values())

    def decide(
        self,
        key: str,
        collected_at: datetime,
        *,
        source_expiry: datetime | None = None,
    ) -> PolicyDecision:
        policy = self.resolved(key)
        if not policy.storage_allowed:
            return PolicyDecision(False, policy.policy_id, None, policy.reason)
        expires: datetime | None = None
        if policy.retention is not None:
            expires = collected_at + policy.retention
        if policy.spec.inherit_source_expiry and source_expiry is not None:
            expires = source_expiry if expires is None else min(expires, source_expiry)
        return PolicyDecision(True, policy.policy_id, expires, policy.reason)


def _resolve(spec: DataPolicySpec, settings: Settings | None) -> ResolvedPolicy:
    retention = timedelta(days=spec.retention_days) if spec.retention_days else None
    allowed = spec.storage_allowed
    reason = spec.description
    if spec.retention_from_setting:
        days = getattr(settings, spec.retention_from_setting, None) if settings else None
        if days is None:
            allowed = False
            reason = (
                f"storage denied: set {spec.retention_from_setting.upper()} "
                "(TREND_AGENT_...) to a retention period permitted by the access terms"
            )
        else:
            retention = timedelta(days=int(days))
    authorized = not spec.requires_authorization or allowed
    return ResolvedPolicy(spec, retention, allowed, authorized, reason)
