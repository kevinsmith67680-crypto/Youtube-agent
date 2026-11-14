"""Verification configuration (``config/verification.yaml``), validated strictly.

Every threshold the claim verifier and the gate use lives here. ``version_tag`` is stored
with every verification run so a decision can be traced to the rules that produced it.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from trend_agent.discovery.config import ConfigError, resolve_config_dir
from trend_agent.schemas.verification import EvidenceKind

VERIFIER_VERSION = "verify-1.0.0"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Freshness(_Strict):
    """How long a record can support time-sensitive claims, by evidence kind.

    ``fresh`` and ``usable`` are ages measured from the publication time (else the
    collection time). Older than ``usable`` is STALE.
    """

    fresh: dict[EvidenceKind, timedelta] = Field(
        default_factory=lambda: {
            EvidenceKind.MEASUREMENT: timedelta(days=3),
            EvidenceKind.SURGE_DETECTION: timedelta(days=3),
            EvidenceKind.STATISTICAL_CALCULATION: timedelta(days=3),
            EvidenceKind.VIDEO_METADATA: timedelta(days=7),
            EvidenceKind.COMMUNITY_POST: timedelta(days=7),
            EvidenceKind.SEARCH_SUGGESTION: timedelta(days=7),
            EvidenceKind.RELATED_QUERY: timedelta(days=7),
            EvidenceKind.NEWS_REPORT: timedelta(days=14),
            EvidenceKind.OFFICIAL_ANNOUNCEMENT: timedelta(days=30),
            EvidenceKind.CODE_RELEASE: timedelta(days=30),
            EvidenceKind.REPOSITORY_METADATA: timedelta(days=30),
            # Twitch Developer Agreement: Twitch data is cached for 24 hours at most
            EvidenceKind.GAME_CATALOG: timedelta(hours=24),
            EvidenceKind.ENTITY_RECORD: timedelta(days=90),
            EvidenceKind.CURATED_REFERENCE: timedelta(days=365),
            EvidenceKind.SYNTHETIC_TEST_DATA: timedelta(days=3650),
        }
    )
    usable_factor: float = Field(default=4.0, ge=1.0, description="usable = fresh x factor")
    novelty_window: timedelta = Field(
        default=timedelta(days=45),
        description="'new'/'just announced' claims need evidence published within this",
    )


class Matching(_Strict):
    term_coverage: float = Field(default=0.6, gt=0, le=1)
    min_content_terms: int = Field(default=1, ge=0)
    percent_tolerance: float = Field(default=2.0, ge=0, description="percentage points")
    relative_tolerance: float = Field(default=0.02, ge=0, description="for other numbers")
    min_independent_sources: int = Field(default=2, ge=2)
    max_excerpt_chars: int = Field(default=4000, ge=200)


class RetryPolicy(_Strict):
    max_attempts: int = Field(default=3, ge=1, le=5)
    retry_on_malformed: bool = True
    retry_on_unsupported_title: bool = True
    stop_on_repeated_claim: bool = True


class GeneratorSettings(_Strict):
    default: str = "template"
    anthropic_model: str = "claude-opus-5-5"
    anthropic_max_tokens: int = Field(default=8000, ge=512, le=64000)
    anthropic_timeout_seconds: float = Field(default=120.0, gt=0)
    server_side_fallbacks: bool = True


class Vocabulary(_Strict):
    """Words that may appear in titles/rationales without evidence (non-factual)."""

    title_neutral: tuple[str, ...] = ()
    stopwords: tuple[str, ...] = ()


class VerificationConfig(_Strict):
    version: int = 1
    config_version: str = "verification-v1"
    freshness: Freshness = Freshness()
    matching: Matching = Matching()
    retry: RetryPolicy = RetryPolicy()
    generator: GeneratorSettings = GeneratorSettings()
    vocabulary: Vocabulary = Vocabulary()
    bundle_ttl: timedelta = timedelta(hours=6)
    allow_synthetic_evidence: bool = False
    max_bundle_records: int = Field(default=40, ge=1, le=200)
    max_observations: int = Field(default=14, ge=0, le=200)


class LoadedVerificationConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: Path | None
    config: VerificationConfig
    digest: str

    @property
    def version_tag(self) -> str:
        return f"{self.config.config_version}@{self.digest}"


def load_verification_config(directory: str | Path | None = None) -> LoadedVerificationConfig:
    root = resolve_config_dir(directory)
    path = root / "verification.yaml"
    if not path.is_file():
        raise ConfigError(f"verification configuration not found: {path}")
    raw = path.read_text(encoding="utf-8")
    try:
        data: Any = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None
    try:
        config = VerificationConfig.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ConfigError(f"{path}: {where}: {first.get('msg')}") from None
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return LoadedVerificationConfig(path=path, config=config, digest=digest)


def default_verification_config() -> LoadedVerificationConfig:
    return LoadedVerificationConfig(path=None, config=VerificationConfig(), digest="defaults")
