"""Migrations, constraints, idempotency, transactions, retention and the SQL quota store."""

from __future__ import annotations

import math
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from tests.conftest import alembic_config
from tests.helpers import FIXED_NOW, TEST_DB_URL
from tests.unit.test_validation_comparability import candidate
from trend_agent.db.models import (
    ProviderRateLimitRow,
    RejectedRecordRow,
    SearchInterestObservationRow,
    SearchQueryRow,
    VideoRow,
    VideoStatisticsRow,
)
from trend_agent.db.repositories import (
    IngestionRunRepository,
    InsertResult,
    ObservationRepository,
    ProviderRepository,
    QueryRepository,
    RejectionRecord,
    SqlQuotaStore,
    VideoRepository,
)
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.ingestion.retention import RetentionService
from trend_agent.ingestion.validation import validate_candidate
from trend_agent.providers.mock import MockSearchInterestProvider
from trend_agent.providers.quota import QuotaWindow
from trend_agent.schemas.enums import IngestionStatus, RecordKind, RejectionReason
from trend_agent.schemas.observation import ValidatedObservation
from trend_agent.schemas.video import (
    ChannelDetails,
    VideoDetails,
    VideoSearchResult,
    VideoStatistics,
)

MOCK = MockSearchInterestProvider().descriptor


def observation(**overrides: Any) -> ValidatedObservation:
    obs = validate_candidate(candidate(**overrides), MOCK, now=FIXED_NOW)
    assert isinstance(obs, ValidatedObservation)
    return obs


def setup_ids(factory: SessionFactory) -> tuple[int, int]:
    with transaction(factory) as s:
        provider_id = ProviderRepository(s).sync(MOCK)
        query_id = QueryRepository(s).get_or_create("ai agents", "AI Agents", "en", "GB")
    return provider_id, query_id


# ----------------------------------------------------------------------------- migrations


def test_migrations_apply_to_clean_database_and_round_trip(db_engine: sa.Engine) -> None:
    from alembic import command

    cfg = alembic_config(TEST_DB_URL)
    command.downgrade(cfg, "base")
    with db_engine.connect() as conn:
        tables = set(sa.inspect(conn).get_table_names())
    assert tables <= {"alembic_version"}
    command.upgrade(cfg, "head")
    with db_engine.connect() as conn:
        tables = set(sa.inspect(conn).get_table_names())
        version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    assert {
        "providers",
        "search_queries",
        "search_interest_observations",
        "videos",
        "video_statistics",
        "ingestion_runs",
        "provider_rate_limits",
        "rejected_records",
        "channels",
        "video_search_results",
    } <= tables
    assert version == "0001"
    command.check(cfg)  # raises if the ORM models and migrations have drifted


def test_timestamps_are_timestamptz_and_session_is_utc(db_engine: sa.Engine) -> None:
    with db_engine.connect() as conn:
        assert conn.execute(sa.text("SHOW timezone")).scalar_one() == "UTC"
        rows = (
            conn.execute(
                sa.text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_name = 'search_interest_observations' "
                    "AND column_name IN ('observation_start', 'observation_end', "
                    "'collected_at', 'available_at', 'created_at')"
                )
            )
            .tuples()
            .all()
        )
    types: dict[str, str] = {str(name): str(kind) for name, kind in rows}
    assert set(types.values()) == {"timestamp with time zone"}
    assert len(types) == 5


# ----------------------------------------------------------------------------- constraints


def test_query_unique_constraint_treats_nulls_as_equal(session_factory: SessionFactory) -> None:
    with transaction(session_factory) as s:
        repo = QueryRepository(s)
        a = repo.get_or_create("ai agents", "AI agents", None, None)
        b = repo.get_or_create("ai agents", "ai AGENTS", None, None)
        c = repo.get_or_create("ai agents", "AI agents", "en", None)
        assert a == b
        assert a != c
    with transaction(session_factory) as s:
        count = s.execute(sa.select(sa.func.count()).select_from(SearchQueryRow)).scalar_one()
        assert count == 2
        original = s.execute(
            sa.select(SearchQueryRow.original_query).where(SearchQueryRow.id == a)
        ).scalar_one()
        assert original == "AI agents"  # first-seen original text preserved


def test_idempotent_observation_inserts_and_conflicts(session_factory: SessionFactory) -> None:
    provider_id, query_id = setup_ids(session_factory)
    obs = observation()
    kwargs: dict[str, Any] = {
        "query_id": query_id,
        "provider_id": provider_id,
        "run_id": None,
        "expires_at": None,
    }
    with transaction(session_factory) as s:
        repo = ObservationRepository(s)
        assert repo.insert(obs, **kwargs)[0] is InsertResult.INSERTED
        assert repo.insert(obs, **kwargs)[0] is InsertResult.ALREADY_PRESENT
        # A re-collection with a later collected_at is the same observation.
        later = obs.model_copy(update={"collected_at": FIXED_NOW + timedelta(hours=1)})
        assert repo.insert(later, **kwargs)[0] is InsertResult.ALREADY_PRESENT
        result, detail = repo.insert(obs.model_copy(update={"value": 7.0}), **kwargs)
        assert result is InsertResult.CONFLICT
        assert "Stored value kept" in detail
        # Same window in a different normalisation batch is a distinct observation.
        other_batch = observation(
            normalization_context=candidate().normalization_context.model_copy(  # type: ignore[union-attr]
                update={"batch_id": "b2"}
            )
        )
        assert repo.insert(other_batch, **kwargs)[0] is InsertResult.INSERTED
    with transaction(session_factory) as s:
        stored = ObservationRepository(s).list_for_query("ai agents")
    assert len(stored) == 2
    assert {o.observation.value for o in stored} == {42.0}


def _raw_observation(provider_id: int, query_id: int, **overrides: Any) -> dict[str, Any]:
    obs = observation()
    values: dict[str, Any] = {
        "query_id": query_id,
        "provider_id": provider_id,
        "provider_version": "1.0.0",
        "measurement_type": "direct_search_interest",
        "search_property": "youtube_search",
        "granularity": "day",
        "observation_start": obs.observation_start,
        "observation_end": obs.observation_end,
        "collected_at": FIXED_NOW,
        "value": 10.0,
        "scale_type": "relative_per_request",
        "normalization_context": {},
        "normalization_key": "0" * 64,
        "geography": "GB",
        "sample_quality": "complete",
        "data_nature": "synthetic",
        "is_synthetic": True,
        "source_reference": "synthetic://x",
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    "overrides",
    [
        {"search_property": "google_web_search"},  # direct interest must be YouTube property
        {"measurement_type": "external_search_interest"},  # external with YouTube property
        {"is_synthetic": False},  # label inconsistency
        {"value": math.nan},
        {"value": math.inf},
        {"value": None},  # complete requires a value
        {"sample_quality": "suppressed"},  # suppressed must not carry a value
        {"data_nature": "unavailable", "is_synthetic": False},
        {"observation_end": FIXED_NOW - timedelta(days=400)},  # end before start
        {"measurement_type": "made_up"},
        {"normalization_key": "short"},
    ],
)
def test_database_rejects_invalid_rows_even_if_validation_is_bypassed(
    session_factory: SessionFactory, overrides: dict[str, Any]
) -> None:
    provider_id, query_id = setup_ids(session_factory)
    with pytest.raises((IntegrityError, sa.exc.StatementError)), transaction(session_factory) as s:
        s.execute(
            sa.insert(SearchInterestObservationRow).values(
                **_raw_observation(provider_id, query_id, **overrides)
            )
        )


def test_transaction_rollback_leaves_no_partial_writes(session_factory: SessionFactory) -> None:
    provider_id, query_id = setup_ids(session_factory)

    def failing_unit_of_work() -> None:
        with transaction(session_factory) as s:
            ObservationRepository(s).insert(
                observation(),
                query_id=query_id,
                provider_id=provider_id,
                run_id=None,
                expires_at=None,
            )
            raise RuntimeError("boom mid-transaction")

    with pytest.raises(RuntimeError):
        failing_unit_of_work()
    with transaction(session_factory) as s:
        assert ObservationRepository(s).list_for_query("ai agents") == []


def test_timestamps_round_trip_as_aware_utc(session_factory: SessionFactory) -> None:
    provider_id, query_id = setup_ids(session_factory)
    obs = observation()
    with transaction(session_factory) as s:
        ObservationRepository(s).insert(
            obs, query_id=query_id, provider_id=provider_id, run_id=None, expires_at=None
        )
    with transaction(session_factory) as s:
        stored = ObservationRepository(s).list_for_query("ai agents")[0].observation
    assert stored.observation_start == obs.observation_start
    assert stored.observation_start.utcoffset() == timedelta(0)
    assert stored.collected_at == FIXED_NOW
    assert stored.available_at == obs.available_at
    assert stored.collected_at != stored.observation_end  # never conflated


def test_all_rejection_reasons_are_storable(session_factory: SessionFactory) -> None:
    with transaction(session_factory) as s:
        provider_id = ProviderRepository(s).sync(MOCK)
        runs = IngestionRunRepository(s)
        run_id = runs.start(
            provider_id=provider_id,
            provider_name="mock",
            operation="t",
            request_params={"api_key": "secret", "value": math.nan},
            started_at=FIXED_NOW,
        )
        runs.add_rejections(
            run_id,
            provider_id,
            [
                RejectionRecord(
                    RecordKind.SEARCH_INTEREST_OBSERVATION,
                    reason,
                    "detail",
                    {"value": math.nan, "token": "abc"},
                )
                for reason in RejectionReason
            ],
            None,
        )
    with transaction(session_factory) as s:
        rows = s.execute(sa.select(RejectedRecordRow)).scalars().all()
        params = IngestionRunRepository(s).get(run_id).request_params  # type: ignore[union-attr]
    assert len(rows) == len(RejectionReason)
    assert rows[0].payload == {"value": "nan", "token": "***REDACTED***"}
    assert params == {"api_key": "***REDACTED***", "value": "nan"}


# ----------------------------------------------------------------------------- videos/retention


def video(vid: str = "vidAAAAAAA1", collected: datetime = FIXED_NOW) -> VideoDetails:
    return VideoDetails(
        video_id=vid,
        title="T",
        description="D",
        channel_id="UCaaaaaaaaaaaaaaaaaaaaaa",
        channel_title="C",
        published_at=FIXED_NOW - timedelta(days=3),
        duration_seconds=60,
        duration_iso="PT1M",
        category_id="28",
        statistics=VideoStatistics(view_count=10, like_count=None, comment_count=0),
        collected_at=collected,
    )


def test_video_upserts_retention_and_purge(session_factory: SessionFactory) -> None:
    provider_id, query_id = setup_ids(session_factory)
    expires = FIXED_NOW + timedelta(days=30)
    with transaction(session_factory) as s:
        repo = VideoRepository(s)
        repo.upsert_video_details(
            video(), provider_id=provider_id, expires_at=expires, persist_description=False
        )
        assert repo.insert_statistics(
            "vidAAAAAAA1",
            VideoStatistics(view_count=10),
            collected_at=FIXED_NOW,
            provider_id=provider_id,
            run_id=None,
            expires_at=expires,
        )
        assert not repo.insert_statistics(  # idempotent snapshot
            "vidAAAAAAA1",
            VideoStatistics(view_count=10),
            collected_at=FIXED_NOW,
            provider_id=provider_id,
            run_id=None,
            expires_at=expires,
        )
        # A later search snippet must not extend the expiry of the full record.
        snippet = VideoSearchResult(
            video_id="vidAAAAAAA1",
            title="T2",
            description=None,
            channel_id="UCaaaaaaaaaaaaaaaaaaaaaa",
            channel_title=None,
            published_at=None,
            rank=1,
            page=1,
        )
        assert not repo.insert_video_from_search(
            snippet,
            provider_id=provider_id,
            collected_at=FIXED_NOW + timedelta(days=10),
            expires_at=FIXED_NOW + timedelta(days=40),
            persist_description=True,
        )
        assert repo.insert_search_result(
            snippet,
            query_id=query_id,
            provider_id=provider_id,
            run_id=None,
            region="GB",
            language=None,
            order="relevance",
            published_after=None,
            published_before=None,
            collected_at=FIXED_NOW,
            expires_at=expires,
        )
        repo.upsert_channel(
            ChannelDetails(
                channel_id="UCaaaaaaaaaaaaaaaaaaaaaa",
                title="C",
                description=None,
                custom_url=None,
                country="GB",
                published_at=None,
                collected_at=FIXED_NOW,
            ),
            provider_id=provider_id,
            expires_at=expires,
        )
        assert repo.fresh_channel_ids(["UCaaaaaaaaaaaaaaaaaaaaaa", "UCzz"], FIXED_NOW) == {
            "UCaaaaaaaaaaaaaaaaaaaaaa"
        }
    with transaction(session_factory) as s:
        row = s.get(VideoRow, "vidAAAAAAA1")
        assert row is not None
        assert row.title == "T"
        assert row.description is None  # descriptions not persisted when disabled
        assert row.metadata_expires_at == expires

    clock_now = [FIXED_NOW + timedelta(days=29)]
    retention = RetentionService(session_factory, clock=lambda: clock_now[0])
    report = retention.report(horizon=timedelta(days=2))
    assert report.expiring_videos_within_horizon == 1
    assert sum(report.expired.values()) == 0
    assert sum(retention.purge_expired().values()) == 0

    clock_now[0] = FIXED_NOW + timedelta(days=30, seconds=1)
    deleted = retention.purge_expired()
    assert deleted["videos"] == 1
    assert deleted["channels"] == 1
    assert deleted["video_statistics"] == 1
    assert deleted["video_search_results"] == 1
    with transaction(session_factory) as s:
        assert s.execute(sa.select(sa.func.count()).select_from(VideoRow)).scalar_one() == 0
        assert (
            s.execute(sa.select(sa.func.count()).select_from(VideoStatisticsRow)).scalar_one() == 0
        )


def test_deleting_a_video_cascades_to_derived_rows(session_factory: SessionFactory) -> None:
    provider_id, _ = setup_ids(session_factory)
    expires = FIXED_NOW + timedelta(days=30)
    with transaction(session_factory) as s:
        repo = VideoRepository(s)
        repo.upsert_video_details(
            video(), provider_id=provider_id, expires_at=expires, persist_description=True
        )
        repo.insert_statistics(
            "vidAAAAAAA1",
            VideoStatistics(view_count=1),
            collected_at=FIXED_NOW,
            provider_id=provider_id,
            run_id=None,
            expires_at=expires,
        )
        assert repo.delete_videos(["vidAAAAAAA1"]) == 1
    with transaction(session_factory) as s:
        assert (
            s.execute(sa.select(sa.func.count()).select_from(VideoStatisticsRow)).scalar_one() == 0
        )


def test_observation_expiry_purge(session_factory: SessionFactory) -> None:
    provider_id, query_id = setup_ids(session_factory)
    with transaction(session_factory) as s:
        ObservationRepository(s).insert(
            observation(),
            query_id=query_id,
            provider_id=provider_id,
            run_id=None,
            expires_at=FIXED_NOW + timedelta(days=1),
        )
    retention = RetentionService(session_factory, clock=lambda: FIXED_NOW + timedelta(days=2))
    assert retention.report().expired["search_interest_observations"] == 1
    assert retention.purge_expired()["search_interest_observations"] == 1


# ----------------------------------------------------------------------------- quota store


def test_sql_quota_store_is_atomic_and_tracks_exhaustion(session_factory: SessionFactory) -> None:
    store = SqlQuotaStore(session_factory)
    window = QuotaWindow(datetime(2026, 3, 1, 8, tzinfo=UTC), datetime(2026, 3, 2, 8, tzinfo=UTC))
    granted: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(10):
            ok, _ = store.try_consume(provider="p", bucket="b", window=window, limit=25, cost=1)
            with lock:
                granted.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert granted.count(True) == 25  # never over-granted under concurrency
    assert store.get_usage(provider="p", bucket="b", window=window) == (25, False)

    store.mark_exhausted(provider="p", bucket="other", window=window)
    assert store.try_consume(provider="p", bucket="other", window=window, limit=99, cost=1) == (
        False,
        0,
    )
    assert store.get_usage(provider="p", bucket="other", window=window) == (0, True)
    with transaction(session_factory) as s:
        rows = s.execute(sa.select(ProviderRateLimitRow)).scalars().all()
    assert {r.bucket for r in rows} == {"b", "other"}
    assert all(r.window_end > r.window_start for r in rows)


def test_run_status_lifecycle(session_factory: SessionFactory) -> None:
    with transaction(session_factory) as s:
        runs = IngestionRunRepository(s)
        run_id = runs.start(
            provider_id=None,
            provider_name="nope",
            operation="t",
            request_params={},
            started_at=FIXED_NOW,
        )
        runs.finish(
            run_id,
            status=IngestionStatus.FAILED,
            completed_at=FIXED_NOW + timedelta(seconds=1),
            received=0,
            accepted=0,
            rejected=0,
            duplicate=0,
            withheld=0,
            quota_used=0,
            quota_detail={},
            error_type="provider_not_found",
            error_message="x",
            summary={},
        )
    with transaction(session_factory) as s:
        row = IngestionRunRepository(s).get(run_id)
        assert row is not None
        assert row.status is IngestionStatus.FAILED
        bad_update = (
            sa.update(type(row))
            .where(type(row).id == run_id)
            .values(completed_at=FIXED_NOW - timedelta(days=1))
        )
        with pytest.raises(IntegrityError):
            s.execute(bad_update)
