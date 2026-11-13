"""Evidence registry: issued IDs, versioning, integrity, bundles and reference resolution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.helpers import make_settings
from tests.unit.verification.helpers import LOADED, world
from trend_agent.discovery.config import ConfigError
from trend_agent.schemas.verification import (
    EVIDENCE_ID_PATTERN,
    EvidenceDraft,
    EvidenceKind,
    EvidenceSourceKind,
    EvidenceStatus,
    FreshnessStatus,
    MeasurementSemantics,
    TrustLevel,
    ViolationCode,
)
from trend_agent.verification.registry import (
    EvidenceRegistry,
    EvidenceSigner,
    MemoryEvidenceStore,
    content_hash,
)

NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)


def _draft(text: str = "Aurora Labs releases Nimbus 4.", **over: object) -> EvidenceDraft:
    data: dict[str, object] = {
        "evidence_key": "discovery_item:1",
        "source_kind": EvidenceSourceKind.DISCOVERY_ITEM,
        "provider_name": "rss_feed",
        "source_reference": "https://aurora-labs.example/blog/nimbus-4",
        "published_at": NOW - timedelta(days=2),
        "collected_at": NOW - timedelta(days=1),
        "evidence_kind": EvidenceKind.OFFICIAL_ANNOUNCEMENT,
        "measurement_semantics": MeasurementSemantics.SOURCE_STATEMENT,
        "trust_level": TrustLevel.OFFICIAL,
        "excerpt": text,
    }
    data.update(over)
    return EvidenceDraft.model_validate(data)


def _registry() -> EvidenceRegistry:
    return EvidenceRegistry(
        MemoryEvidenceStore(), EvidenceSigner.development(), LOADED.config, clock=lambda: NOW
    )


def test_ids_are_issued_signed_and_idempotent() -> None:
    reg = _registry()
    a = reg.register(_draft())
    b = reg.register(_draft())
    assert a.evidence_id == b.evidence_id
    assert EVIDENCE_ID_PATTERN.match(a.evidence_id)
    assert a.version == 1
    assert a.is_current
    assert a.content_hash == content_hash(_draft())
    assert reg.signer.verify_record(a) is None
    other = EvidenceRegistry(
        MemoryEvidenceStore(), EvidenceSigner(b"another-installation-key!"), LOADED.config
    ).register(_draft())
    assert other.evidence_id != a.evidence_id  # unguessable without the key


def test_changed_source_creates_new_version_and_supersedes_old() -> None:
    reg = _registry()
    v1 = reg.register(_draft())
    v2 = reg.register(_draft("Aurora Labs releases Nimbus 4 (updated)."))
    assert v2.version == 2
    assert v2.evidence_id != v1.evidence_id
    store = reg.store
    assert isinstance(store, MemoryEvidenceStore)
    old = store.records[v1.evidence_id]
    assert old.status is EvidenceStatus.SUPERSEDED
    assert not old.is_current
    assert store.current("discovery_item:1") == v2


def test_tampered_record_fails_integrity() -> None:
    reg = _registry()
    rec = reg.register(_draft())
    forged = rec.model_copy(update={"excerpt": "Aurora Labs releases Nimbus 9."})
    assert reg.signer.verify_record(forged) == "content hash mismatch"
    moved = rec.model_copy(update={"evidence_key": "discovery_item:2"})
    assert reg.signer.verify_record(moved) is not None
    store = reg.store
    assert isinstance(store, MemoryEvidenceStore)
    store.records[rec.evidence_id] = forged
    bundle = reg.issue_bundle([rec], target_id=1)
    res = reg.resolve([rec.evidence_id], bundle, allow_synthetic=False)
    assert res.problems[0].code is ViolationCode.EVIDENCE_INTEGRITY


def test_resolution_rejects_fabricated_substituted_malformed_and_synthetic() -> None:
    w = world()
    ctx = w.context("nimbus")
    other = w.records["off:voltx9"].evidence_id  # real record of another topic
    synthetic = w.records["syn:nimbus4"].evidence_id
    res = w.registry.resolve(
        [
            ctx.records[0].evidence_id,
            "ev_" + "a" * 26,
            other,
            "src_aurora_blog",
            synthetic,
        ],
        ctx.bundle,
        allow_synthetic=False,
    )
    codes = {p.evidence_id: p.code for p in res.problems}
    assert ctx.records[0].evidence_id in res.records
    assert codes["ev_" + "a" * 26] is ViolationCode.UNKNOWN_EVIDENCE_ID
    assert codes[other] is ViolationCode.EVIDENCE_NOT_IN_BUNDLE
    assert codes["src_aurora_blog"] is ViolationCode.MALFORMED_EVIDENCE_ID
    assert codes[synthetic] is ViolationCode.EVIDENCE_NOT_IN_BUNDLE
    syn_ctx = w.context("production_with_synthetic")
    res = w.registry.resolve([synthetic], syn_ctx.bundle, allow_synthetic=False)
    assert res.problems[0].code is ViolationCode.SYNTHETIC_EVIDENCE


def test_forged_bundle_and_expired_evidence_are_rejected() -> None:
    reg = _registry()
    rec = reg.register(_draft(expires_at=NOW - timedelta(minutes=1)))
    bundle = reg.issue_bundle([rec], target_id=1)
    res = reg.resolve([rec.evidence_id], bundle, allow_synthetic=False)
    assert res.problems[0].code is ViolationCode.EVIDENCE_UNAVAILABLE
    forged = bundle.model_copy(update={"evidence_ids": (*bundle.evidence_ids, "ev_" + "b" * 26)})
    res = reg.resolve([rec.evidence_id], forged, allow_synthetic=False)
    assert res.problems[0].code is ViolationCode.EVIDENCE_INTEGRITY


def test_freshness_by_kind() -> None:
    reg = _registry()
    rec = reg.register(_draft())
    assert reg.freshness(rec) is FreshnessStatus.FRESH
    assert reg.freshness(rec, NOW + timedelta(days=60)) is FreshnessStatus.AGING
    assert reg.freshness(rec, NOW + timedelta(days=200)) is FreshnessStatus.STALE


def test_production_requires_a_signing_key() -> None:
    with pytest.raises(ConfigError):
        EvidenceSigner.from_settings(make_settings(environment="production"))
    s = EvidenceSigner.from_settings(
        make_settings(environment="production", verification_signing_key="k" * 32)
    )
    assert s.key_id != EvidenceSigner.development().key_id
    with pytest.raises(ValueError, match="16 bytes"):
        EvidenceSigner(b"short")
