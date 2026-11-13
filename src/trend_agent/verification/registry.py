"""Persistent, versioned evidence registry with application-issued, signed identifiers.

Why a generator cannot fabricate or substitute an evidence ID:

1. **IDs are issued only here.** ``ev_`` + 26 base32 characters of an HMAC-SHA256 over
   (evidence key, version, content hash) with a server-side key the generator never sees.
   A made-up ID is not in the registry (``unknown_evidence_id``).
2. **Bundles scope what may be cited.** Each generation request receives a signed bundle
   of the IDs it was shown. Citing a real ID from elsewhere (another topic, an older
   request) fails with ``evidence_not_in_bundle``.
3. **Integrity is re-checked on use.** Resolution recomputes the content hash and the
   HMAC from the stored record, so an edited row or a copied ID no longer verifies.
4. **Existence is not support.** A resolvable ID only makes the record *available*; the
   claim verifier separately checks that the record supports the specific claim.

Records are immutable. A change in the underlying source registers a new version and
marks the previous one superseded (the database enforces immutability with a trigger).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from trend_agent.config import Settings
from trend_agent.discovery.config import ConfigError
from trend_agent.schemas.verification import (
    EVIDENCE_ID_PATTERN,
    EvidenceBundle,
    EvidenceDraft,
    EvidenceRecord,
    EvidenceStatus,
    FreshnessStatus,
    ViolationCode,
)
from trend_agent.utils.time import Clock, utc_now
from trend_agent.verification.config import VerificationConfig

_DEV_KEY = hashlib.sha256(b"trend-agent development evidence key - not for production").digest()
PRODUCTION_ENVIRONMENTS = frozenset({"production", "prod"})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(draft: EvidenceDraft) -> str:
    """SHA-256 of every field of the draft (the identity of this version's content)."""
    payload = draft.model_dump(mode="json", include=set(EvidenceDraft.model_fields))
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class EvidenceSigner:
    """HMAC signer for evidence IDs and bundle tokens."""

    def __init__(self, key: bytes) -> None:
        if len(key) < 16:
            raise ValueError("evidence signing key must be at least 16 bytes")
        self._key = key
        self.key_id = hashlib.sha256(b"key-id:" + key).hexdigest()[:12]

    @classmethod
    def development(cls) -> EvidenceSigner:
        return cls(_DEV_KEY)

    @classmethod
    def from_settings(cls, settings: Settings) -> EvidenceSigner:
        secret = settings.verification_signing_key
        if secret is None:
            if settings.environment.lower() in PRODUCTION_ENVIRONMENTS:
                raise ConfigError("TREND_AGENT_VERIFICATION_SIGNING_KEY is required in production")
            return cls.development()
        return cls(secret.get_secret_value().encode("utf-8"))

    def _mac(self, message: str) -> bytes:
        return hmac.new(self._key, message.encode("utf-8"), hashlib.sha256).digest()

    def evidence_id(self, evidence_key: str, version: int, digest: str) -> str:
        mac = self._mac(f"evidence|{evidence_key}|{version}|{digest}")
        return "ev_" + base64.b32encode(mac).decode("ascii").lower().rstrip("=")[:26]

    def bundle_token(self, bundle_id: str, ids: Sequence[str], issued_at: datetime) -> str:
        return self._mac(f"bundle|{bundle_id}|{','.join(ids)}|{issued_at.isoformat()}").hex()

    def verify_record(self, record: EvidenceRecord) -> str | None:
        """None when intact, else the reason the record fails verification."""
        if record.signer_key_id != self.key_id:
            return "signed with a different key"
        draft = EvidenceDraft.model_validate(
            record.model_dump(include=set(EvidenceDraft.model_fields))
        )
        digest = content_hash(draft)
        if not hmac.compare_digest(digest, record.content_hash):
            return "content hash mismatch"
        expected = self.evidence_id(record.evidence_key, record.version, digest)
        if not hmac.compare_digest(expected, record.evidence_id):
            return "identifier does not match its content"
        return None

    def verify_bundle(self, bundle: EvidenceBundle) -> bool:
        expected = self.bundle_token(bundle.bundle_id, bundle.evidence_ids, bundle.issued_at)
        return hmac.compare_digest(expected, bundle.token)


class EvidenceStore(Protocol):
    def lock(self, evidence_key: str) -> None: ...
    def current(self, evidence_key: str) -> EvidenceRecord | None: ...
    def insert(self, record: EvidenceRecord, supersedes: EvidenceRecord | None) -> None: ...
    def get_many(self, evidence_ids: Iterable[str]) -> dict[str, EvidenceRecord]: ...
    def save_bundle(self, bundle: EvidenceBundle) -> None: ...
    def get_bundle(self, bundle_id: str) -> EvidenceBundle | None: ...
    def mark_source_removed(self, evidence_key: str) -> int: ...


class MemoryEvidenceStore:
    """In-memory store (tests, evaluation worlds, dry runs)."""

    def __init__(self) -> None:
        self.records: dict[str, EvidenceRecord] = {}
        self.by_key: dict[str, list[str]] = {}
        self.bundles: dict[str, EvidenceBundle] = {}

    def lock(self, evidence_key: str) -> None:
        return None

    def current(self, evidence_key: str) -> EvidenceRecord | None:
        for eid in reversed(self.by_key.get(evidence_key, [])):
            rec = self.records[eid]
            if rec.is_current:
                return rec
        return None

    def insert(self, record: EvidenceRecord, supersedes: EvidenceRecord | None) -> None:
        if supersedes is not None:
            self.records[supersedes.evidence_id] = supersedes.model_copy(
                update={"is_current": False, "status": EvidenceStatus.SUPERSEDED}
            )
        self.records[record.evidence_id] = record
        self.by_key.setdefault(record.evidence_key, []).append(record.evidence_id)

    def get_many(self, evidence_ids: Iterable[str]) -> dict[str, EvidenceRecord]:
        return {i: self.records[i] for i in evidence_ids if i in self.records}

    def save_bundle(self, bundle: EvidenceBundle) -> None:
        self.bundles[bundle.bundle_id] = bundle

    def get_bundle(self, bundle_id: str) -> EvidenceBundle | None:
        return self.bundles.get(bundle_id)

    def mark_source_removed(self, evidence_key: str) -> int:
        n = 0
        for eid in self.by_key.get(evidence_key, []):
            rec = self.records[eid]
            if rec.status is not EvidenceStatus.SOURCE_REMOVED:
                self.records[eid] = rec.model_copy(
                    update={"is_current": False, "status": EvidenceStatus.SOURCE_REMOVED}
                )
                n += 1
        return n


@dataclass(frozen=True)
class ReferenceProblem:
    evidence_id: str
    code: ViolationCode
    detail: str


@dataclass
class Resolution:
    records: dict[str, EvidenceRecord] = field(default_factory=dict)
    problems: list[ReferenceProblem] = field(default_factory=list)

    def problem_for(self, evidence_id: str) -> ReferenceProblem | None:
        return next((p for p in self.problems if p.evidence_id == evidence_id), None)


def freshness_of(
    record: EvidenceRecord | EvidenceDraft, now: datetime, config: VerificationConfig
) -> FreshnessStatus:
    basis = record.published_at or record.collected_at
    fresh = config.freshness.fresh.get(record.evidence_kind, timedelta(days=7))
    age = now - basis
    if age <= fresh:
        return FreshnessStatus.FRESH
    if age <= fresh * config.freshness.usable_factor:
        return FreshnessStatus.AGING
    return FreshnessStatus.STALE


class EvidenceRegistry:
    def __init__(
        self,
        store: EvidenceStore,
        signer: EvidenceSigner,
        config: VerificationConfig,
        clock: Clock = utc_now,
    ) -> None:
        self.store = store
        self.signer = signer
        self.config = config
        self._clock = clock

    def register(self, draft: EvidenceDraft) -> EvidenceRecord:
        """Idempotent: the same content returns the existing record; new content a new version."""
        if (
            draft.excerpt is not None
            and len(draft.excerpt) > self.config.matching.max_excerpt_chars
        ):
            draft = draft.model_copy(
                update={"excerpt": draft.excerpt[: self.config.matching.max_excerpt_chars]}
            )
        digest = content_hash(draft)
        self.store.lock(draft.evidence_key)
        current = self.store.current(draft.evidence_key)
        if current is not None and current.content_hash == digest:
            return current
        version = 1 if current is None else current.version + 1
        basis = draft.published_at or draft.collected_at
        fresh = self.config.freshness.fresh.get(draft.evidence_kind, timedelta(days=7))
        record = EvidenceRecord(
            **draft.model_dump(),
            evidence_id=self.signer.evidence_id(draft.evidence_key, version, digest),
            version=version,
            content_hash=digest,
            status=EvidenceStatus.RECORDED,
            is_current=True,
            registered_at=self._clock(),
            fresh_until=basis + fresh,
            signer_key_id=self.signer.key_id,
        )
        self.store.insert(record, current)
        return record

    def issue_bundle(
        self,
        records: Sequence[EvidenceRecord],
        *,
        target_id: int | None,
        synthetic: bool = False,
    ) -> EvidenceBundle:
        issued = self._clock()
        bundle_id = str(uuid.uuid4())
        ids = tuple(dict.fromkeys(r.evidence_id for r in records))
        bundle = EvidenceBundle(
            bundle_id=bundle_id,
            evidence_ids=ids,
            issued_at=issued,
            expires_at=issued + self.config.bundle_ttl,
            token=self.signer.bundle_token(bundle_id, ids, issued),
            target_id=target_id,
            is_synthetic=synthetic,
        )
        self.store.save_bundle(bundle)
        return bundle

    def resolve(
        self,
        evidence_ids: Iterable[str],
        bundle: EvidenceBundle,
        *,
        allow_synthetic: bool,
        now: datetime | None = None,
    ) -> Resolution:
        """Resolve cited IDs; every failure is a typed problem, never an exception."""
        now = now or self._clock()
        out = Resolution()
        wanted = list(dict.fromkeys(evidence_ids))
        if not self.signer.verify_bundle(bundle):
            for eid in wanted:
                out.problems.append(
                    ReferenceProblem(eid, ViolationCode.EVIDENCE_INTEGRITY, "bundle token invalid")
                )
            return out
        allowed = set(bundle.evidence_ids)
        well_formed = []
        for eid in wanted:
            if not isinstance(eid, str) or not EVIDENCE_ID_PATTERN.match(eid):
                out.problems.append(
                    ReferenceProblem(
                        str(eid), ViolationCode.MALFORMED_EVIDENCE_ID, "not an issued identifier"
                    )
                )
            else:
                well_formed.append(eid)
        found = self.store.get_many(well_formed)
        for eid in well_formed:
            rec = found.get(eid)
            if rec is None:
                out.problems.append(
                    ReferenceProblem(eid, ViolationCode.UNKNOWN_EVIDENCE_ID, "no such evidence")
                )
                continue
            if eid not in allowed:
                out.problems.append(
                    ReferenceProblem(
                        eid,
                        ViolationCode.EVIDENCE_NOT_IN_BUNDLE,
                        "evidence exists but was not provided for this request",
                    )
                )
                continue
            broken = self.signer.verify_record(rec)
            if broken is not None:
                out.problems.append(ReferenceProblem(eid, ViolationCode.EVIDENCE_INTEGRITY, broken))
                continue
            if rec.status is EvidenceStatus.SOURCE_REMOVED or (
                rec.expires_at is not None and rec.expires_at <= now
            ):
                out.problems.append(
                    ReferenceProblem(
                        eid, ViolationCode.EVIDENCE_UNAVAILABLE, "source removed or expired"
                    )
                )
                continue
            if rec.is_synthetic and not allow_synthetic:
                out.problems.append(
                    ReferenceProblem(
                        eid,
                        ViolationCode.SYNTHETIC_EVIDENCE,
                        "synthetic test data cannot support claims about the real world",
                    )
                )
                continue
            out.records[eid] = rec
        return out

    def freshness(self, record: EvidenceRecord, now: datetime | None = None) -> FreshnessStatus:
        return freshness_of(record, now or self._clock(), self.config)
