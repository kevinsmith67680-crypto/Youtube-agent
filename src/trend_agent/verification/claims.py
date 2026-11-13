"""Deterministic claim extraction.

Two sources of claims are verified:

* **Declared claims** - the ``factual_claims`` a generator lists, with its citations.
* **Extracted claims** - every factual-looking sentence of the title, rationale and
  audience-intent text. A generator cannot avoid verification by leaving a statement out
  of ``factual_claims``.

``extract`` turns a sentence into checkable elements (numbers with their kind and unit,
dates, trend/search wording, release-status wording, certainty and attribution markers,
product-detail verbs, URLs). The extraction is rule-based and English-only; its known
blind spots are listed in docs/verification_layer.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from enum import StrEnum

from trend_agent.schemas.verification import (
    ClaimCertainty,
    ClaimDraft,
    ClaimOrigin,
    ClaimType,
    RecommendationDraft,
)
from trend_agent.verification.text import WORD_NUMBERS, fold, sentences
from trend_agent.verification.untrusted import URL


class NumberKind(StrEnum):
    PERCENT = "percent"
    MULTIPLIER = "multiplier"
    SEARCH_COUNT = "search_count"  # an absolute number of searches/queries
    COUNT = "count"  # views, players, downloads, ...
    CURRENCY = "currency"
    QUANTITY = "quantity"  # a value with a physical/technical unit
    STATISTIC = "statistic"  # severity, score, p-value, confidence, ...
    PLAIN = "plain"


DETAIL_KINDS = frozenset(
    {NumberKind.COUNT, NumberKind.CURRENCY, NumberKind.QUANTITY, NumberKind.PLAIN}
)


@dataclass(frozen=True)
class NumberMention:
    raw: str
    value: float
    kind: NumberKind
    unit: str | None
    start: int
    end: int


@dataclass(frozen=True)
class DateMention:
    raw: str
    year: int | None
    month: int | None = None
    day: int | None = None

    @property
    def granularity(self) -> str:
        return "day" if self.day else "month" if self.month else "year"

    def agrees(self, other: DateMention) -> bool:
        """Equal at this mention's granularity (other must be at least as precise)."""
        if self.year is not None and other.year != self.year:
            return False
        if self.month is not None and other.month != self.month:
            return False
        return not (self.day is not None and other.day != self.day)


@dataclass(frozen=True)
class TrendFeatures:
    search: bool  # refers to searching / search interest / demand
    youtube: bool  # refers to YouTube Search specifically
    growth: bool
    decline: bool
    volume: bool  # absolute search volume
    statistical: tuple[str, ...]  # statistical vocabulary used
    confirmed_wording: bool


@dataclass(frozen=True)
class Features:
    text: str
    numbers: tuple[NumberMention, ...]
    dates: tuple[DateMention, ...]
    trend: TrendFeatures | None
    release: str | None  # released | announced | upcoming | None
    denial: bool
    novelty: bool
    speculative: bool
    attributed: bool
    feature_assertion: bool
    urls: tuple[str, ...]
    identifiers: tuple[str, ...]

    @property
    def factual(self) -> bool:
        return bool(
            self.numbers
            or self.dates
            or self.trend
            or self.release
            or self.denial
            or self.novelty
            or self.feature_assertion
            or self.urls
        )


_NUM = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_MAG = r"(?:\s*(k|m|b|bn|mn|thousand|million|billion)\b)?"
_MAGNITUDE = {
    "k": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "mn": 1e6,
    "million": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
}
_P_PERCENT = re.compile(_NUM + r"\s*(%|percent\b|per cent\b|pct\b)", re.IGNORECASE)
_P_MULT = re.compile(_NUM + r"\s*(x|\u00d7|times|fold)(?![a-z0-9])", re.IGNORECASE)
_P_SEARCH_COUNT = re.compile(
    _NUM + _MAG + r"\+?\s+(?:monthly\s+|daily\s+|weekly\s+|new\s+)?(searches|search queries|"
    r"queries|search volume|people (?:are )?search(?:ing|ed)?)\b",
    re.IGNORECASE,
)
_P_COUNT = re.compile(
    _NUM + _MAG + r"\+?\s+(?:monthly\s+|daily\s+|weekly\s+|new\s+|active\s+)?(views|viewers|"
    r"people|users|players|subscribers|downloads|videos|uploads|mentions|posts|stars|"
    r"comments|likes|items|skins|weapons|levels|maps|characters|languages)\b",
    re.IGNORECASE,
)
_P_CURRENCY = re.compile(
    r"([$€£¥])\s?" + _NUM + _MAG + r"|" + _NUM + _MAG + r"\s*(usd|eur|gbp|dollars|euros|"
    r"pounds|v-?bucks)\b",
    re.IGNORECASE,
)
_P_QUANTITY = re.compile(
    _NUM + _MAG + r"\s*(gb|tb|mb|kb|ghz|mhz|w|watts?|hz|fps|mp|mah|nm|inch(?:es)?|cores?|threads?|"
    r"tokens?|parameters?|params|b parameters|k context|context|hours?|hrs?|minutes?|mins?|"
    r"seconds?|secs?|days?|weeks?|months?|years?|gbps|mbps|tflops|tops|bit|bits)\b",
    re.IGNORECASE,
)
_P_STAT = re.compile(
    r"\b(severity|anomaly score|z[ -]?score|cusum|p[ -]?value|confidence(?: level| score)?|"
    r"significance|surge score|trend score|readiness|freshness)\b(?:\s*(?:of|is|was|at|=|:|<|>"
    r"|≤|≥|<=|>=))?\s*" + _NUM,
    re.IGNORECASE,
)
_P_PVAL = re.compile(r"\bp\s*(?:<|=|<=|≤)\s*" + _NUM, re.IGNORECASE)
_P_ANY_NUMBER = re.compile(r"(?<![\w.])" + _NUM + r"(?![\w])")
_IDENT_KEYWORD = re.compile(
    r"\b(?:version|ver|v|season|chapter|patch|update|gen|generation|series|model|mark|mk|"
    r"part|episode|ep|level|tier|wave|act|volume|vol|phase|build|release|no|number)\.?\s*#?$|#$",
    re.IGNORECASE,
)
# A name token right before the number: GPT-5, RTX 5090, Python 3.13, iPhone 17, gpt-5.
_IDENT_NAME = re.compile(r"(?:\b[A-Z][A-Za-z]*|\b[a-z]+[A-Z][A-Za-z]*|\b[A-Za-z]+(?=-))[\s-]?$")
_NOT_NAMES = frozenset(
    """over about nearly around almost roughly approximately under top best first last only
    just more less than up to by in on at from since after before for with and or of the a an
    is was are were be been has have had costs cost priced rose rise jumped grew fell surged
    spiked increased decreased doubled reached hit hits got gets added adds supports includes
    offers drops dropped ships shipped took takes needs need requires required every per day
    days week weeks month months year years hour hours minute minutes""".split()
)
_IDENT_SUFFIX = re.compile(
    r"^(?:\.\d|[- ](?:pro|max|ultra|mini|plus|ti|super|turbo|flash|lite|nano|xl|se)\b)",
    re.IGNORECASE,
)
_W_SEARCH_DISCLAIMER = re.compile(
    r"\b(?:not|isn't|is not|are not|aren't|never|no|without)\b[^.]{0,30}\b(?:search|"
    r"demand|queries)\b",
    re.IGNORECASE,
)

_MONTHS = {
    m: i + 1
    for i, ms in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        )
    )
    for m in ms
}
MONTH_NAMES = frozenset(_MONTHS)
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_P_ISO = re.compile(r"\b(20\d{2}|19\d{2})-(\d{2})-(\d{2})\b")
_P_MDY = re.compile(
    rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+((?:19|20)\d{{2}}))?\b",
    re.IGNORECASE,
)
_P_DMY = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_RE})\.?(?:,?\s+((?:19|20)\d{{2}}))?\b",
    re.IGNORECASE,
)
_P_MY = re.compile(rf"\b({_MONTH_RE})\.?\s+((?:19|20)\d{{2}})\b", re.IGNORECASE)
_P_YEAR = re.compile(r"(?<![\w.-])((?:19|20)\d{2})(?![\w.-])")

_W_SEARCH = re.compile(
    r"\bsearch(?:es|ed|ing)?\b|\bqueries\b|\blooked up\b|\bgoogl(?:ed|ing)\b|"
    r"\b(?:search|viewer|audience|user)? ?demand\b|\binterest (?:in|for)\b.{0,40}\b(?:is|has|"
    r"are)\b|\b(?:people|everyone|viewers|users|audiences?|gamers|developers)\b.{0,25}\b(?:"
    r"look(?:ing)? for|want(?:ing)?|asking (?:for|about))\b",
    re.IGNORECASE | re.DOTALL,
)
_W_YOUTUBE_SEARCH = re.compile(
    r"\byoutube\b.{0,30}\b(?:search|queries|searches|searching|demand)\b|\b(?:search(?:es|ing)?"
    r"|queries|demand|looked up)\b.{0,40}\b(?:on|in|across) youtube\b",
    re.IGNORECASE | re.DOTALL,
)
_W_GROWTH = re.compile(
    r"\b(?:surg(?:e|es|ed|ing)|spik(?:e|es|ed|ing)|skyrocket\w*|soar\w*|explod\w*|explosion|"
    r"boom\w*|grow(?:s|ing|n|th)?|grew|ris(?:e|es|ing|en)|rose|increas\w*|jump\w*|doubl\w*|"
    r"gone up|went up|going up|"
    r"tripl\w*|quadrupl\w*|trending|viral|breakout|climb\w*|rocket\w*|blowing up|taking off|"
    r"through the roof|hot topic|on fire|peak(?:ed|ing)?|record high|all[- ]time high|"
    r"high demand|massive interest|huge interest|rapidly)\b",
    re.IGNORECASE,
)
_W_DECLINE = re.compile(
    r"\b(?:declin\w*|dropp(?:ed|ing)|fell|falling|decreas\w*|cool(?:ed|ing) off|fad(?:ed|ing))\b",
    re.IGNORECASE,
)
_W_VOLUME = re.compile(
    r"\bsearch volume\b|\bmonthly searches\b|\bsearches (?:per|a|each|every) (?:day|month|week|"
    r"hour)\b|\bnumber of searches\b",
    re.IGNORECASE,
)
_W_STAT = re.compile(
    r"\b(?:statistically significant|statistical(?:ly)? (?:confirmed|proven)|significan(?:t|ce)"
    r"|anomaly score|z[ -]?score|cusum|p[ -]?value|confidence (?:level|interval|score)|"
    r"severity|surge score|trend score|baseline|readiness)\b",
    re.IGNORECASE,
)
_W_CONFIRMED = re.compile(
    r"\b(?:confirmed|verified|proven|definitely|guaranteed|official(?:ly)?)\b", re.IGNORECASE
)
_W_RELEASED = re.compile(
    r"\b(?:released|launched|releases(?! on| in| next)|launches(?! on| in| next)|ships(?! on| in)|"
    r"is out|are out|out now|now (?:out|available|live)|available now|"
    r"is available|are available|shipped|went live|is live|rolled out|rolling out|dropped|"
    r"has arrived|arrived|landed|now in the (?:item )?shop|in the item shop|is back)\b",
    re.IGNORECASE,
)
_W_ANNOUNCED = re.compile(
    r"\b(?:announced|unveiled|revealed|introduced|teased|confirmed)\b", re.IGNORECASE
)
_W_UPCOMING = re.compile(
    r"\b(?:upcoming|coming soon|coming (?:next|this|in)|will (?:be )?(?:released|launch\w*|"
    r"arrive|ship|drop|debut|come)|set to (?:launch|release|arrive|debut)|expected (?:to|on|in)|"
    r"scheduled (?:for|to)|planned for|pre-?orders?|launches on|releases on|arrives on|"
    r"to arrive|next (?:week|month|season|year|update|patch))\b",
    re.IGNORECASE,
)
_W_DENIAL = re.compile(
    r"\b(?:cancel+ed|cancel+ing|delayed|postponed|scrapped|discontinued|not (?:coming|real|"
    r"happening|true|available|released|planned)|fake|hoax|debunked|false|denied|denies|no plans|"
    r"does not exist|doesn't exist|never (?:existed|announced|released)|isn't real|is not real|"
    r"misleading|clickbait|fabricated|not (?:an? )?official)\b",
    re.IGNORECASE,
)
_W_NOVELTY = re.compile(
    r"\b(?:new|newly|brand[- ]new|just (?:announced|released|launched|dropped|arrived)|latest|"
    r"this week|today|yesterday|recently|breaking|first look|fresh)\b",
    re.IGNORECASE,
)
_W_SPECULATIVE = re.compile(
    r"\b(?:rumou?r(?:ed|s)?|leak(?:ed|s)?|alleged(?:ly)?|reportedly|unconfirmed|speculat\w*|"
    r"might|may|could|possibly|potential(?:ly)?|is said to|are said to|tipped|likely|"
    r"expected|appears to|seems to|supposedly|according to rumou?rs)\b",
    re.IGNORECASE,
)
_W_ATTRIBUTION = re.compile(
    r"\b(?:according to|reported by|reports? from|per (?:the|a|an)|says|said|claims?|claimed|"
    r"stated|states|announced by|wrote|posted|in (?:a|an|its|their|his|her) (?:post|blog|"
    r"announcement|tweet|video|thread|statement|changelog|release notes))\b",
    re.IGNORECASE,
)
_W_FEATURE = re.compile(
    r"\b(?:supports?|supported|includes?|including|features?|featuring|has|have|comes with|"
    r"offers?|adds?|added|introduces?|lets (?:you|users)|allows?|can now|now (?:has|supports|"
    r"includes|lets)|costs?|priced at|runs on|requires?|compatible with|works (?:with|on)|"
    r"gives|grants|unlocks|located|found (?:at|in|near)|spawns?|drops? (?:from|at|in)|"
    r"available (?:on|for|in)|capable of|enables?|integrates? with|powered by|built on)\b",
    re.IGNORECASE,
)


def _value(raw: str, magnitude: str | None) -> float:
    v = float(raw.replace(",", ""))
    if magnitude:
        v *= _MAGNITUDE.get(magnitude.lower(), 1.0)
    return v


def _is_identifier(text: str, start: int, end: int) -> bool:
    """A number that is part of a name or version (GPT-5, RTX 5090, Chapter 6, v2.1)."""
    before = text[max(0, start - 24) : start]
    after = text[end : end + 8]
    if _IDENT_KEYWORD.search(before):
        return True
    name = _IDENT_NAME.search(before)
    if name is not None:
        word = re.sub(r"[\s-]+$", "", name.group(0)).lower()
        if word not in _NOT_NAMES and word not in _MONTHS:
            return True
    return bool(_IDENT_SUFFIX.match(after))


def numbers(text: str) -> tuple[NumberMention, ...]:
    found: list[NumberMention] = []
    taken: list[tuple[int, int]] = []

    def free(s: int, e: int) -> bool:
        return all(e <= a or s >= b for a, b in taken)

    def add(m: re.Match[str], value: float, kind: NumberKind, unit: str | None) -> None:
        if free(m.start(), m.end()):
            found.append(NumberMention(m.group(0), value, kind, unit, m.start(), m.end()))
            taken.append((m.start(), m.end()))

    for m in _P_STAT.finditer(text):
        add(m, _value(m.group(2), None), NumberKind.STATISTIC, m.group(1).lower())
    for m in _P_PVAL.finditer(text):
        add(m, _value(m.group(1), None), NumberKind.STATISTIC, "p_value")
    for m in _P_PERCENT.finditer(text):
        add(m, _value(m.group(1), None), NumberKind.PERCENT, "%")
    for m in _P_SEARCH_COUNT.finditer(text):
        add(m, _value(m.group(1), m.group(2)), NumberKind.SEARCH_COUNT, "searches")
    for m in _P_CURRENCY.finditer(text):
        if m.group(1):
            add(m, _value(m.group(2), m.group(3)), NumberKind.CURRENCY, m.group(1))
        else:
            add(m, _value(m.group(4), m.group(5)), NumberKind.CURRENCY, m.group(6).lower())
    for m in _P_MULT.finditer(text):
        if not _is_identifier(text, m.start(), m.end()):
            add(m, _value(m.group(1), None), NumberKind.MULTIPLIER, "x")
    for m in _P_COUNT.finditer(text):
        add(m, _value(m.group(1), m.group(2)), NumberKind.COUNT, m.group(3).lower().rstrip("s"))
    for m in _P_QUANTITY.finditer(text):
        if not _is_identifier(text, m.start(), m.end()):
            unit = m.group(3).lower()
            unit = unit[:-1] if unit.endswith("s") and len(unit) > 3 else unit
            add(m, _value(m.group(1), m.group(2)), NumberKind.QUANTITY, unit)
    date_spans = [
        (d.start(), d.end()) for p in (_P_ISO, _P_MDY, _P_DMY, _P_MY) for d in p.finditer(text)
    ]
    date_spans += [(y.start(), y.end()) for y in _P_YEAR.finditer(text)]
    for m in _P_ANY_NUMBER.finditer(text):
        s, e = m.start(), m.end()
        if not free(s, e) or any(not (e <= a or s >= b) for a, b in date_spans):
            continue
        if _is_identifier(text, s, e):
            continue
        add(m, _value(m.group(1), None), NumberKind.PLAIN, None)
    lowered = text.lower()
    for word, factor in WORD_NUMBERS.items():
        for m in re.finditer(rf"\b{word}\b", lowered):
            if free(m.start(), m.end()):
                found.append(
                    NumberMention(word, factor, NumberKind.MULTIPLIER, "x", m.start(), m.end())
                )
    return tuple(sorted(found, key=lambda n: n.start))


def identifiers(text: str) -> tuple[str, ...]:
    taken = [(n.start, n.end) for n in numbers(text)]
    taken += [
        (m.start(), m.end()) for p in (_P_ISO, _P_MDY, _P_DMY, _P_MY) for m in p.finditer(text)
    ]
    out = []
    for m in _P_ANY_NUMBER.finditer(text):
        if any(not (m.end() <= a or m.start() >= b) for a, b in taken):
            continue
        if _is_identifier(text, m.start(), m.end()):
            left = re.search(r"[A-Za-z][\w.]*[\s-]?$", text[: m.start()])
            out.append((left.group(0) if left else "") + m.group(0))
    return tuple(out)


def dates(text: str) -> tuple[DateMention, ...]:
    out: list[DateMention] = []
    spans: list[tuple[int, int]] = []

    def add(m: re.Match[str], year: int | None, month: int | None, day: int | None) -> None:
        if any(not (m.end() <= a or m.start() >= b) for a, b in spans):
            return
        spans.append((m.start(), m.end()))
        out.append(DateMention(m.group(0), year, month, day))

    for m in _P_ISO.finditer(text):
        add(m, int(m.group(1)), int(m.group(2)), int(m.group(3)))
    for m in _P_MDY.finditer(text):
        if m.group(1).lower() == "may" and not m.group(3):
            continue  # "may 5" is too often the modal verb
        add(
            m, int(m.group(3)) if m.group(3) else None, _MONTHS[m.group(1).lower()], int(m.group(2))
        )
    for m in _P_DMY.finditer(text):
        add(
            m, int(m.group(3)) if m.group(3) else None, _MONTHS[m.group(2).lower()], int(m.group(1))
        )
    for m in _P_MY.finditer(text):
        add(m, int(m.group(2)), _MONTHS[m.group(1).lower()], None)
    for m in _P_YEAR.finditer(text):
        add(m, int(m.group(1)), None, None)
    return tuple(out)


def trend_features(text: str, nums: tuple[NumberMention, ...]) -> TrendFeatures | None:
    folded = fold(text)
    disclaimed = bool(_W_SEARCH_DISCLAIMER.search(folded))
    search = bool(_W_SEARCH.search(folded)) and not disclaimed
    youtube = bool(_W_YOUTUBE_SEARCH.search(folded)) and not disclaimed
    growth = bool(_W_GROWTH.search(folded)) or (
        any(n.kind in (NumberKind.PERCENT, NumberKind.MULTIPLIER) for n in nums)
        and bool(re.search(r"\b(up|more|higher|increase|rise|growth|jump)\b", folded))
    )
    decline = bool(_W_DECLINE.search(folded))
    volume = bool(_W_VOLUME.search(folded)) or any(n.kind is NumberKind.SEARCH_COUNT for n in nums)
    stat = tuple(sorted({m.group(0).lower() for m in _W_STAT.finditer(folded)}))
    stat_numbers = any(n.kind is NumberKind.STATISTIC for n in nums)
    if not (search or volume or stat or stat_numbers or growth or decline):
        return None
    if not (search or volume or stat or stat_numbers) and not growth:
        return None
    return TrendFeatures(
        search=search or volume,
        youtube=youtube,
        growth=growth,
        decline=decline,
        volume=volume,
        statistical=stat,
        confirmed_wording=bool(_W_CONFIRMED.search(folded)),
    )


def release_wording(text: str) -> str | None:
    folded = fold(text)
    if _W_UPCOMING.search(folded):
        return "upcoming"
    if _W_RELEASED.search(folded):
        return "released"
    if _W_ANNOUNCED.search(folded):
        return "announced"
    return None


def has_denial(text: str) -> bool:
    return bool(_W_DENIAL.search(fold(text)))


def extract(text: str) -> Features:
    nums = numbers(text)
    folded = fold(text)
    return Features(
        text=text,
        numbers=nums,
        dates=dates(text),
        trend=trend_features(text, nums),
        release=release_wording(text),
        denial=bool(_W_DENIAL.search(folded)),
        novelty=bool(_W_NOVELTY.search(folded)),
        speculative=bool(_W_SPECULATIVE.search(folded)),
        attributed=bool(_W_ATTRIBUTION.search(folded)),
        feature_assertion=bool(_W_FEATURE.search(folded)),
        urls=tuple(URL.findall(text)),
        identifiers=identifiers(text),
    )


def infer_type(f: Features, has_entities: bool) -> ClaimType:
    if f.trend is not None:
        return ClaimType.TREND
    if f.release and f.dates:
        return ClaimType.RELEASE_DATE
    if f.release:
        return ClaimType.RELEASE_STATUS
    if any(n.kind in DETAIL_KINDS for n in f.numbers) or f.feature_assertion:
        return ClaimType.PRODUCT_DETAIL
    if f.numbers:
        return ClaimType.NUMERICAL
    if has_entities:
        return ClaimType.ENTITY_EXISTENCE
    return ClaimType.OTHER


# ------------------------------------------------------------------ candidate claims


@dataclass(frozen=True)
class CandidateClaim:
    index: int
    origin: ClaimOrigin
    field: str
    text: str
    cited_ids: tuple[str, ...]
    declared_type: ClaimType | None = None
    certainty: ClaimCertainty = ClaimCertainty.CONFIRMED
    attribution: str | None = None
    subject: str | None = None
    features: Features = dc_field(default_factory=lambda: extract(""))


def candidate_claims(draft: RecommendationDraft) -> list[CandidateClaim]:
    """Declared claims first, then sentences extracted from free-text fields."""
    out: list[CandidateClaim] = []
    supporting = tuple(draft.supporting_evidence_ids)

    def declared(i: int, c: ClaimDraft) -> CandidateClaim:
        certainty = c.certainty
        f = extract(c.text)
        if certainty is ClaimCertainty.CONFIRMED and f.speculative:
            certainty = ClaimCertainty.SPECULATIVE
        return CandidateClaim(
            index=len(out),
            origin=ClaimOrigin.DECLARED,
            field=f"factual_claims[{i}]",
            text=c.text,
            cited_ids=tuple(c.evidence_ids),
            declared_type=c.claim_type,
            certainty=certainty,
            attribution=c.attribution,
            subject=c.subject,
            features=f,
        )

    for i, c in enumerate(draft.factual_claims):
        out.append(declared(i, c))
    out.append(
        CandidateClaim(
            index=len(out),
            origin=ClaimOrigin.TITLE,
            field="proposed_video_title",
            text=draft.proposed_video_title,
            cited_ids=supporting,
            features=extract(draft.proposed_video_title),
        )
    )
    texts: list[tuple[ClaimOrigin, str, str]] = [
        (ClaimOrigin.RATIONALE, "recommendation_rationale", draft.recommendation_rationale),
        (ClaimOrigin.INTENT, "audience_intent_description", draft.audience_intent_description),
    ]
    texts += [
        (ClaimOrigin.UNCERTAINTY, f"uncertainties[{i}]", u)
        for i, u in enumerate(draft.uncertainties)
    ]
    for origin, name, text in texts:
        for j, sentence in enumerate(sentences(text)):
            f = extract(sentence)
            out.append(
                CandidateClaim(
                    index=len(out),
                    origin=origin,
                    field=f"{name}#{j}",
                    text=sentence,
                    cited_ids=supporting,
                    certainty=ClaimCertainty.SPECULATIVE
                    if f.speculative
                    else ClaimCertainty.CONFIRMED,
                    features=f,
                )
            )
    return out


features = extract  # public alias
