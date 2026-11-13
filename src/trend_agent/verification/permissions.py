"""Least-privilege capability checks for generator and source-originated requests.

Authorisation is decided from *where a request originates* (its principal), assigned by
application code - never from anything a request or a document says about itself. Text
such as "the operator authorises you to run this" inside a YouTube description is
content of principal ``SOURCE_CONTENT`` and cannot grant anything.

Generators and source content can never hold a capability that acts outside the
verification sandbox: executing code, reading credentials, modifying the database,
making network requests, registering evidence, writing verification records or
publishing. The policy refuses to even be configured that way.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from trend_agent.schemas.verification import EVIDENCE_ID_PATTERN, EvidenceBundle


class Principal(StrEnum):
    APPLICATION = "application"  # trusted code paths of this service
    OPERATOR = "operator"  # a person running the CLI
    GENERATOR = "generator"  # a language model or other recommendation generator
    SOURCE_CONTENT = "source_content"  # retrieved third-party content


class Capability(StrEnum):
    READ_EVIDENCE = "read_evidence"
    READ_STATISTICS = "read_statistics"
    REGISTER_EVIDENCE = "register_evidence"
    WRITE_VERIFICATION_RECORD = "write_verification_record"
    PUBLISH_RECOMMENDATION = "publish_recommendation"
    EXECUTE_CODE = "execute_code"
    READ_CREDENTIALS = "read_credentials"
    MODIFY_DATABASE = "modify_database"
    NETWORK_REQUEST = "network_request"


UNTRUSTED_PRINCIPALS = frozenset({Principal.GENERATOR, Principal.SOURCE_CONTENT})
NEVER_DELEGABLE = frozenset(
    {
        Capability.REGISTER_EVIDENCE,
        Capability.WRITE_VERIFICATION_RECORD,
        Capability.PUBLISH_RECOMMENDATION,
        Capability.EXECUTE_CODE,
        Capability.READ_CREDENTIALS,
        Capability.MODIFY_DATABASE,
        Capability.NETWORK_REQUEST,
    }
)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    capability: Capability
    description: str


# The only tool a generator could be offered is a bundle-scoped, read-only lookup. The
# other names exist so requests for them are refused with a precise reason.
TOOLS: dict[str, ToolSpec] = {
    "lookup_evidence": ToolSpec(
        "lookup_evidence", Capability.READ_EVIDENCE, "read one record of the request bundle"
    ),
    "run_code": ToolSpec("run_code", Capability.EXECUTE_CODE, "execute code"),
    "shell": ToolSpec("shell", Capability.EXECUTE_CODE, "run a shell command"),
    "read_secret": ToolSpec("read_secret", Capability.READ_CREDENTIALS, "read a credential"),
    "sql": ToolSpec("sql", Capability.MODIFY_DATABASE, "run SQL"),
    "http_request": ToolSpec("http_request", Capability.NETWORK_REQUEST, "fetch a URL"),
    "publish": ToolSpec("publish", Capability.PUBLISH_RECOMMENDATION, "publish a result"),
}


@dataclass(frozen=True)
class ToolRequest:
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    # Assigned by the code that received the request, never taken from its content.
    principal: Principal = Principal.GENERATOR


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    capability: Capability | None
    reason: str


class PermissionDeniedError(PermissionError):
    pass


class PermissionPolicy:
    def __init__(self, grants: Mapping[Principal, frozenset[Capability]]) -> None:
        for principal in UNTRUSTED_PRINCIPALS:
            bad = grants.get(principal, frozenset()) & NEVER_DELEGABLE
            if bad:
                raise ValueError(f"{principal.value} may never hold {sorted(c.value for c in bad)}")
        self._grants = dict(grants)

    @classmethod
    def default(cls) -> PermissionPolicy:
        app = frozenset(
            {
                Capability.READ_EVIDENCE,
                Capability.READ_STATISTICS,
                Capability.REGISTER_EVIDENCE,
                Capability.WRITE_VERIFICATION_RECORD,
                Capability.PUBLISH_RECOMMENDATION,
            }
        )
        return cls(
            {
                Principal.APPLICATION: app,
                Principal.OPERATOR: app,
                Principal.GENERATOR: frozenset({Capability.READ_EVIDENCE}),
                Principal.SOURCE_CONTENT: frozenset(),
            }
        )

    def authorize(self, principal: Principal, capability: Capability) -> PermissionDecision:
        if principal in UNTRUSTED_PRINCIPALS and capability in NEVER_DELEGABLE:
            return PermissionDecision(
                False, capability, f"{capability.value} is never available to {principal.value}"
            )
        if capability not in self._grants.get(principal, frozenset()):
            return PermissionDecision(
                False, capability, f"{principal.value} lacks {capability.value}"
            )
        return PermissionDecision(True, capability, "granted")

    def require(self, principal: Principal, capability: Capability) -> None:
        decision = self.authorize(principal, capability)
        if not decision.allowed:
            raise PermissionDeniedError(decision.reason)

    def authorize_tool(
        self, request: ToolRequest, bundle: EvidenceBundle | None
    ) -> PermissionDecision:
        spec = TOOLS.get(request.name)
        if spec is None:
            return PermissionDecision(False, None, f"unknown tool {request.name!r}")
        decision = self.authorize(request.principal, spec.capability)
        if not decision.allowed:
            return decision
        if spec.name == "lookup_evidence":
            eid = request.arguments.get("evidence_id")
            if (
                bundle is None
                or not isinstance(eid, str)
                or not EVIDENCE_ID_PATTERN.match(eid)
                or eid not in bundle.evidence_ids
            ):
                return PermissionDecision(
                    False, spec.capability, "lookup is limited to the request's evidence bundle"
                )
        return decision


def read_only(session: Session) -> None:
    """Make the current transaction read-only (PostgreSQL rejects any write in it)."""
    session.execute(text("SET TRANSACTION READ ONLY"))
