"""Builders shared by discovery unit tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trend_agent.discovery.config import load_discovery_config
from trend_agent.discovery.extraction import DeterministicExtractor, Gazetteer
from trend_agent.discovery.models import Category, DiscoveryItem, EvidenceType, ExtractedEntity

ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = ROOT / "config"
NOW = datetime(2026, 3, 1, 12, tzinfo=UTC)

LOADED = load_discovery_config(CONFIG_DIR)
EXTRACTOR = DeterministicExtractor(Gazetteer(LOADED.gazetteer()))


def item(
    title: str,
    text: str | None = None,
    *,
    evidence: EvidenceType = EvidenceType.OFFICIAL_ANNOUNCEMENT,
    category: Category = Category.AI,
    **kw: Any,
) -> DiscoveryItem:
    base: dict[str, Any] = {
        "provider_name": "test",
        "source_key": "test:source",
        "source_reference": f"https://example.invalid/{abs(hash(title))}",
        "evidence_type": evidence,
        "title": title,
        "text": text,
        "collected_at": NOW,
        "category_hint": category,
    }
    base.update(kw)
    return DiscoveryItem(**base)


def extract(*args: Any, **kw: Any) -> dict[str, ExtractedEntity]:
    return {e.canonical_name: e for e in EXTRACTOR.extract(item(*args, **kw))}
