"""Entity resolution for queries and videos, with version awareness and verified facts.

Builds on the verification layer's catalogue and ``EntityVerifier`` (existence,
confirmation, ambiguity, denial, release dates). Phase 5 adds:

* **versioned names** - "Nimbus 4" is family *Nimbus*, version *4*; "Skyforge Arena
  Season 7" is family *Skyforge Arena*, version *season 7*. A video mentioning the same
  family with another version is a version mismatch, never a match;
* **verified change facts** - release dates, breaking changes, new seasons and event
  windows, taken only from credible records (official sources, curated entries).

Nothing here invents a detail: an attribute no credible record states stays unknown.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from trend_agent.schemas.search_intent import EntityRef
from trend_agent.verification import claims as claim_text
from trend_agent.verification.entities import (
    WELL_KNOWN,
    EntityCatalog,
    EntityCheck,
    EntityState,
    EntityVerifier,
    RecordView,
    name_candidates,
    unresolved,
)
from trend_agent.verification.text import NEUTRAL_VOCABULARY, STOPWORDS, fold, tokens

_VERSION = re.compile(
    r"(?:\b(?:season|chapter|ch|s|wave|act|part|patch|update)\s*\d+(?:\s+season\s*\d+)?\b|"
    r"\bv?\d+(?:\.\d+)*[a-z]?\b)\s*$",
    re.IGNORECASE,
)
_VERSION_AFTER = re.compile(
    r"^\s*(?:(?:season|chapter|ch|wave|act|part|patch|update)\s*\d+(?:\s+season\s*\d+)?|"
    r"v?\d+(?:\.\d+)*[a-z]?)\b",
    re.IGNORECASE,
)
_ITEM_CUES = frozenset(
    tokens(
        "where find get location locations spawn spawns map unlock unlocked unlocking obtain "
        "earn buy shop item items new guide fast free easy all season chapter event"
    )
)
_BREAKING = re.compile(
    r"\b(?:breaking changes?|deprecated|no longer (?:supported|works?|available)|removed|"
    r"replaced|incompatible|requires (?:a )?migration|vault(?:s|ed)|retired|discontinued)\b",
    re.IGNORECASE,
)
_NEW_PHASE = re.compile(
    r"\b(?:new season|season \d+ (?:is live|begins|starts|launches)|is live|now live|"
    r"released|launch(?:es|ed)|available today|out now)\b",
    re.IGNORECASE,
)
_ENDS = re.compile(r"\b(?:until|through|ends? on|ending|runs until|available until)\b", re.I)


def normalise_version(text: str) -> str:
    t = fold(text).strip()
    t = re.sub(r"\bch\b", "chapter", t)
    t = re.sub(r"^v(?=\d)", "", t)
    return re.sub(r"\s+", " ", t)


@dataclass(frozen=True)
class VersionedName:
    family: tuple[str, ...]
    version: str | None
    display: str = ""

    @property
    def family_text(self) -> str:
        return self.display or " ".join(self.family)


def split_version(name: str, version: str | None = None) -> VersionedName:
    """Family tokens and normalised version of a product/game name."""
    m = _VERSION.search(name)
    head = name[: m.start()].strip() if m and m.start() > 0 else name
    if version:
        v = normalise_version(version)
        full = tokens(name)
        vt = tokens(v)
        fam = full[: -len(vt)] if vt and full[-len(vt) :] == vt else full
        return VersionedName(tuple(fam), v, head)
    if m and m.start() > 0:
        fam = tokens(name[: m.start()])
        if fam:
            return VersionedName(tuple(fam), normalise_version(m.group(0)), head)
    return VersionedName(tuple(tokens(name)), None, name)


def versions_mentioned(text: str, family: Sequence[str]) -> list[str]:
    """Versions written right after the family name in ``text`` ("Nimbus 3", "Season 6")."""
    if not family:
        return []
    folded = fold(text)
    pattern = r"\b" + r"[\s\-]+".join(re.escape(t) for t in family) + r"s?\b"
    out = []
    for m in re.finditer(pattern, folded):
        tail = folded[m.end() : m.end() + 24]
        v = _VERSION_AFTER.match(tail)
        if v:
            out.append(normalise_version(v.group(0)))
    # Bare season/chapter mentions ("Season 6 guide") also identify a version for games.
    for m in re.finditer(r"\b(?:season|chapter)\s*\d+(?:\s+season\s*\d+)?\b", folded):
        out.append(normalise_version(m.group(0)))
    return list(dict.fromkeys(out))


@dataclass(frozen=True)
class ChangeEvent:
    when: date
    description: str
    evidence_id: str
    breaking: bool


@dataclass(frozen=True)
class EntityFacts:
    """Verified facts about one entity (credible records only)."""

    ref: EntityRef
    versioned: VersionedName
    entity_type: str | None
    release_date: date | None
    release_state: str
    changes: tuple[ChangeEvent, ...] = ()
    event_end: date | None = None
    conflicts: tuple[str, ...] = ()
    denied: bool = False

    @property
    def latest_change(self) -> ChangeEvent | None:
        return max(self.changes, key=lambda c: c.when) if self.changes else None


def _ref(check: EntityCheck, version: str | None) -> EntityRef:
    entity = check.entity
    return EntityRef(
        name=entity.canonical_name if entity else check.name,
        entity_key=entity.key if entity else None,
        entity_type=entity.entity_type if entity else None,
        version=version,
        state=check.state.value,
        evidence_ids=check.credible_ids or check.mentioning_ids,
        candidates=check.candidates,
    )


@dataclass
class QueryEntities:
    refs: list[EntityRef] = field(default_factory=list)
    facts: list[EntityFacts] = field(default_factory=list)
    unknown_names: list[str] = field(default_factory=list)

    @property
    def primary(self) -> EntityFacts | None:
        """The most specific resolved entity of the query (versioned, then non-item)."""
        good = [
            f
            for f in self.facts
            if f.ref.state not in ("ambiguous", "unknown") and f.entity_type != "game_item"
        ]
        if not good:
            return self.item
        return max(good, key=lambda f: (f.versioned.version is not None, len(f.versioned.family)))

    @property
    def item(self) -> EntityFacts | None:
        """An in-game item the query asks about (it must itself be verified)."""
        items = [
            f
            for f in self.facts
            if f.entity_type == "game_item" or f.ref.entity_type == "item_phrase"
        ]
        return items[0] if items else None


class QueryEntityResolver:
    def __init__(
        self,
        catalog: EntityCatalog,
        views: Mapping[str, RecordView],
        *,
        now: datetime,
        category: str | None = None,
    ) -> None:
        self.catalog = catalog
        self.views = views
        self.now = now
        self.verifier = EntityVerifier(catalog, views, now=now, category=category)

    def resolve(self, query: str) -> QueryEntities:
        out = QueryEntities()
        mentions = self.catalog.find(query)
        for m in mentions:
            check = self.verifier.check_mention(m, query)
            entity = check.entity
            version = entity.version if entity else None
            out.refs.append(_ref(check, version))
            out.facts.append(self.facts_for(check))
        candidates = name_candidates(query, title_mode=True) + _lowercase_versioned(query)
        for name in unresolved(candidates, mentions, query):
            check = self.verifier.check_name(name, query)
            out.unknown_names.append(name)
            out.refs.append(_ref(check, split_version(name).version))
            out.facts.append(self.facts_for(check))
        return out

    def _removal(self, name: str, ids: Sequence[str]) -> str | None:
        """A credible record sentence stating that ``name`` was removed or vaulted."""
        n = re.escape(name.casefold())
        pattern = re.compile(
            rf"\b(?:vaults?|vaulted|removes?|removed|retires?|retired|discontinues?)\W+"
            rf"(?:(?:the|a|an)\s+)?(?:\w+\W+){{0,2}}{n}\b|\b{n}\W+(?:(?:is|was|has been|"
            rf"have been|are|were|got)\s+)?(?:now\s+)?(?:vaulted|removed|retired|"
            rf"discontinued|no longer available)\b",
            re.IGNORECASE,
        )
        for eid in ids:
            view = self.views.get(eid)
            if view is None:
                continue
            for sentence in view.sentences:
                if pattern.search(sentence.casefold()):
                    return eid
        return None

    def _mark_removed(self, entities: QueryEntities, facts: EntityFacts, eid: str) -> None:
        ref = facts.ref.model_copy(
            update={"state": "removed", "evidence_ids": (*facts.ref.evidence_ids, eid)}
        )
        new = EntityFacts(
            ref=ref,
            versioned=facts.versioned,
            entity_type=facts.entity_type,
            release_date=facts.release_date,
            release_state=facts.release_state,
            changes=facts.changes,
            event_end=facts.event_end,
            conflicts=facts.conflicts,
            denied=True,
        )
        entities.facts[entities.facts.index(facts)] = new
        entities.refs[:] = [ref if r is facts.ref or r == facts.ref else r for r in entities.refs]

    def resolve_item(self, query: str, entities: QueryEntities) -> None:
        """For item-type queries: identify the item phrase and verify it (or record why not).

        A credible record saying the item was removed or vaulted marks it ``removed``:
        a location guide for an item that is not in the current version is never proposed.
        """
        if entities.item is not None:
            current = entities.item
            credible = self.verifier.check_name(current.ref.name, query).credible_ids
            hit = self._removal(current.ref.name, credible)
            if hit is not None and current.ref.state == "confirmed":
                self._mark_removed(entities, current, hit)
            return
        mentions = self.catalog.find(query)
        covered: set[int] = set()
        for m in mentions:
            covered.update(range(m.start, m.end))
        toks = tokens(query)
        words = [
            t
            for i, t in enumerate(toks)
            if i not in covered and t not in STOPWORDS and t not in _ITEM_CUES and not t.isdigit()
        ]
        if not words:
            return
        phrase = " ".join(words[:4])
        check = self.verifier.check_name(phrase, query)
        ref = _ref(check, None).model_copy(update={"entity_type": "item_phrase"})
        if check.entity is not None:
            ref = _ref(check, None)
        entities.refs.append(ref)
        facts = self.facts_for(check)
        item_facts = EntityFacts(
            ref=ref,
            versioned=facts.versioned,
            entity_type=check.entity.entity_type if check.entity else "item_phrase",
            release_date=facts.release_date,
            release_state=facts.release_state,
            changes=facts.changes,
            event_end=facts.event_end,
            conflicts=facts.conflicts,
            denied=facts.denied,
        )
        entities.facts.append(item_facts)
        hit = self._removal(phrase, check.credible_ids)
        if hit is not None and ref.state == "confirmed":
            self._mark_removed(entities, item_facts, hit)

    def facts_for(self, check: EntityCheck) -> EntityFacts:
        entity = check.entity
        name = entity.canonical_name if entity else check.name
        versioned = split_version(name, entity.version if entity else None)
        ref = _ref(check, versioned.version)
        changes: list[ChangeEvent] = []
        event_end: date | None = None
        for eid in check.credible_ids:
            view = self.views.get(eid)
            if view is None or view.record.published_at is None:
                continue
            when = view.record.published_at.date()
            # A credible record about the entity: its release notes speak for it.
            for sentence in view.sentences:
                breaking = bool(_BREAKING.search(sentence))
                if breaking or _NEW_PHASE.search(sentence):
                    changes.append(ChangeEvent(when, sentence[:240], eid, breaking))
                if _ENDS.search(sentence):
                    for d in claim_text.dates(sentence):
                        if d.month and d.day:
                            year = d.year or when.year
                            event_end = date(year, d.month, d.day)
        release = (
            check.release_dates[0][0] if len({d for d, _ in check.release_dates}) == 1 else None
        )
        return EntityFacts(
            ref=ref,
            versioned=versioned,
            entity_type=entity.entity_type if entity else None,
            release_date=release,
            release_state=check.release,
            changes=tuple(sorted(set(changes), key=lambda c: (c.when, c.evidence_id))),
            event_end=event_end,
            conflicts=check.conflicts,
            denied=bool(check.denied_by),
        )


def _lowercase_versioned(query: str) -> list[str]:
    """Lower-case queries ("nimbus 5 install") still name versioned products."""
    out = []
    for m in re.finditer(r"\b([a-z][a-z]+)\s+(v?\d+(?:\.\d+)*)\b", fold(query)):
        head = m.group(1)
        if head in STOPWORDS or head in NEUTRAL_VOCABULARY or head in WELL_KNOWN:
            continue
        if head in {"season", "chapter", "part", "episode", "level", "top", "under", "wave"}:
            continue
        out.append(m.group(0))
    return out


def as_of_day(value: datetime | None) -> date:
    return (value or datetime.now(UTC)).date()


STATE_OK = frozenset({EntityState.CONFIRMED.value, EntityState.REPORTED.value})
