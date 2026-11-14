"""Search-optimised titles: deterministic strategies plus checks that apply to any title.

Titles use only the monitored query's words and *confirmed* entity names. A title must
match the planned video and the search intent; misleading clickbait, invented statistics,
unsupported superlatives and results the creator has not produced are rejected here, and
every surviving title is still verified by the verification gate (entities, release
wording, numbers) before it can be shown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from trend_agent.recommendations.format_selector import FormatSpec
from trend_agent.schemas.recommendation import TitleStrategy

T = TitleStrategy

STOPWORDS = frozenset(
    "a an and are as at be by can do does for from how i in is it of on or the to vs what "
    "when where which who why with your you my our this that new".split()
)
_SYNONYMS = {
    "installation": "install",
    "installing": "install",
    "installed": "install",
    "setup": "install",
    "set": "install",
    "tutorial": "guide",
    "walkthrough": "guide",
    "guides": "guide",
    "migrating": "migrate",
    "migration": "migrate",
    "upgrade": "migrate",
    "upgrading": "migrate",
    "find": "location",
    "where": "location",
    "locate": "location",
    "location": "location",
    "comparison": "compare",
    "versus": "compare",
    "vs": "compare",
    "benchmarks": "benchmark",
    "review": "review",
    "reviewed": "review",
    "testing": "test",
    "tested": "test",
    "tests": "test",
}
_CLICKBAIT = re.compile(
    r"(won'?t believe|shocking|insane|mind[- ]?blowing|secret(?:s)? (?:they|nobody)|"
    r"they don'?t want|gone wrong|destroy(?:s|ed)?|crushes|banned|!!|\?\?|you need to see|"
    r"changes everything|game[- ]?changer|unbelievable|jaw[- ]?dropping|exposed)",
    re.IGNORECASE,
)
_SUPERLATIVE = re.compile(
    r"\b(best|fastest|ultimate|#1|number one|perfect|flawless|guaranteed|world'?s first|"
    r"most powerful|only guide)\b",
    re.IGNORECASE,
)
_PROMISE = re.compile(r"\b(go viral|get (?:more )?views|views fast|blow up|rank #?1)\b", re.I)
_NUMBER = re.compile(r"(?<![A-Za-z])\d+(?:[.,]\d+)?\s*(?:%|x\b|k\b|m\b|million|times)?", re.I)


def tokens(text: str) -> list[str]:
    raw = re.findall(r"[a-z0-9]+", text.lower())
    return [_SYNONYMS.get(t, t) for t in raw if t not in STOPWORDS]


def query_coverage(title: str, query: str) -> float:
    q = set(tokens(query))
    if not q:
        return 1.0
    return round(len(q & set(tokens(title))) / len(q), 3)


@dataclass(frozen=True)
class TitleCheck:
    accepted: bool
    reasons: tuple[str, ...]
    coverage: float


def check_title(
    text: str,
    *,
    query: str,
    allowed_names: tuple[str, ...],
    soft_max: int,
    hard_max: int,
) -> TitleCheck:
    reasons: list[str] = []
    accepted = True
    t = " ".join(text.split())
    if len(t) > hard_max:
        accepted = False
        reasons.append(f"longer than {hard_max} characters")
    elif len(t) > soft_max:
        reasons.append(f"longer than the recommended {soft_max} characters")
    if _CLICKBAIT.search(t):
        accepted = False
        reasons.append("misleading clickbait wording")
    if _SUPERLATIVE.search(t):
        accepted = False
        reasons.append("unsupported superlative")
    if _PROMISE.search(t):
        accepted = False
        reasons.append("promises views or virality")
    caps = [w for w in re.findall(r"\b[A-Z]{4,}\b", t) if not any(w in n for n in allowed_names)]
    if len(caps) >= 2:
        accepted = False
        reasons.append("shouting in capitals")
    # numbers only where they are part of the query or a confirmed name (e.g. "Season 8")
    allowed_text = " ".join((query, *allowed_names)).lower()
    for m in _NUMBER.finditer(t):
        num = m.group(0).strip().lower()
        if num and num not in allowed_text:
            accepted = False
            reasons.append(f"number not in the query or a confirmed name: '{m.group(0).strip()}'")
            break
    cov = query_coverage(t, query)
    if cov < 0.34:
        reasons.append("few of the search query's words")
    return TitleCheck(accepted, tuple(reasons), cov)


@dataclass(frozen=True)
class TitleDraft:
    strategy: TitleStrategy
    text: str
    rationale: str


def title_drafts(
    spec: FormatSpec,
    *,
    topic: str,
    sub_intent: str | None,
    other: str | None,
    event: str | None,
    has_troubleshooting: bool,
) -> list[TitleDraft]:
    """Titles for one format, in the format's preferred strategy order. Placeholders are
    confirmed entity names only; no numbers, results or superlatives are introduced."""
    fam = spec.family
    out: list[TitleDraft] = []

    def add(strategy: TitleStrategy, text: str, why: str) -> None:
        if strategy in spec.title_strategies and all(d.text != text for d in out):
            out.append(TitleDraft(strategy, text, why))

    where = f" in {event}" if event and event.lower() not in topic.lower() else ""
    if fam == "install":
        add(
            T.DIRECT_ANSWER,
            f"How to Install {topic}: Complete Setup Guide",
            "answers the installation query directly with the exact product name",
        )
        add(
            T.TUTORIAL,
            f"{topic} Setup Tutorial: From Download to First Project",
            "tutorial wording for viewers who want the whole first-run workflow",
        )
        if has_troubleshooting:
            add(
                T.PROBLEM_SOLVING,
                f"{topic} Setup Problems and How to Fix Them",
                "targets viewers stuck during setup (a requirement in the analysis)",
            )
    elif fam == "migration":
        add(
            T.DIRECT_ANSWER,
            f"How to Migrate to {topic}: Step-by-Step Guide",
            "answers the migration query directly",
        )
        add(
            T.TUTORIAL,
            f"Upgrading a Real Project to {topic}",
            "a worked example rather than a summary",
        )
        add(
            T.PROBLEM_SOLVING,
            f"{topic} Migration: Fixing the Breaking Changes",
            "targets viewers hitting upgrade errors",
        )
    elif fam in ("location", "challenge") and spec.outline == "location":
        add(
            T.DIRECT_ANSWER,
            f"Where to Find the {topic}{where}",
            "mirrors the item-location search query",
        )
        add(
            T.TUTORIAL,
            f"{topic} Location Guide{f' ({event})' if where and event else ''}",
            "guide wording for the same location intent",
        )
    elif fam == "event":
        subject = event or topic
        add(
            T.DIRECT_ANSWER,
            f"{subject}: Complete Event Guide",
            "covers the whole time-limited event",
        )
        add(
            T.TIME_SENSITIVE_NEWS,
            f"{subject}: What's New and What to Do First",
            "for viewers arriving while the event is running",
        )
        add(
            T.TUTORIAL,
            f"{subject} Guide: Rewards, Locations and Tips",
            "lists what the guide covers; items must be verified before filming",
        )
    elif fam == "experiment":
        add(
            T.EXPERIMENT,
            f"I Tested {topic} on a Real Project",
            "an experiment title; the result is recorded during production, not assumed",
        )
        add(
            T.CURIOSITY,
            f"What Can {topic} Actually Do? A Hands-On Test",
            "curiosity framed as an open question, without claiming an outcome",
        )
    elif fam == "challenge":
        add(
            T.EXPERIMENT,
            f"Can {topic} Automate a Real Workflow?",
            "a prospective challenge; the outcome is recorded, not assumed",
        )
        add(
            T.CURIOSITY,
            f"Automating a Real Task with {topic}",
            "curiosity framing without a claimed result",
        )
    elif fam == "build":
        add(
            T.EXPERIMENT,
            f"Can {topic} Build a Working App?",
            "a prospective challenge; the answer comes from the recorded build",
        )
        add(
            T.TUTORIAL,
            f"Building an App with {topic}, Step by Step",
            "tutorial framing of the same build",
        )
    elif fam in ("comparison", "benchmark") and other:
        add(
            T.COMPARISON,
            f"{topic} vs {other}: Practical Test",
            "compares two confirmed entities on the same tasks",
        )
        add(
            T.EXPERIMENT,
            f"Testing {topic} and {other} on the Same Tasks",
            "experiment framing; no winner is claimed in advance",
        )
    elif fam == "benchmark":
        add(
            T.EXPERIMENT,
            f"Benchmarking {topic}: A Repeatable Test",
            "a transparent benchmark title without results",
        )
    elif fam == "explanation":
        add(
            T.DIRECT_ANSWER,
            f"What Changed in {topic}, Explained",
            "explains the change from official notes",
        )
        add(
            T.TIME_SENSITIVE_NEWS,
            f"{topic}: What's New and What It Means for You",
            "news framing for viewers arriving now",
        )
    elif fam == "review":
        add(
            T.DIRECT_ANSWER,
            f"{topic} Review After Real Use",
            "a review title; the verdict comes from real use",
        )
        add(T.EXPERIMENT, f"I Tested {topic} on Real Work", "experiment framing of the review")
    elif fam == "tutorial":
        add(T.TUTORIAL, f"{topic} Tutorial for Beginners", "a beginner tutorial")
        add(
            T.DIRECT_ANSWER,
            f"How to Use {topic}: A Practical Guide",
            "answers 'how to use' directly",
        )
        add(
            T.PROBLEM_SOLVING,
            f"Getting Started with {topic}: Common Mistakes to Avoid",
            "problem-first framing",
        )
    elif fam == "demonstration":
        add(T.DIRECT_ANSWER, f"{topic}: A Hands-On First Look", "a demonstration title")
        add(
            T.TIME_SENSITIVE_NEWS,
            f"{topic} First Look: What You Can Do With It",
            "first-look framing",
        )
    elif fam == "strategy":
        add(T.TUTORIAL, f"{topic} Strategy Guide", "a strategy guide title")
        add(
            T.PROBLEM_SOLVING,
            f"Struggling with {topic}? Strategies That Help",
            "problem-first framing",
        )
    elif fam == "troubleshooting":
        add(
            T.PROBLEM_SOLVING,
            f"{topic} Performance Settings, Tested",
            "settings measured before and after",
        )
        add(
            T.DIRECT_ANSWER,
            f"How to Improve {topic} Performance",
            "answers the optimisation query directly",
        )
    if not out:  # always at least one neutral, intent-matching title
        out.append(
            TitleDraft(
                spec.title_strategies[0],
                f"{topic}: {spec.label}",
                "neutral fallback built from the confirmed topic and format",
            )
        )
    del sub_intent
    return out


def sentence_case(text: str, names: tuple[str, ...]) -> str:
    """Lower-case every word except confirmed names (Title Case words read as unknown product
    names to the claim extractor; titles are checked in sentence case)."""
    protected: dict[str, str] = {}
    out = text
    for i, n in enumerate(sorted((n for n in names if n), key=len, reverse=True)):
        token = f"\x00{i}\x00"
        pattern = re.compile(re.escape(n), re.IGNORECASE)
        if pattern.search(out):
            protected[token] = n
            out = pattern.sub(token, out)
    out = out.lower()
    for token, n in protected.items():
        out = out.replace(token, n)
    return out[:1].upper() + out[1:]
