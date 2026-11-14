"""Category-specific video formats and deterministic format selection.

Each format states which search intents it serves, its planning hours (configurable
assumptions), whether it needs a verified result (an experiment or benchmark must actually
be run before anything is claimed), and generic software/hardware needs. Selection ranks
formats by how directly they answer the detected intent and the Phase 5 gap; creator
preferences re-order only among formats that serve the intent.
"""

from __future__ import annotations

from dataclasses import dataclass

from trend_agent.schemas.recommendation import (
    FormatChoice,
    HookType,
    ProductionProfile,
    TitleStrategy,
    VideoFormat,
)

F = VideoFormat
T = TitleStrategy
H = HookType
P = ProductionProfile


@dataclass(frozen=True)
class FormatSpec:
    fmt: VideoFormat
    category: str
    label: str
    family: str  # diversity: concepts in one family answer the same informational need
    sub_intents: frozenset[str]
    primary_intents: frozenset[str]
    hours: tuple[float, float]  # planning range (research + recording + editing)
    profile: ProductionProfile
    requires_verified_results: bool
    software: tuple[str, ...]
    hardware: tuple[str, ...]
    title_strategies: tuple[TitleStrategy, ...]
    hook_types: tuple[HookType, ...]
    outline: str  # outline template key
    premise: str  # "{topic}" placeholder; never states results


def _s(*v: str) -> frozenset[str]:
    return frozenset(v)


CATALOG: dict[VideoFormat, FormatSpec] = {
    s.fmt: s
    for s in (
        # ------------------------------------------------------------ AI
        FormatSpec(
            F.LOCAL_AI_INSTALLATION_GUIDE,
            "ai",
            "Local AI installation guide",
            "install",
            _s("model_installation", "local_model_setup", "software_installation"),
            _s("tutorial", "problem_solving"),
            (4, 8),
            P.STANDARD_TUTORIAL,
            False,
            ("the tool's official installer or package",),
            ("a computer that meets the tool's published requirements",),
            (T.DIRECT_ANSWER, T.TUTORIAL, T.PROBLEM_SOLVING),
            (H.DIRECT_TUTORIAL, H.PROBLEM_FIRST, H.DEMONSTRATION_FIRST),
            "install",
            "a complete, current setup walkthrough for {topic}, from download to a first "
            "working result",
        ),
        FormatSpec(
            F.AI_AGENT_TUTORIAL,
            "ai",
            "AI agent tutorial",
            "tutorial",
            _s("ai_agent_tutorial", "api_integration", "ai_automation"),
            _s(
                "tutorial",
            ),
            (6, 10),
            P.STANDARD_TUTORIAL,
            False,
            ("{topic}",),
            (),
            (T.TUTORIAL, T.DIRECT_ANSWER, T.PROBLEM_SOLVING),
            (H.DIRECT_TUTORIAL, H.DEMONSTRATION_FIRST, H.PROBLEM_FIRST),
            "tutorial",
            "a step-by-step tutorial that sets up {topic} for a real task",
        ),
        FormatSpec(
            F.HANDS_ON_AI_EXPERIMENT,
            "ai",
            "Hands-on AI experiment",
            "experiment",
            _s(
                "tool_demonstration",
                "model_installation",
                "ai_agent_tutorial",
                "release_explanation",
                "benchmark",
            ),
            _s("demonstration", "tutorial", "review"),
            (8, 16),
            P.STANDARD_TUTORIAL,
            True,
            ("{topic}", "screen recording software"),
            (),
            (T.EXPERIMENT, T.CURIOSITY),
            (H.CHALLENGE, H.DEMONSTRATION_FIRST, H.PROBLEM_FIRST),
            "experiment",
            "a practical test of what {topic} can do on a real task, with the result "
            "recorded rather than assumed",
        ),
        FormatSpec(
            F.BUILD_AN_APP_TUTORIAL,
            "ai",
            "Build-an-app tutorial",
            "build",
            _s("ai_agent_tutorial", "model_installation", "api_integration", "tool_demonstration"),
            _s("tutorial", "demonstration"),
            (10, 20),
            P.DEEP_TECHNICAL_VIDEO,
            True,
            ("{topic}", "a code editor"),
            (),
            (T.EXPERIMENT, T.TUTORIAL),
            (H.CHALLENGE, H.DEMONSTRATION_FIRST, H.DIRECT_TUTORIAL),
            "build",
            "building a small, working application with {topic} on camera",
        ),
        FormatSpec(
            F.AI_MODEL_COMPARISON,
            "ai",
            "AI model comparison",
            "comparison",
            _s("model_comparison", "benchmark", "release_explanation"),
            _s("comparison", "review"),
            (10, 20),
            P.DEEP_TECHNICAL_VIDEO,
            True,
            ("{topic}", "{other}"),
            (),
            (T.COMPARISON, T.EXPERIMENT),
            (H.COMPARISON, H.CHALLENGE, H.PROBLEM_FIRST),
            "comparison",
            "a side-by-side practical test of {topic} and {other} on the same tasks",
        ),
        FormatSpec(
            F.BENCHMARK_INVESTIGATION,
            "ai",
            "Benchmark investigation",
            "benchmark",
            _s("benchmark", "model_comparison"),
            _s("comparison", "review"),
            (12, 24),
            P.DEEP_TECHNICAL_VIDEO,
            True,
            ("{topic}",),
            (),
            (T.EXPERIMENT, T.COMPARISON),
            (H.CHALLENGE, H.COMPARISON, H.PROBLEM_FIRST),
            "benchmark",
            "a transparent, repeatable benchmark of {topic}",
        ),
        FormatSpec(
            F.AI_AUTOMATION_CHALLENGE,
            "ai",
            "AI automation challenge",
            "challenge",
            _s("ai_automation", "ai_agent_tutorial"),
            _s("demonstration", "tutorial"),
            (6, 12),
            P.STANDARD_TUTORIAL,
            True,
            ("{topic}",),
            (),
            (T.EXPERIMENT, T.CURIOSITY),
            (H.CHALLENGE, H.DEMONSTRATION_FIRST, H.PROBLEM_FIRST),
            "experiment",
            "a challenge to automate a real workflow with {topic}",
        ),
        FormatSpec(
            F.AI_PRODUCT_DEMONSTRATION,
            "ai",
            "AI product demonstration",
            "demonstration",
            _s("tool_demonstration", "release_explanation", "model_pricing"),
            _s("demonstration", "news", "informational"),
            (3, 6),
            P.STANDARD_TUTORIAL,
            False,
            ("{topic}",),
            (),
            (T.TIME_SENSITIVE_NEWS, T.DIRECT_ANSWER),
            (H.DEMONSTRATION_FIRST, H.DIRECT_TUTORIAL, H.PROBLEM_FIRST),
            "demonstration",
            "a hands-on look at what {topic} offers, showing each feature on screen",
        ),
        FormatSpec(
            F.AI_TOOL_REVIEW,
            "ai",
            "AI tool review",
            "review",
            _s("tool_demonstration", "model_pricing"),
            _s(
                "review",
            ),
            (6, 12),
            P.STANDARD_TUTORIAL,
            True,
            ("{topic}",),
            (),
            (T.DIRECT_ANSWER, T.EXPERIMENT),
            (H.PROBLEM_FIRST, H.DEMONSTRATION_FIRST, H.CHALLENGE),
            "review",
            "an evidence-based review of {topic} after using it on real work",
        ),
        FormatSpec(
            F.AI_CAPABILITY_INVESTIGATION,
            "ai",
            "AI capability investigation",
            "investigation",
            _s("release_explanation", "benchmark", "tool_demonstration"),
            _s("informational", "news", "demonstration"),
            (8, 14),
            P.STANDARD_TUTORIAL,
            True,
            ("{topic}",),
            (),
            (T.CURIOSITY, T.EXPERIMENT),
            (H.CHALLENGE, H.PROBLEM_FIRST, H.DEMONSTRATION_FIRST),
            "experiment",
            "an investigation of what {topic} can and cannot do, tested on screen",
        ),
        # ------------------------------------------------------- technology
        FormatSpec(
            F.SOFTWARE_MIGRATION_GUIDE,
            "technology",
            "Software migration guide",
            "migration",
            _s(
                "version_migration",
            ),
            _s("tutorial", "problem_solving"),
            (6, 12),
            P.STANDARD_TUTORIAL,
            False,
            ("{topic}", "a sample project"),
            (),
            (T.DIRECT_ANSWER, T.TUTORIAL, T.PROBLEM_SOLVING),
            (H.PROBLEM_FIRST, H.DIRECT_TUTORIAL, H.DEMONSTRATION_FIRST),
            "migration",
            "migrating a real project to {topic}, step by step",
        ),
        FormatSpec(
            F.PROGRAMMING_TUTORIAL,
            "technology",
            "Programming tutorial",
            "tutorial",
            _s("programming_tutorial", "version_migration", "feature_demonstration"),
            _s(
                "tutorial",
            ),
            (6, 10),
            P.STANDARD_TUTORIAL,
            False,
            ("{topic}", "a code editor"),
            (),
            (T.TUTORIAL, T.DIRECT_ANSWER),
            (H.DIRECT_TUTORIAL, H.DEMONSTRATION_FIRST, H.PROBLEM_FIRST),
            "tutorial",
            "a practical coding tutorial built on {topic}",
        ),
        FormatSpec(
            F.DEVELOPER_TOOL_WALKTHROUGH,
            "technology",
            "Developer tool walkthrough",
            "install",
            _s("software_installation", "feature_demonstration"),
            _s("tutorial", "demonstration"),
            (4, 8),
            P.STANDARD_TUTORIAL,
            False,
            ("{topic}",),
            (),
            (T.DIRECT_ANSWER, T.TUTORIAL),
            (H.DIRECT_TUTORIAL, H.DEMONSTRATION_FIRST, H.PROBLEM_FIRST),
            "install",
            "a walkthrough of setting up and using {topic}",
        ),
        FormatSpec(
            F.TECHNOLOGY_EXPLANATION,
            "technology",
            "Technology explanation",
            "explanation",
            _s(
                "version_migration",
                "security_explanation",
                "release_explanation",
                "feature_demonstration",
            ),
            _s("informational", "news"),
            (3, 6),
            P.STANDARD_TUTORIAL,
            False,
            (),
            (),
            (T.DIRECT_ANSWER, T.TIME_SENSITIVE_NEWS, T.CURIOSITY),
            (H.PROBLEM_FIRST, H.DIRECT_TUTORIAL, H.DEMONSTRATION_FIRST),
            "explanation",
            "a clear explanation of what changed in {topic}, citing the official notes",
        ),
        FormatSpec(
            F.TECHNICAL_EXPERIMENT,
            "technology",
            "Technical experiment",
            "experiment",
            _s("feature_demonstration", "hardware_benchmark", "version_migration"),
            _s(
                "demonstration",
            ),
            (8, 14),
            P.STANDARD_TUTORIAL,
            True,
            ("{topic}",),
            (),
            (T.EXPERIMENT, T.CURIOSITY),
            (H.CHALLENGE, H.DEMONSTRATION_FIRST, H.PROBLEM_FIRST),
            "experiment",
            "a measured experiment with {topic}, reported as recorded",
        ),
        FormatSpec(
            F.PRODUCT_COMPARISON,
            "technology",
            "Product comparison",
            "comparison",
            _s(
                "product_comparison",
            ),
            _s(
                "comparison",
            ),
            (8, 16),
            P.STANDARD_TUTORIAL,
            True,
            ("{topic}", "{other}"),
            (),
            (T.COMPARISON,),
            (H.COMPARISON, H.PROBLEM_FIRST, H.CHALLENGE),
            "comparison",
            "a practical comparison of {topic} and {other} on the same tasks",
        ),
        FormatSpec(
            F.HARDWARE_REVIEW,
            "technology",
            "Hardware review",
            "review",
            _s(
                "product_review",
            ),
            _s(
                "review",
            ),
            (8, 16),
            P.STANDARD_TUTORIAL,
            True,
            (),
            ("{topic}",),
            (T.DIRECT_ANSWER, T.EXPERIMENT),
            (H.PROBLEM_FIRST, H.DEMONSTRATION_FIRST, H.CHALLENGE),
            "review",
            "a hands-on review of {topic}",
        ),
        FormatSpec(
            F.HARDWARE_BENCHMARK,
            "technology",
            "Hardware benchmark",
            "benchmark",
            _s(
                "hardware_benchmark",
            ),
            _s("comparison", "review"),
            (12, 24),
            P.DEEP_TECHNICAL_VIDEO,
            True,
            ("benchmark software",),
            ("{topic}",),
            (T.EXPERIMENT, T.COMPARISON),
            (H.CHALLENGE, H.COMPARISON, H.PROBLEM_FIRST),
            "benchmark",
            "a repeatable benchmark of {topic}",
        ),
        FormatSpec(
            F.CYBERSECURITY_DEMONSTRATION,
            "technology",
            "Cybersecurity demonstration",
            "explanation",
            _s(
                "security_explanation",
            ),
            _s(
                "informational",
            ),
            (6, 12),
            P.STANDARD_TUTORIAL,
            True,
            ("an isolated test environment",),
            (),
            (T.DIRECT_ANSWER, T.PROBLEM_SOLVING),
            (H.PROBLEM_FIRST, H.DIRECT_TUTORIAL, H.DEMONSTRATION_FIRST),
            "explanation",
            "a defensive, safe demonstration of the {topic} issue and its fix",
        ),
        FormatSpec(
            F.PRACTICAL_BUILD_CHALLENGE,
            "technology",
            "Practical build challenge",
            "build",
            _s("programming_tutorial", "feature_demonstration"),
            _s("tutorial", "demonstration"),
            (10, 20),
            P.DEEP_TECHNICAL_VIDEO,
            True,
            ("{topic}", "a code editor"),
            (),
            (T.EXPERIMENT, T.TUTORIAL),
            (H.CHALLENGE, H.DEMONSTRATION_FIRST, H.DIRECT_TUTORIAL),
            "build",
            "building a complete small project with {topic} under a fixed time budget",
        ),
        # ----------------------------------------------------------- gaming
        FormatSpec(
            F.ITEM_LOCATION_WALKTHROUGH,
            "gaming",
            "Item-location walkthrough",
            "location",
            _s("item_location", "reward_unlock"),
            _s(
                "gaming_guide",
            ),
            (2, 4),
            P.QUICK_RESPONSE,
            False,
            ("the game",),
            ("a capture setup",),
            (T.DIRECT_ANSWER, T.TUTORIAL),
            (H.DIRECT_TUTORIAL, H.DEMONSTRATION_FIRST, H.PROBLEM_FIRST),
            "location",
            "the fastest verified route to {topic}, shown in-game",
        ),
        FormatSpec(
            F.LIMITED_TIME_EVENT_GUIDE,
            "gaming",
            "Limited-time event guide",
            "event",
            _s("event_guide", "item_location", "reward_unlock", "challenge_completion"),
            _s("gaming_guide", "news"),
            (3, 6),
            P.QUICK_RESPONSE,
            False,
            ("the game",),
            ("a capture setup",),
            (T.TIME_SENSITIVE_NEWS, T.DIRECT_ANSWER, T.TUTORIAL),
            (H.DIRECT_TUTORIAL, H.PROBLEM_FIRST, H.DEMONSTRATION_FIRST),
            "event",
            "a complete guide to {topic} while it is running",
        ),
        FormatSpec(
            F.NEW_SEASON_EXPLANATION,
            "gaming",
            "New-season explanation",
            "explanation",
            _s("game_update", "event_guide"),
            _s("news", "informational", "gaming_guide"),
            (3, 6),
            P.STANDARD_TUTORIAL,
            False,
            ("the game",),
            ("a capture setup",),
            (T.TIME_SENSITIVE_NEWS, T.DIRECT_ANSWER),
            (H.DEMONSTRATION_FIRST, H.DIRECT_TUTORIAL, H.PROBLEM_FIRST),
            "explanation",
            "what {topic} changes, citing the official patch notes",
        ),
        FormatSpec(
            F.CHALLENGE_COMPLETION_GUIDE,
            "gaming",
            "Challenge completion guide",
            "challenge",
            _s("challenge_completion", "reward_unlock"),
            _s(
                "gaming_guide",
            ),
            (2, 5),
            P.QUICK_RESPONSE,
            False,
            ("the game",),
            ("a capture setup",),
            (T.DIRECT_ANSWER, T.TUTORIAL),
            (H.DIRECT_TUTORIAL, H.PROBLEM_FIRST, H.DEMONSTRATION_FIRST),
            "location",
            "how to complete {topic}, shown step by step in-game",
        ),
        FormatSpec(
            F.GAME_UPDATE_BREAKDOWN,
            "gaming",
            "Game update breakdown",
            "explanation",
            _s(
                "game_update",
            ),
            _s("news", "informational"),
            (3, 6),
            P.STANDARD_TUTORIAL,
            False,
            ("the game",),
            (),
            (T.TIME_SENSITIVE_NEWS, T.DIRECT_ANSWER),
            (H.DEMONSTRATION_FIRST, H.PROBLEM_FIRST, H.DIRECT_TUTORIAL),
            "explanation",
            "a breakdown of the {topic} update from the official notes",
        ),
        FormatSpec(
            F.GAMEPLAY_STRATEGY,
            "gaming",
            "Gameplay strategy",
            "strategy",
            _s("strategy_guide", "weapon_guide"),
            _s(
                "gaming_guide",
            ),
            (4, 8),
            P.STANDARD_TUTORIAL,
            True,
            ("the game",),
            ("a capture setup",),
            (T.TUTORIAL, T.PROBLEM_SOLVING),
            (H.PROBLEM_FIRST, H.DEMONSTRATION_FIRST, H.CHALLENGE),
            "strategy",
            "strategies for {topic}, tested in matches",
        ),
        FormatSpec(
            F.PERFORMANCE_OPTIMIZATION_GUIDE,
            "gaming",
            "Performance optimisation guide",
            "troubleshooting",
            _s("game_settings", "game_troubleshooting"),
            _s("problem_solving", "tutorial"),
            (4, 8),
            P.STANDARD_TUTORIAL,
            True,
            ("the game", "a frame-time overlay"),
            ("the test PC",),
            (T.PROBLEM_SOLVING, T.DIRECT_ANSWER),
            (H.PROBLEM_FIRST, H.DIRECT_TUTORIAL, H.DEMONSTRATION_FIRST),
            "troubleshooting",
            "settings for {topic}, measured before and after",
        ),
        FormatSpec(
            F.NEW_GAME_REVIEW,
            "gaming",
            "New game review",
            "review",
            _s(
                "gameplay_demonstration",
            ),
            _s("review", "entertainment"),
            (8, 16),
            P.STANDARD_TUTORIAL,
            True,
            ("the game",),
            ("a capture setup",),
            (T.DIRECT_ANSWER, T.CURIOSITY),
            (H.DEMONSTRATION_FIRST, H.PROBLEM_FIRST, H.CHALLENGE),
            "review",
            "a review of {topic} after playing it",
        ),
        FormatSpec(
            F.GAMING_AI_EXPERIMENT,
            "gaming",
            "Gaming AI experiment",
            "experiment",
            _s("gameplay_demonstration", "strategy_guide"),
            _s(
                "entertainment",
            ),
            (10, 20),
            P.DEEP_TECHNICAL_VIDEO,
            True,
            ("the game",),
            ("a capture setup",),
            (T.EXPERIMENT, T.CURIOSITY),
            (H.CHALLENGE, H.DEMONSTRATION_FIRST, H.PROBLEM_FIRST),
            "experiment",
            "an experiment applying an AI tool to {topic}",
        ),
        FormatSpec(
            F.COMPETITIVE_CHALLENGE,
            "gaming",
            "Competitive challenge",
            "challenge",
            _s("challenge_completion", "strategy_guide"),
            _s("entertainment", "gaming_guide"),
            (6, 12),
            P.STANDARD_TUTORIAL,
            True,
            ("the game",),
            ("a capture setup",),
            (T.EXPERIMENT, T.CURIOSITY),
            (H.CHALLENGE, H.DEMONSTRATION_FIRST, H.PROBLEM_FIRST),
            "experiment",
            "a competitive challenge built on {topic}",
        ),
    )
}

# Gap types each family answers best (Phase 5 GapType values)
_GAP_FIT: dict[str, frozenset[str]] = {
    "install": _s(
        "missing_specific_answer", "version_mismatch", "potentially_outdated", "no_relevant_results"
    ),
    "migration": _s("version_mismatch", "potentially_outdated", "missing_specific_answer"),
    "location": _s("missing_specific_answer", "version_mismatch", "no_relevant_results"),
    "event": _s("version_mismatch", "potentially_outdated", "missing_specific_answer"),
    "experiment": _s("insufficient_practical_demonstration", "format_gap"),
    "build": _s("insufficient_practical_demonstration", "format_gap", "missing_specific_answer"),
    "comparison": _s("format_gap", "missing_specific_answer"),
    "tutorial": _s("missing_specific_answer", "potentially_outdated", "version_mismatch"),
    "explanation": _s("version_mismatch", "potentially_outdated", "low_relevance"),
}


def spec(fmt: VideoFormat) -> FormatSpec:
    return CATALOG[fmt]


def family(fmt: VideoFormat) -> str:
    return CATALOG[fmt].family


@dataclass(frozen=True)
class RankedFormat:
    spec: FormatSpec
    score: float
    serves_intent: bool
    preferred: bool
    rationale: str


def select_formats(
    *,
    category: str | None,
    primary_intent: str,
    sub_intent: str | None,
    gap_type: str | None,
    preferred: frozenset[str] = frozenset(),
    has_second_entity: bool = False,
    limit: int = 5,
) -> list[RankedFormat]:
    """Formats of the opportunity's category, best answer to the intent first.

    Comparison formats need a second verified entity ({other}); without one they are
    never proposed (no invented competitor)."""
    out: list[RankedFormat] = []
    for s in CATALOG.values():
        if category and s.category != category:
            continue
        if "{other}" in " ".join((*s.software, s.premise)) and not has_second_entity:
            continue
        serves_sub = sub_intent is not None and sub_intent in s.sub_intents
        serves_primary = primary_intent in s.primary_intents
        if not (serves_sub or serves_primary):
            continue
        score = (2.0 if serves_sub else 0.0) + (1.0 if serves_primary else 0.0)
        gap_fit = bool(gap_type and gap_type in _GAP_FIT.get(s.family, frozenset()))
        if gap_fit:
            score += 0.75
        is_pref = s.fmt.value in preferred
        if is_pref:
            score += 0.5
        why = []
        if serves_sub:
            why.append(f"directly serves the '{sub_intent}' search intent")
        elif serves_primary:
            why.append(f"serves the '{primary_intent}' intent")
        if gap_fit:
            why.append(f"answers the detected '{gap_type}' gap")
        if is_pref:
            why.append("a format in the creator profile")
        out.append(RankedFormat(s, score, serves_sub or serves_primary, is_pref, "; ".join(why)))
    out.sort(key=lambda r: (-r.score, r.spec.hours[0], r.spec.fmt.value))
    return out[:limit]


def as_choice(r: RankedFormat) -> FormatChoice:
    return FormatChoice(
        video_format=r.spec.fmt,
        label=r.spec.label,
        rationale=r.rationale,
        serves_intent=r.serves_intent,
        preferred_by_creator=r.preferred,
    )


def intent_alignment(fmt: VideoFormat, primary_intent: str, sub_intent: str | None) -> float:
    """1.0 = the format directly serves the sub-intent; 0.7 = the primary intent; 0.3 = not."""
    s = CATALOG[fmt]
    if sub_intent is not None and sub_intent in s.sub_intents:
        return 1.0
    if primary_intent in s.primary_intents:
        return 0.7
    return 0.3
