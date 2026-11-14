"""Opening hooks (the first ~5-15 seconds): prospective wording, no invented results.

Templates describe what the video *will* show or test. The checker rejects, from any
generator, hooks that state an untested result ("it built a perfect app in 30 seconds"),
invent numbers, add artificial urgency or promise views; surviving hooks are verified as
claims by the verification gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from trend_agent.recommendations.format_selector import FormatSpec
from trend_agent.schemas.recommendation import HookType

H = HookType

# a completed outcome stated as fact (allowed only as a question or a plan)
_RESULT = re.compile(
    r"\b(?:i|we|it|they|this (?:agent|model|tool))\s+(?:just\s+)?(?:built|created|made|beat|"
    r"outperformed|crushed|destroyed|broke|solved|finished|completed|wrote|coded|won|failed|"
    r"managed to|turned out|proved)\b|\b(?:is|was|turned out)\s+(?:way\s+)?(?:faster|better|"
    r"slower|worse|smarter|cheaper)\s+than\b|\bbeats\b|\bthe winner is\b|\bresults? (?:show|"
    r"prove)\b|\bin (?:just|only|under)?\s*\d+\s*(?:seconds?|minutes?|hours?)\b",
    re.IGNORECASE,
)
_PROSPECTIVE = re.compile(
    r"\b(?:going to|will|let'?s|we'?ll|i'?ll|test whether|see (?:if|whether)|find out|can it|"
    r"could it|does it|is it)\b|\?",
    re.IGNORECASE,
)
_URGENCY = re.compile(
    r"\b(?:hurry|last chance|before it'?s too late|right now before|don'?t miss|act fast|"
    r"limited time only|before everyone else|while you still can)\b",
    re.IGNORECASE,
)
_PROMISE = re.compile(r"\b(?:go viral|views|subscribers|blow up|guaranteed?)\b", re.IGNORECASE)
_NUMBER = re.compile(r"(?<![A-Za-z])\d+(?:[.,]\d+)?")


@dataclass(frozen=True)
class HookCheck:
    accepted: bool
    reasons: tuple[str, ...]
    seconds: float


def estimate_seconds(text: str) -> float:
    return round(len(text.split()) / 2.7, 1)  # ~160 spoken words per minute


def check_hook(
    text: str, *, allowed_names: tuple[str, ...], word_range: tuple[int, int]
) -> HookCheck:
    reasons: list[str] = []
    words = len(text.split())
    if words < word_range[0] or words > word_range[1]:
        reasons.append(
            f"{words} words: outside the {word_range[0]}-{word_range[1]} word range for "
            "5-15 seconds"
        )
    if _RESULT.search(text) and not _PROSPECTIVE.search(text):
        reasons.append("states an untested result as fact")
    if _URGENCY.search(text):
        reasons.append("artificial urgency")
    if _PROMISE.search(text):
        reasons.append("promises views or reach")
    allowed = " ".join(allowed_names).lower()
    for m in _NUMBER.finditer(text):
        if m.group(0) not in allowed:
            reasons.append(f"number not from a confirmed name: '{m.group(0)}'")
            break
    hard = [r for r in reasons if "word range" not in r]
    return HookCheck(
        not hard and words <= word_range[1] * 1.5, tuple(reasons), estimate_seconds(text)
    )


@dataclass(frozen=True)
class HookDraft:
    hook_type: HookType
    text: str


def hook_drafts(
    spec: FormatSpec, *, topic: str, other: str | None, event: str | None, count: int
) -> list[HookDraft]:
    """Prospective hooks in the format's preferred hook order."""
    where = f" in {event}" if event and event.lower() not in topic.lower() else ""
    fam = spec.family
    templates: dict[HookType, str]
    if spec.outline == "location":
        templates = {
            H.DIRECT_TUTORIAL: f"Here's exactly where to find the {topic}{where}, and what you "
            "need to check before you go.",
            H.PROBLEM_FIRST: f"Can't find the {topic}? We'll go straight to its location{where}, "
            "no long intro.",
            H.DEMONSTRATION_FIRST: f"This is the {topic}. Let's head straight to where it is"
            f"{where} and how to pick it up.",
        }
    elif fam == "event":
        templates = {
            H.DIRECT_TUTORIAL: f"Everything you need to get started in {event or topic}, in the "
            "order you'll actually need it.",
            H.PROBLEM_FIRST: f"New to {event or topic}? Here's what to do first, and what is "
            "still unconfirmed.",
            H.DEMONSTRATION_FIRST: f"Let's jump straight into {event or topic} and walk through "
            "each change, on screen.",
        }
    elif fam in ("comparison", "benchmark") and other:
        templates = {
            H.COMPARISON: f"{topic} or {other}? We're putting both through the same tasks to see "
            "which one fits your workflow.",
            H.CHALLENGE: f"Same tasks, same machine: let's find out how {topic} and {other} "
            "actually compare.",
            H.PROBLEM_FIRST: f"Choosing between {topic} and {other}? Let's test both instead of "
            "guessing.",
        }
    elif fam in ("experiment", "build", "challenge", "benchmark", "review"):
        templates = {
            H.CHALLENGE: f"We're going to test whether {topic} can handle a real project, and "
            "show exactly what happens.",
            H.DEMONSTRATION_FIRST: f"Let's put {topic} to work on a real task and see what it "
            "can and can't do.",
            H.PROBLEM_FIRST: f"Can {topic} actually help with real work? Let's find out with a "
            "real project, start to finish.",
        }
    elif fam == "migration":
        templates = {
            H.PROBLEM_FIRST: f"Upgrading to {topic} and something broke? Let's migrate a real "
            "project step by step.",
            H.DIRECT_TUTORIAL: f"Let's move a real project to {topic}, one change at a time, "
            "and fix what breaks along the way.",
            H.DEMONSTRATION_FIRST: f"Here's a project on the old version. By the end of this "
            f"video it will run on {topic}.",
        }
    elif fam == "explanation":
        templates = {
            H.PROBLEM_FIRST: f"Heard about {topic} but not sure what changed? Let's go through "
            "it using the official notes.",
            H.DIRECT_TUTORIAL: f"Here's what changed in {topic}, explained step by step with the "
            "official sources on screen.",
            H.DEMONSTRATION_FIRST: f"Let's look at {topic} on screen and walk through each "
            "change, one at a time.",
        }
    elif fam in ("strategy", "troubleshooting") or spec.category == "gaming":
        templates = {
            H.PROBLEM_FIRST: f"Struggling with {topic}? Let's go through what to try, tested in "
            "real matches.",
            H.DEMONSTRATION_FIRST: f"Let's jump into {topic} and test each approach on screen.",
            H.CHALLENGE: f"We're going to test which approaches to {topic} actually hold up, "
            "and show every attempt.",
            H.DIRECT_TUTORIAL: f"Here's a step-by-step plan for {topic}, shown in-game.",
        }
    else:  # install / tutorial / demonstration / troubleshooting / strategy
        templates = {
            H.DIRECT_TUTORIAL: f"Let's set up {topic} step by step, from download to your first "
            "working project.",
            H.PROBLEM_FIRST: f"If you're stuck getting {topic} running, this walkthrough starts "
            "from scratch and covers the parts that are easy to miss.",
            H.DEMONSTRATION_FIRST: f"This is {topic}. Let's go from download to a first "
            "working project together, one step at a time.",
        }
    order = [h for h in spec.hook_types if h in templates] + [
        h for h in templates if h not in spec.hook_types
    ]
    return [HookDraft(h, templates[h]) for h in order[:count]]
