"""Deterministic story classification (with an optional, constrained assist hook).

For each story: kind (Show HN / Ask HN / Launch HN / Tell HN / story), subject (product
launch, release, announcement, question, discussion), primary and secondary category,
relevance, ambiguity, a grounded project name for Show/Launch HN titles, and the search
intents such a story *might* motivate. Everything comes from the title, the linked
domain and (for text posts) the sanitised text: nothing is inferred beyond them.

Story text is untrusted HTML. It is stripped and sanitised, and instruction-like content
is withheld (a flagged title excludes the story entirely).

Optional assistance: a ``StoryAssist`` may break a tie *only* between the categories the
deterministic classifier already found ambiguous; it can never introduce a category,
override a clear decision, or add entities. No assist is configured by default.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from trend_agent.discovery.models import Category, EntityType, SearchIntent
from trend_agent.discovery.text import domain_of, strip_html
from trend_agent.hackernews.config import ClassifierSettings
from trend_agent.hackernews.models import HNItem
from trend_agent.verification.untrusted import injection_signals, sanitize

DEFAULT_TERMS: dict[Category, tuple[str, ...]] = {
    Category.AI: (
        "ai", "llm", "llms", "gpt", "claude", "gemini", "llama", "mistral", "qwen", "deepseek",
        "openai", "anthropic", "agent", "agents", "agentic", "copilot", "coding agent",
        "language model", "foundation model", "inference", "fine-tuning",
        "rag", "embedding", "embeddings", "transformer", "diffusion", "multimodal",
        "speech recognition", "voice ai", "tts", "text-to-speech", "text-to-video",
        "image generation", "video generation", "robot", "robotics", "benchmark", "benchmarks",
        "neural",
        "machine learning", "deep learning", "ml", "mcp", "prompt", "chatbot", "reasoning",
        "open-weight", "open weights", "gguf", "ollama", "local llm", "hugging face",
    ),
    Category.TECHNOLOGY: (
        "framework", "library", "sdk", "api", "apis", "cli", "compiler", "runtime",
        "database", "postgres", "sqlite", "kubernetes", "docker", "cloud", "serverless",
        "rust", "python", "golang", "typescript", "javascript", "zig", "wasm",
        "webassembly", "linux", "kernel", "browser", "terminal", "editor", "ide", "git",
        "open source", "open-source", "self-hosted", "developer", "developers", "devtools",
        "programming", "programming language", "security", "vulnerability", "cve",
        "exploit", "encryption", "infrastructure", "observability", "testing",
    ),
    Category.GAMING: (
        "game", "games", "gaming", "game engine", "godot", "unity", "unreal", "bevy",
        "procedural generation", "multiplayer", "indie game", "steam", "itch.io", "roguelike",
        "emulator", "shader", "voxel", "game dev", "gamedev", "netcode",
    ),
}  # fmt: skip
DEFAULT_DOMAINS: dict[Category, tuple[str, ...]] = {
    Category.AI: (
        "arxiv.org", "huggingface.co", "openai.com", "anthropic.com", "deepmind.google",
        "ai.meta.com", "mistral.ai", "deepseek.com", "ollama.com", "paperswithcode.com",
    ),
    Category.TECHNOLOGY: (
        "crates.io", "pypi.org", "npmjs.com", "lwn.net", "rust-lang.org", "go.dev",
        "python.org", "kernel.org",
    ),
    Category.GAMING: (
        "store.steampowered.com", "itch.io", "godotengine.org", "unity.com",
        "unrealengine.com", "gamedeveloper.com",
    ),
}  # fmt: skip

# A code host says the link is software, not what the story is about (a game or an AI
# tool hosted on GitHub is still a game / an AI tool): a small technology hint only.
CODE_HOSTS = ("github.com", "gitlab.com", "codeberg.org", "sourceforge.net", "sr.ht")
CODE_HOST_WEIGHT = 0.5
TOPICAL_DOMAIN_WEIGHT = 1.5

_PREFIX = re.compile(r"^\s*(?P<kind>Show|Ask|Launch|Tell)\s+HN\s*:\s*", re.IGNORECASE)
# "Show HN: Name - description" (hyphen, en or em dash), "Show HN: Name: ...",
# "Show HN: Name (YC X25) ..."
_PROJECT = re.compile(
    r"^(?P<name>[A-Za-z0-9][\w.+#@/-]*(?:\s[A-Z0-9][\w.+#@/-]*){0,3})\s*(?:[\u2013\u2014:|]|\s-\s|\(|,|$)"
)
_VERSION = re.compile(r"(?<![\w.])v?\d{1,4}\.\d{1,4}(?:\.\d{1,6})?\b")
_RELEASE = re.compile(
    r"(?i)\b(released?|releases|release notes|changelog|is out|now generally available|ga)\b"
)
_ANNOUNCE = re.compile(
    r"(?i)\b(announc\w*|introduc\w*|unveil\w*|launch(?:es|ed|ing)?|now available|"
    r"open[- ]sourc\w*|debuts?)\b"
)
_GENERIC_NAMES = frozenset(
    "i we my our a an the this that it new how why what when where who".split()
)
_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#.-]*")


class StoryAssist(Protocol):
    """Optional tie-breaker (e.g. an LLM). Must return one of ``candidates`` or None."""

    def choose(self, title: str, candidates: Sequence[Category]) -> Category | None: ...


@dataclass
class StoryClassification:
    kind: str  # show_hn | ask_hn | launch_hn | tell_hn | story
    subject: str  # product_launch | release | announcement | question | discussion
    primary: Category  # Category.OTHER when nothing relevant was found
    secondary: Category | None
    relevance: float
    ambiguous: bool
    scores: dict[str, float]
    matched: dict[str, list[str]]
    project_name: str | None
    entity_type: EntityType | None
    intents: list[str]
    flags: list[str] = field(default_factory=list)
    clean_title: str = ""
    clean_text: str | None = None
    domain: str | None = None
    assisted: bool = False

    @property
    def relevant(self) -> bool:
        return self.primary is not Category.OTHER

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "primary_category": self.primary.value,
            "secondary_category": self.secondary.value if self.secondary else None,
            "relevance": self.relevance,
            "ambiguous": self.ambiguous,
            "category_scores": self.scores,
            "matched_terms": self.matched,
            "project_name": self.project_name,
            "entity_type": self.entity_type.value if self.entity_type else None,
            "potential_intents": self.intents,
            "flags": self.flags,
            "domain": self.domain,
            "assisted": self.assisted,
            "basis": "deterministic: title, linked domain and sanitised text only",
        }


def _tokens(text: str) -> str:
    return " " + " ".join(_TOKEN.findall(text.lower())) + " "


def _kind(title: str) -> str:
    m = _PREFIX.match(title)
    return f"{m['kind'].lower()}_hn" if m else "story"


def _subject(kind: str, title: str) -> str:
    if kind in ("show_hn", "launch_hn"):
        return "product_launch"
    if kind == "ask_hn" or title.rstrip().endswith("?"):
        return "question"
    if _VERSION.search(title) and (_RELEASE.search(title) or len(title) < 60):
        return "release"
    if _RELEASE.search(title):
        return "release"
    if _ANNOUNCE.search(title):
        return "announcement"
    return "discussion"


def project_name(title: str) -> str | None:
    """The project a Show/Launch HN title names (grounded: it is the title's own text)."""
    m = _PREFIX.match(title)
    if not m or m["kind"].lower() not in ("show", "launch"):
        return None
    rest = title[m.end() :].strip()
    pm = _PROJECT.match(rest)
    if not pm:
        return None
    name = pm["name"].strip(" .,-")
    words = name.split()
    if not (2 <= len(name) <= 40) or len(words) > 4 or name.lower() in _GENERIC_NAMES:
        return None
    if words[0].lower() in _GENERIC_NAMES:  # "Show HN: I built …" names no project
        return None
    return name


_INTENTS = {
    "product_launch": [SearchIntent.EXPLANATION, SearchIntent.HOW_TO, SearchIntent.TUTORIAL],
    "release": [SearchIntent.EXPLANATION, SearchIntent.TUTORIAL, SearchIntent.NEWS],
    "announcement": [SearchIntent.NEWS, SearchIntent.EXPLANATION],
    "question": [SearchIntent.EXPLANATION, SearchIntent.COMPARISON],
    "discussion": [SearchIntent.EXPLANATION, SearchIntent.NEWS],
}


def classify_story(
    item: HNItem,
    settings: ClassifierSettings,
    assist: StoryAssist | None = None,
) -> StoryClassification:
    flags: list[str] = []
    raw_title = item.title or ""
    title = sanitize(strip_html(raw_title), 300)
    if injection_signals(raw_title):
        flags.append("prompt_injection_suspected")
    text: str | None = None
    if item.text:
        plain = strip_html(item.text)
        if injection_signals(plain):
            flags.append("text_withheld_instruction_like")
        else:
            text = sanitize(plain, 1000) or None
    url = (
        item.url
        if isinstance(item.url, str) and item.url.startswith(("http://", "https://"))
        else None
    )
    domain = domain_of(url) if url else None
    kind = _kind(title)
    subject = _subject(kind, title)
    terms = {c: settings.terms.get(c) or DEFAULT_TERMS[c] for c in DEFAULT_TERMS}
    domains = {c: settings.domains.get(c) or DEFAULT_DOMAINS[c] for c in DEFAULT_DOMAINS}
    title_tokens = _tokens(_PREFIX.sub("", title))
    text_tokens = _tokens(text or "")
    scores: dict[str, float] = {}
    matched: dict[str, list[str]] = {}
    for cat, words in terms.items():
        score, hits = 0.0, []
        for word in words:
            needle = f" {word.lower()} "
            if needle in title_tokens:
                score += 1.0
                hits.append(word)
            elif needle in text_tokens:
                score += 0.4
                hits.append(word)
        if domain and any(domain == d or domain.endswith("." + d) for d in domains[cat]):
            score += TOPICAL_DOMAIN_WEIGHT
            hits.append(f"domain:{domain}")
        elif (
            cat is Category.TECHNOLOGY
            and domain
            and any(domain == d or domain.endswith("." + d) for d in CODE_HOSTS)
        ):
            score += CODE_HOST_WEIGHT
            hits.append(f"code_host:{domain}")
        scores[cat.value] = round(score, 2)
        matched[cat.value] = hits
    ranked = sorted(terms, key=lambda c: scores[c.value], reverse=True)
    top, second = ranked[0], ranked[1]
    top_score, second_score = scores[top.value], scores[second.value]
    relevance = round(min(1.0, top_score / 3.0), 3)
    primary: Category = top if relevance >= settings.min_relevance else Category.OTHER
    secondary = (
        second
        if primary is not Category.OTHER
        and second_score >= top_score * settings.secondary_ratio
        and second_score > 0
        else None
    )
    ambiguous = bool(secondary is not None and second_score >= top_score * settings.ambiguity_ratio)
    assisted = False
    if ambiguous and assist is not None and secondary is not None:
        choice = assist.choose(title, (top, secondary))
        if choice in (top, secondary) and choice is not top:
            primary, secondary, assisted = choice, top, True
    if "prompt_injection_suspected" in flags:
        primary = Category.OTHER
    name = project_name(title) if primary is not Category.OTHER else None
    entity_type: EntityType | None = None
    if name:
        entity_type = EntityType.AI_PRODUCT if primary is Category.AI else EntityType.SOFTWARE
        if primary is Category.GAMING and re.search(r"(?i)\bgame\b", title):
            entity_type = EntityType.GAME
    return StoryClassification(
        kind=kind,
        subject=subject,
        primary=primary,
        secondary=secondary,
        relevance=relevance,
        ambiguous=ambiguous,
        scores=scores,
        matched=matched,
        project_name=name,
        entity_type=entity_type,
        intents=[i.value for i in _INTENTS[subject]],
        flags=flags,
        clean_title=title,
        clean_text=text,
        domain=domain,
        assisted=assisted,
    )


def activity_strength(score: int | None, comments: int | None) -> float:
    """0-1 from cumulative points and comments (a ranking input, never a rate)."""
    s = max(score or 0, 0)
    c = max(comments or 0, 0)
    return round(min(1.0, (math.log10(1 + s) / 3.0) * 0.6 + (math.log10(1 + c) / 2.5) * 0.4), 3)
