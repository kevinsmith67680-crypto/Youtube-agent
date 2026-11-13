"""Deterministic claim verification.

For every claim the verifier asks whether the *cited* records support *this* claim -
not merely whether they exist:

* every named entity of the claim is mentioned by a supporting record and exists
  (``entities.py``); unknown names must appear verbatim in a supporting record;
* every number (price, size, count, version-independent quantity) and every date
  appears in a supporting record with a matching value;
* release wording ("released", "coming", "announced") matches what a supporting record
  says about that entity;
* the claim's remaining content words are covered by the supporting records
  (``matching.term_coverage``), and all salient terms (capitalised words, numbers) are;
* trend, search and statistics wording is checked against the engine (``authority.py``).

Then every record of the request bundle is scanned for credible contradictions (denials,
different release dates, different prices/specs, releases still in the future). Status:

CONTRADICTED  a credible record conflicts with the claim (wins over any support);
VERIFIED      supported, with at least one credible record (official, curated,
              authoritative data) among the supporting records;
CORROBORATED  supported only by lower-trust records from >= 2 independent sources;
UNVERIFIED    anything else (including single low-trust sources and stale evidence).

A claim supported only by bundle records it did not cite is re-cited by code
(``RECITE``), so every published citation is one the verifier checked.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from trend_agent.schemas.verification import (
    AuthoritativeStatistics,
    ClaimAction,
    ClaimCertainty,
    ClaimOrigin,
    ClaimStatus,
    ClaimType,
    ClaimVerdict,
    FreshnessStatus,
    ViolationCode,
)
from trend_agent.verification import claims as cl
from trend_agent.verification.authority import check_trend
from trend_agent.verification.config import VerificationConfig
from trend_agent.verification.entities import (
    EntityCatalog,
    EntityCheck,
    EntityMention,
    EntityState,
    EntityVerifier,
    RecordView,
    denied_names,
    name_candidates,
    unresolved,
)
from trend_agent.verification.registry import ReferenceProblem, freshness_of
from trend_agent.verification.text import (
    NEUTRAL_VOCABULARY,
    contains_sequence,
    content_terms,
    fingerprint_text,
    tokens,
)
from trend_agent.verification.untrusted import SUSPICIOUS_OUTPUT

# Words handled by dedicated checks (release, certainty, attribution) or carrying no fact.
GLUE = frozenset(
    tokens(
        """released release releases launched launch launches available availability announced
        announce announcement confirmed confirm official officially rumored rumoured rumor rumour
        reportedly reported report reports according leak leaked said say says claim claims
        claimed expected upcoming coming soon now new latest week today will set arrive arrives
        arrived arriving out live drop dropped dropping shop item items also currently
        recently just first source sources post posted blog per stated states noted notes note
        video videos channel creator creators viewer viewers audience audiences people users
        topic topics tutorial guide explain explains explained help helps learn support supports
        supported include includes including feature features featuring offer offers add adds
        added introduce introduces let lets allow allows enable enables cost costs priced price
        run runs require requires work works give gives grant grants unlock unlocks has have
        come comes capable integrate integrates power powered build built within using use
        called named known dubbed titled name names"""
    )
)


_HEDGE = re.compile(
    r"\b(?:no|not|unconfirmed|unknown|unclear|uncertain|cannot|can't|isn't|aren't|without|"
    r"may|might|could|unverified)\b",
    re.IGNORECASE,
)


class SemanticVerifier(Protocol):
    """Optional second opinion (e.g. an NLI model or LLM judge). It can only downgrade."""

    name: str

    def entails(self, claim: str, evidence: Sequence[str]) -> bool | None: ...


@dataclass
class VerificationInputs:
    views: dict[str, RecordView]
    catalog: EntityCatalog
    stats: AuthoritativeStatistics
    query: str
    now: datetime
    config: VerificationConfig
    category: str | None = None
    topic_entity_keys: frozenset[str] = frozenset()
    grounding_terms: frozenset[str] = frozenset()
    problems: Mapping[str, ReferenceProblem] = field(default_factory=dict)
    allowed_urls: frozenset[str] = frozenset()


@dataclass
class _Support:
    ok: bool
    contributing: list[RecordView]
    missing: list[str]


class ClaimVerifier:
    def __init__(
        self, inputs: VerificationInputs, semantic: SemanticVerifier | None = None
    ) -> None:
        self.inputs = inputs
        self.semantic = semantic
        self.entities = EntityVerifier(
            inputs.catalog,
            inputs.views,
            now=inputs.now,
            category=inputs.category,
            topic_entity_keys=inputs.topic_entity_keys,
        )
        self._bundle_tokens: frozenset[str] = (
            frozenset().union(*(v.token_set for v in inputs.views.values()))
            if inputs.views
            else frozenset()
        )

    # ------------------------------------------------------------------ public
    def verify(self, claim: cl.CandidateClaim) -> ClaimVerdict:
        f = claim.features
        reasons: list[str] = []
        violations: list[ViolationCode] = []
        for eid in claim.cited_ids:
            p = self.inputs.problems.get(eid)
            if p is not None:
                violations.append(p.code)
                reasons.append(f"{eid}: {p.detail}")
        title = claim.origin is ClaimOrigin.TITLE
        mentions = self.inputs.catalog.find(claim.text)
        context = " ".join([claim.text, claim.subject or ""])
        checks = [self.entities.check_mention(m, context) for m in mentions]
        if claim.subject and not any(tokens(claim.subject) == tokens(m.text) for m in mentions):
            checks.append(self.entities.check_name(claim.subject, context))
        unknown = unresolved(name_candidates(claim.text, title_mode=title), mentions, claim.text)
        unknown_checks = [
            c
            for c in (self.entities.check_name(n, context) for n in unknown)
            if not self._composite_known(c, mentions)
        ]
        has_entities = bool(checks or unknown_checks)
        ctype = cl.infer_type(f, has_entities)
        if f.trend is None and claim.declared_type not in (None, ClaimType.OTHER, ClaimType.TREND):
            ctype = claim.declared_type
        # Core: what the recommendation is about. Trend wording is never core: the trend
        # statement is supplied by the engine and replaces whatever the generator wrote.
        core = title or (
            ctype
            in (
                ClaimType.ENTITY_EXISTENCE,
                ClaimType.RELEASE_STATUS,
                ClaimType.RELEASE_DATE,
                ClaimType.EVENT_DETAIL,
            )
            and self._subject_is_topic(claim, checks, unknown_checks)
        )
        unknown_found = [c for c in unknown_checks if c.state is EntityState.UNKNOWN]
        name_digits = set(tokens(self.inputs.query))
        for chk in checks:
            if chk.entity is not None:
                for n in chk.entity.names():
                    name_digits.update(tokens(n))
        name_numbers = [
            n for n in f.numbers if n.kind is cl.NumberKind.PLAIN and n.raw in name_digits
        ]
        factual_elements = (
            len(f.numbers) > len(name_numbers)
            or bool(f.dates or f.trend or f.release or f.denial or f.novelty)
            or f.feature_assertion
            or bool(f.urls)
        )
        is_factual = (
            factual_elements or bool(unknown_found) or claim.origin is ClaimOrigin.DECLARED or title
        )
        if SUSPICIOUS_OUTPUT.search(claim.text):
            violations.append(ViolationCode.SUSPICIOUS_OUTPUT)
            reasons.append("instruction-like or credential-related text in generator output")
        bad_urls = [u for u in f.urls if u.rstrip("/.,") not in self.inputs.allowed_urls]
        if bad_urls:
            violations.append(ViolationCode.UNLISTED_URL)
            reasons.append("URL not taken from the evidence: " + ", ".join(bad_urls))
        if (
            claim.origin is ClaimOrigin.UNCERTAINTY
            and _HEDGE.search(claim.text)
            and not (f.numbers or f.dates or f.urls or unknown_found or f.trend)
        ):
            is_factual = False  # a disclaimer ("not confirmed", "no direct measurement")
        if not is_factual and not violations:
            return self._verdict(
                claim,
                ctype,
                ClaimStatus.UNVERIFIED,
                ClaimAction.NOT_FACTUAL,
                reasons=("no factual element",),
            )

        cited = [self.inputs.views[e] for e in claim.cited_ids if e in self.inputs.views]
        trend_status: ClaimStatus | None = None
        if f.trend is not None:
            tc = check_trend(f, self.inputs.stats, now=self.inputs.now, config=self.inputs.config)
            trend_status = tc.status
            violations.extend(tc.violations)
            reasons.extend(tc.reasons)
        detail_numbers = [
            n
            for n in f.numbers
            if (f.trend is None or n.kind in (cl.NumberKind.CURRENCY, cl.NumberKind.QUANTITY))
            and not (n.kind is cl.NumberKind.PLAIN and n.raw in name_digits)
        ]
        specific = bool(detail_numbers or f.dates or f.release or unknown_found)
        needs_support = specific if title else f.trend is None or specific
        status = ClaimStatus.UNVERIFIED
        supporting: list[RecordView] = []
        recited = False
        if needs_support:
            sup = self._support(
                claim, f, checks, unknown_checks, detail_numbers, cited, coverage=not title
            )
            if not sup.ok:
                wide = self._support(
                    claim,
                    f,
                    checks,
                    unknown_checks,
                    detail_numbers,
                    list(self.inputs.views.values()),
                    coverage=not title,
                )
                if wide.ok:
                    recited = True
                    if not title:
                        violations.append(ViolationCode.CITATION_MISMATCH)
                        reasons.append("the cited records do not support the claim; re-cited")
                    sup = wide
            if sup.ok:
                supporting = sup.contributing
                status = self._status_from(supporting)
                if status is ClaimStatus.UNVERIFIED:
                    reasons.append("supported only by a single lower-trust source")
            else:
                reasons.extend(sup.missing)
        if trend_status is not None:
            if needs_support:
                status = _worst(status, trend_status)
            else:
                status = trend_status
                supporting = [
                    self.inputs.views[e]
                    for e in self.inputs.stats.evidence_ids
                    if e in self.inputs.views
                ]
        if title:
            ungrounded = self._ungrounded(claim.text)
            if ungrounded:
                violations.append(ViolationCode.UNGROUNDED_TERMS)
                reasons.append("title words not found in any evidence: " + ", ".join(ungrounded))
                status = ClaimStatus.UNVERIFIED
            elif not needs_support and trend_status is None:
                status = ClaimStatus.VERIFIED
            if not supporting:
                supporting = [
                    v
                    for v in self.inputs.views.values()
                    if any(v.mentions(c.entity) for c in checks if c.entity is not None)
                ]
        contradicting = self._contradictions(claim, f, checks, detail_numbers)
        if contradicting:
            status = ClaimStatus.CONTRADICTED
            reasons[:0] = [r for _, r in contradicting]  # the decisive reason comes first
        # Entity rules (a claim that itself denies a name is not asserting that it exists).
        own_denied = {tuple(tokens(n)) for n in denied_names(claim.text, self.inputs.catalog)}
        for chk in checks + unknown_checks:
            if tuple(tokens(chk.name)) in own_denied:
                continue
            if chk.state in (EntityState.UNKNOWN, EntityState.REJECTED):
                violations.append(ViolationCode.UNKNOWN_ENTITY)
                reasons.extend(chk.reasons or (f"unknown entity '{chk.name}'",))
                if status is not ClaimStatus.CONTRADICTED:
                    status = ClaimStatus.UNVERIFIED
            elif chk.state is EntityState.AMBIGUOUS:
                violations.append(ViolationCode.AMBIGUOUS_ENTITY)
                reasons.extend(chk.reasons)
                if status is not ClaimStatus.CONTRADICTED:
                    status = ClaimStatus.UNVERIFIED
            elif chk.state in (EntityState.RUMORED, EntityState.REPORTED) and ctype in (
                ClaimType.ENTITY_EXISTENCE,
                ClaimType.RELEASE_STATUS,
                ClaimType.RELEASE_DATE,
            ):
                violations.append(ViolationCode.UNCONFIRMED_ENTITY)
                reasons.append(f"'{chk.name}' is {chk.state.value}, not confirmed")
                if status is ClaimStatus.VERIFIED and not any(v.credible for v in supporting):
                    status = ClaimStatus.UNVERIFIED
            if chk.disputed_by:
                reasons.append(f"'{chk.name}' is disputed by {', '.join(chk.disputed_by)}")
        # Freshness and novelty.
        if status in (ClaimStatus.VERIFIED, ClaimStatus.CORROBORATED) and supporting:
            time_sensitive = f.novelty or ctype in (
                ClaimType.RELEASE_STATUS,
                ClaimType.RELEASE_DATE,
                ClaimType.EVENT_DETAIL,
            )
            if time_sensitive:
                fresh = [
                    freshness_of(v.record, self.inputs.now, self.inputs.config) for v in supporting
                ]
                if all(x is FreshnessStatus.STALE for x in fresh):
                    status = ClaimStatus.UNVERIFIED
                    violations.append(ViolationCode.STALE_EVIDENCE)
                    reasons.append("all supporting evidence is stale")
            if f.novelty:
                newest = max(
                    (v.record.published_at or v.record.collected_at for v in supporting),
                    default=None,
                )
                window = self.inputs.config.freshness.novelty_window
                if newest is not None and self.inputs.now - newest > window:
                    status = ClaimStatus.UNVERIFIED
                    violations.append(ViolationCode.OUTDATED)
                    reasons.append(
                        f"described as new, but the newest supporting record is from "
                        f"{newest.date().isoformat()}"
                    )
        # Optional semantic second opinion (can only downgrade).
        if (
            self.semantic is not None
            and status in (ClaimStatus.VERIFIED, ClaimStatus.CORROBORATED)
            and supporting
        ):
            judged = self.semantic.entails(claim.text, [v.record.excerpt or "" for v in supporting])
            if judged is False:
                status = ClaimStatus.UNVERIFIED
                reasons.append(f"semantic check ({self.semantic.name}) found no entailment")
        if (
            ViolationCode.SUSPICIOUS_OUTPUT in violations
            or ViolationCode.UNLISTED_URL in violations
        ) and status is not ClaimStatus.CONTRADICTED:
            status = ClaimStatus.UNVERIFIED
        # Attribution requirements.
        hedged = claim.certainty is not ClaimCertainty.CONFIRMED or f.speculative
        attribution_ok = self._attribution_ok(claim, supporting)
        if hedged and not (f.attributed or claim.attribution):
            violations.append(ViolationCode.MISSING_ATTRIBUTION)
            reasons.append("speculative or reported information without attribution")
        action, label, published = self._action(
            claim, f, status, supporting, hedged, attribution_ok, recited, violations, ctype
        )
        return self._verdict(
            claim,
            ctype,
            status,
            action,
            supporting=tuple(v.record.evidence_id for v in supporting),
            contradicting=tuple(dict.fromkeys(e for e, _ in contradicting)),
            reasons=tuple(dict.fromkeys(reasons)),
            violations=tuple(dict.fromkeys(violations)),
            label=label,
            published=published,
            core=core,
        )

    def _composite_known(self, chk: EntityCheck, mentions: Sequence[EntityMention]) -> bool:
        """'Season 7 of Skyforge Arena' is known when each part is (catalogue or evidence)."""
        if chk.state is not EntityState.UNKNOWN:
            return False
        parts = [p for p in re.split(r"\s+(?:of|the|and|de)\s+", chk.name) if p.strip()]
        if len(parts) < 2:
            return False
        known = {" ".join(tokens(m.text)) for m in mentions}
        for part in parts:
            key = " ".join(tokens(part))
            if key in known or self.inputs.catalog.lookup(part):
                continue
            if any(v.contains(part) for v in self.inputs.views.values()):
                continue
            if all(t.isdigit() or t in NEUTRAL_VOCABULARY for t in tokens(part)):
                continue
            return False
        return True

    def _subject_is_topic(
        self,
        claim: cl.CandidateClaim,
        checks: Sequence[EntityCheck],
        unknown: Sequence[EntityCheck],
    ) -> bool:
        """Is the claim *about* a topic entity (its declared or first-named subject)?"""
        topic = self.inputs.topic_entity_keys
        if claim.subject:
            st = tokens(claim.subject)
            return any(
                c.entity is not None
                and c.entity.key in topic
                and any(tokens(n) == st for n in c.entity.names())
                for c in checks
            )
        toks = tokens(claim.text)
        first: tuple[int, bool] | None = None
        for c in [*checks, *unknown]:
            names = c.entity.names() if c.entity else (c.name,)
            for n in names:
                nt = tokens(n)
                for i in range(len(toks) - len(nt) + 1):
                    if nt and toks[i : i + len(nt)] == nt:
                        is_topic = c.entity is not None and c.entity.key in topic
                        if first is None or i < first[0]:
                            first = (i, is_topic)
                        break
        return bool(first and first[1])

    # ---------------------------------------------------------------- support
    def _terms(
        self, text: str, checks: Sequence[EntityCheck], unknown: Sequence[EntityCheck]
    ) -> tuple[list[str], list[str]]:
        name_tokens: set[str] = set()
        for chk in [*checks, *unknown]:
            for n in chk.entity.names() if chk.entity else (chk.name,):
                name_tokens.update(tokens(n))
        number_tokens = set(tokens(" ".join(n.raw for n in cl.numbers(text))))
        date_tokens = set(tokens(" ".join(d.raw for d in cl.dates(text))))
        terms = [
            t
            for t in content_terms(text)
            if t not in GLUE
            and t not in name_tokens
            and t not in number_tokens
            and t not in date_tokens
        ]
        salient = []
        for word in text.split()[1:]:
            w = word.strip(".,:;!?()[]\"'")
            if w[:1].isupper() or any(ch.isdigit() for ch in w):
                for t in tokens(w):
                    if t in terms:
                        salient.append(t)
        return terms, salient

    def _support(
        self,
        claim: cl.CandidateClaim,
        f: cl.Features,
        checks: Sequence[EntityCheck],
        unknown: Sequence[EntityCheck],
        detail_numbers: Sequence[cl.NumberMention],
        views: Sequence[RecordView],
        *,
        coverage: bool = True,
    ) -> _Support:
        resolved = [c for c in checks if c.entity is not None]
        # Measurements and surge records describe a query, not the world: they never
        # support product, release or existence claims.
        views = [v for v in views if v.statement]
        if resolved or unknown:
            relevant = [
                v
                for v in views
                if any(v.mentions(c.entity) for c in resolved if c.entity)
                or any(v.contains(u.name) for u in unknown)
            ]
        else:
            relevant = list(views)
        missing: list[str] = []
        if not relevant:
            return _Support(False, [], ["no cited record mentions the claim's subject"])
        contributing: set[str] = set()  # records matching a specific element
        naming: set[str] = set()  # records that merely name the subject
        for c in resolved:
            assert c.entity is not None
            hits = [v for v in relevant if v.mentions(c.entity)]
            if not hits:
                missing.append(f"no supporting record mentions {c.entity.canonical_name}")
            naming.update(v.record.evidence_id for v in hits)
        for u in unknown:
            hits = [v for v in relevant if v.contains(u.name)]
            if not hits:
                missing.append(f"no supporting record mentions '{u.name}'")
            naming.update(v.record.evidence_id for v in hits)
        for n in detail_numbers:
            hits = [
                v
                for v in relevant
                if any(_number_match(n, m, self.inputs.config) for m in v.numbers)
            ]
            if not hits:
                missing.append(f"no supporting record states {n.raw}")
            contributing.update(v.record.evidence_id for v in hits)
        for d in f.dates:
            hits = [v for v in relevant if any(d.agrees(x) for x in v.dates)]
            if not hits:
                missing.append(f"no supporting record states the date {d.raw}")
            contributing.update(v.record.evidence_id for v in hits)
        if f.release:
            names = [n for c in resolved if c.entity for n in c.entity.names()] + [
                u.name for u in unknown
            ]
            hits = [v for v in relevant if self._release_matches(v, f.release, names)]
            if not hits:
                missing.append(f"no supporting record says it is {f.release}")
            contributing.update(v.record.evidence_id for v in hits)
        terms, salient = self._terms(claim.text, checks, unknown) if coverage else ([], [])
        union = frozenset().union(*(v.token_set for v in relevant))
        if terms:
            covered = [t for t in terms if t in union]
            share = len(covered) / len(terms)
            if share < self.inputs.config.matching.term_coverage:
                missing.append(
                    f"only {share:.0%} of the claim's content is in the supporting records "
                    f"(missing: {', '.join(t for t in terms if t not in union)})"
                )
            lost = [t for t in salient if t not in union]
            if lost:
                missing.append("salient terms not in the supporting records: " + ", ".join(lost))
            best = max((sum(t in v.token_set for t in terms) for v in relevant), default=0)
            for v in relevant:
                # Cite the records that carry the most of the claim's content.
                if best and sum(t in v.token_set for t in terms) >= max(1, best - 1):
                    contributing.add(v.record.evidence_id)
        elif coverage and not (resolved or unknown or detail_numbers or f.dates or f.release):
            missing.append("nothing in the claim can be matched to evidence")
        if not contributing:
            contributing = naming
        chosen = [v for v in relevant if v.record.evidence_id in contributing]
        if not chosen:
            missing.append("no record matches the claim's elements")
        return _Support(not missing, chosen, missing)

    def _release_matches(self, v: RecordView, level: str, names: Sequence[str]) -> bool:
        now = self.inputs.now.date()
        for fact in v.record.facts:
            if (
                fact.kind.value == "release_date"
                and isinstance(fact.value, str)
                and (not names or fact.subject in names)
            ):
                d = datetime.fromisoformat(fact.value[:10]).date()
                if level == "released" and d <= now:
                    return True
                if level in ("upcoming", "announced") and d > now:
                    return True
        pool = v.sentences_about(names) if names else v.sentences
        for s in pool:
            w = cl.release_wording(s)
            if w is None or cl.has_denial(s):
                continue
            if w == level or (level == "announced" and w in ("released", "upcoming")):
                return True
        return False

    def _status_from(self, supporting: Sequence[RecordView]) -> ClaimStatus:
        if any(v.credible for v in supporting):
            return ClaimStatus.VERIFIED
        independent = {v.source_identity for v in supporting if v.source_identity}
        if len(independent) >= self.inputs.config.matching.min_independent_sources:
            return ClaimStatus.CORROBORATED
        return ClaimStatus.UNVERIFIED

    # ---------------------------------------------------------- contradictions
    def _contradictions(
        self,
        claim: cl.CandidateClaim,
        f: cl.Features,
        checks: Sequence[EntityCheck],
        detail_numbers: Sequence[cl.NumberMention],
    ) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        now = self.inputs.now.date()
        negated = f.denial
        claim_tokens = tokens(claim.text)
        # A claim that itself denies the name ("There is no X") agrees with the denial.
        own = {tuple(tokens(n)) for n in denied_names(claim.text, self.inputs.catalog)}
        if not negated:
            for key, ids in self.entities.denied.items():
                if key not in own and contains_sequence(claim_tokens, key):
                    out.extend((e, f"a credible source denies '{' '.join(key)}'") for e in ids)
        for chk in checks:
            if chk.entity is None:
                continue
            name = chk.entity.canonical_name
            if chk.conflicts and (f.release is not None or f.dates):
                out.extend((e, chk.conflicts[0]) for _, e in chk.release_dates)
            if chk.release_dates and len({d for d, _ in chk.release_dates}) == 1:
                d0 = chk.release_dates[0][0]
                src = [e for _, e in chk.release_dates]
                if f.release == "released" and d0 > now and not negated:
                    out.extend(
                        (e, f"{name} is scheduled for {d0.isoformat()}, not yet released")
                        for e in src
                    )
                if f.release == "upcoming" and d0 <= now and not negated:
                    out.extend((e, f"{name} was already released on {d0.isoformat()}") for e in src)
                if f.release or claim.declared_type in (ClaimType.RELEASE_DATE,):
                    for d in f.dates:
                        probe = cl.DateMention(d0.isoformat(), d0.year, d0.month, d0.day)
                        if not d.agrees(probe):
                            out.extend(
                                (e, f"credible release date is {d0.isoformat()}, not {d.raw}")
                                for e in src
                            )
            for n in detail_numbers:
                if n.kind not in (cl.NumberKind.CURRENCY, cl.NumberKind.QUANTITY):
                    continue
                for v in self.inputs.views.values():
                    if not v.credible or not v.mentions(chk.entity):
                        continue
                    same = [m for m in v.numbers if m.kind is n.kind and _unit(m) == _unit(n)]
                    if same and not any(_number_match(n, m, self.inputs.config) for m in same):
                        out.append(
                            (
                                v.record.evidence_id,
                                f"credible record states {same[0].raw}, not {n.raw}",
                            )
                        )
        return out

    # ---------------------------------------------------------------- helpers
    def _ungrounded(self, text: str) -> list[str]:
        allowed = self.inputs.grounding_terms | self._bundle_tokens
        vocab = NEUTRAL_VOCABULARY | frozenset(self.inputs.config.vocabulary.title_neutral)
        extra_stops = frozenset(self.inputs.config.vocabulary.stopwords)
        out = []
        for t in content_terms(text, extra_stops):
            if t in vocab or t in allowed or t.isdigit():
                continue
            out.append(t)
        return out

    def _attribution_ok(self, claim: cl.CandidateClaim, supporting: Sequence[RecordView]) -> bool:
        said = tokens(" ".join([claim.text, claim.attribution or ""]))
        for v in supporting:
            r = v.record
            for who in (r.publisher, r.author, r.provider_name):
                if who and any(t in said for t in tokens(who) if len(t) > 2):
                    return True
        return False

    def _action(
        self,
        claim: cl.CandidateClaim,
        f: cl.Features,
        status: ClaimStatus,
        supporting: Sequence[RecordView],
        hedged: bool,
        attribution_ok: bool,
        recited: bool,
        violations: Sequence[ViolationCode],
        ctype: ClaimType,
    ) -> tuple[ClaimAction, str | None, str | None]:
        title = claim.origin is ClaimOrigin.TITLE
        if status is ClaimStatus.CONTRADICTED:
            return ClaimAction.REMOVE, None, None
        if ctype is ClaimType.TREND and not title:
            if status is ClaimStatus.VERIFIED:
                return ClaimAction.REPLACE_AUTHORITATIVE, None, self.inputs.stats.statement
            return ClaimAction.REMOVE, None, None
        if title:
            if status is ClaimStatus.VERIFIED and not (ctype is ClaimType.TREND and f.numbers):
                return ClaimAction.KEEP, None, claim.text
            return ClaimAction.REMOVE, None, None
        publishers = sorted({(v.record.publisher or v.record.provider_name) for v in supporting})
        if status is ClaimStatus.VERIFIED:
            return (ClaimAction.RECITE if recited else ClaimAction.KEEP), None, claim.text
        if status is ClaimStatus.CORROBORATED:
            label = (
                f"Reported by {len(publishers)} independent sources ({', '.join(publishers)}); "
                "not confirmed by an official source."
            )
            return ClaimAction.KEEP_ATTRIBUTED, label, _attributed(claim.text, publishers, f)
        blocking = {
            ViolationCode.UNKNOWN_ENTITY,
            ViolationCode.AMBIGUOUS_ENTITY,
            ViolationCode.SUSPICIOUS_OUTPUT,
            ViolationCode.UNLISTED_URL,
            ViolationCode.STALE_EVIDENCE,
            ViolationCode.OUTDATED,
            ViolationCode.EVIDENCE_INTEGRITY,
        }
        if supporting and hedged and attribution_ok and not blocking & set(violations):
            label = f"Unconfirmed: reported by {', '.join(publishers)}; not verified."
            return ClaimAction.KEEP_ATTRIBUTED, label, _attributed(claim.text, publishers, f)
        return ClaimAction.REMOVE, None, None

    def _verdict(
        self,
        claim: cl.CandidateClaim,
        ctype: ClaimType,
        status: ClaimStatus,
        action: ClaimAction,
        *,
        supporting: tuple[str, ...] = (),
        contradicting: tuple[str, ...] = (),
        reasons: tuple[str, ...] = (),
        violations: tuple[ViolationCode, ...] = (),
        label: str | None = None,
        published: str | None = None,
        core: bool = False,
    ) -> ClaimVerdict:
        if action is ClaimAction.NOT_FACTUAL:
            published = claim.text
        return ClaimVerdict(
            index=claim.index,
            origin=claim.origin,
            field=claim.field,
            text=claim.text,
            claim_type=ctype,
            certainty=claim.certainty,
            status=status,
            action=action,
            cited_ids=claim.cited_ids,
            supporting_ids=supporting,
            contradicting_ids=contradicting,
            reasons=reasons,
            violations=violations,
            uncertainty_label=label,
            published_text=published,
            core=core,
            fingerprint=fingerprint_text(claim.text),
        )


def _attributed(text: str, publishers: Sequence[str], f: cl.Features) -> str:
    if f.attributed:
        return text
    return f"{text.rstrip('.')} (reported by {', '.join(publishers)}; unconfirmed)."


def _unit(n: cl.NumberMention) -> str | None:
    return n.unit.lower().rstrip("s") if n.unit else None


def _number_match(
    claim: cl.NumberMention, record: cl.NumberMention, config: VerificationConfig
) -> bool:
    if (
        record.kind not in (claim.kind, cl.NumberKind.PLAIN)
        and claim.kind is not cl.NumberKind.PLAIN
    ):
        return False
    cu, ru = _unit(claim), _unit(record)
    if cu and ru and cu != ru and record.kind is not cl.NumberKind.PLAIN:
        return False
    tol = max(1e-9, config.matching.relative_tolerance * abs(claim.value))
    return abs(claim.value - record.value) <= tol


_ORDER = {
    ClaimStatus.VERIFIED: 3,
    ClaimStatus.CORROBORATED: 2,
    ClaimStatus.UNVERIFIED: 1,
    ClaimStatus.CONTRADICTED: 0,
}


def _worst(a: ClaimStatus, b: ClaimStatus) -> ClaimStatus:
    return a if _ORDER[a] <= _ORDER[b] else b
