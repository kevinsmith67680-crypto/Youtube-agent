"""Typed Twitch Helix records and the definition of every stored value.

Source: https://dev.twitch.tv/docs/api/reference/ (Get Top Games, Get Games, Get Streams).
Unknown fields are ignored. A record missing a documented identifier is skipped with a
counted reason, never guessed around; optional fields (IGDB ID, language, title) may be
empty strings, which the API documents.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from trend_agent.utils.time import parse_rfc3339

PROVIDER_NAME = "twitch"
SIGNAL_CLASS = "LIVESTREAM_ACTIVITY"
SIGNAL_LABEL = "livestream activity on Twitch (external gaming attention)"

# Evidence classes the dashboard and the API show (never merged into one number).
EVIDENCE_CLASSES: dict[str, str] = {
    "TWITCH_ACTIVITY": "Top-game rankings and live streams from the Twitch Helix API: "
    "external gaming attention on another platform. Not YouTube search interest.",
    "OBSERVED_VIEWERS": "Concurrent viewers of the live streams actually returned by the API "
    "at collection time, summed. Not a total for the game unless the sample is complete, "
    "and never a number of searches.",
    "PARTIAL_SAMPLE": "The collection stopped before the API's list ended (page limit or "
    "error), so viewer and stream counts cover only part of the live streams.",
    "RANK_CHANGE": "Change in a game's position in Get Top Games between two stored "
    "observations, over their actual interval. Positions only; Twitch does not return "
    "game viewer totals from that endpoint.",
    "PROXY_OPPORTUNITY": "A possible YouTube video opportunity inferred from Twitch "
    "attention plus verified game entities. A hypothesis to investigate, not demand.",
    "DIRECT_YOUTUBE_SEARCH_INTEREST": "Relative interest in YouTube Search from a "
    "live-verified direct provider. Twitch data never produces this class.",
}

MEASUREMENT_DEFINITIONS: dict[str, str] = {
    "game_id": "Twitch's stable ID of the category or game.",
    "rank": "1-based position in Get Top Games at collection time (the API orders by "
    "viewers but does not return the viewer numbers).",
    "viewer_count": "Concurrent viewers of one live stream as returned by Get Streams at "
    "collection time.",
    "viewers_observed": "Sum of viewer_count over the distinct live streams returned in one "
    "collection. Equals the game's live audience only when sample_status is 'complete', "
    "and even then streams can be missed while the list re-orders during paging.",
    "streams_observed": "Number of distinct live streams returned in one collection.",
    "sample_status": "complete = every page was read (empty cursor) without errors; "
    "partial = stopped at the page limit or after an error; unknown = the response did "
    "not allow a judgement.",
    "observed_at": "When this system started the collection (UTC).",
}

NON_GAME_REASON = "non_game_category"


def _blank_to_none(value: Any) -> Any:
    return None if isinstance(value, str) and not value.strip() else value


class TwitchGame(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str = Field(min_length=1, max_length=32, pattern=r"^\d+$")
    name: str = Field(min_length=1, max_length=300)
    box_art_url: str | None = None
    igdb_id: str | None = None

    @field_validator("box_art_url", "igdb_id", mode="before")
    @classmethod
    def _empty(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @property
    def has_igdb(self) -> bool:
        return bool(self.igdb_id and self.igdb_id.isdigit())


class TwitchStream(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str = Field(min_length=1, max_length=40)
    user_id: str = Field(min_length=1, max_length=40)
    user_login: str = Field(min_length=1, max_length=64)
    user_name: str | None = None
    game_id: str | None = None  # empty string from the API when no category is set
    game_name: str | None = None
    type: str | None = None
    title: str | None = None
    viewer_count: int = Field(ge=0)
    started_at: datetime
    language: str | None = None

    @field_validator("game_id", "game_name", "title", "language", "user_name", mode="before")
    @classmethod
    def _empty(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @field_validator("started_at", mode="before")
    @classmethod
    def _time(cls, value: Any) -> Any:
        return parse_rfc3339(value) if isinstance(value, str) else value


def _problem(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(p) for p in first.get("loc", ())) or "record"
    kind = "missing" if first.get("type") == "missing" else "invalid"
    return f"{kind}:{where}"


def parse_game(raw: Any) -> tuple[TwitchGame | None, str | None]:
    if not isinstance(raw, dict):
        return None, "malformed:not_an_object"
    try:
        return TwitchGame.model_validate(raw), None
    except ValidationError as exc:
        return None, _problem(exc)


def parse_stream(raw: Any) -> tuple[TwitchStream | None, str | None]:
    if not isinstance(raw, dict):
        return None, "malformed:not_an_object"
    try:
        stream = TwitchStream.model_validate(raw)
    except (ValidationError, ValueError) as exc:
        return None, _problem(exc) if isinstance(exc, ValidationError) else "invalid:started_at"
    if stream.type not in (None, "live"):
        return None, f"not_live:{stream.type}"
    return stream, None


def category_kind(name: str, igdb_id: str | None, non_games: Iterable[str]) -> str:
    """'game' only when IGDB knows it; listed non-game categories are never games."""
    lowered = {n.casefold() for n in non_games}
    if name.casefold() in lowered:
        return "non_game"
    return "game" if igdb_id and igdb_id.isdigit() else "unknown"
