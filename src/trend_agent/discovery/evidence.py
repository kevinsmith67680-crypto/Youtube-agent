"""Evidence quality and independent-source corroboration.

Corroboration counts **independent origins**, not mentions. Items are the same origin when
they share a canonical URL, a publisher, a (provider, author) pair, or near-identical text
(syndicated/reposted copies). Ten reposts of one press release are one origin.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from trend_agent.discovery.extraction import ATTRIBUTE_PATTERNS
from trend_agent.discovery.models import EvidenceType
from trend_agent.discovery.text import canonical_url, domain_of, jaccard, shingles

BASE_QUALITY: dict[EvidenceType, float] = {
    EvidenceType.OFFICIAL_ANNOUNCEMENT: 90.0,
    EvidenceType.CODE_RELEASE: 85.0,
    EvidenceType.RELATED_QUERY: 80.0,
    EvidenceType.SEARCH_SUGGESTION: 75.0,
    EvidenceType.VIDEO_TITLE: 55.0,
    EvidenceType.VIDEO_DESCRIPTION: 45.0,
    EvidenceType.CURATED_SEED: 50.0,
    EvidenceType.COMMUNITY_DISCUSSION: 35.0,
    EvidenceType.SYNTHETIC_TEST_DATA: 50.0,
}

FLAG_PENALTIES: dict[str, float] = {
    "missing_published_at": 15.0,
    "conflicting_dates": 10.0,
    "outdated": 30.0,
    "future_dated": 20.0,
    "speculative": 25.0,
    "low_quality_claim": 20.0,
}

SYNDICATION_CONTAINMENT = 0.6
SHORT_TEXT_JACCARD = 0.8
MIN_SHINGLES = 8


@dataclass(frozen=True)
class EvidenceView:
    item_id: int | str
    provider: str
    evidence_type: EvidenceType  # effective type (simulated type for synthetic data)
    source_reference: str
    title: str
    collected_at: datetime
    url: str | None = None
    publisher: str | None = None
    author: str | None = None
    excerpt: str | None = None
    published_at: datetime | None = None
    reliability: float = 0.7
    flags: frozenset[str] = field(default_factory=frozenset)

    @property
    def observed_at(self) -> datetime:
        return self.published_at or self.collected_at


def quality_flags(
    *,
    evidence_type: EvidenceType,
    title: str,
    text: str | None,
    url: str | None,
    published_at: datetime | None,
    collected_at: datetime,
    max_age_days: int,
) -> frozenset[str]:
    flags: set[str] = set()
    if published_at is None:
        flags.add("missing_published_at")
    else:
        if published_at < collected_at - timedelta(days=max_age_days):
            flags.add("outdated")
        if published_at > collected_at + timedelta(days=1):
            flags.add("future_dated")
    body = f"{title} {text or ''}"
    if ATTRIBUTE_PATTERNS["speculative"].search(body):
        flags.add("speculative")
    if evidence_type is EvidenceType.COMMUNITY_DISCUSSION and (not url or len(title.split()) < 4):
        flags.add("low_quality_claim")
    return frozenset(flags)


def evidence_quality(view: EvidenceView) -> float:
    base = BASE_QUALITY.get(view.evidence_type, 40.0)
    score = base * (0.6 + 0.4 * max(0.0, min(1.0, view.reliability)))
    score -= sum(FLAG_PENALTIES.get(flag, 0.0) for flag in view.flags)
    return round(max(0.0, min(100.0, score)), 2)


def _origin_tokens(view: EvidenceView) -> frozenset[str]:
    return shingles(f"{view.title} {view.excerpt or ''}")


def same_text(a: frozenset[str], b: frozenset[str]) -> bool:
    """Syndicated / reposted copies of one text.

    Substantial texts (>= ``MIN_SHINGLES`` 3-word shingles) match on *containment*: a
    rewritten repost that keeps most of the original counts as a copy even with a new
    title or an extra sentence. Short texts (titles) must be near-identical, since
    containment over a handful of shingles would merge unrelated posts.
    """
    if not a or not b:
        return False
    smaller = min(len(a), len(b))
    if smaller >= MIN_SHINGLES:
        return len(a & b) / smaller >= SYNDICATION_CONTAINMENT
    return jaccard(a, b) >= SHORT_TEXT_JACCARD


def independent_origins(evidence: Sequence[EvidenceView]) -> list[list[EvidenceView]]:
    """Group evidence into independent origins (union-find over sameness rules)."""
    n = len(evidence)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    keys: dict[str, int] = {}
    toks = [_origin_tokens(v) for v in evidence]
    for i, view in enumerate(evidence):
        identity = [
            f"url:{canonical_url(view.url)}" if view.url else None,
            f"pub:{(view.publisher or '').lower()}" if view.publisher else None,
            f"dom:{domain_of(view.url)}" if view.url and not view.publisher else None,
            f"author:{view.provider}:{view.author.lower()}" if view.author else None,
        ]
        for key in identity:
            if key is None:
                continue
            if key in keys:
                union(i, keys[key])
            else:
                keys[key] = i
        for j in range(i):
            if same_text(toks[i], toks[j]):
                union(i, j)
    groups: dict[int, list[EvidenceView]] = {}
    for i, view in enumerate(evidence):
        groups.setdefault(find(i), []).append(view)
    return sorted(groups.values(), key=lambda g: min(str(v.item_id) for v in g))


def corroboration_score(independent: int) -> float:
    """0 origins -> 0; each further independent origin adds less (1 -> 30, 2 -> 51, 3 -> 66)."""
    if independent <= 0:
        return 0.0
    return round(100.0 * (1.0 - 0.7**independent), 2)


_ISO_DATE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
_LONG_DATE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|"
    r"December)\s+(\d{1,2}),\s+(20\d\d)\b"
)
_RELEASE_CUE = re.compile(r"(?i)\b(releas\w*|launch\w*|available|ships?|out)\b")
_MONTHS = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]


def claimed_release_dates(text: str, names: Sequence[str] = ()) -> list[date]:
    """Explicit release dates stated near a release cue (e.g. 'released on May 3, 2026').

    With ``names``, only sentences naming one of them count, so a date claimed for one
    product is not attributed to another product mentioned in the same article.
    """
    found: list[date] = []
    # Whole-name matches only: "Lattice 5.0" must not match "Lattice 5.0.1", nor
    # "Nimbus Coder" a sentence about "Nimbus Coder 2".
    patterns = [
        re.compile(r"(?<![\w.])" + re.escape(n) + r"(?!\w|\.\d|\s+\d)", re.IGNORECASE)
        for n in names
        if n
    ]
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if not _RELEASE_CUE.search(sentence):
            continue
        if patterns and not any(p.search(sentence) for p in patterns):
            continue
        for y, m, d in _ISO_DATE.findall(sentence):
            try:
                found.append(date(int(y), int(m), int(d)))
            except ValueError:
                continue
        for month, d, y in _LONG_DATE.findall(sentence):
            try:
                found.append(date(int(y), _MONTHS.index(month.lower()) + 1, int(d)))
            except ValueError:
                continue
    return found


def resolve_release_date(
    claims: Sequence[tuple[date, EvidenceType]],
) -> tuple[date | None, bool]:
    """Pick the most authoritative claimed date; report whether sources conflict (> 1 day)."""
    if not claims:
        return None, False
    rank = [
        EvidenceType.OFFICIAL_ANNOUNCEMENT,
        EvidenceType.CODE_RELEASE,
        EvidenceType.VIDEO_DESCRIPTION,
        EvidenceType.VIDEO_TITLE,
        EvidenceType.COMMUNITY_DISCUSSION,
    ]
    ordered = sorted(claims, key=lambda c: (rank.index(c[1]) if c[1] in rank else len(rank), c[0]))
    dates = {d for d, _ in claims}
    conflict = (max(dates) - min(dates)).days > 1
    return ordered[0][0], conflict
