"""Typed Hacker News item records and the definition of every stored value.

The official API (https://github.com/HackerNews/API) returns ``null`` for unknown IDs and
items flagged ``deleted`` or ``dead``. Unknown fields are ignored; a record without the
documented ``id`` is a schema change and is reported, not guessed around.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

LISTS = ("newstories", "topstories", "beststories", "showstories", "askstories")
LIST_LABEL = {
    "newstories": "new",
    "topstories": "top",
    "beststories": "best",
    "showstories": "show",
    "askstories": "ask",
}
LIST_BY_TYPE = {v: k for k, v in LIST_LABEL.items()}
DISCUSSION_URL = "https://news.ycombinator.com/item?id={id}"
SIGNAL_CLASS = "COMMUNITY_ACTIVITY"
SIGNAL_LABEL = "community discussion activity (Hacker News)"

MEASUREMENT_DEFINITIONS: dict[str, str] = {
    "story_id": "Hacker News item ID: stable and unique across the site.",
    "score": (
        "The story's points as returned by the API at collection time (votes, after Hacker "
        "News' own adjustments). Cumulative; not views, not searches, not unique people."
    ),
    "comments": "The item's 'descendants': total comments at collection time.",
    "published_at": "The item's 'time' (Unix seconds) converted to UTC.",
    "collected_at": "When this system received the record.",
    "score_delta": "Score at a later observation minus an earlier one, same story.",
    "score_per_hour": (
        "score_delta divided by the actual hours between the two observations; reported "
        "only when they are at least the configured minimum apart."
    ),
    "comments_per_hour": "Comment-count change per actual elapsed hour (same rule).",
}


class HNItem(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int = Field(gt=0)
    type: str | None = None
    by: str | None = None
    time: int | None = None
    title: str | None = None
    url: str | None = None
    text: str | None = None
    score: int | None = None
    descendants: int | None = None
    deleted: bool = False
    dead: bool = False

    @property
    def published_at(self) -> datetime | None:
        return datetime.fromtimestamp(self.time, tz=UTC) if self.time else None

    @property
    def discussion_url(self) -> str:
        return DISCUSSION_URL.format(id=self.id)

    def skip_reason(self) -> str | None:
        """Why this record is not a usable story (None when it is)."""
        if self.deleted:
            return "deleted"
        if self.dead:
            return "dead"
        if self.type != "story":
            return f"not_a_story:{self.type or 'unknown'}"
        if not (self.title or "").strip():
            return "missing_title"
        if self.time is None:
            return "missing_time"
        return None


def parse_item(raw: Any) -> tuple[HNItem | None, str | None]:
    """(item, problem). ``None`` raw (unknown ID) is a normal 'missing' outcome."""
    if raw is None:
        return None, "missing"
    if not isinstance(raw, dict):
        return None, "malformed:not_an_object"
    try:
        return HNItem.model_validate(raw), None
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        return None, f"malformed:{where}"
