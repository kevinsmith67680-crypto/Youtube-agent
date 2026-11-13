"""Shared fixtures for verification-layer unit tests (offline, in-memory)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trend_agent.verification.config import LoadedVerificationConfig, load_verification_config
from trend_agent.verification.gate import GateDecision, VerificationGate
from trend_agent.verification.generation import ScriptedGenerator
from trend_agent.verification.world import World, load_world

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "config"
WORLD = CONFIG / "verification" / "world.yaml"
LOADED: LoadedVerificationConfig = load_verification_config(CONFIG)


def world() -> World:
    return load_world(WORLD, LOADED)


def gate_for(w: World, loaded: LoadedVerificationConfig = LOADED, **kw: Any) -> VerificationGate:
    return VerificationGate(w.registry, loaded, clock=lambda: w.now, **kw)


def base_draft(**over: Any) -> dict[str, Any]:
    draft: dict[str, Any] = {
        "proposed_video_title": "Nimbus 4 Tutorial: Image Editing and Coding Mode for Beginners",
        "topic_id": 11,
        "search_query_id": 101,
        "audience_search_intent": "tutorial",
        "supporting_evidence_ids": ["{{ev:off:nimbus4-launch}}", "{{ev:off:nimbus4-docs}}"],
        "factual_claims": [
            {
                "text": "Nimbus 4 was released by Aurora Labs on November 4, 2026.",
                "claim_type": "release_date",
                "subject": "Nimbus 4",
                "evidence_ids": ["{{ev:off:nimbus4-launch}}"],
            },
            {
                "text": "The Pro plan costs $20 per month.",
                "claim_type": "product_detail",
                "subject": "Nimbus 4",
                "evidence_ids": ["{{ev:off:nimbus4-launch}}"],
            },
        ],
        "recommendation_rationale": (
            "Beginners need a walkthrough of the new image editing feature."
        ),
    }
    draft.update(over)
    return draft


def claim(text: str, *ids: str, **kw: Any) -> dict[str, Any]:
    return {"text": text, "evidence_ids": list(ids), **kw}


def run(
    w: World, target: str, *steps: Any, gate: VerificationGate | None = None, **ctx: Any
) -> tuple[GateDecision, ScriptedGenerator]:
    generator = ScriptedGenerator(list(steps))
    decision = (gate or gate_for(w)).run(w.context(target, **ctx), generator)
    return decision, generator


def verdict(decision: GateDecision, fragment: str) -> Any:
    f = fragment.casefold()
    return next(v for v in decision.verdicts if f in v.text.casefold())
