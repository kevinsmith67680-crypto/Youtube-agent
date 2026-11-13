"""Entity verification: canonical resolution, existence, release status and conflicts.

Before a claim about a product, model, game event or item can be verified, every named
entity in it is checked:

* **resolution** against the Phase 2 entity catalogue (canonical names and aliases) -
  several candidates make the name AMBIGUOUS unless the topic or the evidence pins one;
* **existence** - a name that is neither catalogued nor present in any evidence text is
  UNKNOWN (this is how invented products and fake in-game items are caught);
* **confirmation** - CONFIRMED needs a credible record (official source, curated entry);
  lower-trust mentions only make it REPORTED (>= 2 independent sources) or RUMORED;
* **release status and dates** from credible records only, with disagreements recorded
  as conflicts and credible denials ("the leak is fake", "delayed") recorded as such.

Nothing here fills in missing details: an attribute no record states stays unknown.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from trend_agent.discovery.models import EntityOrigin, EntityStatus
from trend_agent.schemas.verification import (
    CREDIBLE,
    EvidenceRecord,
    FactKind,
    TrustLevel,
)
from trend_agent.verification import claims as claim_text
from trend_agent.verification.text import (
    NEUTRAL_VOCABULARY,
    STOPWORDS,
    contains_sequence,
    sentences,
    tokens,
)

# Platforms and generic proper nouns that are not product claims by themselves.
WELL_KNOWN = frozenset(
    """youtube google reddit github twitter x tiktok instagram facebook discord twitch steam
    playstation xbox nintendo windows macos linux android ios chatgpt""".split()
)
COMMON_ACRONYMS = frozenset(
    """ai api apis gpu gpus cpu cpus pc pcs tv diy faq vs ui ux npc npcs xp dlc fps rpg mmo llm
    llms sdk cli ide os usb ram ssd hdr vr ar ml nlp seo ceo cto io ok id url urls html css js
    json sql aws gcp usa uk eu new top best how why what free pro""".split()
)


@dataclass(frozen=True)
class CatalogEntity:
    key: str
    canonical_name: str
    entity_type: str
    category: str
    status: EntityStatus
    origin: EntityOrigin
    aliases: tuple[str, ...] = ()
    release_date: date | None = None
    release_level: str | None = None
    version: str | None = None
    synthetic: bool = False
    evidence_id: str | None = None

    def names(self) -> tuple[str, ...]:
        return (self.canonical_name, *self.aliases)


@dataclass(frozen=True)
class EntityMention:
    text: str
    candidates: tuple[CatalogEntity, ...]
    start: int
    end: int


class EntityCatalog:
    def __init__(self, entities: Iterable[CatalogEntity] = ()) -> None:
        self._entities: dict[str, CatalogEntity] = {}
        self._index: dict[tuple[str, ...], list[CatalogEntity]] = {}
        self._max = 1
        for e in entities:
            self.add(e)

    def add(self, entity: CatalogEntity) -> None:
        self._entities[entity.key] = entity
        for name in entity.names():
            key = tuple(tokens(name))
            if not key:
                continue
            bucket = self._index.setdefault(key, [])
            if all(b.key != entity.key for b in bucket):
                bucket.append(entity)
            self._max = max(self._max, len(key))

    def entities(self) -> list[CatalogEntity]:
        return list(self._entities.values())

    def get(self, key: str) -> CatalogEntity | None:
        return self._entities.get(key)

    def lookup(self, name: str) -> list[CatalogEntity]:
        return list(self._index.get(tuple(tokens(name)), []))

    def find(self, text: str) -> list[EntityMention]:
        """Longest, non-overlapping catalogue matches (token positions)."""
        toks = tokens(text)
        out: list[EntityMention] = []
        i = 0
        while i < len(toks):
            hit = None
            for n in range(min(self._max, len(toks) - i), 0, -1):
                key = tuple(toks[i : i + n])
                if key in self._index:
                    hit = (n, self._index[key])
                    break
            if hit is None:
                i += 1
                continue
            n, cands = hit
            out.append(EntityMention(" ".join(toks[i : i + n]), tuple(cands), i, i + n))
            i += n
        return out


class EntityState(StrEnum):
    CONFIRMED = "confirmed"  # credible record (official / curated / authoritative)
    REPORTED = "reported"  # >= 2 independent lower-trust sources, not confirmed
    RUMORED = "rumored"  # a single low-trust source or rumour wording
    UNKNOWN = "unknown"  # not catalogued and not present in any evidence
    AMBIGUOUS = "ambiguous"  # several catalogued entities share the name
    REJECTED = "rejected"  # the catalogue rejected it (e.g. ungrounded proposal)


@dataclass(frozen=True)
class EntityCheck:
    name: str
    state: EntityState
    entity: CatalogEntity | None
    candidates: tuple[str, ...] = ()
    release: str = "unknown"  # released | upcoming | announced | unknown
    release_dates: tuple[tuple[date, str], ...] = ()
    mentioning_ids: tuple[str, ...] = ()
    credible_ids: tuple[str, ...] = ()
    denied_by: tuple[str, ...] = ()
    disputed_by: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def exists(self) -> bool:
        return self.state in (EntityState.CONFIRMED, EntityState.REPORTED)


_TODAY = re.compile(r"\b(?:today|available now|now available|out now|is live now)\b", re.I)
_NON_STATEMENT = frozenset(
    {
        "measurement",
        "surge_detection",
        "statistical_calculation",
        "search_suggestion",
        "related_query",
        "synthetic_test_data",
    }
)


@dataclass
class RecordView:
    """Pre-tokenised evidence record used by entity and claim checks."""

    record: EvidenceRecord
    tokens: list[str] = field(default_factory=list)
    token_set: frozenset[str] = frozenset()
    sentences: list[str] = field(default_factory=list)
    numbers: tuple[claim_text.NumberMention, ...] = ()
    dates: tuple[claim_text.DateMention, ...] = ()
    entities: frozenset[str] = frozenset()  # canonical names mentioned

    @classmethod
    def build(cls, record: EvidenceRecord, catalog: EntityCatalog) -> RecordView:
        text = record.excerpt or ""
        fact_bits: list[str] = []
        named: set[str] = set()
        fact_numbers: list[claim_text.NumberMention] = []
        fact_dates: list[claim_text.DateMention] = []
        for f in record.facts:
            if f.subject and f.kind in (FactKind.MENTIONS_ENTITY, FactKind.ENTITY):
                named.add(f.subject)
            for part in (f.subject, f.attribute, f.value if isinstance(f.value, str) else None):
                if part:
                    fact_bits.append(str(part))
            if isinstance(f.value, int | float) and not isinstance(f.value, bool):
                fact_numbers.append(
                    claim_text.NumberMention(
                        str(f.value), float(f.value), claim_text.NumberKind.PLAIN, f.unit, -1, -1
                    )
                )
            if f.kind in (FactKind.RELEASE_DATE, FactKind.EVENT_DATE) and isinstance(f.value, str):
                fact_dates.extend(claim_text.dates(f.value))
            if f.kind is FactKind.PUBLISHED and f.as_of is not None:
                d = f.as_of
                fact_dates.append(
                    claim_text.DateMention(d.date().isoformat(), d.year, d.month, d.day)
                )
        who = [x for x in (record.publisher, record.author) if x]
        full = " ".join([text, *fact_bits, *who])
        for m in catalog.find(full):
            for c in m.candidates:
                named.add(c.canonical_name)
        toks = tokens(full)
        return cls(
            record=record,
            tokens=toks,
            token_set=frozenset(toks),
            sentences=sentences(text) if text else [],
            numbers=claim_text.numbers(text) + tuple(fact_numbers),
            dates=claim_text.dates(text) + tuple(fact_dates),
            entities=frozenset(named),
        )

    def mentions(self, entity: CatalogEntity) -> bool:
        if entity.canonical_name in self.entities:
            return True
        return any(contains_sequence(self.tokens, tokens(n)) for n in entity.names())

    def contains(self, phrase: str) -> bool:
        return contains_sequence(self.tokens, tokens(phrase))

    def sentences_about(self, names: Sequence[str]) -> list[str]:
        keys = [tokens(n) for n in names]
        return [
            s for s in self.sentences if any(contains_sequence(tokens(s), k) for k in keys if k)
        ]

    @property
    def statement(self) -> bool:
        """Does the record say something about the world (not just measure a query)?"""
        kind = self.record.evidence_kind.value
        if kind == "entity_record":
            return self.record.trust_level.value == "curated"
        return kind not in _NON_STATEMENT

    @property
    def credible(self) -> bool:
        return self.record.trust_level in CREDIBLE and not self.record.injection_flags

    @property
    def source_identity(self) -> str | None:
        """Who stands behind the record (None: derived, not an independent source)."""
        r = self.record
        if r.evidence_kind.value in ("entity_record", "statistical_calculation"):
            return None
        who = (r.publisher or r.author or "").strip().lower()
        return f"{r.provider_name}|{who}" if who else f"{r.provider_name}|{r.source_reference}"


class EntityVerifier:
    def __init__(
        self,
        catalog: EntityCatalog,
        views: Mapping[str, RecordView],
        *,
        now: datetime,
        category: str | None = None,
        topic_entity_keys: Iterable[str] = (),
    ) -> None:
        self.catalog = catalog
        self.views = views
        self.now = now
        self.category = category
        self.topic_keys = frozenset(topic_entity_keys)
        self.denied, self.disputed = denial_index(views, catalog)

    # ---------------------------------------------------------------- resolution
    def resolve(
        self, mention: EntityMention, context: str = ""
    ) -> tuple[CatalogEntity | None, tuple[str, ...]]:
        cands = list(mention.candidates)
        if len(cands) == 1:
            return cands[0], ()
        names = tuple(sorted(c.key for c in cands))
        if context:
            ctx = set(tokens(context))
            own = set(tokens(mention.text))
            hits = []
            for c in cands:
                marks = {t for n in c.names() for t in tokens(n)} | set(tokens(c.entity_type))
                marks -= own | STOPWORDS
                if marks & ctx:
                    hits.append(c)
            if len(hits) == 1:
                return hits[0], names
        topic = [c for c in cands if c.key in self.topic_keys]
        if len(topic) == 1:
            return topic[0], names
        same_cat = [c for c in cands if self.category and c.category == self.category]
        if len(same_cat) == 1:
            return same_cat[0], names
        in_evidence = [
            c
            for c in cands
            if any(v.record.evidence_id == c.evidence_id for v in self.views.values())
        ]
        if len(in_evidence) == 1:
            return in_evidence[0], names
        return None, names

    def check_mention(self, mention: EntityMention, context: str = "") -> EntityCheck:
        entity, candidates = self.resolve(mention, context)
        if entity is None:
            return EntityCheck(
                name=mention.text,
                state=EntityState.AMBIGUOUS,
                entity=None,
                candidates=candidates,
                reasons=(f"'{mention.text}' matches {len(candidates)} catalogued entities",),
            )
        return self._check(entity.canonical_name, entity, candidates)

    def check_name(self, name: str, context: str = "") -> EntityCheck:
        """Check a name that is not (or not fully) in the catalogue."""
        found = self.catalog.lookup(name)
        if found:
            mention = EntityMention(name, tuple(found), 0, len(tokens(name)))
            return self.check_mention(mention, context)
        return self._check(name, None, ())

    # ------------------------------------------------------------------- checks
    def _check(
        self, name: str, entity: CatalogEntity | None, candidates: tuple[str, ...]
    ) -> EntityCheck:
        names = entity.names() if entity else (name,)
        mentioning = [
            v
            for v in self.views.values()
            if v.statement and (v.mentions(entity) if entity else v.contains(name))
        ]
        credible = [v for v in mentioning if v.credible]
        reasons: list[str] = []
        if entity is not None and entity.status is EntityStatus.REJECTED:
            return EntityCheck(
                name=name,
                state=EntityState.REJECTED,
                entity=entity,
                candidates=candidates,
                reasons=("rejected by entity grounding",),
            )
        denied: list[str] = []
        disputed: list[str] = []
        for n in names:
            key = tuple(tokens(n))
            denied += self.denied.get(key, [])
            disputed += self.disputed.get(key, [])
        denied = list(dict.fromkeys(denied))
        disputed = list(dict.fromkeys(disputed))
        credible_confirming = [v for v in credible if v.record.evidence_id not in denied]
        dates_found: list[tuple[date, str]] = []
        wording: dict[str, int] = {}
        for v in credible:
            for f in v.record.facts:
                if (
                    f.kind is FactKind.RELEASE_DATE
                    and f.subject in names
                    and isinstance(f.value, str)
                ):
                    dates_found.append((date.fromisoformat(f.value[:10]), v.record.evidence_id))
            for s in v.sentences_about(names):
                level = claim_text.release_wording(s)
                if level:
                    wording[level] = wording.get(level, 0) + 1
                    for d in claim_text.dates(s):
                        if d.day and d.month and d.year:
                            dates_found.append((date(d.year, d.month, d.day), v.record.evidence_id))
                # "X is available today" in a credible post dates the release to its
                # publication day.
                if (
                    level == "released"
                    and _TODAY.search(s)
                    and v.record.published_at is not None
                    and not claim_text.has_denial(s)
                ):
                    dates_found.append((v.record.published_at.date(), v.record.evidence_id))
        if entity is not None and entity.release_date and entity.origin is EntityOrigin.GAZETTEER:
            dates_found.append((entity.release_date, entity.evidence_id or "catalogue"))
        distinct = sorted({d for d, _ in dates_found})
        conflicts: tuple[str, ...] = ()
        if len(distinct) > 1:
            conflicts = (
                "credible sources give different release dates: "
                + ", ".join(f"{d.isoformat()} ({src})" for d, src in sorted(set(dates_found))),
            )
        release = "unknown"
        if len(distinct) == 1:
            release = "released" if distinct[0] <= self.now.date() else "upcoming"
        elif not distinct and wording:
            release = next(w for w in ("released", "upcoming", "announced") if w in wording)
        if entity is None and not mentioning:
            state = EntityState.UNKNOWN
            reasons.append(f"'{name}' is not a known entity and no evidence mentions it")
        elif denied and not credible_confirming:
            state = EntityState.REJECTED
            reasons.append(f"a credible source denies '{name}'")
        elif credible_confirming or (
            entity is not None
            and entity.status is EntityStatus.VERIFIED
            and entity.origin is EntityOrigin.GAZETTEER
        ):
            state = EntityState.CONFIRMED
        elif len({v.source_identity for v in mentioning if v.source_identity}) >= 2:
            state = EntityState.REPORTED
            reasons.append("mentioned only by lower-trust sources")
        elif mentioning or (
            entity is not None and entity.status is EntityStatus.NEEDS_VERIFICATION
        ):
            state = EntityState.RUMORED
            reasons.append("a single lower-trust source; not confirmed")
        else:
            state = EntityState.UNKNOWN
            reasons.append("catalogued but no evidence in this request mentions it")
        if entity is not None and entity.synthetic:
            reasons.append("synthetic entity")
        return EntityCheck(
            name=name,
            state=state,
            entity=entity,
            candidates=candidates,
            release=release,
            release_dates=tuple(sorted(set(dates_found))),
            mentioning_ids=tuple(v.record.evidence_id for v in mentioning),
            credible_ids=tuple(v.record.evidence_id for v in credible),
            denied_by=tuple(denied),
            disputed_by=tuple(disputed),
            conflicts=conflicts,
            reasons=tuple(reasons),
        )


_CAP_SEQ = re.compile(
    r"(?<![\w'])((?:[A-Z][a-z0-9'\u2019]+|[A-Z]{2,}[a-z]*|[a-z]+[A-Z]\w*)"
    r"(?:[ -](?:[A-Z][a-z0-9'\u2019]+|[A-Z]{2,}[a-z]*|[a-z]+[A-Z]\w*|of|the|and|de|"
    r"\d+(?:\.\d+)?))+)"
)
_ODD_CAPS = re.compile(
    r"(?<![\w'])([a-z]+[A-Z][A-Za-z]*|[A-Z][a-z]+[A-Z][A-Za-z]*|[A-Z]{3,})(?![\w'])"
)


def name_candidates(text: str, *, title_mode: bool = False) -> list[str]:
    """Product-like names: identifiers with numbers, CamelCase/ALLCAPS, capitalised runs."""
    out: list[str] = list(claim_text.identifiers(text))
    if not title_mode:
        for m in _CAP_SEQ.finditer(text):
            phrase = re.sub(r"(?:[ -](?:of|the|and|de))+$", "", m.group(1).strip(" -"))
            phrase = re.sub(r"^(?:(?:The|A|An|This|That|These|Those|Its|Their)\s+)+", "", phrase)
            words = [w for w in tokens(phrase) if w not in STOPWORDS]
            if words and not all(w in NEUTRAL_VOCABULARY for w in words):
                out.append(phrase)
    for m in _ODD_CAPS.finditer(text):
        word = m.group(1)
        if word.lower() not in COMMON_ACRONYMS:
            out.append(word)
    date_spans = [d.raw for d in claim_text.dates(text)]
    seen: dict[str, str] = {}
    for c in out:
        toks = tokens(c)
        key = " ".join(toks)
        if not key or key in seen:
            continue
        if all(t in WELL_KNOWN or t in claim_text.MONTH_NAMES or t.isdigit() for t in toks):
            continue
        if any(c in d or d in c for d in date_spans):
            continue
        seen[key] = c
    return list(seen.values())


def unresolved(names: Iterable[str], mentions: Sequence[EntityMention], text: str) -> list[str]:
    """Candidates not covered by a catalogue mention."""
    toks = tokens(text)
    covered: set[int] = set()
    for m in mentions:
        covered.update(range(m.start, m.end))
    out = []
    for n in names:
        nt = tokens(n)
        hit = False
        for i in range(len(toks) - len(nt) + 1):
            if toks[i : i + len(nt)] == nt and set(range(i, i + len(nt))) <= covered:
                hit = True
                break
        if not hit:
            out.append(n)
    return out


def trust_of(views: Iterable[RecordView]) -> TrustLevel | None:
    from trend_agent.schemas.verification import TRUST_RANK

    levels = [v.record.trust_level for v in views]
    return max(levels, key=lambda t: TRUST_RANK[t]) if levels else None


_PREFIX_CUES = re.compile(
    r"\b(?:there (?:is|are) no|no plans to (?:release|launch|ship|make|bring)|"
    r"(?:is|are) not (?:releasing|launching|planning|making)|never (?:announced|released|"
    r"existed|made)|no such|does not exist|doesn't exist)\b",
    re.IGNORECASE,
)
_SUFFIX_CUES = re.compile(
    r"\b(?:is|are|was|were) (?:fake|a hoax|hoaxes|not real|false|fabricated|misleading|"
    r"clickbait|cancel+ed|delayed|postponed|scrapped|discontinued|debunked|denied)\b|"
    r"\b(?:has|have) been (?:cancel+ed|delayed|postponed|scrapped|debunked|denied)\b|"
    r"\b(?:does not|doesn't|do not|don't) exist\b",
    re.IGNORECASE,
)


def _token_index(text: str, char_pos: int) -> int:
    return len(tokens(text[:char_pos]))


def denied_names(sentence: str, catalog: EntityCatalog | None = None) -> list[str]:
    """Names a sentence denies ("There is no X", "X is fake", "no plans to release X")."""
    names = list(name_candidates(sentence))
    if catalog is not None:
        names += [m.text for m in catalog.find(sentence)]
    toks = tokens(sentence)
    spans: list[tuple[int, int, str]] = []
    for n in dict.fromkeys(names):
        nt = tokens(n)
        for i in range(len(toks) - len(nt) + 1):
            if nt and toks[i : i + len(nt)] == nt:
                spans.append((i, i + len(nt), n))
                break
    out: list[str] = []
    for m in _PREFIX_CUES.finditer(sentence):
        cue_end = _token_index(sentence, m.end())
        after = [sp for sp in spans if cue_end <= sp[0] <= cue_end + 8]
        if after:
            out.append(min(after, key=lambda sp: sp[0])[2])
    for m in _SUFFIX_CUES.finditer(sentence):
        cue_start = _token_index(sentence, m.start())
        before = [sp for sp in spans if sp[1] <= cue_start and cue_start - sp[1] <= 10]
        if before:
            out.append(max(before, key=lambda sp: sp[1])[2])
    return list(dict.fromkeys(out))


def denial_index(
    views: Mapping[str, RecordView], catalog: EntityCatalog
) -> tuple[dict[tuple[str, ...], list[str]], dict[tuple[str, ...], list[str]]]:
    """(credible denials, lower-trust denials): name tokens -> evidence IDs."""
    credible: dict[tuple[str, ...], list[str]] = {}
    other: dict[tuple[str, ...], list[str]] = {}
    for v in views.values():
        for sentence in v.sentences:
            for name in denied_names(sentence, catalog):
                key = tuple(tokens(name))
                if key:
                    (credible if v.credible else other).setdefault(key, []).append(
                        v.record.evidence_id
                    )
    return credible, other
