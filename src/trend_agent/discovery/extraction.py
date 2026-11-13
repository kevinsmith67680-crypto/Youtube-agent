"""Entity extraction from discovery evidence.

Deterministic first: a curated gazetteer (from the seed files) plus conservative patterns
for versions, releases, announcements, game seasons/updates/events and in-game items.
Every extracted name is a substring of the source text ("grounded"); nothing is inferred
from outside knowledge.

An optional extractor (e.g. an LLM) can be plugged in through :class:`EntityExtractor`;
its output always passes :func:`ground`, which rejects names absent from the source.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from trend_agent.discovery.config import GazetteerEntity
from trend_agent.discovery.models import (
    Category,
    DiscoveryItem,
    EntityOrigin,
    EntityStatus,
    EntityType,
    EvidenceType,
    ExtractedEntity,
)
from trend_agent.discovery.text import (
    display_version,
    entity_key,
    normalize_name,
    normalize_text,
    sentences,
)

TRUSTED_EVIDENCE = frozenset(
    {
        EvidenceType.OFFICIAL_ANNOUNCEMENT,
        EvidenceType.CODE_RELEASE,
        EvidenceType.RELATED_QUERY,
        EvidenceType.SEARCH_SUGGESTION,
    }
)
BASE_CONFIDENCE: dict[EvidenceType, float] = {
    EvidenceType.OFFICIAL_ANNOUNCEMENT: 0.85,
    EvidenceType.CODE_RELEASE: 0.85,
    EvidenceType.RELATED_QUERY: 0.8,
    EvidenceType.SEARCH_SUGGESTION: 0.8,
    EvidenceType.VIDEO_TITLE: 0.65,
    EvidenceType.VIDEO_DESCRIPTION: 0.6,
    EvidenceType.CURATED_SEED: 0.6,
    EvidenceType.COMMUNITY_DISCUSSION: 0.45,
    EvidenceType.SYNTHETIC_TEST_DATA: 0.5,
}

VARIANTS = (
    "Pro|Ultra|Mini|Max|Flash|Turbo|Lite|Plus|Nano|Coder|Instruct|Vision|Super|Ti|XT|"
    "Sonnet|Opus|Haiku|Preview|Thinking|Air|Edge|Omni"
)
_VERSION = r"v?\d+(?:\.\d+){0,3}[a-z]?"
_ROMAN = r"(?:IX|IV|VI{0,3}|XI{0,3}|I{2,3})"
_VERSION_TAIL = re.compile(
    rf"^(?P<sep>[ \-]?)(?P<tail>(?:(?:{VARIANTS})[ \-])?{_VERSION}"
    rf"(?:[ \-](?:{VARIANTS}))*)(?![\w.])"
)
_ROMAN_TAIL = re.compile(rf"^(?P<sep> )(?P<tail>{_ROMAN})(?![\w])")
_UNKNOWN_VERSIONED = re.compile(
    rf"(?<![\w.])(?P<name>(?:[A-Z]|i[A-Z])[A-Za-z0-9]*(?:[.+][A-Za-z0-9]+)*"
    rf"(?:[ \-](?:[A-Z]|i[A-Z])[A-Za-z0-9]*(?:\.[a-z]+)?){{0,2}})"
    rf"(?P<sep>[ \-]?)(?P<ver>v?\d+(?:\.\d+){{0,3}})(?P<variant>(?:[ \-](?:{VARIANTS}))*)(?![\w.])"
)
_INTRO = re.compile(
    r"(?i:\b(?:introducing|announcing|meet|unveil(?:s|ed|ing)?|launch(?:es|ed|ing)?"
    r"|debut(?:s|ed)?|now available:?))\s+(?:(?i:the|our|new)\s+)*"
    r"(?P<name>(?:[A-Z]|i[A-Z])[A-Za-z0-9]*(?:[.+\-][A-Za-z0-9]+)*"
    r"(?:\s(?:[A-Z]|i[A-Z])[A-Za-z0-9]*(?:[.+\-][A-Za-z0-9]+)*){0,3})"
)
_RELEASE_TITLE = re.compile(
    r"^(?:(?P<name>.*?)\s+)?v?(?P<ver>\d+(?:\.\d+){0,3}"
    r"(?:[-.]?(?:rc|beta|alpha|a|b|dev|post)\.?\d*)?)\s*$",
    re.IGNORECASE,
)
_SEASON = re.compile(r"\b(?P<label>Chapter\s\d+(?:,?\s+Season\s\d+)?|Season\s\d+)\b")
_GAME_UPDATE = re.compile(r"\b(?:Update|Patch|Version|Hotfix)\s?v?(?P<ver>\d+(?:\.\d+){1,2})\b")
_EVENT = re.compile(
    r"\b(?P<name>(?:[A-Z][a-z']+ ){1,3}"  # same line only: never span title/body
    r"(?:Event|Festival|Fest|Celebration|Tournament|Cup|Showdown|Takeover|Championship"
    r"|Carnival|Gala|Invasion|Rally))\b"
)
_EVENT_CUE = re.compile(
    r"(?i)\b(limited[- ]time|event|live now|starts?|ends?|runs? (?:until|through)|"
    r"available until|returns?)\b"
)
_ITEM_LIST = re.compile(
    r"(?i:\bnew\s+(?:items?|weapons?|skins?|rewards?|cosmetics?|gear|loot))\s*"
    r"(?i:include|includes|including|are|such as|like|:)\s*(?P<list>[^.\n;]+)"
)
_ITEM_SPLIT = re.compile(r",\s*(?:and\s+)?|\s+and\s+|\s*&\s*")
_ITEM_NAME = re.compile(
    r"^(?:the\s+|a\s+|an\s+)?(?P<name>[A-Z][A-Za-z'\-]*(?:\s[A-Z][A-Za-z'\-]*){0,3})$"
)
_SPECULATIVE = re.compile(
    r"(?i)\b(rumou?rs?|leak(?:s|ed)?|reportedly|unconfirmed|allegedly|speculat\w*|might|"
    r"could be|supposedly)\b"
)
_COMPARISON_CUE = re.compile(
    r"(?i)\b(vs\.?|versus|compared|comparison|outperform\w*|beats?|better than|against|than)\b"
)

ATTRIBUTE_PATTERNS: dict[str, re.Pattern[str]] = {
    "local": re.compile(
        r"(?i)\b(open[- ]weights?|open[- ]source|runs? locally|run it locally|locally|"
        r"self[- ]host\w*|gguf|ollama|download(?:able)?)\b"
    ),
    "api": re.compile(r"\bAPIs?\b"),
    "pricing": re.compile(
        r"(?i)(\bpric(?:e|es|ing)\b|\$\s?\d|\bper million tokens\b|\bsubscription\b|"
        r"\bfree tier\b|\bpaid plan\b)"
    ),
    "free": re.compile(r"(?i)\bfree\b"),
    "install": re.compile(r"(?i)\b(install\w*|pip install|npm install|setup|set up)\b"),
    "breaking": re.compile(r"(?i)\b(breaking changes?|migrat\w+|deprecat\w+)\b"),
    "benchmark": re.compile(r"(?i)\b(benchmarks?|outperform\w*|scores?|leaderboard)\b"),
    "challenges": re.compile(r"(?i)\b(challenges?|quests?|missions?|objectives?)\b"),
    "rewards": re.compile(r"(?i)\b(rewards?|unlock\w*|earn)\b"),
    "bugs": re.compile(r"(?i)\b(bugs?|crash\w*|errors?|glitch\w*|issues?)\b"),
    "performance": re.compile(r"(?i)\b(fps|performance|settings|optimi[sz]\w*)\b"),
    "limited_time": re.compile(r"(?i)\b(limited[- ]time|ends?|until|for a limited)\b"),
    "speculative": _SPECULATIVE,
}

# Capitalised words that are not product names on their own.
_STOP_CAPS = frozenset(
    """
    a an the this that our new top best how why what when where who is are get install run
    using use update patch version season chapter episode part day week level act vol step
    introducing announcing meet today now here full official free watch live breaking
    january february march april may june july august september october november december
    monday tuesday wednesday thursday friday saturday sunday i we you my in on at to of for
    and or with vs versus round game games guide tutorial review every all each
    """.split()
)

_CONTEXT_TYPES: list[tuple[re.Pattern[str], EntityType]] = [
    (re.compile(r"(?i)\b(model|llm|weights|parameters|checkpoint)\b"), EntityType.AI_MODEL),
    (re.compile(r"(?i)\b(framework|library|sdk|package)\b"), EntityType.FRAMEWORK),
    (
        re.compile(
            r"(?i)\b(gpu|cpu|chip|laptop|phone|headset|glasses|console|thermostat|device|"
            r"wearable|processor|graphics card)\b"
        ),
        EntityType.HARDWARE,
    ),
    (
        re.compile(r"(?i)\b(agent|assistant|app|chatbot|copilot|browser|platform)\b"),
        EntityType.AI_PRODUCT,
    ),
    (re.compile(r"(?i)\b(game|multiplayer|battle royale|rpg|shooter)\b"), EntityType.GAME),
]


def infer_type(context: str, category: Category | None) -> EntityType:
    for pattern, entity_type in _CONTEXT_TYPES:
        if pattern.search(context):
            if entity_type is EntityType.AI_PRODUCT and category is not Category.AI:
                return EntityType.SOFTWARE
            return entity_type
    match category:
        case Category.AI:
            return EntityType.AI_PRODUCT
        case Category.GAMING:
            return EntityType.GAME
        case _:
            return EntityType.SOFTWARE


def release_level(version: str) -> str:
    """'major' | 'minor' | 'patch' | 'prerelease' for a version string."""
    v = version.lower().lstrip("v")
    if re.search(r"(rc|beta|alpha|dev|a\d|b\d)", v):
        return "prerelease"
    parts = [int(p) for p in re.findall(r"\d+", v)[:3]]
    if not parts:
        return "patch"
    if len(parts) == 1:
        return "major"
    if parts[0] == 0:  # 0.y.z: semver gives no stability promise; treat 0.y.0 as minor
        return "minor" if len(parts) < 3 or parts[2] == 0 else "patch"
    if all(p == 0 for p in parts[1:]):
        return "major"
    if len(parts) < 3 or parts[2] == 0:
        return "minor"
    return "patch"


# ------------------------------------------------------------------------- gazetteer


@dataclass(frozen=True)
class GazetteerMatch:
    category: Category
    entity: GazetteerEntity
    matched_text: str
    start: int
    end: int
    ambiguous: bool


class Gazetteer:
    """Longest-match lookup of curated entity names and aliases."""

    def __init__(self, entries: Iterable[tuple[Category, GazetteerEntity]]) -> None:
        self._entries = list(entries)
        self._patterns: list[tuple[re.Pattern[str], Category, GazetteerEntity]] = []
        for category, entity in self._entries:
            insensitive = [entity.name, *entity.aliases]
            for name in sorted(set(insensitive), key=len, reverse=True):
                self._patterns.append(
                    (
                        re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])", re.IGNORECASE),
                        category,
                        entity,
                    )
                )
            for alias in entity.case_sensitive_aliases:
                self._patterns.append(
                    (re.compile(rf"(?<![\w]){re.escape(alias)}(?![\w])"), category, entity)
                )
        # Longest names first so 'GitHub Copilot' wins over 'Copilot'.
        self._patterns.sort(key=lambda p: len(p[0].pattern), reverse=True)

    @property
    def entries(self) -> list[tuple[Category, GazetteerEntity]]:
        return self._entries

    def find(self, text: str) -> list[GazetteerMatch]:
        taken: list[tuple[int, int]] = []
        matches: list[GazetteerMatch] = []
        lowered = text.lower()
        for pattern, category, entity in self._patterns:
            for m in pattern.finditer(text):
                if any(m.start() < e and s < m.end() for s, e in taken):
                    continue
                ambiguous = bool(entity.context_terms) and not any(
                    re.search(rf"(?<![\w]){re.escape(term.lower())}(?![\w])", lowered)
                    for term in entity.context_terms
                )
                taken.append((m.start(), m.end()))
                matches.append(
                    GazetteerMatch(category, entity, m.group(0), m.start(), m.end(), ambiguous)
                )
        return sorted(matches, key=lambda m: m.start)

    def lookup(self, name: str) -> list[tuple[Category, GazetteerEntity]]:
        key = normalize_name(name)
        return [
            (cat, ent)
            for cat, ent in self._entries
            if key
            in {normalize_name(n) for n in (ent.name, *ent.aliases, *ent.case_sensitive_aliases)}
        ]


# ------------------------------------------------------------------------- extractors


class EntityExtractor(Protocol):
    name: str

    def extract(self, item: DiscoveryItem) -> list[ExtractedEntity]: ...


@dataclass
class _Mention:
    name: str
    entity_type: EntityType
    category: Category
    matched_text: str
    confidence: float
    extractor: str
    origin: EntityOrigin = EntityOrigin.EXTRACTED
    parent_name: str | None = None
    parent_type: EntityType | None = None
    version: str | None = None
    aliases: set[str] = field(default_factory=set)
    flags: set[str] = field(default_factory=set)
    start: int = -1


def _strip_leading_stops(name: str) -> str:
    words = name.split()
    while words and words[0].lower().strip(".,:") in _STOP_CAPS:
        words.pop(0)
    while words and words[-1].lower().strip(".,:") in _STOP_CAPS:
        words.pop()
    return " ".join(words)


class DeterministicExtractor:
    """Gazetteer + pattern extraction. Pure function of (item, gazetteer)."""

    name = "deterministic-v1"

    def __init__(self, gazetteer: Gazetteer, known: Gazetteer | None = None) -> None:
        self._gazetteer = gazetteer
        # Entities discovered earlier (database), so later evidence can corroborate them.
        self.known = known or Gazetteer([])

    def extract(self, item: DiscoveryItem) -> list[ExtractedEntity]:
        text = normalize_text(item.full_text)
        base = BASE_CONFIDENCE.get(item.evidence_type, 0.5)
        if item.is_synthetic and "simulated_evidence_type" in item.metadata:
            simulated = EvidenceType(item.metadata["simulated_evidence_type"])
            base = BASE_CONFIDENCE.get(simulated, base)
        category = item.category_hint
        mentions: list[_Mention] = []

        # 1. Source hint (a release feed or game news feed is *about* this entity).
        hint_type = EntityType(item.entity_hint_type) if item.entity_hint_type else None
        if item.entity_hint and hint_type is not None:
            mentions.append(
                _Mention(
                    item.entity_hint,
                    hint_type,
                    category or Category.OTHER,
                    item.entity_hint,
                    0.9,
                    "source_hint",
                )
            )
            self._release_title(item, hint_type, mentions)

        # 2. Gazetteer names, with version suffixes ("Llama 4", "GPT-5", "GTA VI"), then
        #    previously discovered entities (never overlapping a curated match).
        curated = self._gazetteer.find(text)
        spans = [(m.start, m.end) for m in curated]
        known_matches = [
            m for m in self.known.find(text) if not any(m.start < e and s < m.end for s, e in spans)
        ]
        gaz = sorted(curated + known_matches, key=lambda m: m.start)
        from_known = {id(m) for m in known_matches}
        tails: dict[int, re.Match[str]] = {}
        for m in gaz:
            if m.entity.entity_type is EntityType.COMPANY:
                continue
            rest = text[m.end :]
            tail = _VERSION_TAIL.match(rest)
            if tail is None and m.entity.entity_type is EntityType.GAME:
                tail = _ROMAN_TAIL.match(rest)
            if tail is not None and not _is_year(tail.group("tail")):
                tails[m.start] = tail
        for m in gaz:
            # A context term, a version suffix or another known entity of the same
            # category in the item is enough to resolve an ambiguous name.
            supported = m.start in tails or any(
                o.category is m.category
                and o.entity.name != m.entity.name
                and not (o.entity.context_terms and o.ambiguous and o.start not in tails)
                for o in gaz
            )
            ambiguous = m.ambiguous and not supported
            is_known = id(m) in from_known
            mention = _Mention(
                m.entity.name,
                m.entity.entity_type,
                m.category,
                m.matched_text,
                0.4 if ambiguous else (base if is_known else 0.9),
                "known_entity" if is_known else "gazetteer",
                EntityOrigin.EXTRACTED if is_known else EntityOrigin.GAZETTEER,
                start=m.start,
            )
            if ambiguous:
                mention.flags.add("ambiguous_context")
                mentions.append(mention)
                continue
            mentions.append(mention)
            tail = tails.get(m.start)
            if tail is not None:
                full = f"{m.matched_text}{tail.group('sep')}{tail.group('tail')}"
                mentions.append(
                    _Mention(
                        _canonical_versioned(m.entity.name, tail.group("sep"), tail.group("tail")),
                        m.entity.entity_type,
                        m.category,
                        full,
                        base,
                        "gazetteer_version",
                        parent_name=m.entity.name,
                        parent_type=m.entity.entity_type,
                        version=tail.group("tail"),
                        start=m.start,
                    )
                )

        known_spans = [(m.start, m.end) for m in gaz]
        # 3. Unknown versioned names ("Nimbus Coder 2", "Lattice 5.0").
        for um in _UNKNOWN_VERSIONED.finditer(text):
            if any(um.start() < e and s < um.end() for s, e in known_spans):
                continue
            name = _strip_leading_stops(um.group("name"))
            if not name or name.lower() in _STOP_CAPS or _is_year(um.group("ver")):
                continue
            if um.group("name").split()[-1].lower() in _STOP_CAPS:
                continue  # "... Update 3.2" etc. handled by game patterns
            version = um.group("ver") + (um.group("variant") or "")
            canonical = f"{name}{um.group('sep')}{version}"
            context = _sentence_at(text, um.start())
            mentions.append(
                _Mention(
                    canonical,
                    infer_type(context, category),
                    category or Category.OTHER,
                    um.group(0),
                    base,
                    "versioned_name",
                    parent_name=name,
                    version=version,
                    start=um.start(),
                    flags={"type_inferred"},
                )
            )

        # 4. Announced names without a version ("Introducing Skyforge Arena").
        for im in _INTRO.finditer(text):
            name = _strip_leading_stops(im.group("name"))
            known_names = {normalize_name(x.name) for x in mentions} | {
                normalize_name(x.parent_name) for x in mentions if x.parent_name
            }
            if not name or len(name) < 3 or normalize_name(name) in known_names:
                continue
            context = _sentence_at(text, im.start())
            confidence = base - 0.1
            mentions.append(
                _Mention(
                    name,
                    infer_type(context, category),
                    category or Category.OTHER,
                    name,
                    confidence,
                    "announcement",
                    start=im.start("name"),
                    flags={"type_inferred"},
                )
            )

        # 5. Game structure (only around a known game).
        games = [
            x
            for x in mentions
            if x.entity_type is EntityType.GAME
            and x.parent_name is None
            and "ambiguous_context" not in x.flags
        ]
        if games:
            hinted = [g for g in games if g.extractor == "source_hint"]
            primary = hinted[0] if hinted else min(games, key=lambda g: g.start)
            self._game_entities(text, primary, games, base, mentions)

        _unify_family_types(mentions)
        return _finalise(mentions, text, item)

    def _release_title(
        self, item: DiscoveryItem, hint_type: EntityType, mentions: list[_Mention]
    ) -> None:
        if item.evidence_type is not EvidenceType.CODE_RELEASE and not (
            item.is_synthetic
            and item.metadata.get("simulated_evidence_type") == EvidenceType.CODE_RELEASE.value
        ):
            return
        match = _RELEASE_TITLE.match(item.title.strip())
        if match is None or item.entity_hint is None:
            return
        version = match.group("ver")
        name_part = (match.group("name") or "").strip()
        if name_part and normalize_name(name_part).replace(" ", "") not in {
            normalize_name(item.entity_hint).replace(" ", ""),
            "release",
            "version",
        }:
            return  # title names something else; leave it to the general patterns
        canonical = f"{item.entity_hint} {display_version(version)}"
        mention = _Mention(
            canonical,
            hint_type,
            item.category_hint or Category.OTHER,
            item.title.strip(),
            0.9,
            "release_feed",
            parent_name=item.entity_hint,
            parent_type=hint_type,
            version=version,
        )
        mention.flags.add(f"release_level:{release_level(version)}")
        mentions.append(mention)

    def _game_entities(
        self,
        text: str,
        primary: _Mention,
        games: list[_Mention],
        base: float,
        mentions: list[_Mention],
    ) -> None:
        def game_for(position: int) -> _Mention:
            # Nearest game mentioned earlier in the same sentence, else the item's game.
            sentence_start = max(text.rfind(".", 0, position), text.rfind("\n", 0, position))
            preceding = [g for g in games if sentence_start < g.start < position]
            return max(preceding, key=lambda g: g.start) if preceding else primary

        for m in _SEASON.finditer(text):
            game = game_for(m.start())
            label = re.sub(r"\s+", " ", m.group("label"))
            mentions.append(
                _Mention(
                    f"{game.name} {label}",
                    EntityType.GAME_UPDATE,
                    game.category,
                    m.group(0),
                    base,
                    "game_season",
                    parent_name=game.name,
                    parent_type=EntityType.GAME,
                    version=label,
                )
            )
        for m in _GAME_UPDATE.finditer(text):
            game = game_for(m.start())
            mentions.append(
                _Mention(
                    f"{game.name} Update {m.group('ver')}",
                    EntityType.GAME_UPDATE,
                    game.category,
                    m.group(0),
                    base,
                    "game_update",
                    parent_name=game.name,
                    parent_type=EntityType.GAME,
                    version=m.group("ver"),
                )
            )
        events: list[_Mention] = []
        has_cue = bool(_EVENT_CUE.search(text))
        game_keys = {normalize_name(g.name) for g in games}
        for m in _EVENT.finditer(text):
            name = _strip_leading_stops(m.group("name"))
            if len(name.split()) < 2 or any(normalize_name(name).startswith(k) for k in game_keys):
                continue
            game = game_for(m.start())
            event = _Mention(
                name,
                EntityType.GAME_EVENT,
                game.category,
                m.group(0),
                base if has_cue else base - 0.2,
                "game_event",
                parent_name=game.name,
                parent_type=EntityType.GAME,
            )
            events.append(event)
        unique_events = {normalize_name(e.name): e for e in events}
        mentions.extend(events)
        parent = next(iter(unique_events.values())) if len(unique_events) == 1 else primary
        known = {normalize_name(x.name) for x in mentions}
        for m in _ITEM_LIST.finditer(text):
            for part in _ITEM_SPLIT.split(m.group("list").strip()):
                named = _ITEM_NAME.match(part.strip())
                if named is None:
                    continue
                name = named.group("name")
                if normalize_name(name) in known or name.lower() in _STOP_CAPS:
                    continue
                mentions.append(
                    _Mention(
                        name,
                        EntityType.GAME_ITEM,
                        primary.category,
                        name,
                        base,
                        "game_item",
                        parent_name=parent.name,
                        parent_type=parent.entity_type,
                    )
                )


def _unify_family_types(mentions: list[_Mention]) -> None:
    """Versions of one family share a type.

    Otherwise 'X 2' (sentence says 'model') and 'X 1.5' (no cue) would become unrelated
    entities of different types that could never be compared or linked. Types from the
    source hint, a release feed or the gazetteer anchor the family; otherwise the most
    confident inferred type wins.
    """
    anchors = {"source_hint", "release_feed", "gazetteer", "gazetteer_version", "known_entity"}
    families: dict[str, list[_Mention]] = {}
    for m in mentions:
        family = normalize_name(m.parent_name or m.name)
        families.setdefault(family, []).append(m)
    for members in families.values():
        inferred = [m for m in members if "type_inferred" in m.flags]
        if not inferred or len(members) < 2:
            continue
        anchored = [m for m in members if m.extractor in anchors]
        if anchored:
            chosen = anchored[0].entity_type
            if any(m.extractor in anchors for m in members):
                for m in inferred:
                    m.flags.discard("type_inferred")
        else:
            typed = sorted(inferred, key=lambda m: (-m.confidence, m.start))
            chosen = next(
                (m.entity_type for m in typed if m.entity_type is not EntityType.AI_PRODUCT),
                typed[0].entity_type,
            )
        for m in inferred:
            m.entity_type = chosen


_BETWEEN = (
    r"\W{0,3}(?:vs\.?|versus|v\.|compared (?:to|with)|against|outperforms?|beats?|"
    r"better than|or)\W{0,3}"
)


def _compared(a: _Mention, b: _Mention, text: str) -> bool:
    """True if the text puts a comparison cue directly between the two names."""
    names_a = {a.name, a.matched_text, *a.aliases}
    names_b = {b.name, b.matched_text, *b.aliases}
    for x in names_a:
        for y in names_b:
            if not x or not y:
                continue
            for left, right in ((x, y), (y, x)):
                pattern = rf"(?<![\w]){re.escape(left)}{_BETWEEN}{re.escape(right)}(?![\w])"
                if re.search(pattern, text, re.IGNORECASE):
                    return True
    return False


def _is_year(version: str) -> bool:
    digits = re.match(r"v?(\d+)", version)
    if digits is None:
        return False
    number = digits.group(1)
    return len(number) == 4 and 1900 <= int(number) <= 2100


def _canonical_versioned(base: str, sep: str, tail: str) -> str:
    return f"{base}{sep}{tail}" if sep else f"{base}{tail}"


def _sentence_at(text: str, position: int) -> str:
    """The sentence containing character ``position`` (the context of that mention)."""
    start = max(
        text.rfind(". ", 0, position),
        text.rfind("! ", 0, position),
        text.rfind("? ", 0, position),
        text.rfind("\n", 0, position),
    )
    ends = [
        i
        for i in (
            text.find(". ", position),
            text.find("! ", position),
            text.find("? ", position),
            text.find("\n", position),
        )
        if i != -1
    ]
    end = min(ends) if ends else len(text)
    return text[start + 1 : end + 1].strip()


_HEAD_NOUNS = frozenset(
    "event festival fest carnival celebration tournament cup showdown takeover championship "
    "gala invasion rally update season patch model release".split()
)


def _head_noun_refs(name: str) -> set[str]:
    """Nominal back-references: 'Frostfall Festival' can be called 'the festival'."""
    last = name.split()[-1].lower() if name.split() else ""
    return {f"the {last}"} if last in _HEAD_NOUNS else set()


def _sentence_scopes(sents: list[str], names: dict[str, set[str]]) -> dict[str, list[str]]:
    """Sentences relevant to each entity, with a simple subject-carry coreference rule.

    A sentence that mentions no entity ("Developers can run it locally") is attributed to
    the first entity mentioned in the most recent sentence that mentioned one.
    """
    scopes: dict[str, list[str]] = {key: [] for key in names}
    subject: str | None = None
    for sentence in sents:
        lowered = sentence.lower()
        positions = {
            key: min(lowered.find(a.lower()) for a in aliases if a and a.lower() in lowered)
            for key, aliases in names.items()
            if any(a and a.lower() in lowered for a in aliases)
        }
        # Entities in the opening words are the sentence subject. A later mention ("...
        # upgrade from Lattice 4.0", "... run it locally with Ollama") is an object: the
        # sentence's facts belong to the carried subject, not to it.
        words = lowered.split()
        head_limit = len(" ".join(words[:5]))
        head = {k: p for k, p in positions.items() if p <= head_limit}
        if head:
            subject = min(head, key=lambda k: (head[k], -len(max(names[k], key=len))))
            for key in head:
                scopes[key].append(sentence)
        elif subject is not None:
            scopes[subject].append(sentence)
        else:
            for key in positions:
                scopes[key].append(sentence)
    return scopes


def _finalise(mentions: list[_Mention], text: str, item: DiscoveryItem) -> list[ExtractedEntity]:
    """Merge mentions per entity identity and attach grounded attributes / relations."""
    sents = sentences(text) or [text]
    merged: dict[str, _Mention] = {}
    for m in mentions:
        key = entity_key(m.category.value, m.entity_type.value, m.name)
        current = merged.get(key)
        if current is None:
            m.aliases.add(m.matched_text)
            merged[key] = m
        else:
            current.aliases.add(m.matched_text)
            current.flags |= m.flags
            if m.confidence > current.confidence:
                current.confidence = m.confidence
                current.extractor = m.extractor

    results: list[ExtractedEntity] = []
    names = dict(merged)
    scopes = _sentence_scopes(
        sents, {key: m.aliases | {m.name} | _head_noun_refs(m.name) for key, m in names.items()}
    )
    for key, m in names.items():
        own = scopes.get(key, [])
        scope = " ".join([item.title, *own])
        attributes = {attr for attr, pattern in ATTRIBUTE_PATTERNS.items() if pattern.search(scope)}
        flags = set(m.flags)
        confidence = m.confidence
        if "speculative" in attributes:
            flags.add("speculative")
            confidence -= 0.2
        attributes.discard("speculative")
        related: list[str] = []
        for other_key, other in names.items():
            if other_key == key or other.entity_type is not m.entity_type:
                continue
            if normalize_name(other.name) == normalize_name(m.parent_name or "") or (
                normalize_name(other.parent_name or "") == normalize_name(m.name)
            ):
                continue  # a version is not a competitor of its own family
            if _compared(m, other, text):
                related.append(other.name)
        level = next((f.split(":", 1)[1] for f in flags if f.startswith("release_level:")), None)
        if (
            level is None
            and m.version
            and m.entity_type
            not in (
                EntityType.GAME_UPDATE,
                EntityType.GAME_EVENT,
            )
        ):
            level = release_level(m.version)
            flags.add(f"release_level:{level}")
        confidence = max(0.0, min(1.0, confidence))
        status = EntityStatus.VERIFIED
        evidence_type = item.evidence_type
        if item.is_synthetic and "simulated_evidence_type" in item.metadata:
            evidence_type = EvidenceType(item.metadata["simulated_evidence_type"])
        if m.origin is not EntityOrigin.GAZETTEER and (
            evidence_type not in TRUSTED_EVIDENCE or "speculative" in flags
        ):
            status = EntityStatus.NEEDS_VERIFICATION
        if "ambiguous_context" in flags:
            status = EntityStatus.NEEDS_VERIFICATION
        results.append(
            ExtractedEntity(
                canonical_name=m.name,
                entity_type=m.entity_type,
                category=m.category,
                matched_text=m.matched_text,
                aliases=tuple(sorted(a for a in m.aliases if a and a != m.name)),
                confidence=round(confidence, 3),
                extractor=m.extractor,
                origin=m.origin,
                parent_name=m.parent_name,
                parent_type=m.parent_type,
                version=m.version,
                attributes=tuple(sorted(attributes)),
                related=tuple(sorted(related)),
                status=status,
                flags=tuple(sorted(flags)),
            )
        )
    return sorted(results, key=lambda e: (e.category.value, e.entity_type.value, e.canonical_name))


# ------------------------------------------------------------------------- grounding


def is_grounded(name: str, item: DiscoveryItem) -> bool:
    """True if the entity name (or the item's declared subject) is present in the source."""
    haystack = normalize_name(item.full_text)
    if normalize_name(name) in haystack:
        return True
    hint = item.entity_hint
    return bool(hint) and normalize_name(name).startswith(normalize_name(hint or ""))


def ground(entity: ExtractedEntity, item: DiscoveryItem) -> ExtractedEntity:
    """Reject entities whose names do not occur in the supplied source material."""
    if is_grounded(entity.canonical_name, item):
        return entity
    return entity.model_copy(
        update={
            "status": EntityStatus.REJECTED,
            "flags": tuple(sorted({*entity.flags, "ungrounded"})),
            "confidence": 0.0,
        }
    )


class CompositeExtractor:
    """Runs the deterministic extractor plus optional extra extractors, grounding all."""

    def __init__(
        self, deterministic: DeterministicExtractor, extra: Sequence[EntityExtractor] = ()
    ) -> None:
        self.name = "+".join([deterministic.name, *(e.name for e in extra)])
        self._deterministic = deterministic
        self._extra = list(extra)

    def extract(self, item: DiscoveryItem) -> list[ExtractedEntity]:
        results = {
            entity_key(e.category.value, e.entity_type.value, e.canonical_name): e
            for e in self._deterministic.extract(item)
        }
        for extractor in self._extra:
            for proposed in extractor.extract(item):
                checked = ground(
                    proposed.model_copy(update={"origin": EntityOrigin.LLM_PROPOSED}), item
                )
                key = entity_key(
                    checked.category.value, checked.entity_type.value, checked.canonical_name
                )
                if key not in results:
                    results[key] = checked
        return list(results.values())
