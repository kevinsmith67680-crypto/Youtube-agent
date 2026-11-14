"""Phase 7 recommendations against the real test database.

The Phase 5 synthetic world (synthetic Phase 4 surge, fictional entities, a fake YouTube
Data API transport) produces a stored opportunity; Phase 7 then generates, ranks, stores,
alerts and serves it - offline, with deterministic generators.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from typer.testing import CliRunner

from tests.helpers import FakeYouTube
from tests.integration.test_content_analysis import FakeVideos, _event, _svc
from tests.integration.test_content_analysis import world as phase5_world
from tests.integration.test_detection import World, count
from trend_agent import cli
from trend_agent.content_analysis.llm_client import LLMResult, LLMUsage
from trend_agent.db.content_models import ContentOpportunityRow
from trend_agent.db.recommendation_models import (
    NotificationDeliveryRow,
    NotificationRow,
    ProductionTrackingRow,
    RecommendationFeedbackRow,
    RecommendationHookRow,
    RecommendationOutlineRow,
    RecommendationRunRow,
    RecommendationScoreRow,
    SavedRecommendationRow,
    VideoRecommendationRow,
)
from trend_agent.db.session import transaction
from trend_agent.ingestion.retention import RetentionService
from trend_agent.recommendations.backend import NotFoundError, TransitionError
from trend_agent.recommendations.db_backend import DatabaseBackend
from trend_agent.recommendations.idea_generator import LLMConceptGenerator
from trend_agent.schemas.dashboard import DataClass, GenerateIn, ProductionIn
from trend_agent.schemas.recommendation import (
    CreatorProfile,
    FeedbackType,
    ProductionStatus,
)

runner = CliRunner()
world = phase5_world


@pytest.fixture
def analysed(world: World, fake_youtube: FakeYouTube) -> World:
    """A stored *candidate* opportunity: the (fake) results cover only the previous
    version, so Phase 5 finds a supported gap for "Nimbus Coder 3"."""
    fake = FakeVideos(world.clock.now)
    for k in ("vidNC3tut01", "vidInject01"):
        fake.items.pop(k)
    old = fake.items["vidNC2tut01"]["publishedAt"]
    fake.items["vidNC2tut06"] = {
        "title": "Nimbus Coder 2 project walkthrough",
        "description": "Build an app.",
        "publishedAt": old,
    }
    fake.items["vidNC2tut07"] = {
        "title": "Nimbus Coder 2 settings explained",
        "description": "Every option.",
        "publishedAt": old,
    }
    fake_youtube.default("search", fake.search)
    fake_youtube.default("videos", fake.videos)
    res = asyncio.run(_svc(world).analyse_event(_event(world), include_synthetic=True))
    assert res.status == "completed", res.error
    assert res.report.status.value == "candidate", res.report.status
    return world


def _rec_svc(w: World, **kw: Any) -> Any:
    return w.container.recommendations(clock=w.clock, include_synthetic=True, **kw)


def _opportunity_status(w: World) -> str:
    with transaction(w.factory) as s:
        return str(s.execute(sa.select(ContentOpportunityRow.status)).scalar_one())


class FailingLLM:
    name = "scripted"
    model: str | None = "scripted-model"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> LLMResult:
        self.calls += 1
        return LLMResult(
            status="ok",
            raw_text="not json",
            model=self.model,
            usage=LLMUsage(input_tokens=1200, output_tokens=300),
            detail="unparseable",
            transient=False,
        )


def test_generate_persists_idempotently_and_reuses_unchanged_evidence(analysed: World) -> None:
    w = analysed
    svc = _rec_svc(w)
    res = svc.generate(all_categories=True)
    assert res.considered == 1
    status = _opportunity_status(w)
    if status != "candidate":
        # an insufficient-evidence opportunity yields one labelled research candidate
        assert res.research == res.generated == 1
    else:
        assert res.generated >= 1
    assert res.synthetic
    assert res.llm_calls == 0
    assert res.estimated_cost_usd == 0.0
    n = count(w.factory, VideoRecommendationRow)
    assert n == res.generated
    assert count(w.factory, RecommendationOutlineRow) == n
    assert count(w.factory, RecommendationScoreRow) == n * 7
    assert count(w.factory, RecommendationHookRow) >= 1
    with transaction(w.factory) as s:
        run = s.get(RecommendationRunRow, uuid.UUID(res.run_id))
        assert run is not None
        assert run.status == "completed"
        assert run.generated == res.generated
        assert run.is_synthetic
        rows = list(s.execute(sa.select(VideoRecommendationRow)).scalars())
        ids = sorted(str(r.id) for r in rows)
        assert all(r.is_current and r.is_synthetic for r in rows)
        assert all(r.concept["synthetic"] for r in rows)
        # stored document == column projection
        for r in rows:
            assert r.concept["recommendation_id"] == str(r.id)
            assert r.concept["verification"]["status"] == r.status
            assert r.concept["ranking"]["composite_score"] == r.composite_score
    # unchanged evidence: nothing regenerated, the same IDs are reused
    again = svc.generate(all_categories=True)
    assert again.generated == 0
    assert again.reused == res.generated
    assert count(w.factory, VideoRecommendationRow) == n
    with transaction(w.factory) as s:
        assert (
            sorted(str(r) for r in s.execute(sa.select(VideoRecommendationRow.id)).scalars()) == ids
        )
    # forced regeneration rewrites nothing either: deterministic IDs, idempotent writes
    forced = svc.generate(all_categories=True, force=True)
    assert forced.generated == res.generated
    assert count(w.factory, VideoRecommendationRow) == n
    assert count(w.factory, RecommendationRunRow) == 3


def test_dry_run_stores_nothing(analysed: World) -> None:
    w = analysed
    res = _rec_svc(w).generate(all_categories=True, dry_run=True)
    assert res.dry_run
    assert res.generated >= 1
    assert count(w.factory, VideoRecommendationRow) == 0
    assert count(w.factory, RecommendationRunRow) == 0
    assert count(w.factory, NotificationRow) == 0


def test_category_filter_and_profile_exclusions(analysed: World) -> None:
    w = analysed
    svc = _rec_svc(w)
    assert svc.generate(category="gaming").considered == 0
    svc.update_profile(CreatorProfile(content_exclusions=["nimbus"]))
    res = svc.generate(category="ai")
    assert res.considered == 1
    assert res.generated == 0
    assert any("excluded by the creator profile" in s for s in res.skipped)


def test_failing_llm_falls_back_and_budget_is_enforced(analysed: World) -> None:
    w = analysed
    llm = FailingLLM()
    svc = _rec_svc(w, generator=LLMConceptGenerator(llm, max_attempts=2))
    res = svc.generate(all_categories=True)
    assert llm.calls == 2  # bounded attempts, never indefinite
    assert res.llm_calls == 2
    assert res.input_tokens == 2400
    assert res.generator == "template"
    assert any("template" in n for n in res.notes)
    with transaction(w.factory) as s:
        run = s.get(RecommendationRunRow, uuid.UUID(res.run_id))
        assert run is not None
        assert run.input_tokens == 2400
    # over budget: the LLM is not called at all
    loaded = w.container.recommendation_config()
    tight = loaded.model_copy(
        update={
            "config": loaded.config.model_copy(
                update={"llm": loaded.config.llm.model_copy(update={"daily_token_budget": 100})}
            )
        },
    )
    llm2 = FailingLLM()
    svc2 = _rec_svc(w, generator=LLMConceptGenerator(llm2, max_attempts=2), loaded=tight)
    res2 = svc2.generate(all_categories=True, force=True)
    assert llm2.calls == 0
    assert any("budget" in n for n in res2.notes)


def test_alerts_for_synthetic_data_are_suppressed_unless_enabled(analysed: World) -> None:
    w = analysed
    _rec_svc(w).generate(all_categories=True)
    assert count(w.factory, NotificationRow) == 0  # synthetic: never alerted in production
    loaded = w.container.recommendation_config()
    on = loaded.model_copy(
        update={
            "config": loaded.config.model_copy(
                update={
                    "alerts": loaded.config.alerts.model_copy(
                        update={
                            "alert_on_synthetic": True,
                            "min_score_new_recommendation": 0.0,
                            "min_opportunity_score": 0.0,
                        }
                    )
                }
            )
        },
    )
    svc = _rec_svc(w, loaded=on)
    svc.update_profile(
        CreatorProfile(notification_preferences={"in_app": True, "email": True, "telegram": False})
    )
    svc.generate(all_categories=True, force=True)
    n = count(w.factory, NotificationRow)
    if _opportunity_status(w) == "candidate":
        assert n >= 1
        with transaction(w.factory) as s:
            d = (
                s.execute(
                    sa.select(NotificationDeliveryRow).where(
                        NotificationDeliveryRow.channel == "email"
                    )
                )
                .scalars()
                .first()
            )
            assert d is not None
            assert d.status == "skipped_unconfigured"
            titles = list(s.execute(sa.select(NotificationRow.title)).scalars())
            assert all(t.startswith("[SYNTHETIC]") for t in titles)
    # deduplicated: the same events are never alerted twice
    svc.generate(all_categories=True, force=True)
    assert count(w.factory, NotificationRow) == n


def _backend(w: World) -> DatabaseBackend:
    b = w.container.dashboard_backend(clock=w.clock, include_synthetic=True)
    assert isinstance(b, DatabaseBackend)
    return b


def test_user_state_feedback_and_production_tracking(analysed: World) -> None:
    w = analysed
    _rec_svc(w).generate(all_categories=True)
    b = _backend(w)
    page = b.recommendations(include_research=True, page_size=50)
    assert page.total >= 1
    rid = page.items[0].id
    with pytest.raises(NotFoundError):
        b.save(str(uuid.uuid4()), None)
    st = b.save(rid, "film on Friday")
    assert st.saved
    assert st.notes == "film on Friday"
    assert st.production is not None
    assert st.production.status is ProductionStatus.IDEA
    st = b.feedback(rid, FeedbackType.USEFUL, None, "clear gap")
    assert st.feedback["USEFUL"] == 1
    assert st.feedback["SAVED"] == 1
    with pytest.raises(TransitionError):  # IDEA -> PUBLISHED is not a valid move
        b.production(
            rid, ProductionIn(status=ProductionStatus.PUBLISHED, published_video_id="abcdefghijk")
        )
    for step in (
        ProductionStatus.SCRIPTING,
        ProductionStatus.RECORDING,
        ProductionStatus.EDITING,
        ProductionStatus.READY_TO_PUBLISH,
    ):
        b.production(rid, ProductionIn(status=step))
    with pytest.raises(TransitionError):  # PUBLISHED needs the video ID
        b.production(rid, ProductionIn(status=ProductionStatus.PUBLISHED))
    st = b.production(
        rid, ProductionIn(status=ProductionStatus.PUBLISHED, published_video_id="abcdefghijk")
    )
    assert st.production is not None
    assert st.production.published_video_id == "abcdefghijk"
    assert [h["status"] for h in st.production.history][-1] == "PUBLISHED"
    assert st.feedback["PUBLISHED"] == 1
    assert st.feedback["PRODUCED"] == 1
    saved = b.saved()
    assert [s.recommendation.id for s in saved] == [rid]
    assert b.saved(status="PUBLISHED")[0].production is not None
    # dismissing hides it from the default list (state is kept)
    b.dismiss(rid)
    assert rid not in {r.id for r in b.recommendations(include_research=True).items}
    assert rid in {
        r.id for r in b.recommendations(include_research=True, include_dismissed=True).items
    }
    with transaction(w.factory) as s:
        assert s.execute(sa.select(SavedRecommendationRow.state)).scalar_one() == "DISMISSED"
        assert count(w.factory, ProductionTrackingRow) == 1
        types = list(s.execute(sa.select(RecommendationFeedbackRow.feedback_type)).scalars())
        assert "DISMISSED" in types
    detail = b.recommendation(rid, viewed=True)
    assert detail is not None
    assert detail.user.feedback["VIEWED"] == 1
    assert detail.opportunity is not None


def test_dashboard_reads(analysed: World) -> None:
    w = analysed
    _rec_svc(w).generate(all_categories=True)
    b = _backend(w)
    ov = b.overview()
    assert ov.mode == "database"
    assert ov.monitored_queries
    assert ov.monitored_queries >= 1
    opps = b.opportunities(page_size=10)
    assert opps.total == 1
    o = opps.items[0]
    assert o.synthetic
    assert o.recommendation_count >= 1
    assert b.opportunities(category="gaming").total == 0
    assert b.opportunities(q="nimbus").total == 1
    assert b.opportunities(sort="breakout").total == 1
    d = b.opportunity(o.id)
    assert d is not None
    assert d.recommendations
    trends = b.trends(page_size=50)
    assert trends.total >= 1
    t = next(x for x in trends.items if "nimbus" in x.query)
    assert t.data_class in (DataClass.SYNTHETIC, DataClass.MISSING)
    td = b.trend(t.id)
    assert td is not None
    assert td.series
    assert all(sv.data_class is DataClass.SYNTHETIC for sv in td.series)
    assert any("never combined" in x for x in td.limitations)
    assert b.trend("not-a-number") is None
    assert b.trend("999999") is None
    st = b.system_status()
    assert st.database.startswith("ok (schema 0012")
    assert any(p.name == "email" and not p.configured for p in st.notifications)
    assert {p.name for p in b.providers()} >= {"youtube_data_api", "google_trends"}
    assert b.data_quality().items
    # production mode never shows synthetic records
    prod = w.container.dashboard_backend(clock=w.clock)
    assert prod.opportunities().total == 0
    assert prod.recommendations(include_research=True).total == 0
    assert prod.recommendation(d.recommendations[0].id) is None


def test_generate_via_backend_is_capped(analysed: World) -> None:
    b = _backend(analysed)
    res = b.generate(GenerateIn(all=True, max_opportunities=100))
    assert (
        res.considered
        <= analysed.container.recommendation_config().config.api.max_generate_opportunities
    )


def test_retention_keeps_saved_ideas_and_purges_the_rest(analysed: World) -> None:
    w = analysed
    _rec_svc(w).generate(all_categories=True, force=True)
    b = _backend(w)
    items = b.recommendations(include_research=True, include_suppressed=True, page_size=50).items
    with transaction(w.factory) as s:
        s.execute(
            sa.update(VideoRecommendationRow).values(expires_at=w.clock() - timedelta(hours=1))
        )
    keep = items[0].id
    b.save(keep, None)
    report = RetentionService(w.factory, w.clock).report()
    assert report.expired["video_recommendations"] == len(items) - 1
    RetentionService(w.factory, w.clock).purge_expired()
    with transaction(w.factory) as s:
        left = list(s.execute(sa.select(VideoRecommendationRow)).scalars())
    assert [str(r.id) for r in left] == [keep]
    assert not left[0].is_current  # kept for the creator, no longer a current idea


def test_cli_generate_list_explain_verify_stats(
    analysed: World, monkeypatch: pytest.MonkeyPatch, fake_youtube: FakeYouTube
) -> None:
    w = analysed
    monkeypatch.setattr(cli, "SETTINGS_FACTORY", lambda: w.container.settings)
    monkeypatch.setattr(cli, "CONTAINER_FACTORY", lambda settings: w.container)
    r = runner.invoke(cli.app, ["recommend", "generate"])
    assert r.exit_code == 2  # a category or --all is required
    r = runner.invoke(cli.app, ["recommend", "generate", "--all", "--include-synthetic", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["synthetic"] is True
    r = runner.invoke(
        cli.app,
        ["recommend", "list", "--include-synthetic", "--include-research", "--json"],
    )
    assert r.exit_code == 0, r.output
    recs = json.loads(r.stdout)
    assert recs
    rid = recs[0]["recommendation_id"]
    r = runner.invoke(cli.app, ["recommend", "explain", "--id", rid])
    assert r.exit_code == 0, r.output
    assert "Score components" in r.stdout
    r = runner.invoke(cli.app, ["recommend", "verify", "--id", rid, "--json"])
    assert r.exit_code in (0, 1), r.output
    assert json.loads(r.stdout)["recommendation_id"] == rid
    r = runner.invoke(cli.app, ["recommend", "stats", "--include-synthetic", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["runs"]
    r = runner.invoke(cli.app, ["recommend", "explain", "--id", str(uuid.uuid4())])
    assert r.exit_code == 4


def test_stored_phase6_forecasts_feed_the_recommendations(analysed: World) -> None:
    """Phase 4 surge -> Phase 5 stored opportunity -> Phase 6 stored forecasts -> Phase 7."""
    w = analysed
    with transaction(w.factory) as s:
        query = s.execute(sa.select(ContentOpportunityRow.canonical_query)).scalar_one()
    stored = w.container.forecasting(clock=w.clock).predict(query=query, include_synthetic=True)
    assert stored, "the forecasting service produced no forecast for the opportunity's query"
    res = _rec_svc(w).generate(all_categories=True)
    assert res.generated >= 1
    with transaction(w.factory) as s:
        docs = [
            r.concept
            for r in s.execute(
                sa.select(VideoRecommendationRow).where(VideoRecommendationRow.is_current)
            ).scalars()
        ]
    assert docs
    # every idea carries the stored, usable forecast (the 6 h one is unsupported for daily
    # data and ignored): read from Phase 6, never recomputed
    usable = {o.prediction_id for o, _ in stored if o.status.value == "READY"}
    for d in docs:
        assert d["forecast"]["available"]
        assert d["forecast"]["prediction_id"] in usable
        assert d["forecast"]["synthetic"] is True
        fc = next(c for c in d["ranking"]["components"] if c["name"] == "forecast_opportunity")
        assert fc["raw"]["status"] == "READY"


def test_creator_outcomes_are_opt_in_and_descriptive(analysed: World) -> None:
    """Phase 8: outcomes are creator-entered, consent-gated, upserted per day, and only
    summarised descriptively."""
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from trend_agent.api.app import create_app
    from trend_agent.evaluation.recommendations import outcome_summary

    res = _rec_svc(analysed).generate(all_categories=True)
    assert res.generated > 0
    with transaction(analysed.factory) as s:
        rid = str(s.execute(sa.select(VideoRecommendationRow.id).limit(1)).scalar_one())
    c = analysed.container
    token = "t" * 40
    c.settings = c.settings.model_copy(update={"api_token": SecretStr(token)})
    backend = c.dashboard_backend(clock=analysed.clock, include_synthetic=True)
    client = TestClient(create_app(backend, c.settings, ops=c))
    auth = {"Authorization": f"Bearer {token}"}
    body: dict[str, Any] = {
        "days_since_publication": 7,
        "views": 1200,
        "search_traffic_share": 0.4,
        "consent": True,
    }
    url = f"/api/recommendations/{rid}/outcomes"
    assert client.post(url, json=body).status_code == 401
    assert client.post(url, json=body, headers=auth).status_code == 200
    assert client.post(url, json=body | {"views": 1500}, headers=auth).status_code == 200
    assert client.post(url, json=body | {"consent": False}, headers=auth).status_code == 422
    missing = f"/api/recommendations/{uuid.uuid4()}/outcomes"
    assert client.post(missing, json=body, headers=auth).status_code == 404
    listed = client.get("/api/outcomes", headers=auth).json()
    assert len(listed) == 1  # the same day was updated, not duplicated
    assert listed[0]["views"] == 1500
    summary = outcome_summary(analysed.factory)
    assert summary["status"] == "DESCRIPTIVE"
    assert summary["median_views"] == 1500
    assert "no causal claim" in summary["caveat"]
