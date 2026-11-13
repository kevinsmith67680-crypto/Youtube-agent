from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.unit.content_analysis.helpers import LOADED
from trend_agent.content_analysis.config import ANALYSIS_VERSION
from trend_agent.content_analysis.scheduling import QueueItem, plan, priority, urgency
from trend_agent.schemas.content_opportunity import DemandBasis

NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)
CFG = LOADED.config
TAG = LOADED.version_tag


def item(key: str, cat: str, basis: DemandBasis, **kw: object) -> QueueItem:
    return QueueItem(key=key, query=kw.pop("query", f"{key} tutorial"), category=cat,
                     basis=basis, **kw)  # type: ignore[arg-type]  # fmt: skip


def _plan(items: list[QueueItem], **kw: object) -> object:
    opts: dict[str, object] = {
        "now": NOW,
        "analysis_version": ANALYSIS_VERSION,
        "configuration_version": TAG,
        "search_requests_used": 0,
        "llm_tokens_used": 0,
        "llm_enabled": False,
    }
    opts.update(kw)
    return plan(items, CFG, **opts)  # type: ignore[arg-type]


def test_confirmed_direct_search_outranks_proxy_attention() -> None:
    hi, _ = priority(item("a", "ai", DemandBasis.CONFIRMED_DIRECT_YOUTUBE_SEARCH_SURGE), CFG, NOW)
    lo, _ = priority(item("b", "ai", DemandBasis.POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY), CFG, NOW)
    assert hi > lo


def test_urgency_for_time_sensitive_categories() -> None:
    assert urgency("where to find the lantern season 8", "gaming", CFG) == (
        "time_sensitive_game_event"
    )
    assert urgency("how to install the new model locally", "ai", CFG) == "new_ai_model"
    assert urgency("best pasta recipe", None, CFG) is None


def test_category_allocation_orders_but_does_not_starve() -> None:
    items = [item(f"ai{i}", "ai", DemandBasis.DIRECT_SEARCH_CANDIDATE) for i in range(10)]
    items += [item(f"g{i}", "gaming", DemandBasis.DIRECT_SEARCH_CANDIDATE) for i in range(10)]
    items += [item(f"t{i}", "technology", DemandBasis.DIRECT_SEARCH_CANDIDATE) for i in range(10)]
    p = _plan(items, limit=10)
    cats = [x.item.category for x in p.selected]  # type: ignore[attr-defined]
    assert len(cats) == 10
    assert cats.count("ai") >= cats.count("gaming") >= cats.count("technology") >= 2
    # Only one category has candidates: it receives every slot.
    p2 = _plan([item(f"g{i}", "gaming", DemandBasis.WATCH) for i in range(5)], limit=5)
    assert len(p2.selected) == 5  # type: ignore[attr-defined]


def test_search_budget_and_unchanged_items_are_skipped() -> None:
    fresh = item("done", "ai", DemandBasis.DIRECT_SEARCH_CANDIDATE,
                 last_analysed_at=NOW - timedelta(hours=1),
                 last_versions=(ANALYSIS_VERSION, TAG))  # fmt: skip
    changed = item("upd", "ai", DemandBasis.DIRECT_SEARCH_CANDIDATE,
                   last_analysed_at=NOW - timedelta(hours=1), updated_at=NOW,
                   last_versions=(ANALYSIS_VERSION, TAG))  # fmt: skip
    new_cfg = item("cfg", "ai", DemandBasis.DIRECT_SEARCH_CANDIDATE,
                   last_analysed_at=NOW - timedelta(hours=1),
                   last_versions=("old", TAG))  # fmt: skip
    weak = item("weak", "ai", DemandBasis.INSUFFICIENT_EVIDENCE)
    p = _plan([fresh, changed, new_cfg, weak])
    selected = {x.item.key for x in p.selected}  # type: ignore[attr-defined]
    skipped = {it.key: why for it, why in p.skipped}  # type: ignore[attr-defined]
    assert selected == {"upd", "cfg"}
    assert "unchanged" in skipped["done"]
    assert "insufficient" in skipped["weak"]
    tight = _plan([changed, new_cfg], search_requests_used=CFG.scheduling.daily_search_requests - 2)
    assert len(tight.selected) == 1  # type: ignore[attr-defined]
    assert any("budget" in why for _, why in tight.skipped)  # type: ignore[attr-defined]


def test_llm_budget_shortfall_means_reduced_capability_not_skipping() -> None:
    items = [item(f"a{i}", "ai", DemandBasis.DIRECT_SEARCH_CANDIDATE) for i in range(3)]
    p = _plan(items, llm_enabled=True, llm_tokens_used=CFG.scheduling.daily_llm_tokens)
    assert len(p.selected) == 3  # type: ignore[attr-defined]
    assert not any(x.use_llm for x in p.selected)  # type: ignore[attr-defined]
