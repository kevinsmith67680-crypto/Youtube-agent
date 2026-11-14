"""Experiment registry helpers: every evaluation run is recorded with its dataset identity,
split, data origin, metrics, the baseline it was compared with and the decision taken."""

from __future__ import annotations

import subprocess
import uuid
from functools import lru_cache
from typing import Any

from trend_agent import __version__
from trend_agent.db.repositories.ops import ExperimentRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.logging import storable


@lru_cache(maxsize=1)
def code_version() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 - git from PATH
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        sha = ""
    return f"{__version__}+{sha}" if sha else __version__


def record(
    factory: SessionFactory | None,
    *,
    name: str,
    component: str,
    hypothesis: str,
    config: dict[str, Any],
    dataset: dict[str, Any],
    data_origin: str,
    split: str,
    metrics: dict[str, Any],
    baseline_metrics: dict[str, Any] | None = None,
    decision: str = "recorded",
    decision_reason: str = "measurement only",
) -> uuid.UUID | None:
    """Store an experiment (None when no database is available: results stay in the
    returned report and the caller says so)."""
    if factory is None:
        return None
    with transaction(factory) as s:
        return ExperimentRepository(s).add(
            name=name,
            component=component,
            hypothesis=hypothesis,
            config=storable(config),
            dataset=storable(dataset),
            data_origin=data_origin,
            split=split,
            metrics=storable(metrics),
            baseline_metrics=storable(baseline_metrics or {}),
            decision=decision,
            decision_reason=decision_reason,
            code_version=code_version(),
        )
