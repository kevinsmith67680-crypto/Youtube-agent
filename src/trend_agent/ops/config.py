"""Operations configuration (``config/operations.yaml``): schedule cadence and alerting."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaskConfig(_Strict):
    interval_seconds: int = Field(ge=10)
    timeout_seconds: int = Field(default=600, ge=5)
    enabled: bool = True
    max_per_run: int | None = Field(default=None, ge=1)

    @property
    def interval(self) -> timedelta:
        return timedelta(seconds=self.interval_seconds)


def _default_tasks() -> dict[str, TaskConfig]:
    return {
        "ops_checks": TaskConfig(interval_seconds=60, timeout_seconds=60),
        # Unattended discovery (feeds, GitHub, YouTube, ...): only when
        # TREND_AGENT_SCHEDULED_DISCOVERY=true; bounded by max_per_run fetches per run.
        "discovery": TaskConfig(interval_seconds=3600, timeout_seconds=900, max_per_run=24),
        # Hacker News lists due by their own source intervals + incremental story refresh
        # (also gated by TREND_AGENT_SCHEDULED_DISCOVERY and the HN provider switch).
        "hackernews": TaskConfig(interval_seconds=900, timeout_seconds=600, max_per_run=5),
        # Twitch: due polls by their own cadence (5-minute tick), attention, promotion;
        # max_per_run bounds monitored-game polls per tick.
        "twitch": TaskConfig(interval_seconds=300, timeout_seconds=240, max_per_run=12),
        "monitor_plan": TaskConfig(interval_seconds=300, timeout_seconds=120),
        "content_analysis": TaskConfig(interval_seconds=1800, timeout_seconds=900, max_per_run=10),
        "forecasting": TaskConfig(interval_seconds=3600, timeout_seconds=900),
        "recommendations": TaskConfig(interval_seconds=21600, timeout_seconds=900),
        "retention": TaskConfig(interval_seconds=86400, timeout_seconds=600),
    }


class SchedulerConfig(_Strict):
    tick_seconds: float = Field(default=30, ge=1)
    lease_ttl_seconds: int = Field(default=120, ge=10)
    stale_run_minutes: int = Field(default=120, ge=1)
    failure_retry_seconds: int = Field(default=60, ge=1)
    tasks: dict[str, TaskConfig] = Field(default_factory=_default_tasks)

    @field_validator("tasks", mode="after")
    @classmethod
    def _with_defaults(cls, tasks: dict[str, TaskConfig]) -> dict[str, TaskConfig]:
        """Tasks missing from operations.yaml keep their defaults (new stages included)."""
        return {**_default_tasks(), **tasks}


class WorkerConfig(_Strict):
    tick_seconds: float = Field(default=15, ge=1)
    max_job_batches: int = Field(default=50, ge=1)


class AlertConfig(_Strict):
    stage_failure_threshold: int = Field(default=3, ge=1)
    worker_missing_minutes: int = Field(default=10, ge=1)


class OperationsConfig(_Strict):
    version: int = 1
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    alerts: AlertConfig = Field(default_factory=AlertConfig)


KNOWN_TASKS = frozenset(_default_tasks())


def load_operations_config(directory: str | None) -> OperationsConfig:
    path = Path(directory or "config") / "operations.yaml"
    if not path.exists():
        return OperationsConfig()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = OperationsConfig.model_validate(data)
    unknown = set(cfg.scheduler.tasks) - KNOWN_TASKS
    if unknown:
        raise ValueError(f"unknown scheduler task(s) in {path}: {sorted(unknown)}")
    return cfg
