"""Deterministic text utilities shared by claim extraction and verification."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence

_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)*")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])|\n+")
_ABBREV = re.compile(r"\b(?:vs|e\.g|i\.e|etc|approx|inc|ltd|no|ver|v)\.$", re.IGNORECASE)

STOPWORDS = frozenset(
    """a an the and or but if then than that this these those there here of in on at to for
    from by with without about into onto over under after before during since until as is
    are was were be been being am do does did doing has have had having it its it's they
    them their theirs we us our you your yours he she him her his i me my mine who whom
    which what when where why how will would shall should can could may might must also
    just so such very too more most much many some any each every all both either neither
    not no nor only own same other others another via per vs versus up down out off again
    further once one ones s t now today yet still even ever like within upon among across
    while whether though although because""".split()
)

# Words that carry no checkable fact in a video title or rationale (advice, format,
# generic nouns). A title may use them without evidence.
NEUTRAL_VOCABULARY = frozenset(
    """how guide guides tutorial tutorials explained explain explainer review reviews best
    tip tips trick tricks beginner beginners complete everything need needs know knowing
    first look looks hands setup set use uses using used get gets getting got find finding
    location locations top ultimate full quick quickly easy easiest step steps faq worth
    ways way comparison compare compared comparing which better fix fixes fixing error errors
    problem problems install installing installation build building make making create
    creating start starting started learn learning course walkthrough demo demos showcase
    test tests tested testing real world honest deep dive overview intro introduction basics
    basic advanced pro free fast faster unlock unlocking challenge challenges quest quests
    gameplay play playing strategy strategies win winning video videos watch should worth it
    new latest week weeks month months day days year years thing things work works working
    really actually simple simply right wrong good great amazing awesome insane crazy huge
    big small vs versus analysis breakdown answer answers question questions part parts
    official update updates news today now live early everyone people beginner's ready
    run running try trying tried feature features detail details price pricing cost costs
    spec specs performance benchmark benchmarks local locally online offline api apps app
    model models tool tools workflow workflows code coding developer developers creators
    creator worth upgrade upgrading switch switching""".split()
)

WORD_NUMBERS = {
    "doubled": 2.0,
    "tripled": 3.0,
    "quadrupled": 4.0,
    "twice": 2.0,
    "double": 2.0,
    "triple": 3.0,
}


def fold(text: str) -> str:
    """Unicode-normalised, case-folded text with dashes/underscores/slashes as spaces."""
    t = unicodedata.normalize("NFKC", text).casefold()
    t = t.replace("\u2019", "'").replace("\u2018", "'")
    return re.sub("[-_/\u2010-\u2015]+", " ", t)


def stem(token: str) -> str:
    if token.endswith("'s"):
        token = token[:-2]
    if len(token) > 3 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tokens(text: str) -> list[str]:
    return [stem(t) for t in _TOKEN.findall(fold(text))]


def token_set(text: str) -> frozenset[str]:
    return frozenset(tokens(text))


def content_terms(text: str, extra_stopwords: Iterable[str] = ()) -> list[str]:
    stops = STOPWORDS | frozenset(extra_stopwords)
    out = []
    for t in tokens(text):
        if t in stops or (len(t) < 2 and not t.isdigit()):
            continue
        out.append(t)
    return out


def contains_sequence(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    if not needle:
        return False
    n = len(needle)
    first = needle[0]
    for i, tok in enumerate(haystack):
        if tok == first and list(haystack[i : i + n]) == list(needle):
            return True
    return False


def contains_phrase(text: str, phrase: str) -> bool:
    return contains_sequence(tokens(text), tokens(phrase))


def sentences(text: str) -> list[str]:
    """Split prose into sentences (keeps decimals and common abbreviations intact)."""
    parts: list[str] = []
    buffer = ""
    for chunk in _SENTENCE_END.split(text.strip()):
        if not chunk or not chunk.strip():
            continue
        candidate = f"{buffer} {chunk}".strip() if buffer else chunk.strip()
        if _ABBREV.search(candidate):
            buffer = candidate
            continue
        parts.append(candidate)
        buffer = ""
    if buffer:
        parts.append(buffer)
    return parts


def fingerprint_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(" ".join(tokens(text)).encode("utf-8")).hexdigest()[:24]
