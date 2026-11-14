"""Phase 8 API surface on the isolated demo backend: health probes, correlation IDs,
metrics access and the operating-mode endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.helpers import make_settings
from trend_agent.api.app import create_app
from trend_agent.recommendations.demo import DemoBackend

TOKEN = "test-dashboard-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _client(demo: DemoBackend, token: str | None = TOKEN) -> TestClient:
    return TestClient(create_app(demo, make_settings(api_token=token)))


def test_liveness_and_readiness_are_public(demo: DemoBackend) -> None:
    c = _client(demo)
    live = c.get("/health/live")
    assert live.status_code == 200
    assert live.json()["status"] == "ok"
    ready = c.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["demo"] is True
    assert c.get("/health/database").json()["demo"] is True


def test_detailed_health_and_metrics_need_the_token_when_configured(demo: DemoBackend) -> None:
    c = _client(demo)
    for path in ("/health/workers", "/health/providers", "/metrics", "/api/system/mode"):
        assert c.get(path).status_code == 401, path
        assert c.get(path, headers=AUTH).status_code == 200, path


def test_demo_mode_reports_demonstration_and_no_direct_claims(demo: DemoBackend) -> None:
    body = _client(demo).get("/api/system/mode", headers=AUTH).json()
    assert body["mode"] == "DEMONSTRATION"
    assert body["direct_search_claims_enabled"] is False
    prov = _client(demo).get("/health/providers", headers=AUTH).json()
    assert prov["operating_mode"]["mode"] == "DEMONSTRATION"


def test_request_ids_are_echoed_or_generated(demo: DemoBackend) -> None:
    c = _client(demo)
    r = c.get("/health/live", headers={"X-Request-ID": "trace-1234abcd"})
    assert r.headers["X-Request-ID"] == "trace-1234abcd"
    r = c.get("/health/live", headers={"X-Request-ID": "bad id with spaces\n"})
    assert r.headers["X-Request-ID"] != "bad id with spaces\n"
    assert len(r.headers["X-Request-ID"]) == 32


def test_metrics_count_requests_by_route_template(demo: DemoBackend) -> None:
    c = _client(demo)
    c.get("/api/overview", headers=AUTH)
    text = c.get("/metrics", headers=AUTH).text
    assert "# TYPE trend_agent_http_requests_total counter" in text
    assert 'route="/api/overview"' in text
    assert "trend_agent_http_request_seconds_bucket" in text
    assert TOKEN not in text
