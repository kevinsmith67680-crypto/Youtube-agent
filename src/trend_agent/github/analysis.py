"""Deterministic analysis of GitHub material: relevance, quality gates, release notes.

Nothing here infers what the source does not say:

* Relevance is keyword overlap between the repository's own name/description/topics and
  configured category terms. It ranks discoveries; it is not a quality judgement.
* Quality gates *exclude* material that cannot be a real opportunity (empty, archived,
  forks, curated lists, generic names, instruction-like descriptions) and *flag*
  patterns GitHub data cannot explain (a burst of stars on a days-old repository).
* Release analysis copies lines from the release notes verbatim (sanitised, bounded).
  It never summarises, so it cannot invent features, breaking changes or APIs.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from trend_agent.discovery.models import Category, EntityType
from trend_agent.github.config import GitHubSettings, StrategySettings
from trend_agent.github.models import GitHubRelease, GitHubRepository
from trend_agent.verification.untrusted import injection_signals, sanitize

DEFAULT_TERMS: dict[Category, tuple[str, ...]] = {
    Category.AI: (
        "ai", "agent", "agents", "agentic", "llm", "llms", "gpt", "language model", "model",
        "inference", "rag", "prompt", "diffusion", "multimodal", "embedding", "transformer",
        "machine learning", "deep learning", "neural", "chatbot", "copilot", "autonomous",
        "vision", "speech", "tts", "voice", "text-to-speech", "text-to-video",
        "image generation", "video generation", "benchmark", "mcp", "openai", "claude",
        "gemini", "llama", "ollama", "fine-tuning", "reasoning", "ml", "robot", "robotics",
        "coding agent", "code generation",
    ),
    Category.TECHNOLOGY: (
        "framework", "sdk", "cli", "developer", "developers", "devtools", "developer-tools",
        "api", "library", "compiler", "runtime", "database", "ide", "editor", "automation",
        "browser", "testing", "terminal", "workflow", "open-source", "open source", "tool",
        "toolkit", "platform", "server", "typescript", "python", "rust", "go", "javascript",
        "robotics", "self-hosted", "infrastructure",
    ),
    Category.GAMING: (
        "game", "games", "gaming", "engine", "mod", "modding", "unity", "godot", "unreal",
        "minecraft", "roblox", "emulator",
    ),
}  # fmt: skip

EXCLUDING_FLAGS = frozenset(
    {
        "archived",
        "disabled",
        "fork",
        "mirror",
        "empty_repository",
        "missing_description",
        "curated_list",
        "generic_name",
        "prompt_injection_suspected",
        "low_relevance",
        "private",
        "fake_star_pattern",
        "coordinated_star_pattern",
        "software_lure",
    }
)
# Pirated-software / malware lure wording (repositories that are download bait, not tools).
_LURE = re.compile(
    r"(?i)\b(crack(?:ed)?|keygen|activator|serial key|license key generator|"
    r"full (?:working )?installer|(?:standalone|complete) (?:desktop )?(?:setup|installer)|"
    r"desktop installer|pre-?activated|pre-?configured installer|with all tools pre-?configured|"
    r"full-feature desktop|full version free|free download|mod menu|cheat engine)\b"
)
_CURATED = re.compile(r"(?i)^awesome[-_]|\b(curated list|awesome list|list of)\b")
_FRAMEWORK = re.compile(r"(?i)\b(framework|sdk|library|toolkit)\b")
_AI_PRODUCT = re.compile(
    r"(?i)\b(agents?|assistant|copilot|chatbot|coding agent|llm (?:app|client|gateway))\b"
)


@dataclass
class RepositoryAssessment:
    relevance: float
    matched_terms: tuple[str, ...]
    flags: tuple[str, ...]
    entity_type: EntityType
    age_days: float
    discovery_score: float  # ranking only: recency x relevance x description x adoption
    description: str | None  # sanitised; None when withheld

    @property
    def excluded(self) -> bool:
        return any(f in EXCLUDING_FLAGS for f in self.flags)

    def exclusion_reasons(self) -> list[str]:
        return [f for f in self.flags if f in EXCLUDING_FLAGS]

    def as_dict(self) -> dict[str, Any]:
        return {
            "relevance": self.relevance,
            "matched_terms": list(self.matched_terms),
            "flags": list(self.flags),
            "entity_type": self.entity_type.value,
            "age_days": round(self.age_days, 2),
            "discovery_score": self.discovery_score,
        }


def _tokens(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9+#.]+", " ", text.lower()) + " "


def relevance(
    repo: GitHubRepository, category: Category, settings: GitHubSettings
) -> tuple[float, tuple[str, ...]]:
    """Share of evidence for the category, 0-1: topics count double, name/description once."""
    terms = settings.relevance_terms.get(category) or DEFAULT_TERMS.get(category, ())
    topics = _tokens(" ".join(t.replace("-", " ") for t in repo.topics))
    body = _tokens(f"{repo.name.replace('-', ' ').replace('_', ' ')} {repo.description or ''}")
    matched: list[str] = []
    score = 0.0
    for term in terms:
        needle = f" {term.replace('-', ' ')} "
        hit = False
        if needle in topics:
            score += 2.0
            hit = True
        if needle in body:
            score += 1.0
            hit = True
        if hit:
            matched.append(term)
    return round(min(1.0, score / 6.0), 3), tuple(matched)


def assess_repository(
    repo: GitHubRepository,
    category: Category,
    settings: GitHubSettings,
    now: datetime,
) -> RepositoryAssessment:
    q = settings.quality
    flags: list[str] = []
    for attr in ("archived", "disabled", "fork", "private", "is_template"):
        if getattr(repo, attr):
            flags.append("template" if attr == "is_template" else attr)
    if repo.mirror_url:
        flags.append("mirror")
    if repo.size == 0:
        flags.append("empty_repository")
    raw = repo.description or ""
    signals = injection_signals(raw)
    description: str | None = sanitize(raw, 300) or None
    if signals:
        flags.append("prompt_injection_suspected")
        description = None
    elif len((description or "").strip()) < q.min_description_chars:
        flags.append("missing_description")
    if _CURATED.search(repo.name) or _CURATED.search(raw):
        flags.append("curated_list")
    if _LURE.search(raw) or _LURE.search(repo.name.replace("-", " ")):
        flags.append("software_lure")
    if repo.name.lower() in q.generic_names or len(repo.name) < 3:
        flags.append("generic_name")
    rel, matched = relevance(repo, category, settings)
    if rel < q.min_relevance:
        flags.append("low_relevance")
    age = max((now - repo.created_at).total_seconds() / 86400, 0.0)
    if (
        repo.stargazers_count >= q.fake_star_min_stars
        and repo.forks_count <= repo.stargazers_count * q.fake_star_max_fork_ratio
        and repo.size < q.fake_star_max_size_kb
    ):
        flags.append("fake_star_pattern")
    elif (
        repo.stargazers_count >= q.star_anomaly_min_stars
        and age <= q.star_anomaly_max_age_days
        and repo.forks_count <= repo.stargazers_count * q.star_anomaly_max_fork_ratio
    ) or (
        age <= q.young_star_max_age_days
        and repo.size < q.young_star_max_size_kb
        and repo.stargazers_count / max(age, 0.25) >= q.young_star_min_rate_per_day
    ):
        flags.append("star_pattern_unexplained")
    entity_type = EntityType.SOFTWARE
    text = f"{repo.description or ''} {' '.join(repo.topics)}"
    if _FRAMEWORK.search(text):
        entity_type = EntityType.FRAMEWORK
    elif category is Category.AI and _AI_PRODUCT.search(text):
        entity_type = EntityType.AI_PRODUCT
    # Ranking score (not a probability, not demand): newer, more relevant, described, and
    # with some adoption; unexplained star bursts get no adoption credit.
    recency = math.exp(-age / 30.0)
    adoption = (
        0.0
        if "star_pattern_unexplained" in flags
        else min(1.0, math.log10(1 + repo.stargazers_count) / 4.0)
    )
    described = 1.0 if description and len(description) >= q.min_description_chars else 0.4
    score = 100 * (0.35 * recency + 0.35 * rel + 0.15 * described + 0.15 * adoption)
    return RepositoryAssessment(
        relevance=rel,
        matched_terms=matched,
        flags=tuple(flags),
        entity_type=entity_type,
        age_days=age,
        discovery_score=round(score, 2),
        description=description,
    )


def coordinated_cohorts(repos: Sequence[GitHubRepository], settings: GitHubSettings) -> set[int]:
    """Repository IDs in a suspicious cohort: at least ``cohort_min_size`` results created
    within ``cohort_window_hours`` of each other with near-identical star counts. Organic
    projects do not gain the same number of stars at the same time; farmed batches do."""
    q = settings.quality
    pool = sorted(
        (r for r in repos if r.stargazers_count >= q.cohort_min_stars), key=lambda r: r.created_at
    )
    flagged: set[int] = set()
    window = timedelta(hours=q.cohort_window_hours)
    for i, anchor in enumerate(pool):
        group = [r for r in pool[i:] if r.created_at - anchor.created_at <= window]
        if len(group) < q.cohort_min_size:
            continue
        stars = sorted(r.stargazers_count for r in group)
        median = stars[len(stars) // 2]
        close = [
            r for r in group if abs(r.stargazers_count - median) <= median * q.cohort_star_tolerance
        ]
        if len(close) >= q.cohort_min_size:
            flagged.update(r.id for r in close)
    return flagged


def build_search_query(
    base: str, strategy: StrategySettings, now: datetime, language: str | None = None
) -> str:
    """Base query (topic:/keywords/in:) narrowed by date windows and light filters.

    Documented qualifiers only: ``created:>=YYYY-MM-DD``, ``pushed:>=YYYY-MM-DD``,
    ``stars:>=N``, ``fork:false``, ``archived:false``, ``language:X``.
    """
    parts = [" ".join(base.split())]
    if strategy.created_within_days:
        parts.append(f"created:>={(now - timedelta(days=strategy.created_within_days)).date()}")
    if strategy.pushed_within_days:
        parts.append(f"pushed:>={(now - timedelta(days=strategy.pushed_within_days)).date()}")
    if strategy.min_stars:
        parts.append(f"stars:>={strategy.min_stars}")
    if language:
        parts.append(f"language:{language}")
    if strategy.exclude_forks:
        parts.append("fork:false")
    if strategy.exclude_archived:
        parts.append("archived:false")
    return " ".join(parts)


# ---------------------------------------------------------------------------- releases

_VERSION = re.compile(
    r"(?<![\w.])v?(?P<major>\d{1,4})\.(?P<minor>\d{1,4})(?:\.(?P<patch>\d{1,6}))?"
    r"(?P<suffix>[-+.]?(?:alpha|beta|rc|dev|pre|preview|nightly|canary|a|b)[.\-]?\d*)?",
    re.IGNORECASE,
)
_PRE = re.compile(r"(?i)(alpha|beta|rc|dev|pre|preview|nightly|canary|\d(a|b)\d)")
_BREAKING = re.compile(r"(?i)\bbreaking\b|\bbackward[s]?[- ]incompatible\b|⚠")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(?P<title>.+?)\s*#*\s*$")
_CAPABILITY_HEADINGS = re.compile(
    r"(?i)^(new features?|features?|added|what'?s new|highlights|new|enhancements?)\b"
)
_BREAKING_HEADINGS = re.compile(r"(?i)\bbreaking\b")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(?P<text>.+)$")
_API = re.compile(r"(?i)\b(api|apis|endpoint|endpoints|sdk|cli flag|parameter)\b")
_FEAT = re.compile(r"(?i)^feat(\(.+?\))?!?:")
MAX_EXCERPTS = 6
MAX_EXCERPT_CHARS = 200


@dataclass
class ReleaseAnalysis:
    version: str | None
    release_level: str  # major / minor / patch / prerelease / unknown
    breaking_excerpts: list[str] = field(default_factory=list)
    capability_excerpts: list[str] = field(default_factory=list)
    api_excerpts: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def is_breaking(self) -> bool:
        return bool(self.breaking_excerpts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "release_level": self.release_level,
            "breaking_change_lines": self.breaking_excerpts,
            "new_capability_lines": self.capability_excerpts,
            "api_lines": self.api_excerpts,
            "flags": self.flags,
            "basis": "lines quoted verbatim from the release notes; nothing summarised",
        }


def parse_version(*candidates: str | None) -> tuple[str | None, str]:
    """(version text, level) from the tag or title; level 'unknown' when no version."""
    for text in candidates:
        if not text:
            continue
        m = _VERSION.search(text)
        if not m:
            continue
        major, minor = int(m["major"]), int(m["minor"])
        patch = int(m["patch"]) if m["patch"] is not None else 0
        version = f"{major}.{minor}" + (f".{m['patch']}" if m["patch"] is not None else "")
        if m["suffix"] or _PRE.search(text[m.end() : m.end() + 12]):
            suffix = (m["suffix"] or "").lstrip("-+.")
            return (f"{version}-{suffix}" if suffix else version), "prerelease"
        if minor == 0 and patch == 0 and major > 0:
            return version, "major"
        if patch == 0:
            return version, "minor"
        return version, "patch"
    return None, "unknown"


def _clip(line: str) -> str:
    text = sanitize(line, MAX_EXCERPT_CHARS)
    return text.replace("[truncated]", "…")


def analyse_release(release: GitHubRelease) -> ReleaseAnalysis:
    version, level = parse_version(release.tag_name, release.name)
    if release.prerelease and level != "unknown":
        level = "prerelease"
    analysis = ReleaseAnalysis(version=version, release_level=level)
    body = release.body or ""
    if injection_signals(body):
        # Release notes are untrusted text. Instruction-like content is never quoted.
        analysis.flags.append("prompt_injection_suspected")
        return analysis
    section: str | None = None
    for raw in body.splitlines()[:400]:
        heading = _HEADING.match(raw)
        if heading:
            title = heading["title"].strip("*_ :")
            section = (
                "breaking"
                if _BREAKING_HEADINGS.search(title)
                else "capability"
                if _CAPABILITY_HEADINGS.search(title)
                else None
            )
            continue
        bullet = _BULLET.match(raw)
        line = (bullet["text"] if bullet else raw).strip()
        if not line:
            continue
        excerpt = _clip(line)
        if (section == "breaking" and bullet) or _BREAKING.search(line):
            if excerpt not in analysis.breaking_excerpts:
                analysis.breaking_excerpts.append(excerpt)
        elif (
            (section == "capability" and bullet) or _FEAT.match(line)
        ) and excerpt not in analysis.capability_excerpts:
            analysis.capability_excerpts.append(excerpt)
            if _API.search(line) and excerpt not in analysis.api_excerpts:
                analysis.api_excerpts.append(excerpt)
    analysis.breaking_excerpts = analysis.breaking_excerpts[:MAX_EXCERPTS]
    analysis.capability_excerpts = analysis.capability_excerpts[:MAX_EXCERPTS]
    analysis.api_excerpts = analysis.api_excerpts[:MAX_EXCERPTS]
    return analysis
