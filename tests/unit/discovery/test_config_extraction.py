"""Seed configuration parsing, entity extraction, alias resolution and grounding."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.unit.discovery.helpers import CONFIG_DIR, LOADED, NOW, extract, item
from trend_agent.discovery.config import ConfigError, load_discovery_config, resolve_config_dir
from trend_agent.discovery.extraction import (
    CompositeExtractor,
    DeterministicExtractor,
    Gazetteer,
    ground,
    release_level,
)
from trend_agent.discovery.models import (
    Category,
    EntityOrigin,
    EntityStatus,
    EntityType,
    EvidenceType,
    ExtractedEntity,
)
from trend_agent.discovery.text import display_version, entity_key, normalize_name

# ---------------------------------------------------------------------------- 1. seed config


def test_shipped_configuration_loads_and_is_consistent() -> None:
    cfg = LOADED.config
    assert (
        cfg.category_budgets.ai,
        cfg.category_budgets.technology,
        cfg.category_budgets.gaming,
    ) == (
        0.45,
        0.25,
        0.30,
    )
    assert cfg.exploration_share == 0.20
    assert set(LOADED.seeds) == {Category.AI, Category.TECHNOLOGY, Category.GAMING}
    assert all(LOADED.enabled_seeds(c) for c in LOADED.seeds)
    assert {e.name for _, e in LOADED.gazetteer()} >= {
        "Fortnite",
        "Minecraft",
        "Roblox",
        "Grand Theft Auto",
    }
    keys = [s.key for s in LOADED.sources]
    assert len(keys) == len(set(keys))
    assert sum(cfg.scoring.weights.model_dump().values()) == pytest.approx(1.0)


def _write_config(
    root: Path,
    discovery: dict[str, object],
    seeds: dict[str, object] | None = None,
    sources: dict[str, object] | None = None,
) -> Path:
    (root / "seeds").mkdir(parents=True)
    (root / "sources").mkdir()
    (root / "discovery.yaml").write_text(yaml.safe_dump(discovery))
    if seeds is not None:
        (root / "seeds" / "gaming.yaml").write_text(yaml.safe_dump(seeds))
    if sources is not None:
        (root / "sources" / "x.yaml").write_text(yaml.safe_dump(sources))
    return root


def test_new_games_and_seeds_are_added_by_configuration_only(tmp_path: Path) -> None:
    root = _write_config(
        tmp_path / "cfg",
        {"version": 1},
        seeds={
            "category": "gaming",
            "seeds": [
                {
                    "term": "Skyforge Arena event",
                    "subcategory": "skyforge",
                    "priority": 88,
                    "regions": ["GB"],
                    "languages": ["en"],
                    "entity_type": "game_event",
                }
            ],
            "entities": [{"name": "Skyforge Arena", "entity_type": "game", "aliases": ["SFA"]}],
        },
    )
    loaded = load_discovery_config(root)
    seed = loaded.enabled_seeds(Category.GAMING)[0]
    assert (seed.priority, seed.regions, seed.entity_type) == (88, ("GB",), EntityType.GAME_EVENT)
    extractor = DeterministicExtractor(Gazetteer(loaded.gazetteer()))
    found = {
        e.canonical_name for e in extractor.extract(item("New SFA patch", category=Category.GAMING))
    }
    assert "Skyforge Arena" in found  # alias from config, no code change


@pytest.mark.parametrize(
    ("discovery", "message"),
    [
        ({"category_budgets": {"ai": 0.5, "technology": 0.5, "gaming": 0.5}}, "sum to 1.0"),
        (
            {
                "scoring": {
                    "weights": {
                        "novelty": 0.9,
                        "freshness": 0.2,
                        "corroboration": 0.2,
                        "relevance": 0.15,
                        "intent_actionability": 0.1,
                        "evidence_quality": 0.1,
                    }
                }
            },
            "sum to 1.0",
        ),
        ({"regions": ["UK"]}, "invalid region"),
        ({"unknown_option": 1}, "Extra inputs"),
    ],
)
def test_invalid_configuration_is_rejected(
    tmp_path: Path, discovery: dict[str, object], message: str
) -> None:
    root = _write_config(tmp_path / "cfg", discovery)
    with pytest.raises(ConfigError, match=message):
        load_discovery_config(root)


def test_duplicate_source_keys_and_bad_yaml(tmp_path: Path) -> None:
    source = {
        "key": "feed:x",
        "provider": "feeds",
        "name": "x",
        "source_type": "feed",
        "category": "ai",
        "reference": "https://x.example/feed",
    }
    root = _write_config(tmp_path / "a", {"version": 1}, sources={"sources": [source, source]})
    with pytest.raises(ConfigError, match="duplicate source keys"):
        load_discovery_config(root)
    bad = _write_config(tmp_path / "b", {"version": 1})
    (bad / "discovery.yaml").write_text("version: [unclosed")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_discovery_config(bad)


def test_missing_configuration_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TREND_AGENT_DISCOVERY_CONFIG_DIR", str(tmp_path / "nowhere"))
    assert resolve_config_dir(CONFIG_DIR) == CONFIG_DIR
    with pytest.raises(ConfigError):
        resolve_config_dir(tmp_path / "missing")


# ---------------------------------------------------------------------------- 4. extraction


def test_ai_model_release_with_versions_and_grounded_attributes() -> None:
    found = extract(
        "Introducing Nimbus Coder 2",
        "Corvid Labs today released Nimbus Coder 2, an open-weights coding model. Nimbus Coder 2 "
        "outperforms Nimbus Coder 1.5 on SWE-bench. Developers can run it locally or call it "
        "through the API. API pricing starts at $2 per million tokens.",
    )
    model = found["Nimbus Coder 2"]
    assert model.entity_type is EntityType.AI_MODEL
    assert model.parent_name == "Nimbus Coder"
    assert model.status is EntityStatus.VERIFIED
    assert {"local", "api", "pricing", "benchmark"} <= set(model.attributes)
    assert model.related == ("Nimbus Coder 1.5",)
    assert found["Nimbus Coder 1.5"].entity_type is EntityType.AI_MODEL  # family types agree
    assert "Nimbus Coder 1.5" not in found["Nimbus Coder 1.5"].related
    assert "pricing" not in found["Nimbus Coder 1.5"].attributes  # "it" refers to the subject


def test_gazetteer_versions_aliases_and_case_sensitive_acronyms() -> None:
    found = extract("GPT-5 vs Claude Opus 5 and Llama 4 locally", evidence=EvidenceType.VIDEO_TITLE)
    assert {"GPT-5", "Claude Opus 5", "Llama 4"} <= set(found)
    assert found["GPT-5"].parent_name == "GPT"
    assert found["GPT-5"].status is EntityStatus.NEEDS_VERIFICATION  # video title alone
    assert "Claude Opus 5" in found["GPT-5"].related
    gta = extract("GTA 6 gameplay first look", category=Category.GAMING)
    assert gta["Grand Theft Auto 6"].parent_name == "Grand Theft Auto"
    assert "GTA 6" in gta["Grand Theft Auto 6"].aliases
    assert "Grand Theft Auto" not in extract("gta is lowercase here", category=Category.GAMING)


def test_ambiguous_names_need_context() -> None:
    assert extract("Gemini is bright tonight", "stargazing tips")["Gemini"].status is (
        EntityStatus.NEEDS_VERIFICATION
    )
    assert extract("Gemini model update from Google")["Gemini"].status is EntityStatus.VERIFIED


def test_game_event_items_and_updates_only_from_explicit_text() -> None:
    found = extract(
        "Frostfall Festival is live in Skyforge Arena!",
        "The limited-time Frostfall Festival event runs until March 15. New items include the "
        "Glacier Lance and the Rime Glider. Complete the festival challenges to unlock rewards. "
        "Update 3.2 also fixes a crash on startup.",
        category=Category.GAMING,
        entity_hint="Skyforge Arena",
        entity_hint_type="game",
    )
    assert found["Frostfall Festival"].entity_type is EntityType.GAME_EVENT
    assert found["Frostfall Festival"].parent_name == "Skyforge Arena"
    assert {"challenges", "rewards", "limited_time"} <= set(found["Frostfall Festival"].attributes)
    assert found["Glacier Lance"].parent_name == "Frostfall Festival"
    assert found["Rime Glider"].entity_type is EntityType.GAME_ITEM
    assert found["Skyforge Arena Update 3.2"].entity_type is EntityType.GAME_UPDATE
    items = {n for n, e in found.items() if e.entity_type is EntityType.GAME_ITEM}
    assert items == {"Glacier Lance", "Rime Glider"}  # nothing invented


def test_rumoured_items_are_never_extracted_and_claims_are_speculative() -> None:
    found = extract(
        "Secret Void Crown in Skyforge Arena?",
        "Rumor says a hidden Void Crown might be in the festival.",
        evidence=EvidenceType.COMMUNITY_DISCUSSION,
        category=Category.GAMING,
    )
    assert "Void Crown" not in found
    rumor = extract(
        "Nimbus Coder 3 leaked??",
        "Rumor says Nimbus Coder 3 might launch next month.",
        evidence=EvidenceType.COMMUNITY_DISCUSSION,
    )
    assert rumor["Nimbus Coder 3"].status is EntityStatus.NEEDS_VERIFICATION
    assert "speculative" in rumor["Nimbus Coder 3"].flags


def test_release_feed_titles_and_release_levels() -> None:
    found = extract(
        "5.0.0",
        "Major release with breaking changes.",
        evidence=EvidenceType.CODE_RELEASE,
        category=Category.TECHNOLOGY,
        entity_hint="Lattice",
        entity_hint_type="framework",
    )
    release = found["Lattice 5.0"]
    assert release.entity_type is EntityType.FRAMEWORK
    assert release.parent_name == "Lattice"
    assert "release_level:major" in release.flags
    assert [release_level(v) for v in ("5.0.0", "5.1.0", "5.1.2", "0.33.0", "6.0.0rc1", "4")] == [
        "major",
        "minor",
        "patch",
        "minor",
        "prerelease",
        "major",
    ]
    assert display_version("0.33.0") == "0.33"


def test_similar_names_do_not_merge() -> None:
    thermostat = extract(
        "Haldane Home announces the Nimbus 2 smart thermostat",
        "The Nimbus 2 thermostat is a smart home device.",
        category=Category.TECHNOLOGY,
    )
    model = extract("Introducing Nimbus Coder 2", "Nimbus Coder 2 is a coding model.")
    assert thermostat["Nimbus 2"].entity_type is EntityType.HARDWARE
    keys = {
        entity_key(e.category.value, e.entity_type.value, e.canonical_name)
        for e in [thermostat["Nimbus 2"], model["Nimbus Coder 2"]]
    }
    assert len(keys) == 2
    # Same name, different category or type -> different identity.
    assert entity_key("ai", "ai_model", "Atlas") != entity_key("technology", "hardware", "Atlas")
    # Different versions are different entities; equivalent spellings are the same.
    assert (
        normalize_name("Lattice 5.0.0")
        == normalize_name("Lattice 5")
        != normalize_name("Lattice 4.0")
    )
    assert normalize_name("GPT-4o") != normalize_name("GPT-4")


def test_extraction_is_deterministic() -> None:
    a = extract("Introducing Nimbus Coder 2", "Nimbus Coder 2 is a model.")
    b = extract("Introducing Nimbus Coder 2", "Nimbus Coder 2 is a model.")
    assert a == b


class _FakeLLM:
    name = "fake-llm"

    def extract(self, _item: object) -> list[ExtractedEntity]:
        def proposal(name: str, matched: str) -> ExtractedEntity:
            return ExtractedEntity(
                canonical_name=name,
                matched_text=matched,
                entity_type=EntityType.AI_MODEL,
                category=Category.AI,
                confidence=0.9,
                extractor="fake-llm",
            )

        return [
            proposal("Nimbus Coder 2", "Nimbus Coder 2"),
            proposal("Hallucinated Model 9", "?"),
        ]


def test_llm_proposals_are_grounding_checked() -> None:
    composite = CompositeExtractor(
        DeterministicExtractor(Gazetteer(LOADED.gazetteer())), [_FakeLLM()]
    )
    found = {
        e.canonical_name: e
        for e in composite.extract(item("Nimbus Coder 2 is out", "A coding model."))
    }
    invented = found["Hallucinated Model 9"]
    assert invented.status is EntityStatus.REJECTED
    assert "ungrounded" in invented.flags
    assert invented.origin is EntityOrigin.LLM_PROPOSED
    assert found["Nimbus Coder 2"].status is not EntityStatus.REJECTED
    direct = ground(
        invented.model_copy(update={"status": EntityStatus.VERIFIED}), item("Other text")
    )
    assert direct.status is EntityStatus.REJECTED


def test_full_text_keeps_title_and_body_separate() -> None:
    it = item(
        "Frostfall Festival Guide - All Challenges in Skyforge Arena",
        "Every Frostfall Festival challenge.",
    )
    assert it.full_text.startswith(
        "Frostfall Festival Guide - All Challenges in Skyforge Arena. Every"
    )
    assert it.collected_at == NOW
