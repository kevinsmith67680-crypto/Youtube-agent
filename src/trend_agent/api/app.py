"""The dashboard REST API (FastAPI).

The API reads and writes through a ``DashboardBackend`` only: the database backend in
production, or the isolated in-memory demo. It never calls an LLM from a request except
through the bounded, budgeted generation endpoint; it never executes anything named in a
recommendation; it never returns provider credentials.

Errors are structured: ``{"error": <code>, "detail": <message or field list>, "status": n}``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from trend_agent import __version__
from trend_agent.api.security import Authenticator, RateLimiter, client_key
from trend_agent.config import Settings
from trend_agent.recommendations.backend import DashboardBackend, NotFoundError, TransitionError
from trend_agent.schemas.dashboard import (
    AlertView,
    DataQualityView,
    FeedbackIn,
    ForecastView,
    GenerateIn,
    GenerateResult,
    OpportunityDetail,
    OpportunitySummary,
    OverviewView,
    Page,
    ProductionIn,
    ProfileIn,
    ProviderView,
    RecommendationDetail,
    RecommendationSummary,
    SavedIdea,
    SaveIn,
    SystemStatusView,
    TrendDetail,
    TrendSummary,
    UserState,
)
from trend_agent.schemas.recommendation import CreatorProfile

_log = logging.getLogger("trend_agent.api")

UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
KEY_RE = r"^[A-Za-z0-9_.:-]{1,80}$"
MAX_BODY_BYTES = 64 * 1024
EMPTY_STATE = UserState(
    saved=False, saved_at=None, notes=None, dismissed=False, production=None, feedback={}
)

Category = Literal["ai", "technology", "gaming"]
Order = Literal["asc", "desc"]
OppSort = Literal["score", "updated", "severity", "query", "breakout"]
RecSort = Literal["score", "rank", "hours", "generated", "title"]
RecId = Annotated[str, Path(pattern=UUID_RE, description="recommendation ID (UUID)")]
AnyId = Annotated[str, Path(pattern=KEY_RE)]


def _error(status: int, code: str, detail: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": code, "detail": detail, "status": status}
    )


def _strip_summary(r: RecommendationSummary) -> RecommendationSummary:
    return r.model_copy(update={"saved": False, "dismissed": False, "production_status": None})


def _authenticator(request: Request) -> Authenticator:
    auth: Authenticator = request.app.state.auth
    return auth


def read_access(request: Request) -> bool:
    """Reads: with a configured token every read needs it; without one (local view) reads
    are allowed and user-specific fields are removed from the responses."""
    ok = bool(getattr(request.state, "authenticated", False))
    if _authenticator(request).configured and not ok:
        raise HTTPException(401, "missing or invalid bearer token", {"WWW-Authenticate": "Bearer"})
    return ok


def private(request: Request) -> None:
    """Private user data and every change: a configured token, presented, is required."""
    if not _authenticator(request).configured:
        raise HTTPException(
            401,
            "private data and changes need TREND_AGENT_API_TOKEN to be configured on the "
            "server and sent by the dashboard's server-side proxy",
        )
    if not getattr(request.state, "authenticated", False):
        raise HTTPException(401, "missing or invalid bearer token", {"WWW-Authenticate": "Bearer"})


Read = Annotated[bool, Depends(read_access)]
Private = Annotated[None, Depends(private)]
PageQ = Annotated[int, Query(ge=1, le=10_000)]
SizeQ = Annotated[int | None, Query(ge=1, le=500)]
Text = Annotated[str | None, Query(max_length=120)]


def create_app(
    backend: DashboardBackend,
    settings: Settings,
    *,
    clock: Callable[[], float] | None = None,
) -> FastAPI:
    loaded = getattr(backend, "loaded", None)
    api_cfg = loaded.config.api if loaded is not None else None
    page_default = api_cfg.page_size_default if api_cfg else 25
    page_max = api_cfg.page_size_max if api_cfg else 100
    auth = Authenticator(settings.api_token.get_secret_value() if settings.api_token else None)
    limiter_kw: dict[str, Any] = {"clock": clock} if clock else {}
    general = RateLimiter(api_cfg.rate_limit_per_minute if api_cfg else 240, 60.0, **limiter_kw)
    generation = RateLimiter(
        api_cfg.generate_rate_limit_per_hour if api_cfg else 12, 3600.0, **limiter_kw
    )

    app = FastAPI(
        title="YouTube Trend Agent - Intelligence Dashboard API",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.backend = backend
    app.state.auth = auth
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
            return _error(413, "payload_too_large", f"request body above {MAX_BODY_BYTES} bytes")
        authenticated = auth.check(request)
        request.state.authenticated = authenticated
        if request.url.path.startswith("/api/") and request.url.path != "/api/health":
            ok, retry = general.hit(client_key(request, authenticated))
            if not ok:
                resp = _error(429, "rate_limited", "too many requests; slow down")
                resp.headers["Retry-After"] = str(retry)
                return resp
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if not request.url.path.startswith("/api/docs"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; frame-ancestors 'none'"
            )
        return response

    # ------------------------------------------------------------- error shapes
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
                 409: "conflict", 429: "rate_limited", 503: "unavailable"}  # fmt: skip
        resp = _error(exc.status_code, codes.get(exc.status_code, "error"), exc.detail)
        if exc.headers:
            resp.headers.update(exc.headers)
        return resp

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {"location": ".".join(str(x) for x in e.get("loc", ())), "message": e.get("msg", "")}
            for e in exc.errors()
        ]  # the submitted values are not echoed back
        return _error(422, "invalid_request", fields)

    @app.exception_handler(NotFoundError)
    async def not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return _error(404, "not_found", "no such record")

    @app.exception_handler(TransitionError)
    async def conflict(request: Request, exc: TransitionError) -> JSONResponse:
        return _error(409, "invalid_transition", str(exc))

    @app.exception_handler(Exception)
    async def internal(request: Request, exc: Exception) -> JSONResponse:
        _log.exception("api.internal_error path=%s", request.url.path)
        return _error(500, "internal_error", "the request could not be completed")

    def be() -> DashboardBackend:
        return backend

    def size(page_size: int | None) -> int:
        if page_size is None:
            return page_default
        if page_size > page_max:
            raise HTTPException(422, f"page_size must be at most {page_max}")
        return page_size

    api = APIRouter(prefix="/api")

    @api.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "mode": backend.mode,
            "synthetic": backend.synthetic,
            "version": __version__,
            "auth": "configured" if auth.configured else "read-only",
        }

    @api.get("/overview", response_model=OverviewView)
    def overview(ok: Read) -> OverviewView:
        ov = be().overview()
        if not ok:
            ov = ov.model_copy(
                update={"top_recommendations": [_strip_summary(r) for r in ov.top_recommendations]}
            )
        return ov

    # ------------------------------------------------------------ opportunities
    @api.get("/opportunities", response_model=Page[OpportunitySummary])
    def opportunities(
        _ok: Read,
        category: Category | None = None,
        region: Annotated[str | None, Query(pattern=r"^[A-Za-z]{2}$")] = None,
        intent: Annotated[str | None, Query(pattern=r"^[a-z_]{2,48}$")] = None,
        surge: Annotated[str | None, Query(pattern=r"^[a-z_]{2,32}$")] = None,
        status: Annotated[str | None, Query(pattern=r"^[a-z_]{2,32}$")] = None,
        verification: Annotated[str | None, Query(pattern=r"^[A-Za-z_]{2,40}$")] = None,
        min_score: Annotated[float | None, Query(ge=0, le=100)] = None,
        max_score: Annotated[float | None, Query(ge=0, le=100)] = None,
        max_age_hours: Annotated[float | None, Query(gt=0, le=24 * 365)] = None,
        q: Text = None,
        sort: OppSort = "score",
        order: Order = "desc",
        page: PageQ = 1,
        page_size: SizeQ = None,
    ) -> Page[OpportunitySummary]:
        return be().opportunities(
            category=category, region=region, intent=intent, surge=surge, status=status,
            verification=verification, min_score=min_score, max_score=max_score,
            max_age_hours=max_age_hours, q=q, sort=sort, order=order, page=page,
            page_size=size(page_size),
        )  # fmt: skip

    @api.get("/opportunities/{oid}", response_model=OpportunityDetail)
    def opportunity(oid: AnyId, ok: Read) -> OpportunityDetail:
        d = be().opportunity(oid)
        if d is None:
            raise HTTPException(404, "no such opportunity")
        if not ok:
            d = d.model_copy(
                update={"recommendations": [_strip_summary(r) for r in d.recommendations]}
            )
        return d

    # ------------------------------------------------------------------ trends
    @api.get("/trends", response_model=Page[TrendSummary])
    def trends(
        _ok: Read,
        category: Category | None = None,
        q: Text = None,
        page: PageQ = 1,
        page_size: SizeQ = None,
    ) -> Page[TrendSummary]:
        return be().trends(category=category, q=q, page=page, page_size=size(page_size))

    @api.get("/trends/{tid}", response_model=TrendDetail)
    def trend(tid: AnyId, _ok: Read) -> TrendDetail:
        d = be().trend(tid)
        if d is None:
            raise HTTPException(404, "no such trend")
        return d

    @api.get("/forecasts/{fid}", response_model=ForecastView)
    def forecast(fid: Annotated[str, Path(pattern=UUID_RE)], _ok: Read) -> ForecastView:
        f = be().forecast(fid)
        if f is None:
            raise HTTPException(404, "no such forecast")
        return f

    # --------------------------------------------------------- recommendations
    @api.get("/recommendations", response_model=Page[RecommendationSummary])
    def recommendations(
        ok: Read,
        category: Category | None = None,
        status: Annotated[str | None, Query(pattern=r"^[A-Z_]{2,40}$")] = None,
        urgency: Annotated[str | None, Query(pattern=r"^[A-Z_]{2,40}$")] = None,
        min_score: Annotated[float | None, Query(ge=0, le=100)] = None,
        q: Text = None,
        include_research: bool = False,
        include_suppressed: bool = False,
        include_dismissed: bool = False,
        sort: RecSort = "score",
        order: Order = "desc",
        page: PageQ = 1,
        page_size: SizeQ = None,
    ) -> Page[RecommendationSummary]:
        p = be().recommendations(
            category=category, status=status, urgency=urgency, min_score=min_score, q=q,
            include_research=include_research, include_suppressed=include_suppressed,
            include_dismissed=include_dismissed and ok, sort=sort, order=order, page=page,
            page_size=size(page_size),
        )  # fmt: skip
        if not ok:
            p = p.model_copy(update={"items": [_strip_summary(r) for r in p.items]})
        return p

    @api.get("/recommendations/{rid}", response_model=RecommendationDetail)
    def recommendation(rid: RecId, ok: Read, track_view: bool = False) -> RecommendationDetail:
        d = be().recommendation(rid, viewed=track_view and ok)
        if d is None:
            raise HTTPException(404, "no such recommendation")
        if not ok:
            d = d.model_copy(update={"user": EMPTY_STATE})
        return d

    @api.post("/recommendations/generate", response_model=GenerateResult)
    def generate(request: Request, _p: Private, req: GenerateIn = Body(...)) -> GenerateResult:  # noqa: B008
        allowed, retry = generation.hit(client_key(request, True))
        if not allowed:
            raise HTTPException(
                429, "generation rate limit reached; try again later", {"Retry-After": str(retry)}
            )
        if req.category is None and not req.all:
            raise HTTPException(422, "pass a category or all=true")
        return be().generate(req)

    @api.post("/recommendations/{rid}/save", response_model=UserState)
    def save(rid: RecId, _p: Private, body: SaveIn | None = None) -> UserState:
        return be().save(rid, body.notes if body else None)

    @api.delete("/recommendations/{rid}/save", response_model=UserState)
    def unsave(rid: RecId, _p: Private) -> UserState:
        return be().unsave(rid)

    @api.post("/recommendations/{rid}/dismiss", response_model=UserState)
    def dismiss(rid: RecId, _p: Private) -> UserState:
        return be().dismiss(rid)

    @api.post("/recommendations/{rid}/feedback", response_model=UserState)
    def feedback(rid: RecId, body: FeedbackIn, _p: Private) -> UserState:
        return be().feedback(rid, body.feedback_type, body.value, body.notes)

    @api.post("/recommendations/{rid}/production", response_model=UserState)
    def production(rid: RecId, body: ProductionIn, _p: Private) -> UserState:
        return be().production(rid, body)

    @api.get("/saved", response_model=list[SavedIdea])
    def saved(
        _p: Private,
        status: Annotated[str | None, Query(pattern=r"^[A-Z_]{2,32}$")] = None,
        category: Category | None = None,
    ) -> list[SavedIdea]:
        return be().saved(status=status, category=category)

    # ------------------------------------------------------- profile and alerts
    @api.get("/profile", response_model=CreatorProfile)
    def profile(_p: Private) -> CreatorProfile:
        return be().profile()

    @api.put("/profile", response_model=CreatorProfile)
    def update_profile(body: ProfileIn, _p: Private) -> CreatorProfile:
        return be().update_profile(CreatorProfile.model_validate(body.model_dump()))

    @api.get("/alerts", response_model=list[AlertView])
    def alerts(_p: Private, unread_only: bool = False) -> list[AlertView]:
        return be().alerts(unread_only=unread_only)

    @api.post("/alerts/{aid}/read", response_model=AlertView)
    def alert_read(aid: Annotated[str, Path(pattern=UUID_RE)], _p: Private) -> AlertView:
        a = be().mark_alert_read(aid)
        if a is None:
            raise HTTPException(404, "no such alert")
        return a

    # ------------------------------------------------------------------ system
    @api.get("/system/status", response_model=SystemStatusView)
    def system_status(_ok: Read) -> SystemStatusView:
        return be().system_status()

    @api.get("/system/providers", response_model=list[ProviderView])
    def providers(_ok: Read) -> list[ProviderView]:
        return be().providers()

    @api.get("/system/data-quality", response_model=DataQualityView)
    def data_quality(_ok: Read) -> DataQualityView:
        return be().data_quality()

    app.include_router(api)
    return app
