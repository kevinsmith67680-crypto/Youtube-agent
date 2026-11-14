"""The dashboard REST API (FastAPI).

The API reads and writes through a ``DashboardBackend`` only: the database backend in
production, or the isolated in-memory demo. It never calls an LLM from a request except
through the bounded, budgeted generation endpoint; it never executes anything named in a
recommendation; it never returns provider credentials.

Errors are structured: ``{"error": <code>, "detail": <message or field list>, "status": n}``.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from trend_agent import __version__
from trend_agent.api.security import Authenticator, RateLimiter, client_key
from trend_agent.config import Settings
from trend_agent.logging import correlation, valid_correlation_id
from trend_agent.ops import health as ops_health
from trend_agent.ops.metrics import REGISTRY, budget_gauges, database_gauges
from trend_agent.ops.modes import CapabilityBlockedError, SystemCapability
from trend_agent.recommendations.backend import DashboardBackend, NotFoundError, TransitionError
from trend_agent.schemas.dashboard import (
    AlertView,
    DataQualityView,
    FeedbackIn,
    ForecastView,
    GapRow,
    GenerateIn,
    GenerateResult,
    OpportunityDetail,
    OpportunitySummary,
    OutcomeIn,
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

if TYPE_CHECKING:
    from trend_agent.container import AppContainer
    from trend_agent.ops.system import SystemSnapshot

_log = logging.getLogger("trend_agent.api")
SNAPSHOT_TTL_SECONDS = 15.0

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


class _Snapshots:
    """Caches the system snapshot briefly: it reads providers, budgets and heartbeats."""

    def __init__(self, container: AppContainer) -> None:
        from trend_agent.ops.system import SystemService

        self.system = SystemService(container)
        self._lock = threading.Lock()
        self._at = 0.0
        self._value: SystemSnapshot | None = None

    def get(self) -> SystemSnapshot:
        with self._lock:
            now = time.monotonic()
            if self._value is None or now - self._at > SNAPSHOT_TTL_SECONDS:
                self._value = self.system.snapshot()
                self._at = now
            return self._value


def create_app(
    backend: DashboardBackend,
    settings: Settings,
    *,
    clock: Callable[[], float] | None = None,
    ops: AppContainer | None = None,
) -> FastAPI:
    """``ops``: the application container in database mode. It backs the health checks,
    the operating-mode gate on generation and the metrics; without it (demo mode) the
    health endpoints report the isolated synthetic demo."""
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

    snapshots = _Snapshots(ops) if ops is not None else None
    app.state.snapshots = snapshots

    @app.middleware("http")
    async def observe(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-request-id")
        cid = incoming if incoming is not None and valid_correlation_id(incoming) else ""
        cid = cid or uuid.uuid4().hex
        request.state.correlation_id = cid
        started = time.perf_counter()
        with correlation(cid):
            response = await call_next(request)
        route = request.scope.get("route")
        template = getattr(route, "path", None) or "unmatched"
        REGISTRY.http_requests.inc(request.method, template, str(response.status_code))
        REGISTRY.http_latency.observe(time.perf_counter() - started, request.method, template)
        response.headers["X-Request-ID"] = cid
        return response

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
        codes = {
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
            409: "conflict",
            429: "rate_limited",
            503: "unavailable",
        }
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

    def _snapshot_or_503() -> SystemSnapshot:
        if snapshots is None:
            raise HTTPException(503, "demo mode: no operating system state")
        return snapshots.get()

    def _probe(result: ops_health.CheckResult) -> JSONResponse:
        code = 503 if result.status == ops_health.FAIL else 200
        return JSONResponse(result.as_dict(), status_code=code)

    health_router = APIRouter(prefix="/health", tags=["health"])

    @health_router.get("/live")
    def health_live() -> JSONResponse:
        return _probe(ops_health.live())

    @health_router.get("/ready")
    def health_ready() -> JSONResponse:
        if ops is None:
            return _probe(ops_health.ready(None, demo=True))
        return _probe(ops_health.ready(ops.engine, demo=False))

    @health_router.get("/database")
    def health_database() -> JSONResponse:
        if ops is None:
            return _probe(
                ops_health.CheckResult(
                    "database",
                    ops_health.OK,
                    "demo mode: the database is not used",
                    {"demo": True},
                )
            )
        return _probe(ops_health.database(ops.engine))

    @health_router.get("/workers")
    def health_workers(ok: Read) -> JSONResponse:
        if ops is None:
            return _probe(
                ops_health.CheckResult(
                    "workers", ops_health.OK, "demo mode: no workers run", {"demo": True}
                )
            )
        res = ops_health.workers(ops.session_factory, ops.clock())
        if not ok:
            res.data = {"live": res.data.get("live", {})}
        return _probe(res)

    @health_router.get("/providers")
    def health_providers(ok: Read) -> JSONResponse:
        if ops is None:
            return JSONResponse(
                {
                    "check": "providers",
                    "status": ops_health.OK,
                    "demo": True,
                    "detail": "demo mode: only the synthetic demonstration dataset is served",
                    "operating_mode": {
                        "mode": "DEMONSTRATION",
                        "direct_search_claims_enabled": False,
                    },
                }
            )
        snap = snapshots.get() if snapshots is not None else None
        assert snap is not None
        usable = [r for r in snap.readiness if r.usable_for_real_data]
        status = ops_health.OK if usable else ops_health.DEGRADED
        return JSONResponse(
            {
                "check": "providers",
                "status": status,
                "detail": f"{len(usable)} real provider(s) usable; mode {snap.decision.mode.value}",
                "operating_mode": snap.decision.as_dict(),
                "providers": [r.as_dict() for r in snap.readiness],
            }
        )

    app.include_router(health_router)

    @app.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
    def metrics(_ok: Read) -> PlainTextResponse:
        lines = REGISTRY.render()
        if ops is not None:
            snap = snapshots.get() if snapshots is not None else None
            try:
                lines += database_gauges(
                    ops.session_factory, ops.clock(), snap.decision if snap else None
                )
                lines += budget_gauges(snap.budgets if snap else None)
            except Exception:
                _log.exception("metrics.database_gauges_failed")
                lines.append("# database gauges unavailable")
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    api = APIRouter(prefix="/api")

    @api.get("/system/mode")
    def system_mode(_ok: Read) -> dict[str, Any]:
        if snapshots is None:
            return {
                "mode": "DEMONSTRATION",
                "base_mode": "DEMONSTRATION",
                "demo": True,
                "direct_search_claims_enabled": False,
                "reasons": ["demo mode: the isolated synthetic dataset is served; nothing is live"],
                "degradations": [],
                "capabilities": {},
                "signal_classes_usable": [],
                "signal_classes_verified": [],
                "next_mode_requirements": [],
            }
        return _snapshot_or_503().decision.as_dict()

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
            category=category,
            region=region,
            intent=intent,
            surge=surge,
            status=status,
            verification=verification,
            min_score=min_score,
            max_score=max_score,
            max_age_hours=max_age_hours,
            q=q,
            sort=sort,
            order=order,
            page=page,
            page_size=size(page_size),
        )

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

    @api.get("/gaps", response_model=Page[GapRow])
    def gaps(
        _ok: Read,
        category: Category | None = None,
        gap_type: Annotated[str | None, Query(pattern=r"^[a-z_]{2,48}$")] = None,
        strength: Literal["strong", "moderate", "weak"] | None = None,
        primary_only: bool = False,
        include_unverifiable: bool = False,
        q: Text = None,
        sort: Literal["score", "strength", "query", "updated"] = "score",
        order: Order = "desc",
        page: PageQ = 1,
        page_size: SizeQ = None,
    ) -> Page[GapRow]:
        return be().gaps(
            category=category,
            gap_type=gap_type,
            strength=strength,
            primary_only=primary_only,
            include_unverifiable=include_unverifiable,
            q=q,
            sort=sort,
            order=order,
            page=page,
            page_size=size(page_size),
        )

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
            category=category,
            status=status,
            urgency=urgency,
            min_score=min_score,
            q=q,
            include_research=include_research,
            include_suppressed=include_suppressed,
            include_dismissed=include_dismissed and ok,
            sort=sort,
            order=order,
            page=page,
            page_size=size(page_size),
        )
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
        if snapshots is not None:
            try:
                snapshots.get().decision.require(SystemCapability.RECOMMENDATIONS)
            except CapabilityBlockedError as exc:
                raise HTTPException(503, str(exc)) from None
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

    @api.post("/recommendations/{rid}/outcomes")
    def outcome(rid: RecId, body: OutcomeIn, _p: Private) -> dict[str, Any]:
        """Opt-in, creator-entered outcome (database mode only). Descriptive data: it is
        never used to claim that a recommendation caused views."""
        if ops is None:
            raise HTTPException(503, "outcomes are recorded in database mode only")
        import uuid as _uuid

        import sqlalchemy as _sa

        from trend_agent.db.repositories.ops import OutcomeRepository
        from trend_agent.db.session import transaction as _tx

        fields = body.model_dump(exclude={"consent"}) | {
            "consent": True,
            "source": "creator_entered",
            "recorded_at": ops.clock(),
        }
        try:
            with _tx(ops.session_factory) as s:
                oid = OutcomeRepository(s).record(
                    user_key=settings.dashboard_user, recommendation_id=_uuid.UUID(rid), **fields
                )
        except _sa.exc.IntegrityError:
            raise HTTPException(404, "no such recommendation") from None
        return {"id": oid, "recorded": True, "days_since_publication": body.days_since_publication}

    @api.get("/outcomes")
    def outcomes(_p: Private) -> list[dict[str, Any]]:
        if ops is None:
            return []
        from trend_agent.db.repositories.ops import OutcomeRepository
        from trend_agent.db.session import transaction as _tx

        with _tx(ops.session_factory) as s:
            return [
                {
                    "recommendation_id": str(r.recommendation_id),
                    "days": r.days_since_publication,
                    "views": r.views,
                    "search_traffic_share": r.search_traffic_share,
                    "source": r.source,
                    "recorded_at": r.recorded_at.isoformat(),
                }
                for r in OutcomeRepository(s).for_user(settings.dashboard_user)
            ]

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

    # ------------------------------------------------------------------ github
    # External developer activity from the GitHub REST API (database mode only). Every
    # response labels its evidence class; nothing here is search interest or volume.
    github_demo = {
        "demo": True,
        "synthetic": True,
        "detail": "demo mode: GitHub discoveries are not part of the synthetic dataset",
    }

    def github_read() -> Any:
        if ops is None:
            return None
        from trend_agent.github.service import GitHubReadService

        return GitHubReadService(ops.session_factory, clock=ops.clock)

    def github_provider_state() -> dict[str, Any]:
        assert ops is not None
        try:
            ops.discovery()  # registers discovery providers (no network)
            provider = ops.registry.get("github")
            rep = provider.status()
            client = getattr(provider, "client", None)
            return {
                "status": rep.status.value,
                "detail": rep.detail,
                "auth_mode": client.auth_mode() if client else None,
                "api_version": client.api_version if client else None,
            }
        except Exception as exc:  # configuration problems are reported, not raised
            return {"status": "unknown", "detail": f"{type(exc).__name__}"}

    @api.get("/github/summary")
    def github_summary(_ok: Read) -> dict[str, Any]:
        from trend_agent.github.service import EVIDENCE_CLASSES

        svc = github_read()
        if svc is None:
            empty = {"repositories": 0, "releases": 0, "observations": 0}
            return {
                **github_demo,
                **empty,
                "provider": {"status": "demo"},
                "evidence_classes": EVIDENCE_CLASSES,
            }
        return {
            "demo": False,
            "synthetic": False,
            **svc.summary(),
            "provider": github_provider_state(),
        }

    @api.get("/github/repositories")
    def github_repositories(
        _ok: Read,
        category: Category | None = None,
        strategy: Annotated[str | None, Query(pattern=r"^(emerging|active|monitored)$")] = None,
        days: Annotated[int | None, Query(ge=1, le=3650)] = None,
        order: Annotated[str, Query(pattern=r"^(first_seen|score|created)$")] = "first_seen",
        page: PageQ = 1,
        page_size: SizeQ = None,
    ) -> dict[str, Any]:
        n = size(page_size)
        svc = github_read()
        if svc is None:
            return {**github_demo, "items": [], "total": 0, "page": page, "page_size": n}
        data = svc.repositories(
            days=days, category=category, strategy=strategy, limit=n,
            offset=(page - 1) * n, order=order,
        )  # fmt: skip
        return {"demo": False, "synthetic": False, "page": page, "page_size": n, **data}

    @api.get("/github/repositories/{repository_id}")
    def github_repository(
        repository_id: Annotated[int, Path(ge=1, le=10**12)], _ok: Read
    ) -> dict[str, Any]:
        svc = github_read()
        detail: dict[str, Any] | None = (
            svc.repository_detail(repository_id) if svc is not None else None
        )
        if detail is None:
            raise HTTPException(404, "no such repository")
        return detail

    @api.get("/github/releases")
    def github_releases(
        _ok: Read,
        category: Category | None = None,
        days: Annotated[int | None, Query(ge=1, le=3650)] = None,
        page: PageQ = 1,
        page_size: SizeQ = None,
    ) -> dict[str, Any]:
        n = size(page_size)
        svc = github_read()
        if svc is None:
            return {**github_demo, "items": [], "total": 0, "page": page, "page_size": n}
        data = svc.releases(days=days, category=category, limit=n, offset=(page - 1) * n)
        return {"demo": False, "synthetic": False, "page": page, "page_size": n, **data}

    # ------------------------------------------------------------------ hackernews
    # Community discussion activity from the official Hacker News API (database mode
    # only). Every response carries its evidence class and the not-search-demand caveat.
    hn_demo = {
        "demo": True,
        "synthetic": True,
        "detail": "demo mode: Hacker News stories are not part of the synthetic dataset",
    }

    def hn_read() -> Any:
        if ops is None:
            return None
        from trend_agent.hackernews.service import HackerNewsReadService

        return HackerNewsReadService(ops.session_factory, clock=ops.clock)

    def hn_provider_state() -> dict[str, Any]:
        assert ops is not None
        try:
            ops.discovery()  # registers discovery providers (no network)
            rep = ops.registry.get("hackernews_firebase").status()
            return {
                "status": rep.status.value,
                "detail": rep.detail,
                "api_enabled": ops.settings.hackernews_api_enabled,
                "scheduled_discovery": ops.settings.scheduled_discovery,
            }
        except Exception as exc:  # configuration problems are reported, not raised
            return {"status": "unknown", "detail": f"{type(exc).__name__}"}

    @api.get("/hackernews/summary")
    def hackernews_summary(_ok: Read) -> dict[str, Any]:
        from trend_agent.hackernews.service import EVIDENCE_CLASSES, NOT_SEARCH_DEMAND

        svc = hn_read()
        if svc is None:
            return {
                **hn_demo,
                "stories": 0,
                "observations": 0,
                "sources": [],
                "provider": {"status": "demo"},
                "evidence_classes": EVIDENCE_CLASSES,
                "caveat": NOT_SEARCH_DEMAND,
            }
        return {
            "demo": False,
            "synthetic": False,
            **svc.summary(),
            "provider": hn_provider_state(),
        }

    @api.get("/hackernews/stories")
    def hackernews_stories(
        _ok: Read,
        category: Annotated[str | None, Query(pattern=r"^(ai|technology|gaming)$")] = None,
        list_type: Annotated[
            str | None, Query(alias="list", pattern=r"^(new|top|best|show|ask)$")
        ] = None,
        kind: Annotated[
            str | None, Query(pattern=r"^(story|show_hn|ask_hn|launch_hn|tell_hn)$")
        ] = None,
        momentum: Annotated[str | None, Query(pattern=r"^(rising|steady|unknown)$")] = None,
        days: Annotated[int | None, Query(ge=1, le=3650)] = None,
        order: Annotated[
            str, Query(pattern=r"^(published|priority|score|comments|momentum)$")
        ] = "published",
        page: PageQ = 1,
        page_size: SizeQ = None,
    ) -> dict[str, Any]:
        from trend_agent.hackernews.models import LIST_BY_TYPE
        from trend_agent.hackernews.service import NOT_SEARCH_DEMAND

        n = size(page_size)
        svc = hn_read()
        if svc is None:
            return {
                **hn_demo,
                "items": [],
                "total": 0,
                "page": page,
                "page_size": n,
                "caveat": NOT_SEARCH_DEMAND,
            }
        data = svc.stories(
            days=days,
            category=category,
            list_name=LIST_BY_TYPE[list_type] if list_type else None,
            kind=kind,
            momentum_state=momentum,
            limit=n,
            offset=(page - 1) * n,
            order=order,
        )
        return {"demo": False, "synthetic": False, "page": page, "page_size": n, **data}

    @api.get("/hackernews/trending")
    def hackernews_trending(
        _ok: Read,
        hours: Annotated[int, Query(ge=1, le=168)] = 48,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        from trend_agent.hackernews.service import NOT_SEARCH_DEMAND

        svc = hn_read()
        if svc is None:
            return {
                **hn_demo,
                "items": [],
                "measured": 0,
                "awaiting_repeat_observation": 0,
                "window_hours": hours,
                "caveat": NOT_SEARCH_DEMAND,
            }
        return {"demo": False, "synthetic": False, **svc.trending(hours=hours, limit=limit)}

    @api.get("/hackernews/stories/{story_id}")
    def hackernews_story(
        story_id: Annotated[int, Path(ge=1, le=10**12)], _ok: Read
    ) -> dict[str, Any]:
        svc = hn_read()
        detail: dict[str, Any] | None = svc.story_detail(story_id) if svc is not None else None
        if detail is None:
            raise HTTPException(404, "no such story")
        return detail

    app.include_router(api)
    return app
