"""Normalise retrieved video metadata into ``CandidateVideo`` (sanitised, flagged, deduped).

Titles and descriptions are third-party, untrusted text: control/hidden characters are
removed, length is bounded and instruction-like content is flagged (it remains data).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from trend_agent.schemas.search_result import CandidateVideo, EvidenceTier, VideoFormat
from trend_agent.utils.time import ensure_utc
from trend_agent.verification.text import fold
from trend_agent.verification.untrusted import injection_signals, sanitize

_FORMATS: tuple[tuple[VideoFormat, re.Pattern[str]], ...] = (
    (VideoFormat.COMPARISON, re.compile(r"\b(vs\.?|versus|compared?|comparison)\b")),
    (VideoFormat.REVIEW, re.compile(r"\b(review|worth it|impressions|verdict|pros and cons)\b")),
    (
        VideoFormat.TUTORIAL,
        re.compile(
            r"\b(how to|tutorial|step by step|install\w*|set ?up|setup|guide to|getting started|"
            r"beginners?|walkthrough|migrat\w*|course)\b"
        ),
    ),
    (
        VideoFormat.GUIDE,
        re.compile(r"\b(guide|locations?|where to find|how to get|all \w+ locations|tips)\b"),
    ),
    (
        VideoFormat.DEMONSTRATION,
        re.compile(r"\b(demo|in action|hands[- ]on|first look|showcase|tested|testing|gameplay)\b"),
    ),
    (
        VideoFormat.NEWS,
        re.compile(
            r"\b(announced|announcement|news|launch(?:es|ed)?|released?|leaks?|leaked|recap|"
            r"everything we know|what'?s new|is here|revealed)\b"
        ),
    ),
    (
        VideoFormat.ENTERTAINMENT,
        re.compile(r"\b(funny|memes?|moments|montage|reaction|compilation|fails)\b"),
    ),
)


def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def video_format(title: str, duration_seconds: int | None, short_form_seconds: int) -> VideoFormat:
    if duration_seconds is not None and duration_seconds <= short_form_seconds:
        return VideoFormat.SHORT
    t = fold(title)
    for fmt, pattern in _FORMATS:
        if pattern.search(t):
            return fmt
    return VideoFormat.UNKNOWN


def normalise(
    raw: dict[str, Any],
    *,
    variant: str,
    position: int,
    collected_at: datetime,
    expires_at: datetime | None,
    synthetic: bool = False,
    transcript_authorised: bool = False,
) -> CandidateVideo:
    title = sanitize(str(raw.get("title") or ""), 300)
    description = sanitize(raw.get("description"), 1500) or None
    published = raw.get("published_at")
    if isinstance(published, str):
        published = ensure_utc(datetime.fromisoformat(published.replace("Z", "+00:00")))
    transcript = raw.get("transcript") if transcript_authorised else None
    flags = injection_signals(f"{raw.get('title') or ''}\n{raw.get('description') or ''}")
    return CandidateVideo(
        video_id=str(raw["video_id"]),
        title=title,
        description=description,
        channel_id=str(raw.get("channel_id") or "unknown"),
        channel_title=sanitize(raw.get("channel_title"), 120) or None,
        published_at=published,
        duration_seconds=raw.get("duration_seconds"),
        default_language=raw.get("default_language"),
        default_audio_language=raw.get("default_audio_language"),
        url=video_url(str(raw["video_id"])),
        variant=variant,
        api_position=position,
        collected_at=collected_at,
        expires_at=expires_at,
        tier=EvidenceTier.B_CAPTIONS if transcript else EvidenceTier.A_METADATA,
        transcript=sanitize(transcript, 20000) if transcript else None,
        injection_flags=flags,
        synthetic=synthetic,
    )


def dedupe(videos: Iterable[CandidateVideo]) -> list[CandidateVideo]:
    """Keep the first occurrence of each video (canonical query before variants)."""
    seen: set[str] = set()
    out = []
    for v in videos:
        if v.video_id not in seen:
            seen.add(v.video_id)
            out.append(v)
    return out
