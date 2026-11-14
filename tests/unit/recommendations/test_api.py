"""Dashboard API: authentication, authorization, rate limits, CORS, validation, structured
errors, output safety and the user workflow, against the isolated demo backend."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.helpers import make_settings
from trend_agent.api.app import create_app
from trend_agent.api.security import RateLimiter
from trend_agent.recommendations.demo import DemoBackend

TOKEN = "test-dashboard-token-0123456789abcdef"
SECRET_KEY = "sk-ant-should-never-leak-000000000000"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ORIGIN = "http://localhost:3000"


class Tick:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _client(demo: DemoBackend, *, token: str | None = TOKEN, **kw: Any) -> TestClient:
    settings = make_settings(
        api_token=token, anthropic_api_key=SECRET_KEY, api_cors_origins=ORIGIN + ",*"
    )
    return TestClient(create_app(demo, settings, **kw))


def _first_rec(c: TestClient) -> dict[str, Any]:
    items = c.get("/api/recommendations?sort=rank", headers=AUTH).json()["items"]
    assert items
    return items[0]


# ------------------------------------------------------------------ authentication
def test_health_is_public_and_says_how_auth_is_set_up(demo: DemoBackend) -> None:
    r = _client(demo).get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "demo"
    assert body["synthetic"] is True
    assert body["auth"] == "configured"


@pytest.mark.parametrize(
    "path",
    ["/api/overview", "/api/opportunities", "/api/recommendations", "/api/trends",
     "/api/system/status", "/api/saved", "/api/profile", "/api/alerts"],
)  # fmt: skip
def test_configured_token_is_required_for_every_read(demo: DemoBackend, path: str) -> None:
    c = _client(demo)
    r = c.get(path)
    assert r.status_code == 401
    assert r.json() == {
        "error": "unauthorized",
        "detail": "missing or invalid bearer token",
        "status": 401,
    }
    assert r.headers["www-authenticate"] == "Bearer"
    assert c.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get(path, headers={"Authorization": TOKEN}).status_code == 401  # no scheme
    assert c.get(path, headers=AUTH).status_code == 200


def test_without_a_token_reads_are_public_and_stripped_writes_refused(
    demo: DemoBackend,
) -> None:
    owner = _client(demo)
    rid = _first_rec(owner)["id"]
    owner.post(f"/api/recommendations/{rid}/save", headers=AUTH, json={"notes": "mine"})
    c = _client(demo, token=None)
    assert c.get("/api/health").json()["auth"] == "read-only"
    page = c.get("/api/recommendations").json()
    assert page["items"]
    assert not any(i["saved"] for i in page["items"])  # user state is not public
    detail = c.get(f"/api/recommendations/{rid}").json()
    assert detail["user"]["saved"] is False
    assert detail["user"]["notes"] is None
    for method, path in [
        ("post", f"/api/recommendations/{rid}/save"),
        ("post", f"/api/recommendations/{rid}/dismiss"),
        ("post", "/api/recommendations/generate"),
        ("get", "/api/saved"),
        ("get", "/api/profile"),
        ("put", "/api/profile"),
        ("get", "/api/alerts"),
    ]:
        r = c.request(method.upper(), path, json=None if method == "get" else {})
        assert r.status_code == 401, path
        assert "TREND_AGENT_API_TOKEN" in r.json()["detail"]


def test_responses_never_contain_credentials(demo: DemoBackend) -> None:
    c = _client(demo)
    paths = ["/api/health", "/api/overview", "/api/system/status", "/api/system/providers",
             "/api/system/data-quality", "/api/profile", "/api/openapi.json"]  # fmt: skip
    for p in paths:
        text = c.get(p, headers=AUTH).text
        assert TOKEN not in text, p
        assert SECRET_KEY not in text, p


# ---------------------------------------------------------------------- security
def test_security_headers_and_restrictive_cors(demo: DemoBackend) -> None:
    c = _client(demo)
    r = c.get("/api/health", headers={"Origin": ORIGIN})
    assert r.headers["access-control-allow-origin"] == ORIGIN  # the dashboard only
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in r.headers["content-security-policy"]
    other = c.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers  # "*" in config is ignored
    pre = c.options(
        "/api/recommendations",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in pre.headers
    assert "access-control-allow-credentials" not in r.headers


def test_oversized_bodies_are_rejected(demo: DemoBackend) -> None:
    c = _client(demo)
    rid = _first_rec(c)["id"]
    r = c.post(
        f"/api/recommendations/{rid}/save",
        headers={**AUTH, "Content-Type": "application/json"},
        content=json.dumps({"notes": "x" * 70_000}),
    )
    assert r.status_code == 413
    assert r.json()["error"] == "payload_too_large"


def test_rate_limits_are_enforced(demo: DemoBackend) -> None:
    tick = Tick()
    c = _client(demo, clock=tick)
    limit = demo.loaded.config.api.rate_limit_per_minute
    codes = [c.get("/api/system/providers", headers=AUTH).status_code for _ in range(limit + 1)]
    assert codes[:-1] == [200] * limit
    assert codes[-1] == 429
    r = c.get("/api/system/providers", headers=AUTH)
    assert r.json()["error"] == "rate_limited"
    assert int(r.headers["retry-after"]) >= 1
    assert c.get("/api/health").status_code == 200  # health is never limited
    tick.t += 61
    assert c.get("/api/system/providers", headers=AUTH).status_code == 200


def test_generation_has_its_own_hourly_limit(demo: DemoBackend) -> None:
    tick = Tick()
    c = _client(demo, clock=tick)
    n = demo.loaded.config.api.generate_rate_limit_per_hour
    for _ in range(n):
        r = c.post("/api/recommendations/generate", headers=AUTH, json={"all": True})
        assert r.status_code == 200
    r = c.post("/api/recommendations/generate", headers=AUTH, json={"all": True})
    assert r.status_code == 429
    tick.t += 3601
    assert (
        c.post("/api/recommendations/generate", headers=AUTH, json={"all": True}).status_code == 200
    )


def test_rate_limiter_windows() -> None:
    tick = Tick()
    rl = RateLimiter(2, 10.0, clock=tick)
    assert rl.hit("a")[0]
    assert rl.hit("a")[0]
    ok, retry = rl.hit("a")
    assert not ok
    assert 1 <= retry <= 10
    assert rl.hit("b")[0]  # per client
    tick.t += 10
    assert rl.hit("a")[0]


# ---------------------------------------------------------------- validation/errors
@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/api/recommendations/not-a-uuid", 422),
        ("/api/recommendations/00000000-0000-0000-0000-000000000000", 404),
        ("/api/opportunities/00000000-0000-0000-0000-000000000000", 404),
        ("/api/trends/does-not-exist", 404),
        ("/api/trends/../../etc/passwd", 404),
        ("/api/forecasts/xyz", 422),
        ("/api/opportunities?page_size=1000", 422),
        ("/api/opportunities?page_size=101", 422),
        ("/api/opportunities?min_score=101", 422),
        ("/api/opportunities?category=cooking", 422),
        ("/api/opportunities?sort=random()", 422),
        ("/api/opportunities?order=sideways", 422),
        ("/api/recommendations?q=" + "x" * 200, 422),
        ("/api/opportunities?region=GBR", 422),
        ("/api/nothing-here", 404),
    ],
)
def test_invalid_requests_get_structured_errors(demo: DemoBackend, path: str, status: int) -> None:
    r = _client(demo).get(path, headers=AUTH)
    assert r.status_code == status
    body = r.json()
    assert set(body) == {"error", "detail", "status"}
    assert body["status"] == status


def test_validation_errors_do_not_echo_input(demo: DemoBackend) -> None:
    probe = "<script>alert(1)</script>"
    r = _client(demo).get(f"/api/opportunities?category={probe}", headers=AUTH)
    assert r.status_code == 422
    assert probe not in r.text


def test_mutation_bodies_are_validated(demo: DemoBackend) -> None:
    c = _client(demo)
    rid = _first_rec(c)["id"]
    bad = [
        (f"/api/recommendations/{rid}/feedback", {"feedback_type": "LOVE_IT"}),
        (f"/api/recommendations/{rid}/feedback", {"feedback_type": "USEFUL", "extra": 1}),
        (f"/api/recommendations/{rid}/production", {"status": "PUBLISHED",
                                                    "published_video_id": "../../x"}),
        ("/api/recommendations/generate", {"category": "cooking"}),
        ("/api/recommendations/generate", {"all": True, "max_opportunities": 0}),
        ("/api/recommendations/generate", {"category": None, "all": False}),
    ]  # fmt: skip
    for path, body in bad:
        r = c.post(path, headers=AUTH, json=body)
        assert r.status_code == 422, (path, body, r.text)
        assert r.json()["error"] == "invalid_request" or r.json()["status"] == 422


# ------------------------------------------------------------------- user workflow
def test_save_feedback_production_and_dismiss_flow(demo: DemoBackend) -> None:
    c = _client(demo)
    rec = _first_rec(c)
    rid = rec["id"]
    st = c.post(f"/api/recommendations/{rid}/save", headers=AUTH, json={"notes": "Friday"}).json()
    assert st["saved"] is True
    assert st["production"]["status"] == "IDEA"
    st = c.post(
        f"/api/recommendations/{rid}/feedback",
        headers=AUTH,
        json={"feedback_type": "USEFUL", "notes": "good gap"},
    ).json()
    assert st["feedback"]["USEFUL"] == 1
    r = c.post(f"/api/recommendations/{rid}/production", headers=AUTH, json={"status": "PUBLISHED"})
    assert r.status_code == 409
    assert r.json()["error"] == "invalid_transition"
    for s in ("SCRIPTING", "RECORDING", "EDITING", "READY_TO_PUBLISH"):
        assert (
            c.post(
                f"/api/recommendations/{rid}/production", headers=AUTH, json={"status": s}
            ).status_code
            == 200
        )
    r = c.post(f"/api/recommendations/{rid}/production", headers=AUTH, json={"status": "PUBLISHED"})
    assert r.status_code == 409  # a published video needs its ID
    st = c.post(
        f"/api/recommendations/{rid}/production",
        headers=AUTH,
        json={"status": "PUBLISHED", "published_video_id": "dQw4w9WgXcQ"},
    ).json()
    assert st["production"]["published_video_id"] == "dQw4w9WgXcQ"
    saved = c.get("/api/saved", headers=AUTH).json()
    assert [s["recommendation"]["id"] for s in saved] == [rid]
    assert c.get("/api/saved?status=PUBLISHED", headers=AUTH).json()
    other = next(x for x in ("ai", "technology", "gaming") if x != rec["category"])
    assert c.get(f"/api/saved?category={other}", headers=AUTH).json() == []
    # dismissing hides the idea from the default list
    c.post(f"/api/recommendations/{rid}/dismiss", headers=AUTH)
    ids = {
        i["id"] for i in c.get("/api/recommendations?page_size=100", headers=AUTH).json()["items"]
    }
    assert rid not in ids
    ids = {
        i["id"]
        for i in c.get(
            "/api/recommendations?page_size=100&include_dismissed=true", headers=AUTH
        ).json()["items"]
    }
    assert rid in ids


def test_view_tracking_needs_auth(demo: DemoBackend) -> None:
    c = _client(demo)
    rid = _first_rec(c)["id"]
    d = c.get(f"/api/recommendations/{rid}?track_view=true", headers=AUTH).json()
    assert d["user"]["feedback"]["VIEWED"] == 1
    public = _client(demo, token=None)
    public.get(f"/api/recommendations/{rid}?track_view=true")
    d = c.get(f"/api/recommendations/{rid}", headers=AUTH).json()
    assert d["user"]["feedback"]["VIEWED"] == 1  # the anonymous view was not recorded


def test_profile_round_trip_and_validation(demo: DemoBackend) -> None:
    c = _client(demo)
    p = c.get("/api/profile", headers=AUTH).json()
    p["preferred_categories"] = ["gaming"]
    p["available_hours_per_week"] = 6
    r = c.put("/api/profile", headers=AUTH, json=p)
    assert r.status_code == 200, r.text
    assert c.get("/api/profile", headers=AUTH).json()["preferred_categories"] == ["gaming"]
    p["preferred_categories"] = ["cooking"]
    assert c.put("/api/profile", headers=AUTH, json=p).status_code == 422


def test_alerts_listing_and_read_flag(demo: DemoBackend) -> None:
    c = _client(demo)
    alerts = c.get("/api/alerts", headers=AUTH).json()
    assert alerts
    assert all(a["synthetic"] for a in alerts)
    assert all(a["title"].startswith("[SYNTHETIC]") for a in alerts)
    external = [d for a in alerts for d in a["deliveries"] if d["channel"] != "in_app"]
    assert not any(d["status"] == "delivered" for d in external)  # demo never sends out
    aid = alerts[0]["id"]
    assert c.post(f"/api/alerts/{aid}/read", headers=AUTH).json()["read"] is True
    assert aid not in {a["id"] for a in c.get("/api/alerts?unread_only=true", headers=AUTH).json()}
    assert (
        c.post("/api/alerts/00000000-0000-0000-0000-000000000000/read", headers=AUTH).status_code
        == 404
    )


# ---------------------------------------------------------------------- reads
def test_pagination_filtering_and_sorting(demo: DemoBackend) -> None:
    c = _client(demo)
    all_items = c.get("/api/recommendations?page_size=100", headers=AUTH).json()
    total = all_items["total"]
    assert total >= 3
    p1 = c.get("/api/recommendations?page_size=2&page=1", headers=AUTH).json()
    p2 = c.get("/api/recommendations?page_size=2&page=2", headers=AUTH).json()
    assert p1["total"] == p2["total"] == total
    assert {i["id"] for i in p1["items"]}.isdisjoint({i["id"] for i in p2["items"]})
    scores = [i["score"] for i in all_items["items"]]
    assert scores == sorted(scores, reverse=True)
    ranks = [i["rank"] for i in c.get("/api/recommendations?sort=rank&page_size=100",
                                      headers=AUTH).json()["items"]]  # fmt: skip
    assert ranks == sorted(ranks)
    gaming = c.get("/api/recommendations?category=gaming&page_size=100", headers=AUTH).json()
    assert gaming["items"]
    assert all(i["category"] == "gaming" for i in gaming["items"])
    research = c.get(
        "/api/recommendations?include_research=true&page_size=100", headers=AUTH
    ).json()
    assert research["total"] > total
    opps = c.get("/api/opportunities?sort=score&order=asc", headers=AUTH).json()["items"]
    s = [o["opportunity_score"] for o in opps if o["opportunity_score"] is not None]
    assert s == sorted(s)
    assert c.get("/api/opportunities?q=zzzz-nothing", headers=AUTH).json()["total"] == 0


def test_trend_detail_labels_every_series_and_never_mixes_sources(demo: DemoBackend) -> None:
    c = _client(demo)
    for t in c.get("/api/trends", headers=AUTH).json()["items"]:
        d = c.get(f"/api/trends/{t['id']}", headers=AUTH).json()
        for s in d["series"]:
            assert s["data_class"] in ("OBSERVED", "PROXY_SIGNAL", "SYNTHETIC", "MISSING")
            assert s["measurement_definition"]
            assert s["synthetic"] is True
        for f in d["forecasts"]:
            if f["status"] in ("INSUFFICIENT_HISTORY", "SOURCE_UNAVAILABLE",
                               "FORECAST_UNAVAILABLE", "UNSUPPORTED_GRANULARITY"):  # fmt: skip
                assert not any(p["value"] is not None for p in f["trajectory"])
            assert f["synthetic"] is True
