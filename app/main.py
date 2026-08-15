"""FastAPI application entrypoint.

Wires together: settings, the tenancy middleware (§10.2), exception ->
HTTP-status mapping, module routers, the event bus's registration/
subscription wiring, and the hook registry's registration wiring. Week 1
mounted `masterdata`; Week 2 added `ledger` (ADR-005); Week 3 added the
event bus + posting engine (ADR-003/ADR-004); Week 4 added `sales` (order
lifecycle) plus the minimal hook registry and its one demonstration plugin,
`app.plugins.credit_limit` (ADR-006); Week 5 added `inventory` (append-only
stock + summary) and wired `sales.goods_shipped` — the first event with two
subscribers — through both it and `ledger.posting` (ADR-007). Week 6 adds
`receivables` (invoicing, payment application, AR aging) and its four
posting events, closing the Phase 1 O2C line (ADR-008).
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
app.include_router(inventory_router, prefix="/api/v1")
app.include_router(receivables_router, prefix="/api/v1")


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
