"""Deterministic search-result relevance (metadata only unless better evidence exists).

Components (weights in config): entity (exact / family / version mismatch / other entity),
intent (format and intent cues), title relevance, description relevance and keyword
coverage. Similarly named products, models, games, seasons and versions never count as
the same entity: a different version is ``VERSION_MISMATCH``, a homonym with another
entity's context is ``OTHER_ENTITY``.

The score is an uncalibrated relevance index, not the probability that a viewer is
satisfied. An optional ``SemanticScorer`` (e.g. embeddings) can only refine the
description component.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from trend_agent.content_analysis.config import ContentAnalysisConfig
from trend_agent.content_analysis.entity_verification import (
    EntityFacts,
    QueryEntities,
    versions_mentioned,
)
from trend_agent.content_analysis.query_decomposition import _GENERIC, TEMPLATES
from trend_agent.content_analysis.result_normalization import video_format
from trend_agent.schemas.search_intent import (
    IntentClassification,
    PrimaryIntent,
    RequirementPriority,
)
from trend_agent.schemas.search_result import (
    CandidateVideo,
    EntityMatch,
    EvidenceTier,
    RelevanceAssessment,
    VideoFormat,
)
from trend_agent.verification.entities import EntityCatalog
from trend_agent.verification.text import STOPWORDS, contains_sequence, tokens

ALGORITHM_VERSION = "relevance-lexical-1.0"

DESIRED_FORMATS: dict[PrimaryIntent, frozenset[VideoFormat]] = {
    PrimaryIntent.TUTORIAL: frozenset({VideoFormat.TUTORIAL, VideoFormat.GUIDE}),
    PrimaryIntent.PROBLEM_SOLVING: frozenset({VideoFormat.TUTORIAL, VideoFormat.GUIDE}),
    PrimaryIntent.DEMONSTRATION: frozenset({VideoFormat.DEMONSTRATION, VideoFormat.TUTORIAL}),
    PrimaryIntent.COMPARISON: frozenset({VideoFormat.COMPARISON, VideoFormat.REVIEW}),
    PrimaryIntent.REVIEW: frozenset({VideoFormat.REVIEW, VideoFormat.COMPARISON}),
    PrimaryIntent.NEWS: frozenset({VideoFormat.NEWS}),
    PrimaryIntent.INFORMATIONAL: frozenset(
        {VideoFormat.NEWS, VideoFormat.TUTORIAL, VideoFormat.REVIEW, VideoFormat.UNKNOWN}
    ),
    PrimaryIntent.GAMING_GUIDE: frozenset({VideoFormat.GUIDE, VideoFormat.TUTORIAL}),
    PrimaryIntent.ENTERTAINMENT: frozenset({VideoFormat.ENTERTAINMENT, VideoFormat.SHORT}),
    PrimaryIntent.NAVIGATIONAL: frozenset(VideoFormat),
}
_ENTITY_SCORE = {
    EntityMatch.EXACT: 1.0,
    EntityMatch.FAMILY: 0.6,
    EntityMatch.VERSION_MISMATCH: 0.25,
    EntityMatch.OTHER_ENTITY: 0.0,
    EntityMatch.NONE: 0.0,
}


class SemanticScorer(Protocol):
    """Optional similarity in [0, 1] between a query and a video's text."""

    name: str

    def similarity(self, query: str, text: str) -> float: ...


def intent_cues(intent: IntentClassification) -> tuple[str, ...]:
    specs = (
        TEMPLATES.get(intent.sub_intent, _GENERIC[intent.primary_intent])
        if intent.sub_intent
        else _GENERIC[intent.primary_intent]
    )
    cues: list[str] = []
    for spec in specs:
        if spec[2] is RequirementPriority.ESSENTIAL:
            cues.extend(spec[3])
    return tuple(dict.fromkeys(cues))


def _entity_match(
    video_text: str,
    facts: EntityFacts | None,
    catalog: EntityCatalog | None,
) -> tuple[EntityMatch, tuple[str, ...]]:
    if facts is None:
        return EntityMatch.NOT_APPLICABLE, ()
    toks = tokens(video_text)
    fam = facts.versioned.family
    version = facts.versioned.version
    names: list[str] = [facts.ref.name]
    entity = catalog.get(facts.ref.entity_key) if catalog and facts.ref.entity_key else None
    if entity is not None:
        names = list(entity.names())
    alias_hit = any(contains_sequence(toks, tokens(n)) for n in names)
    family_hit = bool(fam) and contains_sequence(toks, list(fam))
    mentioned = versions_mentioned(video_text, fam) if fam else []
    # A homonym: the video carries another catalogued entity's distinguishing context.
    if catalog is not None and fam:
        others = [c for c in catalog.lookup(" ".join(fam)) if entity is None or c.key != entity.key]
        own_marks = (
            (
                {t for n in entity.names() for t in tokens(n)} | set(tokens(entity.entity_type))
                if entity is not None
                else set()
            )
            - set(fam)
            - STOPWORDS
        )
        for other in others:
            marks = (
                ({t for n in other.names() for t in tokens(n)} | set(tokens(other.entity_type)))
                - set(fam)
                - STOPWORDS
            )
            if marks & set(toks) and not (own_marks & set(toks)):
                return EntityMatch.OTHER_ENTITY, ()
    if version is not None:
        if version in mentioned or (alias_hit and not mentioned):
            return (
                EntityMatch.EXACT if alias_hit or version in mentioned else EntityMatch.FAMILY
            ), tuple(mentioned)
        if mentioned:
            return EntityMatch.VERSION_MISMATCH, tuple(mentioned)
        if family_hit:
            return EntityMatch.FAMILY, ()
        return EntityMatch.NONE, ()
    if alias_hit or family_hit:
        return EntityMatch.EXACT, tuple(mentioned)
    return EntityMatch.NONE, ()


def assess(
    video: CandidateVideo,
    query: str,
    intent: IntentClassification,
    entities: QueryEntities,
    config: ContentAnalysisConfig,
    *,
    catalog: EntityCatalog | None = None,
    language: str | None = None,
    semantic: SemanticScorer | None = None,
) -> RelevanceAssessment:
    rc = config.relevance
    w = dict(rc.weights)
    title = video.title
    # A description carrying instruction-like text is not used as relevance evidence:
    # whatever else it says, it cannot raise the video's relevance (title only).
    desc = "" if video.injection_flags else (video.description or "")
    body = video.transcript if video.tier is EvidenceTier.B_CAPTIONS and video.transcript else desc
    full = f"{title}. {body}"
    facts = entities.primary
    match, versions = _entity_match(full, facts, catalog)
    item = entities.item
    if item is not None and item is not facts and match is not EntityMatch.VERSION_MISMATCH:
        item_names = [item.ref.name]
        entry = catalog.get(item.ref.entity_key) if catalog and item.ref.entity_key else None
        if entry is not None:
            item_names = list(entry.names())
        if not any(contains_sequence(tokens(full), tokens(n)) for n in item_names):
            match = EntityMatch.NONE  # about the game/season, not about this item
    q_terms = [t for t in tokens(query) if t not in STOPWORDS]
    t_toks, d_toks = set(tokens(title)), set(tokens(body))
    title_share = sum(t in t_toks for t in q_terms) / len(q_terms) if q_terms else 0.0
    desc_share = sum(t in d_toks for t in q_terms) / len(q_terms) if q_terms else 0.0
    if semantic is not None and body:
        desc_share = max(desc_share, min(1.0, max(0.0, semantic.similarity(query, full))))
    keywords = sum(t in t_toks | d_toks for t in q_terms) / len(q_terms) if q_terms else 0.0
    fmt = video_format(title, video.duration_seconds, config.gaps.short_form_seconds)
    cues = intent_cues(intent)
    lowered_title = tokens(title)
    lowered_full = tokens(full)
    title_cues = tuple(c for c in cues if contains_sequence(lowered_title, tokens(c)))
    any_cues = tuple(c for c in cues if contains_sequence(lowered_full, tokens(c)))
    desired = DESIRED_FORMATS[intent.primary_intent]
    if fmt in desired:
        intent_value = 1.0
    elif title_cues:
        intent_value = 0.7
    elif any_cues:
        intent_value = 0.4
    else:
        intent_value = 0.0
    intent_match = intent_value >= 0.7
    components = {
        "intent": intent_value,
        "title": title_share,
        "description": desc_share,
        "keywords": keywords,
    }
    if match is EntityMatch.NOT_APPLICABLE:
        w["keywords"] += w.pop("entity")
    else:
        components["entity"] = _ENTITY_SCORE[match]
    score = 100.0 * sum(w[k] * components[k] for k in w)
    lang = video.default_audio_language or video.default_language
    lang_ok = None if not (language and lang) else lang.lower().startswith(language.lower()[:2])
    version_specific = intent.time_sensitive and facts is not None and facts.versioned.version
    relevant = (
        score >= rc.relevant_threshold
        and intent_value > 0
        and match not in (EntityMatch.OTHER_ENTITY, EntityMatch.NONE)
    )
    if match is EntityMatch.VERSION_MISMATCH and version_specific:
        relevant = False
    highly = (
        relevant
        and score >= rc.high_threshold
        and intent_match
        and match in (EntityMatch.EXACT, EntityMatch.NOT_APPLICABLE)
    )
    limitations = []
    if video.tier is EvidenceTier.A_METADATA:
        limitations.append("metadata only: title and description inspected, not the video")
    limitations.append("API result order is specific to one request, not a search ranking")
    if lang_ok is None:
        limitations.append("video language not reported")
    if video.injection_flags:
        limitations.append(
            "description contains instruction-like text: treated as data and not used for "
            "relevance or coverage"
        )
    explanation = _explain(match, facts, versions, fmt, title_cues or any_cues, title_share, intent)
    return RelevanceAssessment(
        video_id=video.video_id,
        score=round(min(100.0, max(0.0, score)), 1),
        relevant=relevant,
        highly_relevant=highly,
        entity_match=match,
        matched_entity=facts.ref.name if facts and match is not EntityMatch.NONE else None,
        matched_versions=versions,
        intent_match=intent_match,
        matched_intent_cues=title_cues or any_cues,
        video_format=fmt,
        keyword_coverage=round(keywords, 3),
        language_compatible=lang_ok,
        evidence_tier=video.tier,
        explanation=explanation,
        limitations=tuple(limitations),
        algorithm_version=ALGORITHM_VERSION,
        components={k: round(v, 3) for k, v in components.items()},
    )


def _explain(
    match: EntityMatch,
    facts: EntityFacts | None,
    versions: Sequence[str],
    fmt: VideoFormat,
    cues: Sequence[str],
    title_share: float,
    intent: IntentClassification,
) -> str:
    parts = []
    if facts is not None:
        name = facts.ref.name
        parts.append(
            {
                EntityMatch.EXACT: f"metadata names {name}",
                EntityMatch.FAMILY: f"metadata names the {facts.versioned.family_text} family "
                "without a version",
                EntityMatch.VERSION_MISMATCH: f"metadata refers to version(s) "
                f"{', '.join(versions)}, not {facts.versioned.version}",
                EntityMatch.OTHER_ENTITY: "metadata points to another entity with a similar name",
                EntityMatch.NONE: f"metadata does not name {name}",
            }.get(match, "")
        )
    parts.append(f"apparent format: {fmt.value}")
    if cues:
        parts.append(f"{intent.primary_intent.value} cues: {', '.join(cues[:4])}")
    parts.append(f"{title_share:.0%} of query terms in the title")
    return "; ".join(p for p in parts if p)
