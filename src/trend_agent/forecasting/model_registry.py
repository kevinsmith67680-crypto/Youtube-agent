"""Versioned model registry: metadata, checksummed artifacts, explicit status changes.

Artifacts are plain JSON (parameters, calibrators, intervals, OOD reference, analogs) and
LightGBM's own text format. Nothing is ever unpickled: a model is rebuilt from parameters
after the SHA-256 of every file matches the checksum recorded at registration, and only
files inside the configured artifact root are read.

Statuses: EXPERIMENTAL -> VALIDATED (passed the acceptance checks) -> CHALLENGER/CHAMPION;
RETIRED. A model becomes CHAMPION only through ``promote`` with an explicit comparison;
the production model is never replaced silently, and every change is an event. Models
trained on synthetic data keep ``data_origin = synthetic`` and are only ever selected for
synthetic series.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from trend_agent.schemas.model_metadata import DataOrigin, ModelMetadata, ModelStatus

ALLOWED_FILES = ("model.json", "lightgbm.txt")


class ArtifactError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelEvent:
    model_id: str
    from_status: str | None
    to_status: str
    reason: str
    at: datetime
    evidence: dict[str, Any] = field(default_factory=dict)


class ModelStore(Protocol):
    def add(self, meta: ModelMetadata, location: str) -> None: ...
    def get(self, model_id: str) -> tuple[ModelMetadata, str] | None: ...
    def all(self) -> list[ModelMetadata]: ...
    def set_status(
        self,
        model_id: str,
        status: ModelStatus,
        reason: str,
        at: datetime,
        evidence: dict[str, Any] | None = None,
    ) -> None: ...
    def events(self, model_id: str | None = None) -> list[ModelEvent]: ...


class MemoryModelStore:
    """In-memory registry (offline demo and tests)."""

    def __init__(self) -> None:
        self._models: dict[str, tuple[ModelMetadata, str]] = {}
        self._events: list[ModelEvent] = []

    def add(self, meta: ModelMetadata, location: str) -> None:
        if meta.model_id in self._models:
            raise ValueError(f"model {meta.model_id} already registered")
        self._models[meta.model_id] = (meta, location)
        self._events.append(
            ModelEvent(meta.model_id, None, meta.status.value, "registered", meta.trained_at)
        )

    def get(self, model_id: str) -> tuple[ModelMetadata, str] | None:
        return self._models.get(model_id)

    def all(self) -> list[ModelMetadata]:
        return [m for m, _ in self._models.values()]

    def set_status(
        self,
        model_id: str,
        status: ModelStatus,
        reason: str,
        at: datetime,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        meta, loc = self._models[model_id]
        self._events.append(
            ModelEvent(model_id, meta.status.value, status.value, reason, at, evidence or {})
        )
        self._models[model_id] = (meta.model_copy(update={"status": status}), loc)

    def events(self, model_id: str | None = None) -> list[ModelEvent]:
        return [e for e in self._events if model_id is None or e.model_id == model_id]


class ArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _dir(self, model_id: str) -> Path:
        if not model_id.replace("-", "").replace("_", "").isalnum():
            raise ArtifactError(f"invalid model id {model_id!r}")
        path = (self.root / model_id).resolve()
        if not path.is_relative_to(self.root):
            raise ArtifactError("artifact path escapes the artifact root")
        return path

    def write(
        self, model_id: str, payload: dict[str, Any], lightgbm_text: str | None
    ) -> tuple[str, str]:
        d = self._dir(model_id)
        d.mkdir(parents=True, exist_ok=False)
        files = {"model.json": json.dumps(payload, sort_keys=True, default=_json_default)}
        if lightgbm_text is not None:
            files["lightgbm.txt"] = lightgbm_text
        for name, text in files.items():
            tmp = d / (name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, d / name)
        return model_id, self.checksum(model_id)

    def checksum(self, model_id: str) -> str:
        d = self._dir(model_id)
        h = hashlib.sha256()
        for name in ALLOWED_FILES:
            p = d / name
            if p.exists():
                h.update(name.encode())
                h.update(p.read_bytes())
        return h.hexdigest()

    def read(self, location: str, checksum: str) -> tuple[dict[str, Any], str | None]:
        d = self._dir(location)
        unexpected = [p.name for p in d.iterdir() if p.name not in ALLOWED_FILES]
        if unexpected:
            raise ArtifactError(f"unexpected files in artifact {location}: {unexpected}")
        if self.checksum(location) != checksum:
            raise ArtifactError(f"checksum mismatch for artifact {location}: refusing to load")
        payload = json.loads((d / "model.json").read_text(encoding="utf-8"))
        text = (
            (d / "lightgbm.txt").read_text(encoding="utf-8")
            if (d / "lightgbm.txt").exists()
            else None
        )
        return payload, text


def _json_default(o: Any) -> Any:
    import numpy as np

    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(type(o).__name__)


def new_model_id(name: str, horizon: int, granularity: str) -> str:
    return f"fm_{name}_{granularity}_{horizon}h_{uuid.uuid4().hex[:10]}"


class Registry:
    def __init__(self, store: ModelStore, artifacts: ArtifactStore) -> None:
        self.store = store
        self.artifacts = artifacts
        self._cache: dict[str, tuple[dict[str, Any], str | None]] = {}

    def register(
        self, meta: ModelMetadata, payload: dict[str, Any], lightgbm_text: str | None
    ) -> ModelMetadata:
        location, checksum = self.artifacts.write(meta.model_id, payload, lightgbm_text)
        meta = meta.model_copy(update={"artifact_checksum": checksum})
        self.store.add(meta, location)
        return meta

    def load(self, model_id: str) -> tuple[ModelMetadata, dict[str, Any], str | None]:
        found = self.store.get(model_id)
        if found is None:
            raise ArtifactError(f"model {model_id} not registered")
        meta, location = found
        if model_id not in self._cache:
            self._cache[model_id] = self.artifacts.read(location, meta.artifact_checksum)
        payload, text = self._cache[model_id]
        return meta, payload, text

    def champion(
        self,
        *,
        family: str,
        granularity: str,
        horizon: int,
        target_type: str,
        data_origin: DataOrigin,
        category: str | None = None,
    ) -> ModelMetadata | None:
        """The CHAMPION for this scope (category-specific first, then pooled ``all``)."""
        hits = [
            m
            for m in self.store.all()
            if m.status is ModelStatus.CHAMPION
            and m.target_family.value == family
            and m.granularity == granularity
            and m.horizon_hours == horizon
            and m.target_type.value == target_type
            and m.data_origin is data_origin
        ]
        for scope in (category, "all"):
            for m in hits:
                if m.category_scope == scope:
                    return m
        return None

    def promote(
        self, model_id: str, *, at: datetime, reason: str, evidence: dict[str, Any]
    ) -> ModelMetadata:
        """Make a VALIDATED/CHALLENGER model the CHAMPION of its scope (explicit, logged)."""
        found = self.store.get(model_id)
        if found is None:
            raise ArtifactError(f"model {model_id} not registered")
        meta, _ = found
        if meta.status not in (ModelStatus.VALIDATED, ModelStatus.CHALLENGER):
            raise ValueError(f"only VALIDATED or CHALLENGER models can be promoted ({meta.status})")
        current = self.champion(
            family=meta.target_family.value,
            granularity=meta.granularity,
            horizon=meta.horizon_hours,
            target_type=meta.target_type.value,
            data_origin=meta.data_origin,
            category=meta.category_scope if meta.category_scope != "all" else None,
        )
        if current is not None and current.category_scope == meta.category_scope:
            self.store.set_status(
                current.model_id, ModelStatus.RETIRED, f"replaced by {model_id}", at, evidence
            )
        self.store.set_status(model_id, ModelStatus.CHAMPION, reason, at, evidence)
        got = self.store.get(model_id)
        assert got is not None
        return got[0]

    def retire(self, model_id: str, *, at: datetime, reason: str) -> None:
        self.store.set_status(model_id, ModelStatus.RETIRED, reason, at)

    def mark(
        self,
        model_id: str,
        status: ModelStatus,
        *,
        at: datetime,
        reason: str,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        if status is ModelStatus.CHAMPION:
            raise ValueError("use promote() to make a champion")
        self.store.set_status(model_id, status, reason, at, evidence)
