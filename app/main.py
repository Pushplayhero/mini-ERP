"""FastAPI application entrypoint.

Wires together: settings, the tenancy middleware (§10.2), exception ->
HTTP-status mapping, module routers, the event bus's registration/
subscription wiring, and (Week 4) the hook registry's registration wiring.
Week 1 mounted `masterdata`; Week 2 added `ledger` (ADR-005); Week 3 added
the event bus + posting engine (ADR-003/ADR-004); Week 4 adds `sales`
(order lifecycle) plus the minimal hook registry and its one demonstration
plugin, `app.plugins.credit_limit` (ADR-006). `inventory` / `receivables`
are still empty shells and have no routers yet.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse

from app.core import events, hooks
from app.core.exceptions import (
    ConflictError,
    DomainValidationError,
    NotFoundError,
    TenancyContextError,
)
from app.core.settings import get_settings
from app.core.tenancy import reset_current_company_id, set_current_company_id
from app.modules.ledger import posting as ledger_posting
from app.modules.ledger.router import router as ledger_router
from app.modules.masterdata.router import router as masterdata_router
from app.modules.sales import events as sales_events
from app.modules.sales import service as sales_service
from app.modules.sales.router import router as sales_router
from app.plugins import credit_limit

settings = get_settings()

app = FastAPI(
    title="mini-erp",
    version="0.1.0",
    description=(
        "A minimal, correctness-obsessed open-source ERP kernel. "
        "Phase 1 / Week 2: masterdata + ledger modules, multi-company tenancy groundwork."
    ),
)


@app.middleware("http")
async def tenancy_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Bind the active company context from a trusted tenant header.

    Week 1 has no auth/RBAC (Phase 2 territory), so `X-Company-Id` is the
    documented stand-in for a verified JWT/session claim carrying the
    caller's active company — it is set by a trusted upstream, never by
    request *body* fields a client controls. If the header is absent or
    malformed, no context is bound and any endpoint touching tenant-scoped
    data will fail-closed via `TenancyContextError` (see app.core.db).
    """
    header_value = request.headers.get(settings.tenant_header_name)
    token = None
    if header_value:
        try:
            company_id = uuid.UUID(header_value)
        except ValueError:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"detail": f"Invalid {settings.tenant_header_name} header: not a UUID"},
            )
        token = set_current_company_id(company_id)
    try:
        return await call_next(request)
    finally:
        if token is not None:
            reset_current_company_id(token)


@app.exception_handler(NotFoundError)
async def not_found_handler(_request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})


@app.exception_handler(TenancyContextError)
async def tenancy_context_handler(_request: Request, exc: TenancyContextError) -> JSONResponse:
    # Fail-closed at the DB layer surfaces here as 403: the caller made a
    # syntactically valid request but has no authorized tenant context.
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


@app.get("/health", tags=["ops"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(masterdata_router, prefix="/api/v1")
app.include_router(ledger_router, prefix="/api/v1")
app.include_router(sales_router, prefix="/api/v1")


# ---------------------------------------------------------------------------
# Event bus wiring (Week 3, ADR-004 §"Handler ordering" / ADR-003 Action Item 3)
#
# Registration happens once, at import time, module-level — same as router
# `include_router` above — so subscription order is deterministic (ADR-004:
# "handlers run in subscription order, which is deterministic because
# subscription happens once at app startup"). This is also the wiring the
# replay CLI (`app.cli.replay_outbox`) reuses by importing this module, so
# both the live API process and a replay run always see identical
# event_type -> schema/handler bindings.
# ---------------------------------------------------------------------------
events.register_event(ledger_posting.SYNTHETIC_SALE_EVENT_TYPE, ledger_posting.SyntheticSalePayload)
events.subscribe(
    ledger_posting.SYNTHETIC_SALE_EVENT_TYPE,
    ledger_posting.make_posting_handler(ledger_posting.SYNTHETIC_SALE_EVENT_TYPE),
)

# `sales.order_confirmed` is registered (so publish()/replay validate its
# schema and it gets an outbox row) but deliberately has NO subscriber this
# week (ADR-006 Decision 4) — Week 4 orders stop at `confirmed`, there is no
# shipment/invoice pipeline yet for it to post against. This is the second
# proof, after the Week 3 synthetic event, that "registered with zero
# subscribers" is a valid, silent bus configuration.
events.register_event(
    sales_events.SALES_ORDER_CONFIRMED_EVENT_TYPE, sales_events.SalesOrderConfirmedPayload
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
