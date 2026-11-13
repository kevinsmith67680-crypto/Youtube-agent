"""Service-level behaviour that needs no database."""

from __future__ import annotations

import pytest

from tests.helpers import make_settings
from tests.unit.content_analysis.helpers import LOADED
from trend_agent.content_analysis.llm_client import AnthropicAnalysisLLM, NullAnalysisLLM
from trend_agent.content_analysis.service import _AnalysisStore, make_llm, youtube_available


def test_llm_is_off_unless_enabled_and_keyed() -> None:
    assert isinstance(make_llm(make_settings(), LOADED), NullAnalysisLLM)
    keyed = make_settings(anthropic_api_key="sk-test")
    assert isinstance(make_llm(keyed, LOADED), NullAnalysisLLM)  # disabled in config
    on = LOADED.model_copy(
        update={"config": LOADED.config.model_copy(
            update={"llm": LOADED.config.llm.model_copy(update={"enabled": True})})}
    )  # fmt: skip
    assert isinstance(make_llm(keyed, on), AnthropicAnalysisLLM)
    assert isinstance(make_llm(make_settings(), on), NullAnalysisLLM)


def test_youtube_availability_is_reported_not_assumed() -> None:
    ok, why = youtube_available(make_settings())
    assert not ok
    assert "not set" in why
    ok, _ = youtube_available(make_settings(youtube_api_key="k"))
    assert ok
    ok, why = youtube_available(make_settings(youtube_api_key="k", youtube_enabled=False))
    assert not ok
    assert "disabled" in why


def test_analysis_registry_view_refuses_writes() -> None:
    store = _AnalysisStore(factory=None)  # type: ignore[arg-type]
    with pytest.raises(PermissionError):
        store.lock("k")
    with pytest.raises(PermissionError):
        store.insert(None, None)  # type: ignore[arg-type]
    with pytest.raises(PermissionError):
        store.mark_source_removed("k")
