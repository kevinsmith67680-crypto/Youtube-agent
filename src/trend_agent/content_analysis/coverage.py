"""Requirement coverage over the inspected evidence.

For each relevant video and each information requirement:

* tier A (title + description): COVERED when a cue appears in the title or at least two
  cues in the description; PARTIALLY_COVERED with one description cue; otherwise
  NOT_EVIDENCED. NOT_EVIDENCED means *the inspected metadata does not show it* - never
  that the video omits it;
* tier B/C (authorised captions or content): the same cue logic over the transcript;
  absence is still NOT_EVIDENCED (transcripts can be partial);
* CONTRADICTED when the video's evidence conflicts with verified facts (it is about a
  credibly denied item, or a version-specific need is answered for another version);
* UNKNOWN when the video is not about the query's entity, or nothing was inspected.
"""

from __future__ import annotations

from collections.abc import Sequence

from trend_agent.schemas.search_intent import InformationRequirement
from trend_agent.schemas.search_result import (
    TIER_RANK,
    CandidateVideo,
    CoverageCell,
    CoverageStatus,
    EntityMatch,
    EvidenceTier,
    RelevanceAssessment,
    RequirementSummary,
)
from trend_agent.verification.text import contains_sequence, tokens

_ORDER = {
    CoverageStatus.COVERED: 4,
    CoverageStatus.PARTIALLY_COVERED: 3,
    CoverageStatus.NOT_EVIDENCED: 2,
    CoverageStatus.CONTRADICTED: 1,
    CoverageStatus.UNKNOWN: 0,
}


def _hits(text: str, cues: Sequence[str]) -> list[str]:
    toks = tokens(text)
    return [c for c in cues if contains_sequence(toks, tokens(c))]


def cell(
    req: InformationRequirement,
    video: CandidateVideo,
    rel: RelevanceAssessment,
    *,
    denied_names: Sequence[str] = (),
) -> CoverageCell:
    def make(status: CoverageStatus, note: str, cues: Sequence[str] = ()) -> CoverageCell:
        return CoverageCell(
            requirement_key=req.key,
            video_id=video.video_id,
            status=status,
            evidence_tier=video.tier,
            matched_cues=tuple(dict.fromkeys(cues)),
            note=note,
            evidence_id=video.evidence_id,
        )

    # Instruction-like descriptions are excluded from coverage evidence (title only).
    description = "" if video.injection_flags else (video.description or "")
    full = f"{video.title} {description} {video.transcript or ''}"
    for name in denied_names:
        if contains_sequence(tokens(full), tokens(name)):
            return make(
                CoverageStatus.CONTRADICTED, f"refers to '{name}', which a credible source denies"
            )
    if rel.entity_match in (EntityMatch.OTHER_ENTITY, EntityMatch.NONE):
        return make(CoverageStatus.UNKNOWN, "not about the query's entity")
    if rel.entity_match is EntityMatch.VERSION_MISMATCH and req.requires_verified_entity:
        return make(
            CoverageStatus.CONTRADICTED,
            f"addresses version(s) {', '.join(rel.matched_versions)}, not the queried one",
        )
    if video.tier in (EvidenceTier.B_CAPTIONS, EvidenceTier.C_FULL_CONTENT) and video.transcript:
        hits = _hits(video.transcript, req.cues)
        if len(hits) >= 2:
            return make(CoverageStatus.COVERED, "authorised transcript addresses it", hits)
        if hits:
            return make(CoverageStatus.PARTIALLY_COVERED, "transcript mentions it briefly", hits)
        return make(CoverageStatus.NOT_EVIDENCED, "not found in the authorised transcript")
    title_hits = _hits(video.title, req.cues)
    desc_hits = _hits(description, req.cues)
    if title_hits or len(desc_hits) >= 2:
        return make(
            CoverageStatus.COVERED,
            "title/description suggest it is covered",
            title_hits + desc_hits,
        )
    if desc_hits:
        return make(CoverageStatus.PARTIALLY_COVERED, "description mentions it once", desc_hits)
    return make(
        CoverageStatus.NOT_EVIDENCED,
        "not mentioned in the title or description (content not inspected)",
    )


def summarise(
    req: InformationRequirement, cells: Sequence[CoverageCell], inspected: int
) -> RequirementSummary:
    relevant_cells = [c for c in cells if c.requirement_key == req.key]
    if not relevant_cells:
        best = CoverageStatus.UNKNOWN
    else:
        best = max((c.status for c in relevant_cells), key=_ORDER.__getitem__)
    covered = tuple(c.video_id for c in relevant_cells if c.status is CoverageStatus.COVERED)
    partial = tuple(
        c.video_id for c in relevant_cells if c.status is CoverageStatus.PARTIALLY_COVERED
    )
    tiers = [c.evidence_tier for c in relevant_cells] or [EvidenceTier.D_INSUFFICIENT]
    max_tier = max(tiers, key=TIER_RANK.__getitem__)
    n = len(relevant_cells)
    what = "titles/descriptions" if max_tier is EvidenceTier.A_METADATA else "inspected evidence"
    if not n:
        statement = f"No relevant inspected result to assess '{req.requirement}'."
    elif covered or partial:
        statement = (
            f"{len(covered)} of {n} relevant inspected results' {what} suggest '{req.requirement}' "
            f"is covered ({len(partial)} more mention it briefly)."
        )
    else:
        statement = (
            f"None of {n} relevant inspected results' {what} mention '{req.requirement}'; this "
            "does not show the videos omit it."
        )
    return RequirementSummary(
        requirement_key=req.key,
        requirement=req.requirement,
        priority=req.priority.value,
        best_status=best,
        covered_by=covered,
        partially_by=partial,
        inspected_videos=inspected,
        max_tier=max_tier,
        statement=statement,
    )
