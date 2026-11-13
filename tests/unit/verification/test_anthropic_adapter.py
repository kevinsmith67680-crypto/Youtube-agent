"""The optional Claude adapter, exercised with a fake client in the SDK's response shape.

No request leaves the machine: the real SDK is never imported here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from tests.unit.verification.helpers import base_draft, gate_for, world
from trend_agent.schemas.verification import AbstentionState, PublicationDecision
from trend_agent.verification.anthropic_generator import FALLBACK_BETA, AnthropicGenerator
from trend_agent.verification.generation import (
    GenerationStatus,
    build_request,
    fill_placeholders,
    llm_schema,
)


@dataclass
class Block:
    type: str
    text: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class Details:
    category: str | None


@dataclass
class Response:
    content: list[Block]
    stop_reason: str = "end_turn"
    model: str = "claude-opus-5-5"
    stop_details: Details | None = None


class RateLimitError(Exception):
    status_code = 429


class FakeMessages:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self.beta = type("Beta", (), {})()
        self.beta.messages = FakeMessages(responses)
        self.messages = FakeMessages(list(responses))


def _request() -> Any:
    w = world()
    ctx = w.context("nimbus")
    return ctx, build_request(ctx, 1, [], llm_schema())


def test_request_shape_structured_output_and_fallbacks() -> None:
    ctx, req = _request()
    ids = {r.evidence_key: r.evidence_id for r in ctx.records}
    text = json.dumps(fill_placeholders(base_draft(), ids))
    client = FakeClient([Response([Block("text", text)])])
    gen = AnthropicGenerator(api_key=None, client=client)
    result = gen.generate(req)
    assert result.status is GenerationStatus.OK
    assert result.model == "claude-opus-5-5"
    call = client.beta.messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["betas"] == [FALLBACK_BETA]
    assert call["fallbacks"] == "default"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["system"] == req.system
    assert "tools" not in call  # the model is given nothing that can act


def test_refusal_truncation_errors_and_tool_use_are_mapped() -> None:
    _, req = _request()
    refusal = Response([], stop_reason="refusal", stop_details=Details("cyber"))
    truncated = Response([Block("text", '{"proposed_video_title": "Nim')], stop_reason="max_tokens")
    tool = Response([Block("tool_use", name="run_code", input={"c": "x"}), Block("text", "{}")])
    gen = AnthropicGenerator(
        api_key=None,
        client=FakeClient([refusal, truncated, RateLimitError(), tool, Response([])]),
    )
    r = gen.generate(req)
    assert r.status is GenerationStatus.REFUSED
    assert "cyber" in (r.detail or "")
    assert gen.generate(req).status is GenerationStatus.INCOMPLETE
    err = gen.generate(req)
    assert err.status is GenerationStatus.ERROR
    assert err.transient
    t = gen.generate(req)
    assert t.tool_requests[0].name == "run_code"
    assert gen.generate(req).status is GenerationStatus.ERROR  # no text block


def test_missing_key_or_sdk_is_unavailable_not_a_crash() -> None:
    _, req = _request()
    r = AnthropicGenerator(api_key=None).generate(req)
    assert r.status is GenerationStatus.UNAVAILABLE


def test_adapter_output_still_goes_through_the_gate() -> None:
    w = world()
    ctx = w.context("nimbus")
    ids = {r.evidence_key: r.evidence_id for r in ctx.records}
    lying = fill_placeholders(
        base_draft(recommendation_rationale="Nimbus 4 gets 4 million searches every day."), ids
    )
    tool = Block("tool_use", name="sql", input={"q": "DROP TABLE x"})
    client = FakeClient([Response([tool, Block("text", json.dumps(lying))])])
    d = gate_for(w).run(ctx, AnthropicGenerator(api_key=None, client=client))
    r = d.recommendation
    assert r.verification_status is AbstentionState.PARTIALLY_VERIFIED
    assert r.publication is PublicationDecision.PUBLISH_WITH_LABELS
    assert "4 million" not in (r.recommendation_rationale or "")
    assert "tool_request_denied" in r.violations
    assert r.generator_model == "claude-opus-5-5"
    refused = FakeClient([Response([], stop_reason="refusal")])
    d = gate_for(w).run(ctx, AnthropicGenerator(api_key=None, client=refused))
    assert d.recommendation.verification_status is AbstentionState.SOURCE_UNAVAILABLE
