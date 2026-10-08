"""FastAPI application entrypoint.

Wires together: settings, the authentication middleware (ADR-009 Decision 7),
exception -> HTTP-status mapping, module routers, the event bus's
registration/subscription wiring, and the hook registry's registration wiring.
Week 1 mounted `masterdata`; Week 2 added `ledger` (ADR-005); Week 3 added the
event bus + posting engine (ADR-003/ADR-004); Week 4 added `sales` (order
lifecycle) plus the minimal hook registry and its one demonstration plugin,
`app.plugins.credit_limit` (ADR-006); Week 5 added `inventory` (append-only
stock + summary) and wired `sales.goods_shipped` — the first event with two
subscribers — through both it and `ledger.posting` (ADR-007). Week 6 added
`receivables` (invoicing, payment application, AR aging) and its four
posting events, closing the Phase 1 O2C line (ADR-008).

WEEK9 Slice D (ADR-009, Phase 2) implements `AuthenticationMiddleware`
with bearer-session token resolution, multi-tenant company context and actor
context bound ambiently from the active session, and strict explicit route
classification. See `docs/adr/ADR-009-platform-permissions.md`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.routing import Match

from app.core import events, hooks
from app.core.authorization import (
    Authenticated,
    Public,
    RequiresPermission,
    reset_current_actor,
    set_current_actor,
)
from app.core.db import get_session_factory
from app.core.exceptions import (
    ActionDeniedError,
    ConflictError,
    DomainValidationError,
    NotFoundError,
    TenancyContextError,
)
from app.core.settings import get_settings
from app.core.tenancy import reset_current_company_id, set_current_company_id
from app.modules.inventory import service as inventory_service
from app.modules.inventory.router import router as inventory_router
from app.modules.ledger import posting as ledger_posting
from app.modules.ledger.router import router as ledger_router
from app.modules.masterdata.router import router as masterdata_router
from app.modules.receivables import events as receivables_events
from app.modules.receivables.router import router as receivables_router
from app.modules.sales import events as sales_events
from app.modules.sales import service as sales_service
from app.modules.sales.router import router as sales_router
from app.platform.permissions import service as permissions_service
from app.platform.permissions.context import (
    reset_current_session_id,
    set_current_session_id,
)
from app.platform.permissions.middleware import (
    MAX_CONCURRENT_LOGIN_VERIFICATIONS,
    LoginProtectionMiddleware,
    LoginRateLimiter,
)
from app.platform.permissions.router import router as permissions_router
from app.plugins import credit_limit

settings = get_settings()

_TIER1_PUBLIC_PATHS = frozenset({"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"})
_TIER1_ALLOWED_METHODS = frozenset({"GET", "HEAD"})


def _match_route(request: Request) -> APIRoute | None:
    for route in request.app.routes:
        if isinstance(route, APIRoute):
            match, _ = route.matches(request.scope)
            if match == Match.FULL:
                return route
    return None


def _route_is_public(route: APIRoute) -> bool:
    return any(isinstance(dep.dependency, Public) for dep in route.dependencies)


def _is_framework_doc_endpoint(endpoint: object) -> bool:
    module = getattr(endpoint, "__module__", "") or ""
    return module.startswith("fastapi")


def _classify_routes_or_crash(app: FastAPI) -> None:
    seen_tier1_paths: set[str] = set()

    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        endpoint = getattr(route, "endpoint", None)

        if path in _TIER1_PUBLIC_PATHS:
            if not _is_framework_doc_endpoint(endpoint):
                raise RuntimeError(
                    f"Custom route shadows Tier-1 documentation path {path!r} "
                    f"with endpoint {endpoint!r}. Application endpoints must not shadow "
                    "framework documentation routes."
                )
            if path in seen_tier1_paths:
                raise RuntimeError(
                    f"Duplicate/shadowed Tier-1 documentation route detected for path {path!r}."
                )
            seen_tier1_paths.add(path)

            unallowed = [m for m in methods if m not in _TIER1_ALLOWED_METHODS]
            if unallowed:
                raise RuntimeError(
                    f"Tier-1 public route {path} carries unallowed method(s) {unallowed} — "
                    "only GET/HEAD are permitted on documentation endpoints."
                )
            continue

        if not isinstance(route, APIRoute):
            raise RuntimeError(f"Unclassified non-APIRoute at startup: {route!r}")

        deps = [d.dependency for d in route.dependencies]
        markers = [d for d in deps if isinstance(d, Public | Authenticated | RequiresPermission)]
        if len(markers) != 1:
            raise RuntimeError(
                f"Route {route.path} {route.methods} carries {len(markers)} "
                "classification marker(s) (expected exactly 1: one of Public(), "
                "Authenticated(), or RequiresPermission(...)) — every route must be explicitly and "
                "unambiguously classified (ADR-009 Decision 7c)."
            )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _classify_routes_or_crash(app)
    app.state.login_rate_limiter = LoginRateLimiter()
    app.state.login_verification_semaphore = asyncio.Semaphore(MAX_CONCURRENT_LOGIN_VERIFICATIONS)
    yield


def create_app() -> FastAPI:
    application = FastAPI(
        title="mini-ERP",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.login_rate_limiter = LoginRateLimiter()
    application.state.login_verification_semaphore = asyncio.Semaphore(
        MAX_CONCURRENT_LOGIN_VERIFICATIONS
    )
    application.add_middleware(LoginProtectionMiddleware)
    return application


app = create_app()


@app.middleware("http")
async def authentication_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Resolve bearer token to actor context, session context, and active company context.

    Fail-closed: all non-public routes require a valid bearer token matching an
    active user session.
    """
    if (
        request.url.path in _TIER1_PUBLIC_PATHS
        and request.method in _TIER1_ALLOWED_METHODS
        and _match_route(request) is None
    ):
        return await call_next(request)

    route = _match_route(request)
    if route is not None and _route_is_public(route):
        return await call_next(request)

    auth_header = request.headers.get("authorization")
    parts = auth_header.split(maxsplit=1) if auth_header else []
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        return JSONResponse(
            {"detail": "Not authenticated."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    raw_token = parts[1].strip()

    session_factory = get_session_factory()
    async with session_factory() as db_session:
        try:
            resolved = await permissions_service.resolve_actor(db_session, raw_token)
        except permissions_service.InvalidSessionError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_401_UNAUTHORIZED)
        except permissions_service.UserDisabledError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_403_FORBIDDEN)

    actor_token = set_current_actor(resolved.actor_context)
    session_token = set_current_session_id(resolved.session_id)
    company_token = set_current_company_id(resolved.active_company_id)
    try:
        return await call_next(request)
    finally:
        reset_current_company_id(company_token)
        reset_current_session_id(session_token)
        reset_current_actor(actor_token)


@app.exception_handler(NotFoundError)
async def not_found_handler(_request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})


@app.exception_handler(TenancyContextError)
async def tenancy_context_handler(_request: Request, exc: TenancyContextError) -> JSONResponse:
    # Fail-closed at the DB layer surfaces here as 403: the caller made a
    # syntactically valid request but has no authorized tenant context.
    return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"detail": str(exc)})


@app.exception_handler(ActionDeniedError)
async def action_denied_handler(_request: Request, exc: ActionDeniedError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"detail": str(exc)})


@app.exception_handler(ConflictError)
async def conflict_handler(_request: Request, exc: ConflictError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})


@app.exception_handler(DomainValidationError)
async def domain_validation_handler(_request: Request, exc: DomainValidationError) -> JSONResponse:
    # 422, matching FastAPI's own request-body-validation status code: this
    # is the same "syntactically fine, semantically rejected" class of
    # error, just caught by a domain rule instead of the pydantic schema.
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
    )


@app.get("/health", tags=["ops"], dependencies=[Depends(Public())])
async def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(masterdata_router, prefix="/api/v1")
app.include_router(ledger_router, prefix="/api/v1")
app.include_router(sales_router, prefix="/api/v1")
app.include_router(inventory_router, prefix="/api/v1")
app.include_router(receivables_router, prefix="/api/v1")
# ADR-009 / WEEK9 Slice B: `/auth/*`, not under `/api/v1` — matches the ADR's
# own literal path references (grouped alongside `/health`/`/docs` in the
# Slice D public-route allowlist, not the business-module API surface).
app.include_router(permissions_router)


# ---------------------------------------------------------------------------
# Event bus wiring (ADR-004 §"Handler ordering" / ADR-003 Action Item 3)
#
# Registration happens once, at import time, module-level — same as router
# `include_router` above — so subscription order is deterministic (ADR-004:
# "handlers run in subscription order, which is deterministic because
# subscription happens once at app startup"). This is also the wiring the
# replay CLI (`app.cli.replay_outbox`) reuses by importing this module, so
# both the live API process and a replay run always see identical
# event_type -> schema/handler bindings.
#
# Week 3's `test.synthetic_sale` self-validation event is retired this week
# (ADR-007 "Synthetic event retirement") — its schema/handler are deleted
# from `ledger.posting` entirely, so there is nothing left to register or
# subscribe here; see migration 0005's R1 for how already-outboxed rows of
# that event_type are neutralized rather than left to poison replay.
#
# `sales.order_confirmed` is registered (so publish()/replay validate its
# schema and it gets an outbox row) but deliberately has NO subscriber
# (ADR-006 Decision 4) — nothing yet reacts to "confirmed" on its own; the
# reaction happens downstream, at `ship`.
# ---------------------------------------------------------------------------
events.register_event(
    sales_events.SALES_ORDER_CONFIRMED_EVENT_TYPE, sales_events.SalesOrderConfirmedPayload
)

# `sales.goods_shipped` (ADR-007): the first event through the bus with TWO
# subscribers. Subscription order is normative, not incidental (ADR-007
# Decision 1): inventory's deduction handler MUST run before ledger's
# posting handler — "move the goods, then account for them". Both run
# inside the same transaction `sales.service.ship_order` opened, so this
# ordering affects error attribution (which handler's exception is the one
# that aborts a bad `ship`), not atomicity — either order would still commit
# or roll back everything together. The payload schema
# (`ledger_posting.GoodsShippedPayload`) is owned by `ledger.posting`, not
# `sales.events` — see that module's docstring for why.
events.register_event(
    sales_events.SALES_GOODS_SHIPPED_EVENT_TYPE, ledger_posting.GoodsShippedPayload
)
events.subscribe(
    sales_events.SALES_GOODS_SHIPPED_EVENT_TYPE, inventory_service.handle_goods_shipped
)
events.subscribe(
    sales_events.SALES_GOODS_SHIPPED_EVENT_TYPE,
    ledger_posting.make_posting_handler(sales_events.SALES_GOODS_SHIPPED_EVENT_TYPE),
)

# ADR-008 (Week 6): receivables' four posting events. Each has exactly one
# subscriber — `ledger.posting`'s generic posting handler — mirroring
# `sales.goods_shipped`'s wiring shape (payload schema owned by
# `ledger.posting`, event_type string owned by the publisher).
events.register_event(
    receivables_events.RECEIVABLES_INVOICE_ISSUED_EVENT_TYPE, ledger_posting.InvoiceIssuedPayload
)
events.subscribe(
    receivables_events.RECEIVABLES_INVOICE_ISSUED_EVENT_TYPE,
    ledger_posting.make_posting_handler(receivables_events.RECEIVABLES_INVOICE_ISSUED_EVENT_TYPE),
)

events.register_event(
    receivables_events.RECEIVABLES_INVOICE_VOIDED_EVENT_TYPE, ledger_posting.InvoiceVoidedPayload
)
events.subscribe(
    receivables_events.RECEIVABLES_INVOICE_VOIDED_EVENT_TYPE,
    ledger_posting.make_posting_handler(receivables_events.RECEIVABLES_INVOICE_VOIDED_EVENT_TYPE),
)

events.register_event(
    receivables_events.RECEIVABLES_PAYMENT_RECEIVED_EVENT_TYPE,
    ledger_posting.PaymentReceivedPayload,
)
events.subscribe(
    receivables_events.RECEIVABLES_PAYMENT_RECEIVED_EVENT_TYPE,
    ledger_posting.make_posting_handler(receivables_events.RECEIVABLES_PAYMENT_RECEIVED_EVENT_TYPE),
)

events.register_event(
    receivables_events.RECEIVABLES_PAYMENT_VOIDED_EVENT_TYPE, ledger_posting.PaymentVoidedPayload
)
events.subscribe(
    receivables_events.RECEIVABLES_PAYMENT_VOIDED_EVENT_TYPE,
    ledger_posting.make_posting_handler(receivables_events.RECEIVABLES_PAYMENT_VOIDED_EVENT_TYPE),
)


# ---------------------------------------------------------------------------
# Hook registry wiring (Week 4, ADR-006 Decision 1/2)
#
# Same "once, at import time, module-level" discipline as the event bus
# wiring above. `sales` never imports `app.plugins` (import-linter
# contracts enforce this); this is the one place — the composition root,
# like `app.main` already is for routers/events — allowed to know both
# exist and wire them together.
# ---------------------------------------------------------------------------
hooks.register(sales_service.SALES_ORDER_VALIDATE_CONFIRM, credit_limit.check_credit_limit)
