"""In-process synchronous event bus + outbox durability (ADR-004).

Mirrors `app.core.tenancy`'s style deliberately: module-level private state
(`_schemas`, `_handlers`) plus module-level functions, rather than a class
instance callers would need to import/construct — `app.main` (and, per
ADR-004's own pseudocode, any future publisher module) just does
`from app.core import events` and calls `events.register_event(...)`,
`events.subscribe(...)`, `await events.publish(...)`.

Three normative rules from ADR-004 (Decision 1/2/3, Consensus Revisions R1-R3):

1. **Synchronous, in-process, same-transaction dispatch.** `publish` runs
   every subscribed handler inline, in the caller's DB session/transaction.
   Any handler exception propagates unchanged (fail-closed) — this module
   never catches a handler's exception on the publisher's behalf.
2. **Every publish also writes one row to `outbox`**, in the same
   transaction, before handlers run. The insert goes through a bare
   `sqlalchemy.table()` Core reference (`OUTBOX_TABLE`), not the
   `app.modules.masterdata.models.OutboxEvent` ORM class — `app.core` must
   never import `app.modules.*` (CI-enforced import-linter contract), and
   this is the exact "Core-level reference to shared schema" pattern
   `ledger.service` already uses for `accounts` (see that module's
   docstring for the full rationale).
3. **Event contracts are `event_type` strings + a registered pydantic
   schema** — publishers/consumers never import each other's Python
   modules; `register_event` enforces R2's mandatory `company_id: UUID`
   field so replay (see `app.cli.replay_outbox`) always has a tenant to
   bind a context to, and `publish` cross-checks that field against the
   active request's tenancy context so a handler can never silently act on
   the wrong company's behalf.

`redispatch` (R1) is the replay CLI's entry point: it invokes the
registered handlers directly, *without* touching `outbox` again — going
through `publish` during replay would re-insert every replayed row and make
the queue self-propagate forever.

`unregister`/`reset` (ADR-006 R3, added Week 4): the original Week 3
`_schemas`/`_handlers` dicts were append-only, which `tests/core/test_events.py`
dodged by giving every test a uniquely-named `event_type` rather than ever
cleaning up after itself — a latent test-isolation gap the ADR-006 review
flagged. Both additions are purely additive: no existing behavior of
`register_event`/`subscribe`/`publish`/`redispatch` changes, so the Week 3
test suite passes unmodified against this file.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import column as sa_column
from sqlalchemy import insert
from sqlalchemy import table as sa_table
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.core.tenancy import require_current_company_id

logger = logging.getLogger(__name__)

EventHandler = Callable[[AsyncSession, BaseModel], Awaitable[None]]


class EventBusError(AppError):
    """Base class for all `app.core.events` usage errors."""


class EventSchemaError(EventBusError):
    """`register_event` was given a schema that violates ADR-004 R2.

    Every registered event schema must declare a `company_id: uuid.UUID`
    field — without it, replay (which has no HTTP request to derive a
    tenant from) and the publish-time company-context check both have
    nothing to bind to, and a handler touching tenant-scoped data would hit
    the fail-closed `TenancyContextError` the moment it ran outside a
    request.
    """


class UnknownEventTypeError(EventBusError):
    """`publish`/`redispatch` referenced an `event_type` with no registered schema.

    ADR-004 Decision 3: unregistered-event publishes must fail loudly, never
    silently no-op.
    """


class EventPayloadValidationError(EventBusError):
    """A publish/replay payload failed validation against its registered schema."""


class EventCompanyMismatchError(EventBusError):
    """Payload `company_id` disagrees with the active tenancy context (ADR-004 R2).

    This is a safety net against a handler acting on the wrong company's
    data by accident — a mismatch here is always a bug in the caller, never
    a legitimate state, so it raises rather than silently trusting one side.
    """


# ---------------------------------------------------------------------------
# Registry (module-level state, mirroring app.core.tenancy's ContextVar style)
# ---------------------------------------------------------------------------

_schemas: dict[str, type[BaseModel]] = {}
_handlers: dict[str, list[EventHandler]] = {}

# Lightweight Core reference to the `outbox` table (created by migration
# 0001 / `app.modules.masterdata.models.OutboxEvent`) — see module docstring
# point 2 for why this is a `sqlalchemy.table()` binding and not an ORM
# import.
OUTBOX_TABLE = sa_table(
    "outbox",
    sa_column("id", PGUUID(as_uuid=True)),
    sa_column("event_type"),
    sa_column("payload", JSONB),
    sa_column("occurred_at"),
    sa_column("dispatched_at"),
    sa_column("attempts"),
)


def register_event(event_type: str, schema: type[BaseModel]) -> None:
    """Register the pydantic schema that validates `event_type`'s payloads.

    Raises `EventSchemaError` (ADR-004 R2) if `schema` has no
    `company_id: uuid.UUID` field — checked eagerly, at registration time
    (typically module import / app startup), not at first publish.
    """
    field = schema.model_fields.get("company_id")
    if field is None or field.annotation is not uuid.UUID:
        raise EventSchemaError(
            f"event schema for {event_type!r} must declare a `company_id: uuid.UUID` "
            "field (ADR-004 R2) — every registered event needs one so replay (no HTTP "
            "request, no context to derive a tenant from) and the publish-time "
            "company-context check both have something to bind to"
        )
    _schemas[event_type] = schema


def subscribe(event_type: str, handler: EventHandler) -> None:
    """Register `handler` to run (in subscription order) whenever `event_type` is dispatched.

    Normative per ADR-004: handlers run synchronously, inline, in the
    dispatching transaction — this privilege is documented policy as
    restricted to consistency-critical handlers (posting), not a general
    pub/sub mechanism for arbitrary side effects.
    """
    _handlers.setdefault(event_type, []).append(handler)


def unregister(event_type: str, handler: EventHandler) -> None:
    """Remove `handler` from `event_type`'s subscriber list (ADR-006 R3).

    Silently no-ops if `event_type` has no subscribers at all, or if
    `handler` was never subscribed to it — mirrors `list.remove` guarded
    into `set.discard`-style "removing something already absent is not an
    error" semantics rather than raising. A caller that unregisters
    something twice, or unregisters a handler it never registered, almost
    certainly wants "make sure this is not listening" as its postcondition;
    that postcondition already holds, so raising would only punish a
    defensive caller (e.g. a test's teardown running after the test body
    already unregistered explicitly). See `app.core.hooks.unregister` for
    the same choice, made once and documented in both places.
    """
    handlers = _handlers.get(event_type)
    if handlers is None:
        return
    with suppress(ValueError):
        handlers.remove(handler)


def reset() -> None:
    """Clear every registered schema and subscriber (test-only, ADR-006 R3).

    Not called by any application code path — `app.main` registers schemas/
    subscribers once at import time and that wiring is meant to live for the
    process's whole lifetime. Exists so tests can start from a guaranteed-
    empty registry instead of relying on unique `event_type` naming to avoid
    collisions (Week 3's `tests/core/test_events.py` still uses the naming
    convention and is unaffected by this addition; new tests may use either
    approach). Callers that use this in a shared test session must be
    careful not to wipe out real process-level registrations another test
    depends on — see `tests/core/test_events.py`'s reset-test fixture for
    the save/restore pattern that avoids that trap.
    """
    _schemas.clear()
    _handlers.clear()


def _validate_payload(event_type: str, payload: dict[str, Any]) -> BaseModel:
    schema = _schemas.get(event_type)
    if schema is None:
        raise UnknownEventTypeError(
            f"event_type {event_type!r} is not registered — call register_event() at "
            "startup before publishing or replaying it (ADR-004 Decision 3: unregistered "
            "events fail loudly, never silently no-op)"
        )
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        raise EventPayloadValidationError(
            f"payload for event_type {event_type!r} failed schema validation: {exc}"
        ) from exc


async def _dispatch(session: AsyncSession, event_type: str, payload_model: BaseModel) -> None:
    """Call every subscriber of `event_type`, in subscription order.

    Any handler exception propagates unchanged — ADR-004 Decision 1
    (fail-closed): the caller's whole transaction aborts, no partial
    dispatch is ever swallowed here.
    """
    for handler in _handlers.get(event_type, []):
        await handler(session, payload_model)


async def publish(session: AsyncSession, event_type: str, payload: dict[str, Any]) -> None:
    """Validate, durably record, and synchronously dispatch one event.

    1. `event_type` must be registered (else `UnknownEventTypeError`).
    2. `payload` must validate against the registered schema (else
       `EventPayloadValidationError`).
    3. `payload["company_id"]` must equal the active tenancy context (else
       `EventCompanyMismatchError` — ADR-004 R2's consistency check).
    4. One row is inserted into `outbox`, in `session`'s transaction —
       **not committed here**; the caller owns the transaction boundary
       (same "whoever opened it" rule as ADR-003 R1's `post_journal_entry`).
    5. Every subscribed handler runs, in order, via `session`. A handler
       exception propagates unchanged (Decision 1: fail-closed).
    """
    payload_model = _validate_payload(event_type, payload)

    payload_company_id = payload_model.company_id  # type: ignore[attr-defined]
    active_company_id = require_current_company_id()
    if payload_company_id != active_company_id:
        raise EventCompanyMismatchError(
            f"event {event_type!r} payload company_id {payload_company_id} does not match "
            f"the active tenancy context {active_company_id} — refusing to publish an event "
            "that disagrees with the request's own tenant (ADR-004 R2)"
        )

    await session.execute(
        insert(OUTBOX_TABLE).values(
            id=uuid.uuid4(),
            event_type=event_type,
            payload=payload_model.model_dump(mode="json"),
        )
    )

    await _dispatch(session, event_type, payload_model)


async def redispatch(session: AsyncSession, event_type: str, payload: dict[str, Any]) -> None:
    """Replay-only entry point (ADR-004 R1): dispatch to handlers, skip `outbox`.

    Identical validation to `publish`, minus the outbox write and the
    tenancy-context mismatch check (the replay CLI is responsible for
    binding the correct company context, via
    `app.core.tenancy.company_context(payload["company_id"])`, *before*
    calling this — see `app.cli.replay_outbox`). Never call this from
    request-handling code; use `publish` there so the event is durably
    recorded.
    """
    payload_model = _validate_payload(event_type, payload)
    await _dispatch(session, event_type, payload_model)
