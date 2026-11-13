"""Query decomposition: the information needs a query implies (candidate requirements).

Templates are per sub-intent (falling back to the primary intent). Requirements are
*candidate* needs ("installation procedure", "item location"), never product facts: the
template text names the entity only as the subject of the need. Requirements that depend
on an entity existing are marked ``requires_verified_entity``; for item/event guides they
are also ``blocking`` - if the item cannot be verified, no location is ever proposed.

Optional LLM suggestions can add generic requirements (validated: no numbers, no unknown
names, at most ``important`` priority).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from trend_agent.content_analysis.entity_verification import QueryEntities
from trend_agent.schemas.search_intent import (
    InformationRequirement,
    IntentClassification,
    PrimaryIntent,
    QueryDecomposition,
    RequirementPriority,
    SubIntent,
)
from trend_agent.verification.text import STOPWORDS, tokens

ALGORITHM_VERSION = "decomposition-templates-1.0"
_IDENTIFY = frozenset({"item_exists", "identify_event", "challenge_list", "how_obtain"})
#: Intent words that are not the *subject* of an informational or review query.
_INTENT_WORDS = frozenset(
    "what is are explained explain explanation overview introduction review reviews honest "
    "worth it test tested hands on guide meaning about".split()
)
ESS = RequirementPriority.ESSENTIAL
IMP = RequirementPriority.IMPORTANT
OPT = RequirementPriority.OPTIONAL
Spec = tuple[str, str, RequirementPriority, tuple[str, ...], bool, bool]

_INSTALL: list[Spec] = [
    ("supported_platforms", "Supported operating systems and hardware", IMP,
     ("windows", "mac", "macos", "linux", "system requirements", "requirements", "gpu"),
     True, False),
    ("prerequisites", "Prerequisites before installing", IMP,
     ("prerequisite", "requirements", "before you start", "you need", "python", "node", "docker"),
     False, False),
    ("install_steps", "Installation procedure", ESS,
     ("install", "installation", "setup", "set up", "download", "step by step"), True, False),
    ("authentication", "Authentication / API key setup", IMP,
     ("api key", "login", "sign in", "authenticate", "authentication", "token", "account"),
     True, False),
    ("configuration", "Initial configuration", IMP,
     ("configure", "configuration", "config", "settings"), False, False),
    ("first_example", "A first working example", ESS,
     ("first example", "example", "hello world", "quick start", "getting started", "demo",
      "project"),
     False, False),
    ("troubleshooting", "Troubleshooting common installation errors", OPT,
     ("error", "errors", "fix", "troubleshoot", "troubleshooting", "not working"), False, False),
]  # fmt: skip

TEMPLATES: dict[SubIntent | PrimaryIntent, list[Spec]] = {
    SubIntent.MODEL_INSTALLATION: _INSTALL,
    SubIntent.SOFTWARE_INSTALLATION: [s for s in _INSTALL if s[0] != "authentication"],
    SubIntent.LOCAL_MODEL_SETUP: [
        (
            "hardware_requirements",
            "Hardware requirements (GPU, memory)",
            ESS,
            ("gpu", "vram", "ram", "memory", "hardware", "requirements"),
            True,
            False,
        ),
        *[s for s in _INSTALL if s[0] not in ("authentication", "supported_platforms")],
        (
            "performance",
            "Expected local performance",
            OPT,
            ("speed", "tokens per second", "performance", "benchmark", "fast"),
            False,
            False,
        ),
    ],
    SubIntent.AI_AGENT_TUTORIAL: [
        (
            "agent_concepts",
            "What the agent does and how it works",
            IMP,
            ("what is", "explained", "how it works", "overview", "agent"),
            False,
            False,
        ),
        (
            "setup",
            "Setting the agent up",
            ESS,
            ("install", "setup", "set up", "configure"),
            True,
            False,
        ),
        (
            "build_example",
            "Building a working example",
            ESS,
            ("build", "create", "example", "project", "from scratch", "demo"),
            False,
            False,
        ),
        (
            "tools_integration",
            "Connecting tools or APIs",
            IMP,
            ("tools", "integration", "api", "connect", "mcp"),
            False,
            False,
        ),
        ("troubleshooting", "Troubleshooting", OPT, ("error", "fix", "troubleshoot"), False, False),
    ],
    SubIntent.API_INTEGRATION: [
        (
            "api_key",
            "Getting and using an API key",
            ESS,
            ("api key", "key", "authentication", "token"),
            True,
            False,
        ),
        (
            "first_request",
            "Making a first request",
            ESS,
            ("request", "call", "example", "curl", "python", "sdk", "code"),
            True,
            False,
        ),
        (
            "parameters",
            "Important parameters and options",
            IMP,
            ("parameters", "options", "settings", "model", "streaming"),
            False,
            False,
        ),
        (
            "limits",
            "Pricing and rate limits",
            OPT,
            ("pricing", "rate limit", "limits", "cost"),
            True,
            False,
        ),
        (
            "error_handling",
            "Error handling",
            OPT,
            ("error", "errors", "retry", "handling"),
            False,
            False,
        ),
    ],
    SubIntent.AI_AUTOMATION: [
        (
            "workflow_goal",
            "What is being automated",
            IMP,
            ("automate", "automation", "workflow"),
            False,
            False,
        ),
        (
            "build_steps",
            "Building the automation step by step",
            ESS,
            ("build", "step by step", "setup", "tutorial", "how to"),
            False,
            False,
        ),
        (
            "working_demo",
            "A working end-to-end demo",
            ESS,
            ("demo", "example", "in action"),
            False,
            False,
        ),
    ],
    SubIntent.MODEL_PRICING: [
        (
            "pricing_tiers",
            "Prices and plans",
            ESS,
            ("price", "pricing", "cost", "plan", "plans", "tier", "free", "$"),
            True,
            False,
        ),
        ("limits", "Usage limits", IMP, ("limit", "limits", "rate limit", "quota"), True, False),
        (
            "value",
            "Value compared with alternatives",
            OPT,
            ("worth", "value", "cheaper", "vs"),
            False,
            False,
        ),
    ],
    SubIntent.RELEASE_EXPLANATION: [
        (
            "what_changed",
            "What is new in this release",
            ESS,
            ("new", "features", "what's new", "changes", "release", "announced", "launch"),
            True,
            False,
        ),
        (
            "availability",
            "Availability and access",
            IMP,
            ("available", "release date", "launch", "access", "rollout"),
            True,
            False,
        ),
        (
            "who_for",
            "Who it is for and what it means",
            OPT,
            ("who", "should you", "explained", "means"),
            False,
            False,
        ),
    ],
    SubIntent.VERSION_MIGRATION: [
        (
            "breaking_changes",
            "Breaking changes between versions",
            ESS,
            ("breaking", "changes", "deprecated", "removed", "what changed"),
            True,
            False,
        ),
        (
            "migration_steps",
            "Migration steps",
            ESS,
            ("migrate", "migration", "upgrade", "update", "step by step"),
            True,
            False,
        ),
        (
            "compatibility",
            "Compatibility notes",
            IMP,
            ("compatible", "compatibility", "support"),
            True,
            False,
        ),
        (
            "before_after",
            "Before/after code examples",
            OPT,
            ("example", "before", "after", "code"),
            False,
            False,
        ),
    ],
    SubIntent.ITEM_LOCATION: [
        (
            "identify_event",
            "Which event or season the item belongs to",
            ESS,
            ("event", "season", "update", "chapter"),
            True,
            True,
        ),
        (
            "item_exists",
            "Confirmation that the item exists in the current version",
            ESS,
            ("item", "new"),
            True,
            True,
        ),
        (
            "availability",
            "When the item is available",
            IMP,
            ("available", "limited time", "until", "ends", "now"),
            True,
            False,
        ),
        (
            "location",
            "Where the item is found",
            ESS,
            ("location", "locations", "where", "find", "spawn", "map"),
            True,
            False,
        ),
        (
            "how_obtained",
            "How the item is obtained",
            IMP,
            ("get", "obtain", "earn", "unlock", "how to get", "buy", "shop"),
            True,
            False,
        ),
        (
            "prerequisites",
            "Prerequisites to obtain it",
            OPT,
            ("unlock", "requirement", "level", "need"),
            False,
            False,
        ),
        (
            "expiration",
            "When the event or item expires",
            OPT,
            ("ends", "until", "expire", "limited"),
            True,
            False,
        ),
    ],
    SubIntent.EVENT_GUIDE: [
        ("identify_event", "Which event this is", ESS, ("event", "season"), True, True),
        (
            "event_dates",
            "Event dates and times",
            ESS,
            ("date", "time", "start", "end", "schedule", "when", "until"),
            True,
            False,
        ),
        (
            "activities",
            "Event activities and quests",
            ESS,
            ("quests", "activities", "challenges", "event"),
            True,
            False,
        ),
        ("rewards", "Event rewards", IMP, ("reward", "rewards", "free", "unlock"), True, False),
    ],
    SubIntent.CHALLENGE_COMPLETION: [
        (
            "challenge_list",
            "Which challenges exist",
            ESS,
            ("challenge", "challenges", "quest", "quests", "all"),
            True,
            True,
        ),
        (
            "steps",
            "How to complete each challenge",
            ESS,
            ("complete", "how to", "guide", "walkthrough"),
            False,
            False,
        ),
        ("rewards", "Rewards for completing them", IMP, ("reward", "xp", "unlock"), True, False),
    ],
    SubIntent.REWARD_UNLOCK: [
        (
            "requirements",
            "Unlock requirements",
            ESS,
            ("unlock", "requirement", "how to get"),
            True,
            True,
        ),
        ("steps", "Steps to unlock", ESS, ("steps", "guide", "complete", "how to"), False, False),
        ("deadline", "Deadline", OPT, ("ends", "until", "limited"), True, False),
    ],
    SubIntent.WEAPON_GUIDE: [
        (
            "how_obtain",
            "How to obtain the weapon",
            ESS,
            ("get", "find", "unlock", "location"),
            True,
            True,
        ),
        ("stats", "Weapon stats", IMP, ("stats", "damage", "dps"), True, False),
        ("best_use", "How to use it well", IMP, ("best", "loadout", "build", "tips"), False, False),
    ],
    SubIntent.GAME_UPDATE: [
        (
            "changes",
            "What the update changes",
            ESS,
            ("patch notes", "update", "changes", "new", "season"),
            True,
            False,
        ),
        (
            "release_timing",
            "When it goes live",
            IMP,
            ("release", "date", "time", "live", "when"),
            True,
            False,
        ),
        ("impact", "Impact on gameplay", OPT, ("meta", "nerf", "buff", "changes"), False, False),
    ],
    SubIntent.GAME_SETTINGS: [
        (
            "recommended_settings",
            "Recommended settings",
            ESS,
            ("settings", "best settings", "sensitivity", "graphics", "fps"),
            False,
            False,
        ),
        (
            "explanation",
            "What each setting does",
            IMP,
            ("explained", "what does", "why"),
            False,
            False,
        ),
    ],
    SubIntent.STRATEGY_GUIDE: [
        (
            "strategies",
            "Core strategies",
            ESS,
            ("strategy", "tips", "how to win", "guide"),
            False,
            False,
        ),
        (
            "current_meta",
            "The current meta",
            IMP,
            ("meta", "season", "best", "tier list"),
            True,
            False,
        ),
    ],
    SubIntent.GAMEPLAY_DEMONSTRATION: [
        (
            "gameplay",
            "Actual gameplay footage",
            ESS,
            ("gameplay", "playthrough", "first look"),
            False,
            False,
        ),
    ],
}

_GENERIC: dict[PrimaryIntent, list[Spec]] = {
    PrimaryIntent.INFORMATIONAL: [
        (
            "definition",
            "What it is",
            ESS,
            ("what is", "explained", "explain", "overview", "introduction"),
            False,
            False,
        ),
        (
            "key_facts",
            "Key facts and capabilities",
            IMP,
            ("features", "how it works", "capabilities", "facts"),
            True,
            False,
        ),
        (
            "use_cases",
            "Practical uses",
            OPT,
            ("use cases", "examples", "uses", "how to use"),
            False,
            False,
        ),
    ],
    PrimaryIntent.TUTORIAL: [
        (
            "steps",
            "Step-by-step instructions",
            ESS,
            ("step by step", "tutorial", "how to", "guide", "walkthrough"),
            False,
            False,
        ),
        (
            "prerequisites",
            "Prerequisites",
            IMP,
            ("prerequisite", "requirements", "you need"),
            False,
            False,
        ),
        ("example", "A worked example", IMP, ("example", "demo", "project"), False, False),
    ],
    PrimaryIntent.PROBLEM_SOLVING: [
        (
            "symptom",
            "The specific error or symptom",
            ESS,
            ("error", "crash", "not working", "issue", "problem"),
            False,
            False,
        ),
        (
            "fix_steps",
            "Steps that fix it",
            ESS,
            ("fix", "solution", "solve", "fixed", "how to fix"),
            False,
            False,
        ),
        ("cause", "Why it happens", IMP, ("cause", "why", "reason"), False, False),
    ],
    PrimaryIntent.COMPARISON: [
        (
            "compared_items",
            "The compared alternatives side by side",
            ESS,
            ("vs", "versus", "compare", "comparison", "compared"),
            True,
            False,
        ),
        (
            "criteria",
            "Comparison criteria",
            IMP,
            ("speed", "quality", "price", "performance", "benchmark", "features"),
            False,
            False,
        ),
        (
            "verdict",
            "Which to choose",
            IMP,
            ("best", "winner", "which", "better", "verdict"),
            False,
            False,
        ),
    ],
    PrimaryIntent.REVIEW: [
        (
            "hands_on",
            "Hands-on testing",
            ESS,
            ("review", "hands-on", "tested", "test", "testing"),
            True,
            False,
        ),
        (
            "pros_cons",
            "Strengths and weaknesses",
            IMP,
            ("pros", "cons", "worth", "verdict"),
            False,
            False,
        ),
        (
            "performance",
            "Performance",
            IMP,
            ("performance", "benchmark", "fps", "speed"),
            True,
            False,
        ),
    ],
    PrimaryIntent.DEMONSTRATION: [
        (
            "live_demo",
            "Seeing it in action",
            ESS,
            ("demo", "in action", "showcase", "hands-on", "test", "tested", "first look"),
            True,
            False,
        ),
        (
            "real_task",
            "A realistic task",
            IMP,
            ("example", "project", "task", "real"),
            False,
            False,
        ),
    ],
    PrimaryIntent.NEWS: [
        (
            "what_happened",
            "What happened or changed",
            ESS,
            ("announced", "news", "new", "release", "launch", "update", "leak"),
            True,
            False,
        ),
        (
            "when",
            "When it happens",
            IMP,
            ("date", "release date", "when", "today", "now"),
            True,
            False,
        ),
    ],
    PrimaryIntent.NAVIGATIONAL: [
        (
            "target_resource",
            "The specific resource sought",
            ESS,
            ("official", "channel", "trailer", "docs", "website"),
            True,
            False,
        ),
    ],
    PrimaryIntent.ENTERTAINMENT: [
        (
            "entertaining",
            "Entertaining content on the subject",
            ESS,
            ("funny", "moments", "highlights", "best", "reaction"),
            False,
            False,
        ),
    ],
    PrimaryIntent.GAMING_GUIDE: [
        ("guide_steps", "Guide steps", ESS, ("guide", "how to", "walkthrough"), False, False),
        (
            "current_version",
            "Applies to the current version or season",
            IMP,
            ("season", "chapter", "update", "new"),
            True,
            False,
        ),
    ],
}

for _sub, _primary in (
    (SubIntent.MODEL_COMPARISON, PrimaryIntent.COMPARISON),
    (SubIntent.PRODUCT_COMPARISON, PrimaryIntent.COMPARISON),
    (SubIntent.BENCHMARK, PrimaryIntent.COMPARISON),
    (SubIntent.PRODUCT_REVIEW, PrimaryIntent.REVIEW),
    (SubIntent.HARDWARE_BENCHMARK, PrimaryIntent.REVIEW),
    (SubIntent.TOOL_DEMONSTRATION, PrimaryIntent.DEMONSTRATION),
    (SubIntent.FEATURE_DEMONSTRATION, PrimaryIntent.DEMONSTRATION),
    (SubIntent.TROUBLESHOOTING, PrimaryIntent.PROBLEM_SOLVING),
    (SubIntent.GAME_TROUBLESHOOTING, PrimaryIntent.PROBLEM_SOLVING),
    (SubIntent.SECURITY_EXPLANATION, PrimaryIntent.INFORMATIONAL),
    (SubIntent.PROGRAMMING_TUTORIAL, PrimaryIntent.TUTORIAL),
):
    TEMPLATES.setdefault(_sub, _GENERIC[_primary])


_FOCUSED = frozenset({PrimaryIntent.INFORMATIONAL, PrimaryIntent.REVIEW})


def _focus(intent: IntentClassification, entities: QueryEntities) -> str | None:
    """Words of the query beyond its entities and intent words (e.g. "battery life")."""
    names = {t for f in entities.facts for t in tokens(f.ref.name)}
    names |= {t for r in entities.refs for t in tokens(r.name)}
    words = [
        t
        for t in tokens(intent.query)
        if t not in names and t not in STOPWORDS and t not in _INTENT_WORDS and not t.isdigit()
    ]
    return " ".join(words[:4]) if words and names else None


def decompose(
    intent: IntentClassification,
    entities: QueryEntities,
    *,
    llm_requirements: Sequence[tuple[str, RequirementPriority, tuple[str, ...]]] = (),
) -> QueryDecomposition:
    key: SubIntent | PrimaryIntent
    if intent.sub_intent is not None and intent.sub_intent in TEMPLATES:
        key = intent.sub_intent
        specs = TEMPLATES[intent.sub_intent]
    else:
        key = intent.primary_intent
        specs = _GENERIC[intent.primary_intent]
    primary = entities.primary
    subject_facts = entities.item or primary
    subject = subject_facts.ref.name if subject_facts else None
    notes: list[str] = []
    reqs: list[InformationRequirement] = []
    item = entities.item
    names = [
        f.ref.name.lower() for f in entities.facts if f.ref.state not in ("unknown", "ambiguous")
    ]
    for rkey, text, prio, cues, needs_entity, blocking in specs:
        if rkey in _IDENTIFY and names:
            cues = (*cues, *names)
        if rkey == "item_exists" and item is not None:
            text = f"{text} ({item.ref.name})"
        label = (
            f"{text} ({subject})" if subject and needs_entity and rkey != "item_exists" else text
        )
        reqs.append(
            InformationRequirement(
                key=rkey,
                requirement=label,
                priority=prio,
                cues=cues,
                requires_verified_entity=needs_entity,
                blocking=blocking,
                template=key.value,
            )
        )
    focus = _focus(intent, entities) if key in _FOCUSED else None
    if focus:
        # An attribute question ("<product> battery life"): the attribute is what viewers
        # need; a general definition becomes optional.
        reqs = [
            r.model_copy(update={"priority": OPT}) if r.key == "definition" else r for r in reqs
        ]
        reqs.insert(
            0,
            InformationRequirement(
                key="focus",
                requirement=f"Information about {focus}" + (f" ({subject})" if subject else ""),
                priority=ESS,
                cues=(focus, *[t for t in focus.split() if len(t) >= 4]),
                requires_verified_entity=subject is not None,
                blocking=False,
                template=key.value,
            ),
        )
    for text, prio, cues in llm_requirements[:3]:
        clean = _generic_requirement(text, entities)
        if clean is None:
            notes.append(f"LLM requirement suggestion rejected (not generic): {text[:80]}")
            continue
        reqs.append(
            InformationRequirement(
                key=f"llm_{len(reqs)}",
                requirement=clean,
                priority=prio if prio is not RequirementPriority.ESSENTIAL else IMP,
                cues=tuple(c.lower() for c in cues if 2 <= len(c) <= 30)[:6],
                source="llm_suggested",
                template=key.value,
            )
        )
    if any(r.requires_verified_entity for r in reqs) and primary is None:
        notes.append(
            "Entity-specific requirements are candidate needs only: no entity of the query "
            "could be verified, so no product-specific detail is asserted."
        )
    return QueryDecomposition(
        query=intent.query,
        primary_intent=intent.primary_intent,
        sub_intent=intent.sub_intent,
        requirements=tuple(reqs),
        notes=tuple(notes),
        algorithm_version=ALGORITHM_VERSION,
    )


def _generic_requirement(text: str, entities: QueryEntities) -> str | None:
    """Accept an LLM-suggested requirement only if it asserts nothing product-specific."""
    t = text.strip()
    if not 8 <= len(t) <= 120 or re.search(r"\d", t):
        return None
    known = {w.lower() for ref in entities.refs for w in ref.name.split()}
    for word in re.findall(r"\b[A-Z][a-zA-Z]+\b", t[1:]):
        if word.lower() not in known:
            return None
    return t
