"""Repositories: the only modules that issue SQL."""

from trend_agent.db.repositories.core import (
    IngestionRunRepository,
    ProviderRepository,
    QueryRepository,
    RejectionRecord,
)
from trend_agent.db.repositories.observations import (
    InsertResult,
    ObservationRepository,
    StoredObservation,
)
from trend_agent.db.repositories.quota import SqlQuotaStore
from trend_agent.db.repositories.videos import VideoRepository

__all__ = [
    "IngestionRunRepository",
    "InsertResult",
    "ObservationRepository",
    "ProviderRepository",
    "QueryRepository",
    "RejectionRecord",
    "SqlQuotaStore",
    "StoredObservation",
    "VideoRepository",
]
