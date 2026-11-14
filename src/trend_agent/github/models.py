"""Typed views of the GitHub REST API responses we use, and what every stored value means.

Only documented fields are read. Unknown fields are ignored (GitHub adds fields over
time); a *missing required* field means the documented shape changed and is reported as a
schema change rather than guessed around.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from trend_agent.utils.time import ensure_utc

# What each stored value actually measures. Shown by `trend-agent github status --json`,
# the API and the report, so nobody has to guess what a number means.
MEASUREMENT_DEFINITIONS: dict[str, str] = {
    "repository_id": "GitHub's numeric repository ID: stable across renames and transfers.",
    "stargazers_count": (
        "Cumulative number of accounts that have starred the repository, at collection "
        "time. A running total, not recent growth. Stars are not unique people (accounts "
        "can be automated) and are not demand of any kind."
    ),
    "forks_count": "Cumulative number of forks at collection time (active or not).",
    "open_issues_count": "Open issues plus open pull requests at collection time.",
    "created_at": "When the repository was created on GitHub (the project may be older).",
    "pushed_at": (
        "Time of the most recent push to any branch. A push is not a release and not "
        "necessarily user-visible change."
    ),
    "updated_at": (
        "Last change to the repository object, including metadata such as the description "
        "or star count. Not a release and not a code change."
    ),
    "size_kb": "Repository size in kilobytes as reported by GitHub; 0 means empty.",
    "release.published_at": "When a (non-draft) release was published on GitHub.",
    "release.tag_name": "The git tag the release points to, exactly as published.",
    "collected_at": "When this system received the data from the API.",
    "star_delta": (
        "stargazers_count at a later observation minus an earlier one, over the actual "
        "interval between the two collections. Net change (unstars subtract)."
    ),
    "fork_delta": "forks_count difference between two observations over the actual interval.",
    "stars_per_day": (
        "star_delta divided by the observed interval in days. Only reported when two "
        "observations are at least the configured minimum interval apart."
    ),
}

SIGNAL_LABEL = "external developer activity"


def _utc(value: datetime | None) -> datetime | None:
    return ensure_utc(value) if value is not None else None


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class GitHubOwner(_Lenient):
    login: str = Field(min_length=1, max_length=100)
    id: int
    type: str | None = None


class GitHubLicense(_Lenient):
    spdx_id: str | None = None


class GitHubRepository(_Lenient):
    id: int
    name: str = Field(min_length=1, max_length=100)
    full_name: str = Field(min_length=3, max_length=200)
    owner: GitHubOwner
    html_url: str
    description: str | None = None
    fork: bool = False
    archived: bool = False
    disabled: bool = False
    is_template: bool = False
    private: bool = False
    created_at: datetime
    updated_at: datetime | None = None
    pushed_at: datetime | None = None
    language: str | None = None
    topics: tuple[str, ...] = ()
    stargazers_count: int = Field(ge=0)
    forks_count: int = Field(ge=0)
    open_issues_count: int = Field(default=0, ge=0)
    size: int = Field(default=0, ge=0)
    homepage: str | None = None
    default_branch: str | None = None
    license: GitHubLicense | None = None
    mirror_url: str | None = None

    _norm = field_validator("created_at", "updated_at", "pushed_at")(_utc)

    @field_validator("html_url")
    @classmethod
    def _github_url(cls, value: str) -> str:
        if not value.startswith("https://github.com/"):
            raise ValueError("html_url is not a github.com URL")
        return value

    def snapshot(self) -> dict[str, Any]:
        """The fields we persist per observation (no README, no code, no user data)."""
        return {
            "repository_id": self.id,
            "full_name": self.full_name,
            "name": self.name,
            "owner_login": self.owner.login,
            "owner_type": self.owner.type,
            "html_url": self.html_url,
            "description": self.description,
            "language": self.language,
            "topics": list(self.topics),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "pushed_at": self.pushed_at.isoformat() if self.pushed_at else None,
            "stargazers_count": self.stargazers_count,
            "forks_count": self.forks_count,
            "open_issues_count": self.open_issues_count,
            "size_kb": self.size,
            "archived": self.archived,
            "disabled": self.disabled,
            "fork": self.fork,
            "is_template": self.is_template,
            "license": self.license.spdx_id if self.license else None,
            "homepage": self.homepage,
        }


class GitHubRelease(_Lenient):
    id: int
    tag_name: str = Field(min_length=1, max_length=255)
    name: str | None = None
    html_url: str
    draft: bool = False
    prerelease: bool = False
    created_at: datetime | None = None
    published_at: datetime | None = None
    body: str | None = None
    author: GitHubOwner | None = None

    _norm = field_validator("created_at", "published_at")(_utc)


class RepositorySearchPage(_Lenient):
    total_count: int = Field(ge=0)
    incomplete_results: bool = False
    items: tuple[dict[str, Any], ...] = ()


def parse_items[T: BaseModel](
    model: type[T], raw: list[Any], *, what: str
) -> tuple[list[T], list[str]]:
    """Validate items one by one: a malformed item is skipped with a warning; if most
    items fail, the documented shape has changed and the caller must stop."""
    parsed: list[T] = []
    problems: list[str] = []
    for index, entry in enumerate(raw):
        try:
            parsed.append(model.model_validate(entry))
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ()))
            problems.append(f"{what}[{index}]: {where}: {first.get('msg')}")
    return parsed, problems
