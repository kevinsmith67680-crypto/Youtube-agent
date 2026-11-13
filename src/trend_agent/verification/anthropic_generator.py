"""Optional Claude adapter for recommendation drafts.

The ``anthropic`` SDK is not a project dependency; it is imported lazily and the adapter
reports ``UNAVAILABLE`` when it (or an API key) is missing. The adapter only produces
draft text: it sends no tools, so the model cannot act, and everything it returns goes
through ``parse_draft`` and the verification gate like any other generator's output.

Request shape (Messages API):

* structured output via ``output_config={"format": {"type": "json_schema", ...}}`` with
  the draft schema in the subset the API accepts (``generation.llm_schema``);
* ``fallbacks="default"`` with the ``server-side-fallback-2026-07-01`` beta, so a request
  declined by the model's safety classifiers is retried server-side on the fallback
  model Anthropic recommends for that refusal category (disable with
  ``generator.server_side_fallbacks: false``);
* ``stop_reason`` is checked before the content is read: ``refusal`` and ``max_tokens``
  are mapped to REFUSED / INCOMPLETE, never parsed as a valid draft.

Not exercised against the live API in this repository's test-suite (no key, no network);
tests use a fake client with the SDK's response shape.
"""

from __future__ import annotations

from typing import Any

from trend_agent.verification.generation import (
    GenerationRequest,
    GenerationResult,
    GenerationStatus,
)
from trend_agent.verification.permissions import Principal, ToolRequest

FALLBACK_BETA = "server-side-fallback-2026-07-01"
_TRANSIENT = {
    "RateLimitError",
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
    "OverloadedError",
    "ServiceUnavailableError",
}


class AnthropicGenerator:
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str = "claude-opus-5-5",
        max_tokens: int = 8000,
        timeout_seconds: float = 120.0,
        server_side_fallbacks: bool = True,
        client: Any = None,
    ) -> None:
        self.model: str | None = model
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._timeout = timeout_seconds
        self._fallbacks = server_side_fallbacks
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise LookupError("TREND_AGENT_ANTHROPIC_API_KEY is not set")
        try:
            import anthropic  # type: ignore[import-not-found,unused-ignore]
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise LookupError("the 'anthropic' package is not installed") from exc
        self._client = anthropic.Anthropic(
            api_key=self._api_key, timeout=self._timeout, max_retries=2
        )
        return self._client

    def generate(self, request: GenerationRequest) -> GenerationResult:
        try:
            client = self._get_client()
        except LookupError as exc:
            return GenerationResult(
                GenerationStatus.UNAVAILABLE, None, self.name, self.model, detail=str(exc)
            )
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.prompt}],
            "output_config": {"format": {"type": "json_schema", "schema": request.schema}},
        }
        try:
            if self._fallbacks:
                response = client.beta.messages.create(
                    **kwargs, betas=[FALLBACK_BETA], fallbacks="default"
                )
            else:
                response = client.messages.create(**kwargs)
        except Exception as exc:  # the SDK's typed errors; classified by name (lazy import)
            name = type(exc).__name__
            status = getattr(exc, "status_code", None)
            transient = name in _TRANSIENT or status in (408, 409, 429, 500, 502, 503, 504, 529)
            return GenerationResult(
                GenerationStatus.ERROR,
                None,
                self.name,
                self.model,
                detail=f"{name}{f' ({status})' if status else ''}",
                transient=transient,
            )
        served = getattr(response, "model", None) or self.model
        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details is not None else None
            return GenerationResult(
                GenerationStatus.REFUSED,
                None,
                self.name,
                served,
                stop_reason=stop,
                detail=f"refused (category: {category or 'unspecified'})",
            )
        blocks = list(getattr(response, "content", None) or [])
        texts = [b.text for b in blocks if getattr(b, "type", None) == "text"]
        tools = tuple(
            ToolRequest(
                name=str(getattr(b, "name", "?")),
                arguments=dict(getattr(b, "input", None) or {}),
                principal=Principal.GENERATOR,
            )
            for b in blocks
            if getattr(b, "type", None) == "tool_use"
        )
        raw = texts[0] if texts else None
        if stop == "max_tokens":
            return GenerationResult(
                GenerationStatus.INCOMPLETE,
                raw,
                self.name,
                served,
                stop_reason=stop,
                detail="max_tokens reached",
                tool_requests=tools,
            )
        if raw is None:
            return GenerationResult(
                GenerationStatus.ERROR,
                None,
                self.name,
                served,
                stop_reason=stop,
                detail="no text content",
                tool_requests=tools,
            )
        return GenerationResult(
            GenerationStatus.OK, raw, self.name, served, stop_reason=stop, tool_requests=tools
        )
