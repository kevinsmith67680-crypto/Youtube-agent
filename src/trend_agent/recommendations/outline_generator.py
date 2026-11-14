"""Practical, intent-adapted outlines with explicit statement labels.

Every item is one of VERIFIED_FACT (quoted from a credible, cited record and re-verified by
the gate), PLANNED_DEMONSTRATION (what the creator will show), HYPOTHESIS_TO_TEST (a
question the video answers by testing) or UNKNOWN (not established by the evidence:
research before filming). A hypothesis is never written as a completed result. The
structure follows the search intent: an item-location guide opens with the location, not
with an introduction to the game.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from trend_agent.recommendations.context_builder import Fact, RecommendationContext, Requirement
from trend_agent.recommendations.format_selector import FormatSpec
from trend_agent.schemas.recommendation import Outline, OutlineItem, OutlineSection, StatementLabel

L = StatementLabel

_COMPLETED = re.compile(
    r"\b(?:it|this|the (?:model|agent|tool|game))\s+(?:built|beat|outperformed|failed|won|"
    r"crashed|proved|achieved)\b|\b(?:is|was)\s+(?:faster|better|slower|worse)\s+than\b|"
    r"\bresults? (?:show|showed|prove)\b",
    re.IGNORECASE,
)

#: generated text that reads as an instruction to the system, a shell command or a
#: credential request is removed wherever it appears (it is data, never executed)
UNSAFE_TEXT = re.compile(
    # instructions aimed at the system rather than the viewer
    r"\b(?:ignore|disregard|forget) (?:all |any )?(?:the )?(?:previous|prior|above|earlier) "
    r"(?:instructions|rules|prompts?)\b|\bsystem prompt\b|"
    # requests to disclose credentials (setting up an API key is a normal tutorial topic)
    r"\b(?:reveal|print|send|leak|expose|output|exfiltrate|dump|email)\b"
    r"[^.!?\n]{0,40}\b(?:api[ _-]?keys?|passwords?|access tokens?|secret keys?|credentials)\b|"
    # destructive or remote-execution shell commands
    r"(?:^|[\s`;|&])(?:rm\s+-rf|sudo\s|chmod\s+777|mkfs|dd\s+if=|"
    r"curl\s[^\n]*\|\s*(?:ba)?sh|wget\s[^\n]*\|\s*(?:ba)?sh|:\(\)\s*\{)",
    re.IGNORECASE,
)

_FAILURE_POINTS = {
    "install": (
        "the installer or sign-in step fails on a clean machine",
        "a prerequisite is missing or differs from the official notes",
        "the first project does not run; show the fix or report it honestly",
    ),
    "tutorial": (
        "a step depends on an account or key the viewer may not have",
        "the example works only with a specific version",
    ),
    "experiment": (
        "the task fails or is only partly completed: report it as observed",
        "the result depends on the prompt or settings; show them on screen",
    ),
    "build": (
        "the generated app does not run: keep the failure in the video",
        "the build takes longer than planned; cut, do not fake it",
    ),
    "comparison": (
        "the tasks favour one tool; use the same, stated tasks for both",
        "versions change during production; record which versions were used",
    ),
    "benchmark": (
        "results are not repeatable; run several times and show the spread",
        "hardware differences explain the result; state the test machine",
    ),
    "migration": (
        "a breaking change is not covered by the official notes",
        "a dependency has not been updated for the new version",
    ),
    "location": (
        "the item is not where the source says (patch or different mode)",
        "an unlock condition applies; verify in-game before recording",
    ),
    "event": ("event details change mid-season; check the official page on recording day",),
    "explanation": ("the official notes are updated after recording",),
    "review": ("first impressions differ from longer use; state how long it was used",),
    "demonstration": ("a feature shown in the announcement is not yet available",),
    "strategy": ("a balance patch changes what works",),
    "troubleshooting": ("settings behave differently on other hardware",),
}

_CONCLUSION = {
    "install": "Whether the published setup steps work end to end on a clean machine.",
    "tutorial": "Whether a viewer can repeat every step with only what the video shows.",
    "experiment": "What {topic} actually does on the task, as observed rather than assumed.",
    "build": "Whether {topic} produces a working app, and where human fixes were needed.",
    "comparison": "How {topic} and {other} differ on the same, stated tasks.",
    "benchmark": "What a repeatable test shows about {topic}, with its spread.",
    "migration": "Which changes a real project needs to run on {topic}.",
    "location": "That the route shown reaches the {topic} in the current version.",
    "event": "What a viewer should do first in {topic}, using only confirmed details.",
    "explanation": "What changed in {topic}, according to the official sources.",
    "review": "Who {topic} suits after real use, and who it does not.",
    "demonstration": "Which announced features of {topic} work today.",
    "strategy": "Which strategies hold up in recorded matches.",
    "troubleshooting": "Which settings measurably change performance on the test machine.",
}


def _item(label: StatementLabel, text: str, ids: Sequence[str] = (), note: str | None = None):  # type: ignore[no-untyped-def]
    return OutlineItem(label=label, text=text, evidence_ids=tuple(ids), note=note)


def _fact_items(facts: Sequence[Fact], limit: int) -> list[OutlineItem]:
    out = []
    for f in facts[:limit]:
        who = f.publisher or "the official source"
        out.append(
            _item(L.VERIFIED_FACT, f"According to {who}: {f.text.rstrip('.')}.", (f.evidence_id,))
        )
    return out


def _requirement_items(reqs: Sequence[Requirement], gap_keys: Sequence[str]) -> list[OutlineItem]:
    out = []
    order = {"essential": 0, "important": 1, "optional": 2}
    for r in sorted(reqs, key=lambda q: (order.get(q.priority, 3), q.key)):
        gap_note = "the detected gap" if r.key in gap_keys else None
        if r.established:
            out.append(_item(L.PLANNED_DEMONSTRATION, f"Show on screen: {r.text}", note=gap_note))
        else:
            out.append(
                _item(
                    L.UNKNOWN,
                    f"Research before filming: {r.text} (not established by the inspected "
                    "evidence)",
                    note=gap_note,
                )
            )
    return out


@dataclass(frozen=True)
class OutlineInputs:
    topic: str
    other: str | None
    event: str | None
    hook: str | None


def build_outline(spec: FormatSpec, ctx: RecommendationContext, inp: OutlineInputs) -> Outline:
    topic, other, _event = inp.topic, inp.other, inp.event
    key = spec.outline
    facts = list(ctx.facts)
    reqs = list(ctx.requirements)
    req_items = _requirement_items(reqs, ctx.gap_keys)
    opening = OutlineSection(
        heading="Opening hook",
        purpose="state the viewer benefit in the first seconds",
        items=(
            _item(L.PLANNED_DEMONSTRATION, f"Open with: {inp.hook}")
            if inp.hook
            else _item(
                L.PLANNED_DEMONSTRATION,
                f"Show the end goal: {spec.premise.format(topic=topic, other=other or '')}",
            ),
        ),
    )
    sections: list[OutlineSection] = [opening]
    if key == "location":
        sections.append(
            OutlineSection(
                heading="Where it is (first 30 seconds)",
                purpose="answer the search query immediately",
                items=(
                    *_fact_items([f for f in facts if topic.lower() in f.text.lower()] or facts, 2),
                    _item(
                        L.PLANNED_DEMONSTRATION,
                        f"Show the route to the {topic} on the map and in-game",
                    ),
                ),
            )
        )
        sections.append(
            OutlineSection(
                heading="Prerequisites and availability",
                purpose="what the viewer needs before going",
                items=tuple(i for i in req_items if "where" not in i.text.lower())[:4]
                or (
                    _item(L.UNKNOWN, "Research before filming: unlock conditions and availability"),
                ),
            )
        )
        sections.append(
            OutlineSection(
                heading="Confirm the pickup",
                purpose="prove the route works in the current version",
                items=(
                    _item(L.PLANNED_DEMONSTRATION, f"Pick up the {topic} on camera"),
                    _item(L.UNKNOWN, "Note the game version and mode used for recording"),
                ),
            )
        )
    elif key in ("install", "tutorial", "migration"):
        sections.append(
            OutlineSection(
                heading="What you need",
                purpose="requirements and context from official sources",
                items=tuple(_fact_items(facts, 3))
                or (
                    _item(L.UNKNOWN, f"Research before filming: official requirements for {topic}"),
                ),
            )
        )
        sections.append(
            OutlineSection(
                heading="Step by step",
                purpose="the core walkthrough, in the order a viewer needs it",
                items=tuple(req_items[:7])
                or (_item(L.PLANNED_DEMONSTRATION, f"Walk through {topic} step by step"),),
            )
        )
        sections.append(
            OutlineSection(
                heading="First working result",
                purpose="prove the setup works",
                items=(
                    _item(L.PLANNED_DEMONSTRATION, "Run a first example end to end on screen"),
                    _item(
                        L.HYPOTHESIS_TO_TEST,
                        "Check whether the steps work on a clean "
                        "machine; record any difference from the official notes",
                    ),
                ),
            )
        )
    elif key in ("experiment", "build", "benchmark", "review"):
        sections.append(
            OutlineSection(
                heading="Context",
                purpose="what the tool is, from official sources only",
                items=tuple(_fact_items(facts, 2))
                or (_item(L.UNKNOWN, f"Research before filming: what {topic} officially offers"),),
            )
        )
        task = {
            "build": f"Test whether {topic} can build a small working app from a written brief",
            "benchmark": f"Test {topic} with a repeatable benchmark and record every run",
            "review": f"Use {topic} on real work for a stated period and record what happens",
        }.get(key, f"Test whether {topic} can complete a real task, start to finish")
        sections.append(
            OutlineSection(
                heading="The test",
                purpose="the main demonstration; outcomes are recorded, not predicted",
                items=(
                    _item(
                        L.PLANNED_DEMONSTRATION,
                        "State the task, setup and success criteria before starting",
                    ),
                    _item(L.HYPOTHESIS_TO_TEST, task),
                    _item(
                        L.PLANNED_DEMONSTRATION,
                        "Show the result as it happens, including failures and manual fixes",
                    ),
                ),
            )
        )
        if req_items:
            sections.append(
                OutlineSection(
                    heading="What viewers asked about",
                    purpose="cover the information needs found in the analysis",
                    items=tuple(req_items[:4]),
                )
            )
    elif key == "comparison":
        sections.append(
            OutlineSection(
                heading="The contenders",
                purpose="what each tool is, from official sources",
                items=tuple(_fact_items(facts, 2))
                or (
                    _item(
                        L.UNKNOWN,
                        f"Research before filming: official details of {topic} and {other}",
                    ),
                ),
            )
        )
        sections.append(
            OutlineSection(
                heading="Same tasks, side by side",
                purpose="a fair, stated comparison",
                items=(
                    _item(
                        L.PLANNED_DEMONSTRATION,
                        "Define the tasks and scoring before running either tool",
                    ),
                    _item(
                        L.HYPOTHESIS_TO_TEST,
                        f"Compare {topic} and {other} on each task; "
                        "report the observed differences",
                    ),
                ),
            )
        )
    elif key == "event":
        sections.append(
            OutlineSection(
                heading="What the event is",
                purpose="confirmed facts only",
                items=tuple(_fact_items(facts, 3))
                or (_item(L.UNKNOWN, "Research before filming: the official event details"),),
            )
        )
        sections.append(
            OutlineSection(
                heading="What to do first",
                purpose="the viewer's practical next steps",
                items=tuple(req_items[:5])
                or (_item(L.PLANNED_DEMONSTRATION, "Walk through the first steps in-game"),),
            )
        )
    else:  # explanation / demonstration / strategy / troubleshooting
        sections.append(
            OutlineSection(
                heading="What changed",
                purpose="from official sources",
                items=tuple(_fact_items(facts, 3))
                or (_item(L.UNKNOWN, f"Research before filming: the official notes for {topic}"),),
            )
        )
        sections.append(
            OutlineSection(
                heading="Show it",
                purpose="demonstrate rather than describe",
                items=tuple(req_items[:5])
                or (_item(L.PLANNED_DEMONSTRATION, f"Demonstrate {topic} on screen"),),
            )
        )
    sections.append(
        OutlineSection(
            heading="Close",
            purpose="summarise what was shown, as observed",
            items=(
                _item(
                    L.PLANNED_DEMONSTRATION,
                    "Summarise what worked and what did not, as "
                    "observed; point to the official sources",
                ),
            ),
        )
    )
    required = [
        f"Verify before filming: {i.text.split(': ', 1)[-1]}"
        for s in sections
        for i in s.items
        if i.label is L.UNKNOWN
    ]
    if spec.requires_verified_results:
        required.append("Record the actual result on screen; publish nothing that was not observed")
    return Outline(
        sections=tuple(sections),
        required_evidence=tuple(dict.fromkeys(required)),
        failure_points=_FAILURE_POINTS.get(key, ()),
        conclusion_to_investigate=_CONCLUSION.get(key, "What the video shows, as observed.").format(
            topic=topic, other=other or "the alternative"
        ),
    )


def enforce_labels(outline: Outline, bundle_ids: frozenset[str]) -> tuple[Outline, list[str]]:
    """Label integrity for any generator: a VERIFIED_FACT must cite evidence from the
    bundle (else it becomes UNKNOWN); a hypothesis or plan written as a completed result is
    removed."""
    removed: list[str] = []
    sections = []
    for s in outline.sections:
        items = []
        for i in s.items:
            if UNSAFE_TEXT.search(i.text):
                removed.append(f"{i.text} (instruction-like, command or credential text)")
                continue
            if _COMPLETED.search(i.text) and i.label is not L.VERIFIED_FACT:
                removed.append(f"{i.text} (written as a completed result)")
                continue
            if i.label is L.VERIFIED_FACT:
                cited = tuple(e for e in i.evidence_ids if e in bundle_ids)
                if not cited:
                    items.append(
                        i.model_copy(
                            update={
                                "label": L.UNKNOWN,
                                "evidence_ids": (),
                                "note": "labelled as a fact without evidence from this analysis",
                            }
                        )
                    )
                    continue
                i = i.model_copy(update={"evidence_ids": cited})
            items.append(i)
        if items:
            sections.append(s.model_copy(update={"items": tuple(items)}))
    return outline.model_copy(
        update={"sections": tuple(sections), "removed_items": (*outline.removed_items, *removed)}
    ), removed
