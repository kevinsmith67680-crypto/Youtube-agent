"""Validated structured outputs for the optional LLM analysis assistant.

The schema has no numeric fields at all: an LLM cannot supply search volumes, surge
metrics, scores, dates or counts. Unknown fields are dropped and recorded; malformed,
truncated or refused outputs are typed failures, never partial successes. Valid JSON is
not factual correctness - every sentence is verified afterwards.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from trend_agent.schemas.search_intent import PrimaryIntent, RequirementPriority, SubIntent

ASSIST_VERSION: Literal["analysis-assist/1.0"] = "analysis-assist/1.0"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IntentSuggestion(_Model):
    primary_intent: PrimaryIntent
    sub_intent: SubIntent | None = None
    rationale: str = Field(default="", max_length=300)


class RequirementSuggestion(_Model):
    requirement: str = Field(min_length=3, max_length=160)
    priority: RequirementPriority = RequirementPriority.IMPORTANT
    cues: list[str] = Field(default_factory=list, max_length=8)


class GapHypothesis(_Model):
    text: str = Field(min_length=5, max_length=400)
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    requirement_keys: list[str] = Field(default_factory=list, max_length=6)


class AnalysisAssistOutput(_Model):
    schema_version: Literal["analysis-assist/1.0"] = ASSIST_VERSION
    intent_suggestions: list[IntentSuggestion] = Field(default_factory=list, max_length=3)
    additional_requirements: list[RequirementSuggestion] = Field(default_factory=list, max_length=3)
    gap_hypotheses: list[GapHypothesis] = Field(default_factory=list, max_length=5)
    summary: str = Field(default="", max_length=1200)
    uncertainties: list[str] = Field(default_factory=list, max_length=6)


_NUMERIC_FIELD = re.compile(
    r"(score|severity|volume|growth|surge|views?|count|percent|rate|searches|confidence|"
    r"probability|rank|position|date)",
    re.IGNORECASE,
)
_UNSUPPORTED = {
    "minLength",
    "maxLength",
    "maxItems",
    "minItems",
    "minimum",
    "maximum",
    "pattern",
    "default",
    "title",
    "exclusiveMinimum",
    "exclusiveMaximum",
}


def structured_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema in the subset structured-output APIs accept (re-validated locally)."""

    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: clean(v) for k, v in node.items() if k not in _UNSUPPORTED}
            if out.get("type") == "object" or "properties" in out:
                out["additionalProperties"] = False
            return out
        if isinstance(node, list):
            return [clean(v) for v in node]
        return node

    result: dict[str, Any] = clean(model.model_json_schema())
    return result


@dataclass
class ParsedAssist:
    output: AnalysisAssistOutput | None
    status: str  # ok | malformed | schema_invalid | incomplete | refused | no_output
    violations: list[str] = field(default_factory=list)
    detail: str = ""


def parse_assist(raw: str | None, *, status: str) -> ParsedAssist:
    if status == "refused":
        return ParsedAssist(None, "refused", detail="the model declined")
    if not raw or not raw.strip():
        return ParsedAssist(None, "incomplete" if status == "incomplete" else "no_output")
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(\{.*\})\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        kind = "incomplete" if status == "incomplete" else "malformed"
        return ParsedAssist(None, kind, [f"invalid JSON: {exc.msg}"], exc.msg)
    if not isinstance(data, dict):
        return ParsedAssist(None, "malformed", ["top level is not an object"])
    violations: list[str] = []
    allowed = set(AnalysisAssistOutput.model_fields)
    for key in list(data):
        if key not in allowed:
            kind = "statistic_field" if _NUMERIC_FIELD.search(key) else "unsupported_field"
            violations.append(f"{kind}: dropped {key!r}")
            del data[key]
    for list_key, model in (
        ("intent_suggestions", IntentSuggestion),
        ("additional_requirements", RequirementSuggestion),
        ("gap_hypotheses", GapHypothesis),
    ):
        items = data.get(list_key)
        if isinstance(items, list):
            keep = set(model.model_fields)
            for item in items:
                if isinstance(item, dict):
                    for key in [k for k in item if k not in keep]:
                        violations.append(f"unsupported_field: dropped {list_key}.{key}")
                        del item[key]
    try:
        out = AnalysisAssistOutput.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        return ParsedAssist(None, "schema_invalid", [*violations, f"{where}: {first.get('msg')}"])
    if status == "incomplete":
        return ParsedAssist(None, "incomplete", [*violations, "truncated generation rejected"])
    return ParsedAssist(out, "ok", violations)
