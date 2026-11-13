"""Evidence rules, multiple testing, severity, alert policy, episodes, contract, replay,
calibration and evaluation (all in memory)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError
from scipy import stats

from tests.unit.detection.helpers import CFG, LOADED, meta, noisy, point, run_engine
from trend_agent.detection.alert_policy import DecisionContext, decide
from trend_agent.detection.calibration import calibrate, cusum_arl_table, fit_tail
from trend_agent.detection.config import ScaleFamily
from trend_agent.detection.contract import make_renderer
from trend_agent.detection.deduplication import episode_key
from trend_agent.detection.evaluation import (
    AlertRecord,
    build_world,
    evaluate,
    evaluate_world,
    replay_world,
)
from trend_agent.detection.events import EpisodeManager, MemoryEpisodeStore
from trend_agent.detection.explanations import explain
from trend_agent.detection.replay_adapter import DetectionReplayer
from trend_agent.detection.significance import (
    AlertBudget,
    Hypothesis,
    NullDistribution,
    NullLookup,
    control,
    stratum_key,
)
from trend_agent.detection.synthetic import (
    InjectedEvent,
    SyntheticSeriesSpec,
    benchmark_specs,
    generate,
    label_for,
    null_specs,
)
from trend_agent.monitoring.replay import replay
from trend_agent.schemas.detection import (
    DetectorType,
    MultipleTestingMethod,
    Reason,
    SupportStatus,
)
from trend_agent.schemas.evidence import (
    AlertStage,
    EvidenceRuleError,
    SurgeClassification,
    cap_stage,
    classify,
    combine,
    effective_category,
    validate_stage,
)
from trend_agent.schemas.monitoring import ComparabilityStatus, MeasurementCategory
from trend_agent.schemas.surge import SurgeAlert

M = MeasurementCategory
NOW = datetime(2026, 6, 1, tzinfo=UTC)


# ----------------------------------------------------------------------------- evidence
def test_evidence_classification_rules() -> None:
    assert classify(M.DIRECT_YOUTUBE_SEARCH_INTEREST) is SurgeClassification.DIRECT_SEARCH_SURGE
    assert classify(M.EXTERNAL_SEARCH_INTEREST) is SurgeClassification.EXTERNAL_SEARCH_SURGE
    for proxy in (
        M.VIDEO_ACTIVITY,
        M.SEARCH_SUGGESTION_PRESENCE,
        M.OFFICIAL_EVENT_SIGNAL,
        M.COMMUNITY_ACTIVITY,
    ):
        assert classify(proxy) is SurgeClassification.PROXY_ATTENTION_SIGNAL
    # a proxy-flagged series is never search interest
    assert classify(M.DIRECT_YOUTUBE_SEARCH_INTEREST, is_proxy=True) is (
        SurgeClassification.PROXY_ATTENTION_SIGNAL
    )
    with pytest.raises(EvidenceRuleError):
        classify(M.SYNTHETIC_TEST_OBSERVATION)
    assert effective_category(M.SYNTHETIC_TEST_OBSERVATION, M.VIDEO_ACTIVITY) is M.VIDEO_ACTIVITY
    with pytest.raises(EvidenceRuleError):
        effective_category(M.VIDEO_ACTIVITY, M.DIRECT_YOUTUBE_SEARCH_INTEREST)


def test_only_direct_evidence_can_be_confirmed_and_mixed_signals_are_opportunities() -> None:
    ext = SurgeClassification.EXTERNAL_SEARCH_SURGE
    assert cap_stage(AlertStage.CONFIRMED, ext) is AlertStage.CANDIDATE
    with pytest.raises(EvidenceRuleError):
        validate_stage(AlertStage.CONFIRMED, SurgeClassification.PROXY_ATTENTION_SIGNAL)
    validate_stage(AlertStage.CONFIRMED, SurgeClassification.DIRECT_SEARCH_SURGE)
    assert combine([M.EXTERNAL_SEARCH_INTEREST, M.VIDEO_ACTIVITY]) is (
        SurgeClassification.POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY
    )
    assert combine([M.VIDEO_ACTIVITY, M.DIRECT_YOUTUBE_SEARCH_INTEREST]) is (
        SurgeClassification.DIRECT_SEARCH_SURGE
    )
    assert combine([M.VIDEO_ACTIVITY]) is SurgeClassification.PROXY_ATTENTION_SIGNAL
    assert combine([]) is None


# ----------------------------------------------------------------------------- multiple testing
def test_benjamini_hochberg_and_yekutieli_match_scipy() -> None:
    rng = np.random.default_rng(0)
    ps = np.concatenate([rng.uniform(size=95), [1e-6, 2e-5, 1e-4, 3e-4, 0.002]])
    hyps = [Hypothesis(f"h{i}", float(p), True) for i, p in enumerate(ps)]
    bh = control(hyps, MultipleTestingMethod.BH, 0.05)
    by = control(hyps, MultipleTestingMethod.BY, 0.05)
    expected = {f"h{i}" for i, a in enumerate(stats.false_discovery_control(ps)) if a <= 0.05}
    assert bh.accepted == expected
    assert len(bh.accepted) >= 4
    assert by.accepted <= bh.accepted
    assert control([], MultipleTestingMethod.BH, 0.05).family_size == 0


def test_null_distribution_p_values() -> None:
    rng = np.random.default_rng(1)
    scores = np.abs(rng.standard_t(5, size=20000))
    params = fit_tail(scores.tolist(), 0.95)
    null = NullDistribution.from_parameters("s", "v1", "synthetic", params)
    p_mid, basis_mid = null.p_value_with_basis(float(np.quantile(scores, 0.99)))
    assert basis_mid == "empirical"
    assert p_mid == pytest.approx(0.01, rel=0.1)
    assert null.p_value(0.1) == (1.0, False)  # below the stored tail: conservative
    p_far, basis_far = null.p_value_with_basis(float(scores.max()) * 2)
    floor = 1 / (null.n_total + 1)
    assert basis_far in ("gpd_extrapolated", "conservative_bound")
    assert floor * 0.01 <= p_far <= floor
    assert null.applies_to(True)
    assert not null.applies_to(False)
    lookup = NullLookup({"s": null})
    assert lookup.get("s", synthetic=False) is None


def test_alert_budget() -> None:
    b = AlertBudget(limit_per_day=2, used_last_24h=1)
    assert b.allow()
    b.consume()
    assert not b.allow()


# ----------------------------------------------------------------------------- policy
def _ctx(
    measurement: M = M.DIRECT_YOUTUBE_SEARCH_INTEREST,
    family: Any = None,
    proxy: bool = False,
) -> DecisionContext:
    return DecisionContext(
        classification=classify(measurement, is_proxy=proxy),
        measurement=measurement,
        family=ScaleFamily.INDEX,
        multiple_testing=family,
        comparability=ComparabilityStatus.COMPARABLE,
    )


def test_policy_stages_without_calibration_never_confirm() -> None:
    out = run_engine([*noisy(40), point(40, 80.0), point(41, 82.0)])
    first, second = out.evaluations[-2:]
    d1, d2 = decide(first, _ctx(), CFG), decide(second, _ctx(), CFG)
    assert d1.stage is AlertStage.CANDIDATE
    assert Reason.NOT_PERSISTENT.value in d1.reasons
    assert d2.stage is AlertStage.CANDIDATE
    assert second.persistence == 2
    assert d2.confidence is SupportStatus.EMPIRICALLY_THRESHOLDED
    assert Reason.UNCALIBRATED.value in d2.reasons
    quiet = decide(out.evaluations[30], _ctx(), CFG)
    assert quiet.stage is None
    assert quiet.confidence is SupportStatus.NOT_SUPPORTED


def test_policy_confirms_direct_surge_with_calibration_and_persistence() -> None:
    pts = [*noisy(40), point(40, 80.0), point(41, 82.0)]
    from trend_agent.detection.preprocessing import prepare

    nulls = NullLookup(
        {
            stratum_key(
                "p",
                "day",
                M.DIRECT_YOUTUBE_SEARCH_INTEREST.value,
                "index",
                DetectorType.ROBUST_ANOMALY.value,
            ): NullDistribution(
                "x", "cal-1", "stored_history", 10000, 2.0, np.linspace(2.0, 6.0, 500)
            )
        }
    )
    out = run_engine(pts, nulls=nulls)
    ev = out.evaluations[-1]
    assert ev.hypotheses
    assert ev.hypotheses[0].calibration_version == "cal-1"
    family = control(
        [Hypothesis(h.key, h.p_value, h.exact) for h in ev.hypotheses],
        MultipleTestingMethod.BH,
        0.05,
    )
    d = decide(ev, _ctx(family=family), CFG)
    assert d.stage is AlertStage.CONFIRMED
    assert d.confidence is SupportStatus.STATISTICALLY_SUPPORTED
    # the same evidence on an external or proxy series is capped at CANDIDATE
    ext = decide(ev, _ctx(M.EXTERNAL_SEARCH_INTEREST, family), CFG)
    assert ext.stage is AlertStage.CANDIDATE
    assert Reason.EXTERNAL_EVIDENCE.value in ext.reasons
    vid = decide(ev, _ctx(M.VIDEO_ACTIVITY, family, proxy=True), CFG)
    assert vid.stage is AlertStage.CANDIDATE
    assert Reason.PROXY_EVIDENCE.value in vid.reasons
    sug = decide(ev, _ctx(M.SEARCH_SUGGESTION_PRESENCE, family, proxy=True), CFG)
    assert Reason.SUGGESTION_PRESENCE.value in sug.reasons
    _ = prepare


def test_policy_insufficient_rejected_cold_start_and_late() -> None:
    cold = run_engine([point(i, 4.0 + 5 * i) for i in range(5)]).evaluations[-1]
    d = decide(cold, _ctx(), CFG)
    assert d.confidence is SupportStatus.LOW_CONFIDENCE_COLD_START
    assert d.stage in (AlertStage.WATCH, AlertStage.INSUFFICIENT_EVIDENCE)
    rej = run_engine([point(i, 2.0 + (i % 2)) for i in range(40)] + [point(40, 8.0)]).evaluations[
        -1
    ]
    assert decide(rej, _ctx(), CFG).stage is AlertStage.REJECTED
    missing = run_engine([*noisy(30), point(30, None)]).evaluations[-1]
    assert decide(missing, _ctx(), CFG).stage is AlertStage.INSUFFICIENT_EVIDENCE
    late = dataclasses.replace(
        run_engine([*noisy(40), point(40, 90.0)]).evaluations[-1], is_late=True
    )
    assert decide(late, _ctx(), CFG).stage is None


def test_severity_is_bounded_and_correlated_detectors_are_not_summed() -> None:
    ev = run_engine([*noisy(40), point(40, 90.0)]).evaluations[-1]
    d = decide(ev, _ctx(), CFG)
    assert 0 < d.severity.score <= 100
    comps = {c.name: c for c in d.severity.components}
    assert comps["deviation"].weight == 0
    assert comps["persistence"].weight == 0
    assert comps["statistical_evidence"].weight == pytest.approx(CFG.severity.weights.statistical)
    total = sum(c.contribution for c in d.severity.components) * 100 * d.severity.multiplier
    assert total == pytest.approx(d.severity.score, abs=0.01)


# ----------------------------------------------------------------------------- episodes
def _manager(store: MemoryEpisodeStore, budget: int = 25) -> EpisodeManager:
    return EpisodeManager(
        store, CFG, config_version="test", budget=AlertBudget(limit_per_day=budget)
    )


def _apply_all(
    pts: list[Any],
    *,
    measurement: M = M.DIRECT_YOUTUBE_SEARCH_INTEREST,
    store: MemoryEpisodeStore | None = None,
    budget: int = 25,
) -> tuple[MemoryEpisodeStore, list[Any]]:
    store = store or MemoryEpisodeStore()
    manager = _manager(store, budget)
    m = meta(measurement)
    changes = []
    # Evaluating a whole series at once marks old periods as late; here values arrive in
    # period order (as in live operation), so none of them is late.
    for ev in (dataclasses.replace(e, is_late=False) for e in run_engine(pts).evaluations):
        d = decide(ev, _ctx(measurement), CFG)
        ch = manager.apply(ev, d, m, ev.period_end + timedelta(hours=36), family=ScaleFamily.INDEX)
        if ch is not None:
            changes.append(ch)
    return store, changes


def test_episode_created_once_updated_and_resolved() -> None:
    pts = noisy(40) + [point(40 + i, 80.0 + i) for i in range(4)] + noisy(10, seed=4)
    pts = pts[:44] + [point(44 + i, p.value) for i, p in enumerate(noisy(10, seed=4))]
    store, changes = _apply_all(pts)
    created = [c for c in changes if c.kind == "created"]
    assert len(created) == 1
    assert len(store.episodes) == 1
    ep = next(iter(store.episodes.values()))
    assert ep.status is AlertStage.RESOLVED
    assert ep.peak_value == 83.0
    assert ep.maximum_severity >= ep.severity
    kinds = [c.kind for c in changes]
    assert kinds.count("created") == 1
    assert "resolved" in kinds
    assert sum(1 for _, t in store.transitions if t.notification) == 1


def test_repeated_processing_and_lost_state_do_not_duplicate() -> None:
    pts = noisy(40) + [point(40 + i, 80.0) for i in range(3)]
    store, _ = _apply_all(pts)
    before = (len(store.episodes), len(store.transitions))
    manager = _manager(store)
    for ev in run_engine(pts).evaluations:  # the same observations again (now as history)
        manager.apply(ev, decide(ev, _ctx(), CFG), meta(), NOW, family=ScaleFamily.INDEX)
    assert (len(store.episodes), len(store.transitions)) == before
    # even if they were presented as new (not late), already-applied revisions are ignored
    for ev in run_engine(pts).evaluations:
        fresh = dataclasses.replace(ev, is_late=False)
        manager.apply(fresh, decide(fresh, _ctx(), CFG), meta(), NOW, family=ScaleFamily.INDEX)
    assert (len(store.episodes), len(store.transitions)) == before


def test_reopen_within_window_and_new_episode_after() -> None:
    surge = [point(40 + i, 80.0) for i in range(3)]
    calm = [point(43 + i, 40.0 + (i % 3)) for i in range(5)]
    again = [point(48 + i, 85.0) for i in range(2)]
    store, changes = _apply_all(noisy(40) + surge + calm + again)
    assert [c.kind for c in changes if c.kind in ("created", "resolved", "reopened")] == [
        "created",
        "resolved",
        "reopened",
    ]
    assert len(store.episodes) == 1
    assert next(iter(store.episodes.values())).reopen_count == 1


def test_budget_holds_back_notifications() -> None:
    store, changes = _apply_all([*noisy(40), point(40, 80.0)], budget=0)
    ep = next(iter(store.episodes.values()))
    assert ep.status is AlertStage.WATCH
    assert not any(c.notification for c in changes)
    assert Reason.BUDGET_EXHAUSTED.value in changes[0].reason


def test_episode_keys_dedupe_per_target_but_not_across_targets() -> None:
    a = episode_key(1, "p", M.DIRECT_YOUTUBE_SEARCH_INTEREST, "US", "en", False)
    assert a == meta().key
    assert a != episode_key(2, "p", M.DIRECT_YOUTUBE_SEARCH_INTEREST, "US", "en", False)
    assert a != episode_key(1, "p", M.EXTERNAL_SEARCH_INTEREST, "US", "en", False)
    assert a != episode_key(1, "p", M.DIRECT_YOUTUBE_SEARCH_INTEREST, "US", "en", True)


# ----------------------------------------------------------------------------- contract
def test_contract_payload_validates_and_explains_without_counting_searches() -> None:
    store = MemoryEpisodeStore()
    out = run_engine([*noisy(40), point(40, 80.0)])
    ev = out.evaluations[-1]
    d = decide(ev, _ctx(), CFG)
    manager = EpisodeManager(
        store,
        CFG,
        config_version=LOADED.version_tag,
        budget=AlertBudget(limit_per_day=10),
        renderer=make_renderer(
            loaded=LOADED,
            family=ScaleFamily.INDEX,
            comparability=None,
            multiple_testing=None,
            freshness="source_current",
            now=NOW,
        ),
    )
    ch = manager.apply(ev, d, meta(), NOW, family=ScaleFamily.INDEX)
    assert ch is not None
    alert = SurgeAlert.model_validate(ch.episode.payload)
    assert alert.alert_status is AlertStage.CANDIDATE
    assert alert.synthetic is False
    assert alert.baseline.expected == pytest.approx(ev.baseline.expected, abs=1e-3)
    assert "relative index, not an absolute number of searches" in alert.explanation
    assert alert.multiple_testing is not None
    assert alert.multiple_testing.method is (MultipleTestingMethod.ALERT_BUDGET)
    assert any("no calibrated error rate" in x for x in alert.limitations)
    schema = SurgeAlert.model_json_schema()
    assert schema["properties"]["schema_version"]["const"] == "surge-alert/1.0"


def test_contract_rejects_invalid_claims() -> None:
    store = MemoryEpisodeStore()
    ev = run_engine([*noisy(40), point(40, 80.0)]).evaluations[-1]
    d = decide(ev, _ctx(), CFG)
    manager = EpisodeManager(
        store,
        CFG,
        config_version="t",
        budget=AlertBudget(limit_per_day=10),
        renderer=make_renderer(
            loaded=LOADED,
            family=ScaleFamily.INDEX,
            comparability=None,
            multiple_testing=None,
            freshness=None,
            now=NOW,
        ),
    )
    payload = manager.apply(ev, d, meta(), NOW, family=ScaleFamily.INDEX).episode.payload  # type: ignore[union-attr]
    bad = {
        **payload,
        "alert_status": "confirmed",
        "measurement_type": "external_search_interest",
        "classification": "external_search_surge",
    }
    with pytest.raises(ValidationError, match="cannot reach confirmed"):
        SurgeAlert.model_validate(bad)
    with pytest.raises(ValidationError, match="classification"):
        SurgeAlert.model_validate({**payload, "measurement_type": "video_activity"})
    with pytest.raises(ValidationError, match="STATISTICALLY_SUPPORTED"):
        SurgeAlert.model_validate({**payload, "confidence_status": "statistically_supported"})
    with pytest.raises(ValidationError, match="synthetic"):
        SurgeAlert.model_validate({**payload, "synthetic": True})


def test_explanations_for_indirect_and_cold_start_evidence() -> None:
    store = MemoryEpisodeStore()
    m = meta(M.VIDEO_ACTIVITY, proxy=True)
    ev = run_engine([*noisy(40), point(40, 80.0)]).evaluations[-1]
    d = decide(ev, _ctx(M.VIDEO_ACTIVITY, proxy=True), CFG)
    ch = _manager(store).apply(ev, d, m, NOW, family=ScaleFamily.INDEX)
    assert ch is not None
    text = explain(ch.episode, ev, d, m, family=ScaleFamily.INDEX, cfg=CFG)
    assert "not measured search demand" in text
    assert "not a search-demand surge" in text
    cold = run_engine([point(i, 4.0 + 5 * i) for i in range(5)]).evaluations[-1]
    cd = decide(cold, _ctx(), CFG)
    ep = dataclasses.replace(ch.episode, classification=SurgeClassification.DIRECT_SEARCH_SURGE)
    text2 = explain(ep, cold, cd, meta(), family=ScaleFamily.INDEX, cfg=CFG)
    assert "insufficient historical observations" in text2
    assert "supports monitoring but not confirmation" in text2


# ----------------------------------------------------------------------------- replay
def _replayer(series_ids: set[int]) -> DetectionReplayer:
    return DetectionReplayer(LOADED, {sid: meta(synthetic=False) for sid in series_ids})


def test_detection_replay_has_no_look_ahead() -> None:
    pts = [*noisy(40), point(40, 90.0)]
    spike = pts[-1]
    published = spike.provider_published_at
    assert published is not None
    rp = _replayer({1})
    replay(
        pts,
        start=published - timedelta(hours=12),
        end=published + timedelta(hours=12),
        step=timedelta(hours=12),
        consumers=[rp.consumer()],
    )
    seen = {(d.as_of, d.evaluation.period_start) for d in rp.run.decisions}
    assert (published - timedelta(hours=12), spike.period_start) not in seen
    assert any(
        d.evaluation.period_start == spike.period_start and d.as_of >= published
        for d in rp.run.decisions
    )
    # the spike's observation was invisible before its publication: Tuesday cannot see Monday
    early = [d for d in rp.run.decisions if d.as_of < published]
    assert all(d.evaluation.period_start < spike.period_start for d in early)


def test_revision_does_not_rewrite_earlier_decisions_and_replay_is_deterministic() -> None:
    pts = [*noisy(40), point(40, 90.0)]
    original = pts[-1]
    assert original.provider_published_at is not None
    later = original.provider_published_at + timedelta(days=2)
    revised = point(
        40,
        41.0,
        revision=2,
        revision_kind="revision",
        provider_published_at=later,
        collected_at=later,
        ingested_at=later,
        is_current=True,
    )
    stale = original.model_copy(update={"is_current": False})
    history = [*pts[:-1], stale, revised]
    digests = []
    for _ in range(2):
        rp = _replayer({1})
        replay(
            history,
            start=original.provider_published_at - timedelta(days=1),
            end=later + timedelta(days=1),
            step=timedelta(hours=12),
            consumers=[rp.consumer()],
        )
        digests.append(rp.run.digest())
        at_spike = [
            d for d in rp.run.decisions if d.evaluation.period_start == original.period_start
        ]
        before = [d for d in at_spike if d.as_of < later]
        after = [d for d in at_spike if d.as_of >= later]
        assert before
        assert before[0].evaluation.value == 90.0
        assert before[0].decision.stage is AlertStage.CANDIDATE
        assert after
        assert after[0].evaluation.value == 41.0
        assert after[0].change is not None
        assert after[0].change.kind == "rejected"
        assert rp.run.recomputations.get("revision") == 1
    assert digests[0] == digests[1]


def test_fast_batch_evaluation_equals_stepwise_replay() -> None:
    world = build_world(
        [
            SyntheticSeriesSpec(
                key="s1", seed=1, days=60, base=30, events=(InjectedEvent("step", 45, 30.0),)
            ),
            SyntheticSeriesSpec(key="s2", seed=2, days=60, base=20, weekly=0.4),
        ]
    )
    run_fast, _ = replay_world(world, LOADED, None)
    rp = DetectionReplayer(LOADED, world.metas, categories=world.categories)
    allpts = [p for pts in world.points.values() for p in pts]
    origin = min(p.period_start for p in allpts)
    replay(
        allpts,
        start=origin,
        end=origin + timedelta(days=64),
        step=timedelta(days=1),
        consumers=[rp.consumer()],
    )

    def key(r: Any) -> list[Any]:
        return [
            (
                d.as_of,
                d.series_id,
                d.evaluation.period_start,
                d.decision.stage,
                d.change.kind if d.change else None,
            )
            for d in r.decisions
        ]

    assert key(run_fast) == key(rp.run)


# ----------------------------------------------------------------------------- ground truth
def test_ground_truth_labels_are_defined_by_the_generator() -> None:
    spec = SyntheticSeriesSpec(key="x", seed=1, base=40.0, days=100)
    big = label_for(spec, InjectedEvent("spike", 50, 25.0, 1))
    small = label_for(spec, InjectedEvent("spike", 50, 12.0, 1))
    drop = label_for(spec, InjectedEvent("drop", 50, -20.0))
    assert (big.label, small.label, drop.label) == ("surge", "ambiguous", "not_surge")
    ramp = label_for(spec, InjectedEvent("ramp", 50, 40.0, 10))
    assert ramp.observable_index is not None
    assert ramp.observable_index > ramp.onset_index
    g = generate(SyntheticSeriesSpec(key="m", seed=3, days=200, missing_rate=0.3))
    assert sum(v is None for v in g.values) > 30
    assert all(v != 0 or v is None for v in g.values)
    assert generate(spec).values == generate(spec).values  # deterministic


def test_evaluation_metrics_on_known_alerts() -> None:
    specs = [
        SyntheticSeriesSpec(
            key="a", seed=1, days=100, base=30, events=(InjectedEvent("step", 80, 30.0),)
        ),
        SyntheticSeriesSpec(key="b", seed=2, days=100, base=30),
    ]
    world = build_world(specs)
    from trend_agent.detection.replay_adapter import ReplayRun

    t = specs[0].start
    alerts = [
        AlertRecord("a", 1, t + timedelta(days=82), 80, AlertStage.CANDIDATE, 70.0),
        AlertRecord("a", 2, t + timedelta(days=84), 81, AlertStage.CANDIDATE, 60.0),  # duplicate
        AlertRecord("b", 3, t + timedelta(days=90), 88, AlertStage.CANDIDATE, 40.0),  # false
    ]
    m = evaluate(
        alerts,
        world.generated,
        ReplayRun(),
        dataset="t",
        split="test",
        eval_start=60,
        keys=world.keys,
    )
    assert (m.tp, m.fp, m.fn, m.duplicates) == (1, 1, 0, 1)
    assert m.precision == 0.5
    assert m.recall == 1.0
    assert m.false_alerts_per_day == pytest.approx(1 / 40, abs=1e-4)
    assert m.delay_median_days == pytest.approx((82 - 82.5), abs=1e-6)


def test_false_alert_rate_on_null_series_is_measured() -> None:
    world = build_world(null_specs(40, days=100))
    m = evaluate_world(world, LOADED, None, dataset="null", split="null", eval_start=40)
    assert m.labels == 0
    assert m.tp == 0
    assert m.precision in (None, 0.0)
    assert m.false_alerts_per_day == pytest.approx(m.fp / 60, abs=1e-4)
    assert m.coverage is not None
    assert m.coverage > 0.9


def test_calibration_produces_tails_and_arl_tables_without_test_leakage() -> None:
    train = [s for s in benchmark_specs("train", 60, days=120) if not s.events]
    test = benchmark_specs("test", 60, days=120)
    assert not {s.seed for s in train} & {s.seed for s in test}
    assert not {s.cluster for s in train} & {s.cluster for s in test}
    world = build_world(train)
    cal = calibrate(world.points, LOADED, version="t", source="synthetic", dataset="d", now=NOW)
    key = stratum_key(
        "synthetic_trends",
        "day",
        M.DIRECT_YOUTUBE_SEARCH_INTEREST.value,
        "index",
        DetectorType.ROBUST_ANOMALY.value,
    )
    assert key in cal.strata
    assert cal.strata[key]["n_total"] >= CFG.calibration.min_null_scores
    rows = [r for r in cal.cusum_table if r["k"] == 1.0]
    arls = [r["arl0_empirical"] or r["arl0_lower_bound"] for r in rows]
    assert arls == sorted(arls)  # larger h -> longer in-control run length
    table = cusum_arl_table([[0.0] * 100], k_grid=(1.0,), z_clip=3.0, max_gap=3, h_grid=(6.0,))
    assert table[0]["alarms"] == 0
    assert table[0]["arl0_lower_bound"] == 100
