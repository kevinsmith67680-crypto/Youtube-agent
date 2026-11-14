"""Ground-truth label import (JSON Lines), with REAL and SYNTHETIC kept apart.

Each line::

    {"kind": "surge_event", "subject_key": "target:42", "event_time": "2026-10-03T00:00:00Z",
     "known_at": "2026-10-10T00:00:00Z", "label": {"onset": "...", "kind": "spike"},
     "source": "manual review of YouTube Studio search terms", "labeler": "analyst-1"}

Rules (a line that breaks one is rejected with a reason; nothing is fabricated):

* ``origin`` is given for the whole file and must be ``real`` or ``synthetic``; a file can
  never mix them, and synthetic labels can never be imported as real.
* ``source`` and ``labeler`` are mandatory provenance.
* ``known_at >= event_time``: ground truth cannot be known before the event happened, and
  evaluations only use a label from its ``known_at`` time onward (no look-ahead).
* real labels must not name a synthetic subject (``synthetic:`` / ``demo:`` keys).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from trend_agent.db.repositories.ops import LabelRepository
from trend_agent.db.session import SessionFactory, transaction

KINDS = frozenset(
    {"surge_event", "no_surge_window", "content_gap", "forecast_outcome", "recommendation_outcome"}
)
SYNTHETIC_PREFIXES = ("synthetic:", "demo:", "fictional:")


@dataclass
class ImportReport:
    origin: str
    accepted: int = 0
    duplicates: int = 0
    rejected: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
        }


def _ts(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be an RFC 3339 string")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp needs an explicit UTC offset")
    return dt


def validate(line: dict[str, Any], origin: str) -> dict[str, Any]:
    kind = line.get("kind")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    subject = line.get("subject_key")
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 200:
        raise ValueError("subject_key is required (<= 200 chars)")
    if origin == "real" and subject.startswith(SYNTHETIC_PREFIXES):
        raise ValueError("a real label cannot name a synthetic subject")
    if line.get("origin") not in (None, origin):
        raise ValueError(f"line origin {line.get('origin')!r} != file origin {origin!r}")
    event_time, known_at = _ts(line.get("event_time")), _ts(line.get("known_at"))
    if known_at < event_time:
        raise ValueError("known_at precedes event_time (ground truth from the future)")
    for key in ("source", "labeler"):
        if not isinstance(line.get(key), str) or not line[key].strip():
            raise ValueError(f"{key} is required provenance")
    label = line.get("label", {})
    if not isinstance(label, dict):
        raise ValueError("label must be an object")
    return {
        "kind": kind,
        "subject_key": subject.strip(),
        "event_time": event_time,
        "known_at": known_at,
        "label": label,
        "source": line["source"].strip(),
        "labeler": line["labeler"].strip(),
        "notes": line.get("notes"),
    }


def import_file(factory: SessionFactory, path: Path, *, origin: str) -> ImportReport:
    if origin not in ("real", "synthetic"):
        raise ValueError("origin must be 'real' or 'synthetic'")
    rep = ImportReport(origin)
    good = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError("each line must be a JSON object")
            good.append(validate(obj, origin))
        except (ValueError, json.JSONDecodeError) as exc:
            rep.rejected.append({"line": n, "reason": str(exc)[:200]})
    with transaction(factory) as s:
        repo = LabelRepository(s)
        for g in good:
            if repo.upsert(origin=origin, **g):
                rep.accepted += 1
            else:
                rep.duplicates += 1
    return rep
