"""Lease keeping for claimed jobs.

A claim is valid until ``lease_expires_at``. Long-running work renews the lease in the
background; if renewal fails (another worker reclaimed the job after an expiry), the
holder learns it has lost the lease and must not record a final state. Losing a lease
never corrupts data: observation writes are idempotent.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

from trend_agent.db.repositories.collection_jobs import JobRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.utils.time import Clock


class LeaseState:
    def __init__(self) -> None:
        self.lost = False
        self.renewals = 0


@contextlib.asynccontextmanager
async def keep_lease(
    factory: SessionFactory,
    job_id: uuid.UUID,
    token: uuid.UUID,
    *,
    lease: timedelta,
    clock: Clock,
    interval: timedelta | None = None,
) -> AsyncIterator[LeaseState]:
    state = LeaseState()
    every = (interval or lease / 3).total_seconds()

    async def renew_forever() -> None:
        while True:
            await asyncio.sleep(every)
            with transaction(factory) as session:
                ok = JobRepository(session).renew(job_id, token, clock() + lease)
            if not ok:
                state.lost = True
                return
            state.renewals += 1

    task = asyncio.create_task(renew_forever())
    try:
        yield state
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
