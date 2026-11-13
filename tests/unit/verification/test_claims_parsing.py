"""Claim extraction, draft parsing and the structured-output schema."""

from __future__ import annotations

import json
from typing import Any

import pytest

from trend_agent.schemas.verification import (
    ClaimOrigin,
    RecommendationDraft,
    ViolationCode,
)
from trend_agent.verification.claims import NumberKind, candidate_claims, extract
from trend_agent.verification.generation import (
    GenerationResult,
    GenerationStatus,
    ParseStatus,
    llm_schema,
    parse_draft,
)


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("Nimbus 4 gets 2 million searches per day", NumberKind.SEARCH_COUNT, 2e6),
        ("costs $1,999 with 32GB", NumberKind.CURRENCY, 1999.0),
        ("searches jumped 340% this week", NumberKind.PERCENT, 340.0),
        ("a severity of 87", NumberKind.STATISTIC, 87.0),
        ("p < 0.01", NumberKind.STATISTIC, 0.01),
        ("demand tripled", NumberKind.MULTIPLIER, 3.0),
        ("supports a 10M token context", NumberKind.QUANTITY, 1e7),
    ],
)
def test_numbers_are_typed(text: str, kind: NumberKind, value: float) -> None:
    assert any(n.kind is kind and n.value == value for n in extract(text).numbers)


def test_names_with_numbers_are_identifiers_not_statistics() -> None:
    f = extract("GPT-5, RTX 5090, Python 3.13 and Chapter 6 Season 2 arrived")
    assert not f.numbers
    assert {"GPT-5", "RTX 5090", "Python 3.13"} <= set(f.identifiers)


def test_dates_trend_release_and_certainty_markers() -> None:
    f = extract("Gemini 3 was released on November 18, 2025 according to Google")
    assert [(d.year, d.month, d.day) for d in f.dates] == [(2025, 11, 18)]
    assert f.release == "released"
    assert f.attributed
    t = extract("YouTube searches for it are exploding").trend
    assert t is not None
    assert t.search
    assert t.youtube
    assert t.growth
    disclaimed = extract("Video activity rose 3x but this is not search demand").trend
    assert disclaimed is not None
    assert not disclaimed.search
    assert extract("The item is rumored to arrive next week").speculative
    assert extract("Volt X9 will be released on December 2").release == "upcoming"
    assert not extract("This video helps beginners set up the tool.").factual


def test_candidates_cover_title_rationale_intent_and_uncertainties() -> None:
    draft = RecommendationDraft.model_validate(
        {
            "proposed_video_title": "A title",
            "search_query_id": 1,
            "audience_search_intent": "tutorial",
            "factual_claims": [{"text": "Declared claim.", "evidence_ids": []}],
            "recommendation_rationale": "First sentence. Second sentence with 3.5 numbers.",
            "audience_intent_description": "Viewers want setup help.",
            "uncertainties": ["The release may slip to 2027."],
        }
    )
    origins = [c.origin for c in candidate_claims(draft)]
    assert origins == [
        ClaimOrigin.DECLARED,
        ClaimOrigin.TITLE,
        ClaimOrigin.RATIONALE,
        ClaimOrigin.RATIONALE,
        ClaimOrigin.INTENT,
        ClaimOrigin.UNCERTAINTY,
    ]


def _ok(payload: Any) -> GenerationResult:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return GenerationResult(GenerationStatus.OK, text, "t")


VALID = {
    "proposed_video_title": "Nimbus 4 tutorial",
    "search_query_id": 101,
    "audience_search_intent": "tutorial",
}


def test_parse_handles_malformed_refused_truncated_and_empty() -> None:
    assert parse_draft(_ok("{not json")).status is ParseStatus.MALFORMED
    assert parse_draft(_ok("[1, 2]")).status is ParseStatus.MALFORMED
    assert parse_draft(_ok({"proposed_video_title": "x"})).status is ParseStatus.SCHEMA_INVALID
    refused = GenerationResult(GenerationStatus.REFUSED, None, "t", detail="refused")
    assert parse_draft(refused).status is ParseStatus.REFUSED
    cut = GenerationResult(GenerationStatus.INCOMPLETE, '{"proposed_video_title": "Nim', "t")
    assert parse_draft(cut).status is ParseStatus.INCOMPLETE
    # even a JSON-valid truncated generation is never accepted
    whole = GenerationResult(GenerationStatus.INCOMPLETE, json.dumps(VALID), "t")
    assert parse_draft(whole).draft is None
    assert (
        parse_draft(GenerationResult(GenerationStatus.OK, "  ", "t")).status
        is ParseStatus.NO_OUTPUT
    )
    fenced = parse_draft(_ok("```json\n" + json.dumps(VALID) + "\n```"))
    assert fenced.status is ParseStatus.OK


def test_parse_drops_unsupported_and_statistic_fields_and_self_verification() -> None:
    payload = {
        **VALID,
        "surge_score": 97,
        "search_volume": 50000,
        "notes": "x",
        "verification_status": "verified",
        "factual_claims": [{"text": "c", "evidence_ids": [], "confidence": 0.99}],
    }
    out = parse_draft(_ok(payload))
    assert out.status is ParseStatus.OK
    assert out.draft is not None
    assert out.draft.verification_status == "pending_verification"
    codes = [v.code for v in out.violations]
    assert codes.count(ViolationCode.STATISTIC_FIELD) == 3
    assert ViolationCode.UNSUPPORTED_FIELD in codes
    assert ViolationCode.SELF_VERIFICATION in codes
    assert "surge_score" not in out.draft.model_dump()


def test_schema_validity_is_not_verification() -> None:
    # A schema-valid draft can be entirely false; the draft carries no verified status.
    out = parse_draft(
        _ok(
            {
                **VALID,
                "factual_claims": [
                    {"text": "Nimbus 4 has 9 billion searches.", "evidence_ids": []}
                ],
            }
        )
    )
    assert out.status is ParseStatus.OK
    assert out.draft is not None
    assert out.draft.verification_status == "pending_verification"
    names = set(RecommendationDraft.model_fields)
    for forbidden in ("score", "severity", "volume", "growth", "p_value", "confidence", "fresh"):
        assert not any(forbidden in n for n in names)


def test_llm_schema_uses_the_structured_output_subset() -> None:
    schema = llm_schema()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            assert not {"minLength", "maxLength", "maxItems", "pattern"} & node.keys()
            if node.get("type") == "object" or "properties" in node:
                assert node["additionalProperties"] is False
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    assert "surge_score" not in json.dumps(schema)
