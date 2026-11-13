"""Text utilities shared by extraction, clustering and corroboration."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TOKEN = re.compile(r"[a-z0-9]+(?:[.+#][a-z0-9+#]+)*\+*", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
# Separator between a name and its version number: "Qwen 3", "GPT-5", "Llama_4" -> no gap.
_NAME_VERSION_GAP = re.compile(r"(?<=[a-z])[\s\-_]+(?=v?\d)")
# Trailing ".0" groups of a version: "5.0.0" == "5.0" == "5"; "1.0.5" is untouched.
_TRAILING_ZEROS = re.compile(r"(?<=\d)(?:\.0)+(?![\d.])")

STOPWORDS = frozenset(
    """
    a an the and or of for to in on at by with from into about as is are be was were it its
    this that these those i you we they my your our their me us do does did can could will
    would should new latest now just get got more most very all any some than then so if
    """.split()
)

NUMERIC = re.compile(r"^v?\d+(?:\.\d+)*[a-z]?$")


def strip_html(text: str) -> str:
    return _SPACE.sub(" ", html.unescape(_TAG.sub(" ", text))).strip()


def normalize_text(text: str) -> str:
    return _SPACE.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(unicodedata.normalize("NFKC", text))]


def content_tokens(text: str) -> list[str]:
    return [t for t in tokens(text) if t not in STOPWORDS]


def numbers_in(text: str) -> frozenset[str]:
    """Version-like tokens (used to stop '... 4' merging with '... 5')."""
    return frozenset(t.lstrip("v") for t in tokens(text) if NUMERIC.match(t))


def jaccard(a: set[str] | frozenset[str], b: set[str] | frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def normalize_name(name: str) -> str:
    """Comparison key for entity names: case-folded, version gaps removed.

    'Qwen 3', 'qwen-3' and 'Qwen3' share a key; 'GPT-4o' and 'GPT-4' do not.
    """
    text = normalize_text(name).casefold()
    text = _NAME_VERSION_GAP.sub("", text)
    text = _TRAILING_ZEROS.sub("", text)
    text = re.sub(r"^the\s+", "", text)
    return _SPACE.sub(" ", text).strip()


def entity_key(category: str, entity_type: str, name: str) -> str:
    """Identity of an entity. Same name in another category or type is another entity."""
    return f"{category}|{entity_type}|{normalize_name(name)}"


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text) if s.strip()]


def shingles(text: str, size: int = 3) -> frozenset[str]:
    words = content_tokens(text)
    if len(words) < size:
        return frozenset([" ".join(words)]) if words else frozenset()
    return frozenset(" ".join(words[i : i + size]) for i in range(len(words) - size + 1))


def fingerprint(text: str) -> str:
    return hashlib.sha256(" ".join(content_tokens(text)).encode()).hexdigest()


_TRACKING_PARAMS = re.compile(r"^(utm_|ref$|ref_src$|source$|fbclid$|gclid$)")


def canonical_url(url: str | None) -> str | None:
    """Normalise a URL for syndication detection (scheme, www, tracking params, slash)."""
    if not url:
        return None
    parts = urlsplit(url.strip())
    if not parts.netloc:
        return url.strip()
    host = parts.netloc.lower().removeprefix("www.")
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAMS.match(k.lower())]
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, query, ""))


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    return host or None


def display_version(version: str) -> str:
    """Conventional display form: '5.0.0' -> '5.0', '0.33.0' -> '0.33', '1.2.3' unchanged."""
    v = version.lstrip("vV")
    parts = v.split(".")
    while len(parts) > 2 and parts[-1] == "0":
        parts.pop()
    return ".".join(parts)
