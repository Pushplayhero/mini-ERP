"""CLI tool for managing tenant approval workflows and instances (ADR-012 Action Item 3).

Workflow definition and approval lifecycle operations are tenant-scoped administrative
operations governed by `platform.workflow.read`, `platform.workflow.manage`, and
`platform.workflow.approve` permissions. This CLI binds `WORKFLOW_ADMIN_ACTOR` with
all workflow and business transition permissions, calling into
`app.platform.workflow.service`, enforcing permissions, company locking, and
runtime business validation rules.

Entry points:
    `uv run python -m app.cli.manage_workflows list --company-code <CODE>`
        `[--entity-type <TYPE>] [--transition <TRANSITION>] [--active-only]`
    `uv run python -m app.cli.manage_workflows get --company-code <CODE> --definition-id <UUID>`
    `uv run python -m app.cli.manage_workflows define --company-code <CODE>`
        `--entity-type <TYPE> --transition <TRANSITION> --name <NAME>`
        `--steps '<JSON>' [--allow-self-approval] [--description <DESC>]`
    `uv run python -m app.cli.manage_workflows update --company-code <CODE> --definition-id <UUID>`
        `[--name <NAME>] [--description <DESC>] [--steps '<JSON>']`
        `[--allow-self-approval|--no-self-approval] [--active|--inactive]`
    `uv run python -m app.cli.manage_workflows deactivate --company-code <CODE>`
        `--definition-id <UUID>`
    `uv run python -m app.cli.manage_workflows list-instances --company-code <CODE>`
        `[--entity-type <TYPE>] [--entity-id <UUID>] [--transition <TRANSITION>]`
        `[--status <STATUS>]`
    `uv run python -m app.cli.manage_workflows get-instance --company-code <CODE>`
        `--instance-id <UUID>`
    `uv run python -m app.cli.manage_workflows submit --company-code <CODE>`
        `--entity-type <TYPE> --entity-id <UUID> --transition <TRANSITION> [--actor-id <UUID>]`
    `uv run python -m app.cli.manage_workflows approve --company-code <CODE> --instance-id <UUID>`
        `[--comment <STR>] [--actor-id <UUID>]`
    `uv run python -m app.cli.manage_workflows reject --company-code <CODE> --instance-id <UUID>`
        `[--comment <STR>] [--actor-id <UUID>]`
    `uv run python -m app.cli.manage_workflows cancel --company-code <CODE> --instance-id <UUID>`
        `[--actor-id <UUID>]`
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

# Side-effecting import: wires composition root, target resolver, and approval gate
from app import main as _app_main  # noqa: F401
from app.core.authorization import ActorContext, actor_context
from app.core.db import dispose_engine, get_session_factory
from app.core.exceptions import (
    ActionDeniedError,
    ConflictError,
    DomainValidationError,
    NotFoundError,
)
from app.core.tenancy import company_context
from app.core.workflow import (
    WorkflowEntityType,
    WorkflowInstanceStatus,
    WorkflowTransition,
)
from app.modules.masterdata import service as masterdata_service
from app.platform.workflow import service as workflow_service
from app.platform.workflow.schemas import (
    WorkflowDecisionInput,
    WorkflowDefinitionCreate,
    WorkflowDefinitionRead,
    WorkflowDefinitionUpdate,
    WorkflowInstanceRead,
    WorkflowInstanceSubmit,
    WorkflowStepInput,
)

logger = logging.getLogger("app.cli.manage_workflows")

WORKFLOW_ADMIN_ACTOR = ActorContext(
    user_id=uuid.UUID("00000000-0000-0000-0000-000000000007"),
    permission_codes=frozenset(
        {
            "platform.workflow.read",
            "platform.workflow.manage",
            "platform.workflow.approve",
            "sales.order.confirm",
            "sales.order.cancel",
            "receivables.invoice.void",
            "receivables.payment.void",
            "ledger.journal_entry.reverse",
        }
    ),
    is_system_actor=True,
)


def _validate_non_empty(value: str, *, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise DomainValidationError(f"{field_name} must not be empty.")
    return cleaned


def _parse_uuid(value: str | uuid.UUID, *, field_name: str = "id") -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value).strip())
    except (ValueError, AttributeError) as exc:
        raise DomainValidationError(f"Invalid UUID for {field_name}: {value!r}") from exc


async def _resolve_company_id(
    session: AsyncSession,
    *,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
) -> uuid.UUID:
    if company_code is None and company_id is None:
        raise DomainValidationError("Either company_code or company_id must be provided.")

    cleaned_code = (
        _validate_non_empty(company_code, field_name="Company code")
        if company_code is not None
        else None
    )
    if company_id is not None:
        company = await masterdata_service.load_company_unchecked(session, company_id)
        if cleaned_code is not None and company.code != cleaned_code:
            raise DomainValidationError(
                f"Company ID {company_id} has code {company.code!r}, which does not match "
                f"provided code {cleaned_code!r}."
            )
        return company.id

    assert cleaned_code is not None
    companies = await masterdata_service.list_companies_unchecked(session)
    for c in companies:
        if c.code == cleaned_code:
            return c.id
    raise NotFoundError("Company", cleaned_code)


def _get_actor(actor_id: uuid.UUID | str | None) -> ActorContext:
    if actor_id is None:
        return WORKFLOW_ADMIN_ACTOR
    parsed = _parse_uuid(actor_id, field_name="actor_id")
    return ActorContext(
        user_id=parsed,
        permission_codes=WORKFLOW_ADMIN_ACTOR.permission_codes,
        is_system_actor=True,
    )


# ---------------------------------------------------------------------------
# Definition Operations
# ---------------------------------------------------------------------------


async def list_definitions(
    *,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
    entity_type: str | WorkflowEntityType | None = None,
    transition: str | WorkflowTransition | None = None,
    active_only: bool = False,
    session: AsyncSession | None = None,
) -> list[WorkflowDefinitionRead]:
    """List workflow definitions for a company."""
    parsed_entity = entity_type.strip().lower() if isinstance(entity_type, str) else entity_type
    parsed_trans = transition.strip().lower() if isinstance(transition, str) else transition

    async def _run(s: AsyncSession) -> list[WorkflowDefinitionRead]:
        cid = await _resolve_company_id(s, company_code=company_code, company_id=company_id)
        with actor_context(WORKFLOW_ADMIN_ACTOR), company_context(cid):
            return await workflow_service.list_workflow_definitions(
                s,
                entity_type=parsed_entity,
                transition=parsed_trans,
                active_only=active_only,
            )

    if session is not None:
        return await _run(session)
    async with get_session_factory()() as db_session:
        return await _run(db_session)


async def get_definition(
    definition_id: uuid.UUID | str,
    *,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
    session: AsyncSession | None = None,
) -> WorkflowDefinitionRead:
    """Retrieve a single workflow definition by ID."""
    did = _parse_uuid(definition_id, field_name="definition_id")

    async def _run(s: AsyncSession) -> WorkflowDefinitionRead:
        cid = await _resolve_company_id(s, company_code=company_code, company_id=company_id)
        with actor_context(WORKFLOW_ADMIN_ACTOR), company_context(cid):
            return await workflow_service.get_workflow_definition(s, did)

    if session is not None:
        return await _run(session)
    async with get_session_factory()() as db_session:
        return await _run(db_session)


async def define_workflow(
    data: WorkflowDefinitionCreate | dict[str, Any],
    *,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
    session: AsyncSession | None = None,
) -> WorkflowDefinitionRead:
    """Create a new workflow definition for a company."""
    payload = (
        data
        if isinstance(data, WorkflowDefinitionCreate)
        else WorkflowDefinitionCreate.model_validate(data)
    )

    async def _run(s: AsyncSession) -> WorkflowDefinitionRead:
        cid = await _resolve_company_id(s, company_code=company_code, company_id=company_id)
        with actor_context(WORKFLOW_ADMIN_ACTOR), company_context(cid):
            return await workflow_service.create_workflow_definition(s, payload)

    if session is not None:
        return await _run(session)
    async with get_session_factory()() as db_session:
        return await _run(db_session)


async def update_workflow(
    definition_id: uuid.UUID | str,
    data: WorkflowDefinitionUpdate | dict[str, Any],
    *,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
    session: AsyncSession | None = None,
) -> WorkflowDefinitionRead:
    """Update an existing workflow definition."""
    did = _parse_uuid(definition_id, field_name="definition_id")
    payload = (
        data
        if isinstance(data, WorkflowDefinitionUpdate)
        else WorkflowDefinitionUpdate.model_validate(data)
    )

    async def _run(s: AsyncSession) -> WorkflowDefinitionRead:
        cid = await _resolve_company_id(s, company_code=company_code, company_id=company_id)
        with actor_context(WORKFLOW_ADMIN_ACTOR), company_context(cid):
            return await workflow_service.update_workflow_definition(s, did, payload)

    if session is not None:
        return await _run(session)
    async with get_session_factory()() as db_session:
        return await _run(db_session)


async def deactivate_workflow(
    definition_id: uuid.UUID | str,
    *,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
    session: AsyncSession | None = None,
) -> WorkflowDefinitionRead:
    """Deactivate (soft delete) a workflow definition."""
    did = _parse_uuid(definition_id, field_name="definition_id")

    async def _run(s: AsyncSession) -> WorkflowDefinitionRead:
        cid = await _resolve_company_id(s, company_code=company_code, company_id=company_id)
        with actor_context(WORKFLOW_ADMIN_ACTOR), company_context(cid):
            return await workflow_service.deactivate_workflow_definition(s, did)

    if session is not None:
        return await _run(session)
    async with get_session_factory()() as db_session:
        return await _run(db_session)


# ---------------------------------------------------------------------------
# Instance Operations
# ---------------------------------------------------------------------------


async def list_instances(
    *,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
    entity_type: str | WorkflowEntityType | None = None,
    entity_id: uuid.UUID | str | None = None,
    transition: str | WorkflowTransition | None = None,
    status: str | WorkflowInstanceStatus | None = None,
    session: AsyncSession | None = None,
) -> list[WorkflowInstanceRead]:
    """List workflow instances for a company."""
    parsed_entity = entity_type.strip().lower() if isinstance(entity_type, str) else entity_type
    parsed_trans = transition.strip().lower() if isinstance(transition, str) else transition
    parsed_status = status.strip().upper() if isinstance(status, str) else status
    parsed_eid = _parse_uuid(entity_id, field_name="entity_id") if entity_id else None

    async def _run(s: AsyncSession) -> list[WorkflowInstanceRead]:
        cid = await _resolve_company_id(s, company_code=company_code, company_id=company_id)
        with actor_context(WORKFLOW_ADMIN_ACTOR), company_context(cid):
            return await workflow_service.list_workflow_instances(
                s,
                entity_type=parsed_entity,
                entity_id=parsed_eid,
                transition=parsed_trans,
                status=parsed_status,
            )

    if session is not None:
        return await _run(session)
    async with get_session_factory()() as db_session:
        return await _run(db_session)


async def get_instance(
    instance_id: uuid.UUID | str,
    *,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
    session: AsyncSession | None = None,
) -> WorkflowInstanceRead:
    """Retrieve a single workflow instance by ID."""
    iid = _parse_uuid(instance_id, field_name="instance_id")

    async def _run(s: AsyncSession) -> WorkflowInstanceRead:
        cid = await _resolve_company_id(s, company_code=company_code, company_id=company_id)
        with actor_context(WORKFLOW_ADMIN_ACTOR), company_context(cid):
            return await workflow_service.get_workflow_instance(s, iid)

    if session is not None:
        return await _run(session)
    async with get_session_factory()() as db_session:
        return await _run(db_session)


async def submit_instance(
    data: WorkflowInstanceSubmit | dict[str, Any],
    *,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | str | None = None,
    session: AsyncSession | None = None,
) -> WorkflowInstanceRead:
    """Submit a document for approval."""
    payload = (
        data
        if isinstance(data, WorkflowInstanceSubmit)
        else WorkflowInstanceSubmit.model_validate(data)
    )
    effective_actor = _get_actor(actor_id)

    async def _run(s: AsyncSession) -> WorkflowInstanceRead:
        cid = await _resolve_company_id(s, company_code=company_code, company_id=company_id)
        with actor_context(effective_actor), company_context(cid):
            return await workflow_service.submit_workflow_instance(s, payload)

    if session is not None:
        return await _run(session)
    async with get_session_factory()() as db_session:
        return await _run(db_session)


async def approve_instance(
    instance_id: uuid.UUID | str,
    *,
    comment: str | None = None,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | str | None = None,
    session: AsyncSession | None = None,
) -> WorkflowInstanceRead:
    """Approve current step of a workflow instance."""
    iid = _parse_uuid(instance_id, field_name="instance_id")
    effective_actor = _get_actor(actor_id)
    decision_input = WorkflowDecisionInput(comment=comment) if comment else None

    async def _run(s: AsyncSession) -> WorkflowInstanceRead:
        cid = await _resolve_company_id(s, company_code=company_code, company_id=company_id)
        with actor_context(effective_actor), company_context(cid):
            return await workflow_service.approve_workflow_instance(s, iid, decision_input)

    if session is not None:
        return await _run(session)
    async with get_session_factory()() as db_session:
        return await _run(db_session)


async def reject_instance(
    instance_id: uuid.UUID | str,
    *,
    comment: str | None = None,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | str | None = None,
    session: AsyncSession | None = None,
) -> WorkflowInstanceRead:
    """Reject a workflow instance at current step."""
    iid = _parse_uuid(instance_id, field_name="instance_id")
    effective_actor = _get_actor(actor_id)
    decision_input = WorkflowDecisionInput(comment=comment) if comment else None

    async def _run(s: AsyncSession) -> WorkflowInstanceRead:
        cid = await _resolve_company_id(s, company_code=company_code, company_id=company_id)
        with actor_context(effective_actor), company_context(cid):
            return await workflow_service.reject_workflow_instance(s, iid, decision_input)

    if session is not None:
        return await _run(session)
    async with get_session_factory()() as db_session:
        return await _run(db_session)


async def cancel_instance(
    instance_id: uuid.UUID | str,
    *,
    company_code: str | None = None,
    company_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | str | None = None,
    session: AsyncSession | None = None,
) -> WorkflowInstanceRead:
    """Cancel a PENDING or unconsumed APPROVED workflow instance."""
    iid = _parse_uuid(instance_id, field_name="instance_id")
    effective_actor = _get_actor(actor_id)

    async def _run(s: AsyncSession) -> WorkflowInstanceRead:
        cid = await _resolve_company_id(s, company_code=company_code, company_id=company_id)
        with actor_context(effective_actor), company_context(cid):
            return await workflow_service.cancel_workflow_instance(s, iid)

    if session is not None:
        return await _run(session)
    async with get_session_factory()() as db_session:
        return await _run(db_session)


# ---------------------------------------------------------------------------
# CLI Argument Parser & Handlers
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.manage_workflows",
        description="Tenant Approval Workflow Administrative CLI Tool",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # 1. list
    p_list = subparsers.add_parser("list", help="List workflow definitions for a company")
    p_list.add_argument("--company-code", type=str, help="Target company code")
    p_list.add_argument("--company-id", type=uuid.UUID, help="Target company ID")
    p_list.add_argument(
        "--entity-type",
        type=str,
        choices=[e.value for e in WorkflowEntityType],
        help="Filter by business entity type",
    )
    p_list.add_argument(
        "--transition",
        type=str,
        choices=[t.value for t in WorkflowTransition],
        help="Filter by transition",
    )
    p_list.add_argument(
        "--active-only",
        action="store_true",
        help="Filter active definitions only",
    )

    # 2. get
    p_get = subparsers.add_parser("get", help="Retrieve details of a single workflow definition")
    p_get.add_argument("--company-code", type=str, help="Target company code")
    p_get.add_argument("--company-id", type=uuid.UUID, help="Target company ID")
    p_get.add_argument("--definition-id", type=str, required=True, help="Workflow definition UUID")

    # 3. define
    p_def = subparsers.add_parser("define", help="Define a new workflow for a company")
    p_def.add_argument("--company-code", type=str, help="Target company code")
    p_def.add_argument("--company-id", type=uuid.UUID, help="Target company ID")
    p_def.add_argument(
        "--entity-type",
        type=str,
        required=True,
        choices=[e.value for e in WorkflowEntityType],
        help="Target business entity type",
    )
    p_def.add_argument(
        "--transition",
        type=str,
        required=True,
        choices=[t.value for t in WorkflowTransition],
        help="Target state transition",
    )
    p_def.add_argument("--name", type=str, required=True, help="Descriptive workflow name")
    p_def.add_argument("--description", type=str, help="Optional description")
    p_def.add_argument(
        "--allow-self-approval",
        action="store_true",
        help="Permit requester to approve own submission",
    )
    p_def.add_argument(
        "--steps",
        type=str,
        required=True,
        help="JSON array of step definitions",
    )

    # 4. update
    p_upd = subparsers.add_parser("update", help="Update an existing workflow definition")
    p_upd.add_argument("--company-code", type=str, help="Target company code")
    p_upd.add_argument("--company-id", type=uuid.UUID, help="Target company ID")
    p_upd.add_argument("--definition-id", type=str, required=True, help="Workflow definition UUID")
    p_upd.add_argument("--name", type=str, help="Updated workflow name")
    p_upd.add_argument("--description", type=str, help="Updated description")
    p_upd.add_argument("--steps", type=str, help="Updated JSON array of steps")

    self_group = p_upd.add_mutually_exclusive_group()
    self_group.add_argument(
        "--allow-self-approval", dest="allow_self_approval", action="store_true", default=None
    )
    self_group.add_argument(
        "--no-self-approval", dest="allow_self_approval", action="store_false", default=None
    )

    act_group = p_upd.add_mutually_exclusive_group()
    act_group.add_argument("--active", dest="is_active", action="store_true", default=None)
    act_group.add_argument("--inactive", dest="is_active", action="store_false", default=None)

    # 5. deactivate
    p_deact = subparsers.add_parser("deactivate", help="Deactivate a workflow definition")
    p_deact.add_argument("--company-code", type=str, help="Target company code")
    p_deact.add_argument("--company-id", type=uuid.UUID, help="Target company ID")
    p_deact.add_argument(
        "--definition-id", type=str, required=True, help="Workflow definition UUID"
    )

    # 6. list-instances
    p_linst = subparsers.add_parser("list-instances", help="List workflow instances")
    p_linst.add_argument("--company-code", type=str, help="Target company code")
    p_linst.add_argument("--company-id", type=uuid.UUID, help="Target company ID")
    p_linst.add_argument(
        "--entity-type",
        type=str,
        choices=[e.value for e in WorkflowEntityType],
        help="Filter by business entity type",
    )
    p_linst.add_argument("--entity-id", type=uuid.UUID, help="Filter by target entity UUID")
    p_linst.add_argument(
        "--transition",
        type=str,
        choices=[t.value for t in WorkflowTransition],
        help="Filter by transition",
    )
    p_linst.add_argument(
        "--status",
        type=str,
        choices=[s.value for s in WorkflowInstanceStatus],
        help="Filter by lifecycle status",
    )

    # 7. get-instance
    p_ginst = subparsers.add_parser("get-instance", help="Retrieve details of a workflow instance")
    p_ginst.add_argument("--company-code", type=str, help="Target company code")
    p_ginst.add_argument("--company-id", type=uuid.UUID, help="Target company ID")
    p_ginst.add_argument("--instance-id", type=str, required=True, help="Workflow instance UUID")

    # 8. submit
    p_sub = subparsers.add_parser("submit", help="Submit a target entity for workflow approval")
    p_sub.add_argument("--company-code", type=str, help="Target company code")
    p_sub.add_argument("--company-id", type=uuid.UUID, help="Target company ID")
    p_sub.add_argument(
        "--entity-type",
        type=str,
        required=True,
        choices=[e.value for e in WorkflowEntityType],
        help="Target business entity type",
    )
    p_sub.add_argument("--entity-id", type=uuid.UUID, required=True, help="Target entity UUID")
    p_sub.add_argument(
        "--transition",
        type=str,
        required=True,
        choices=[t.value for t in WorkflowTransition],
        help="Target state transition",
    )
    p_sub.add_argument("--actor-id", type=uuid.UUID, help="Submitting actor UUID (for SoD testing)")

    # 9. approve
    p_app = subparsers.add_parser("approve", help="Approve current step of a workflow instance")
    p_app.add_argument("--company-code", type=str, help="Target company code")
    p_app.add_argument("--company-id", type=uuid.UUID, help="Target company ID")
    p_app.add_argument("--instance-id", type=str, required=True, help="Workflow instance UUID")
    p_app.add_argument("--comment", type=str, help="Approval comment")
    p_app.add_argument("--actor-id", type=uuid.UUID, help="Approving actor UUID")

    # 10. reject
    p_rej = subparsers.add_parser("reject", help="Reject a workflow instance at current step")
    p_rej.add_argument("--company-code", type=str, help="Target company code")
    p_rej.add_argument("--company-id", type=uuid.UUID, help="Target company ID")
    p_rej.add_argument("--instance-id", type=str, required=True, help="Workflow instance UUID")
    p_rej.add_argument("--comment", type=str, help="Rejection comment")
    p_rej.add_argument("--actor-id", type=uuid.UUID, help="Rejecting actor UUID")

    # 11. cancel
    p_can = subparsers.add_parser("cancel", help="Cancel a pending or approved workflow instance")
    p_can.add_argument("--company-code", type=str, help="Target company code")
    p_can.add_argument("--company-id", type=uuid.UUID, help="Target company ID")
    p_can.add_argument("--instance-id", type=str, required=True, help="Workflow instance UUID")
    p_can.add_argument("--actor-id", type=uuid.UUID, help="Cancelling actor UUID")

    return parser


def _parse_json_steps(raw: str) -> list[WorkflowStepInput]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DomainValidationError(f"Invalid JSON for --steps: {exc}") from exc
    if not isinstance(data, list):
        raise DomainValidationError("--steps must be a JSON array of objects.")
    return [WorkflowStepInput.model_validate(item) for item in data]


async def _handle_list(args: argparse.Namespace) -> None:
    items = await list_definitions(
        company_code=args.company_code,
        company_id=args.company_id,
        entity_type=args.entity_type,
        transition=args.transition,
        active_only=args.active_only,
    )
    print(f"Workflow Definitions ({len(items)}):")
    for item in items:
        act_str = "ACTIVE" if item.is_active else "INACTIVE"
        sod_str = "SELF_APPROVED" if item.allow_self_approval else "SOD_ENFORCED"
        print(
            f"  - [{item.id}] {item.entity_type}.{item.transition} [{act_str}|{sod_str}]: "
            f"name={item.name!r} steps={len(item.steps)}"
        )


async def _handle_get(args: argparse.Namespace) -> None:
    res = await get_definition(
        args.definition_id,
        company_code=args.company_code,
        company_id=args.company_id,
    )
    act_str = "ACTIVE" if res.is_active else "INACTIVE"
    print(
        f"Definition ID: {res.id}\n"
        f"Entity: {res.entity_type}\n"
        f"Transition: {res.transition}\n"
        f"Name: {res.name}\n"
        f"Status: {act_str}\n"
        f"Allow Self Approval: {res.allow_self_approval}\n"
        f"Description: {res.description}\n"
        f"Steps ({len(res.steps)}):"
    )
    for s in res.steps:
        print(
            f"  Step {s.step_order}: {s.step_name} (min {s.min_amount}) -> {s.required_permission}"
        )


async def _handle_define(args: argparse.Namespace) -> None:
    steps = _parse_json_steps(args.steps)
    payload = WorkflowDefinitionCreate(
        entity_type=args.entity_type,
        transition=args.transition,
        name=args.name,
        description=args.description,
        allow_self_approval=args.allow_self_approval,
        steps=steps,
    )
    res = await define_workflow(
        payload,
        company_code=args.company_code,
        company_id=args.company_id,
    )
    print(f"Defined workflow '{res.name}' (ID: {res.id}) for {res.entity_type}.{res.transition}")


async def _handle_update(args: argparse.Namespace) -> None:
    update_kwargs: dict[str, Any] = {}
    if args.name is not None:
        update_kwargs["name"] = args.name
    if args.description is not None:
        update_kwargs["description"] = args.description
    if args.allow_self_approval is not None:
        update_kwargs["allow_self_approval"] = args.allow_self_approval
    if args.is_active is not None:
        update_kwargs["is_active"] = args.is_active
    if args.steps is not None:
        update_kwargs["steps"] = _parse_json_steps(args.steps)

    payload = WorkflowDefinitionUpdate(**update_kwargs)
    res = await update_workflow(
        args.definition_id,
        payload,
        company_code=args.company_code,
        company_id=args.company_id,
    )
    print(f"Updated workflow definition '{res.name}' (ID: {res.id})")


async def _handle_deactivate(args: argparse.Namespace) -> None:
    res = await deactivate_workflow(
        args.definition_id,
        company_code=args.company_code,
        company_id=args.company_id,
    )
    print(f"Deactivated workflow definition '{res.name}' (ID: {res.id})")


async def _handle_list_instances(args: argparse.Namespace) -> None:
    items = await list_instances(
        company_code=args.company_code,
        company_id=args.company_id,
        entity_type=args.entity_type,
        entity_id=args.entity_id,
        transition=args.transition,
        status=args.status,
    )
    print(f"Workflow Instances ({len(items)}):")
    for item in items:
        step_str = f"step={item.current_step_order}" if item.current_step_order else "no_step"
        print(
            f"  - [{item.id}] {item.entity_type}:{item.entity_id} {item.transition} "
            f"[{item.status}|{step_str}]: amount={item.target_amount} by={item.requested_by}"
        )


async def _handle_get_instance(args: argparse.Namespace) -> None:
    res = await get_instance(
        args.instance_id,
        company_code=args.company_code,
        company_id=args.company_id,
    )
    print(
        f"Instance ID: {res.id}\n"
        f"Entity: {res.entity_type} {res.entity_id}\n"
        f"Transition: {res.transition}\n"
        f"Status: {res.status}\n"
        f"Target Amount: {res.target_amount}\n"
        f"Current Step: {res.current_step_order}\n"
        f"Requested By: {res.requested_by}\n"
        f"Steps Snapshot: {len(res.steps_snapshot)} steps\n"
        f"Logs ({len(res.logs)}):"
    )
    for lg in res.logs:
        print(f"  - Step {lg.step_order} [{lg.decision}] by {lg.actor_id}: comment={lg.comment!r}")


async def _handle_submit(args: argparse.Namespace) -> None:
    payload = WorkflowInstanceSubmit(
        entity_type=args.entity_type,
        entity_id=args.entity_id,
        transition=args.transition,
    )
    res = await submit_instance(
        payload,
        company_code=args.company_code,
        company_id=args.company_id,
        actor_id=args.actor_id,
    )
    print(
        f"Submitted instance {res.id} for {res.entity_type} {res.entity_id} "
        f"{res.transition} (Status: {res.status}, Amount: {res.target_amount})"
    )


async def _handle_approve(args: argparse.Namespace) -> None:
    res = await approve_instance(
        args.instance_id,
        comment=args.comment,
        company_code=args.company_code,
        company_id=args.company_id,
        actor_id=args.actor_id,
    )
    print(f"Approved instance {res.id} (Status: {res.status}, Step: {res.current_step_order})")


async def _handle_reject(args: argparse.Namespace) -> None:
    res = await reject_instance(
        args.instance_id,
        comment=args.comment,
        company_code=args.company_code,
        company_id=args.company_id,
        actor_id=args.actor_id,
    )
    print(f"Rejected instance {res.id} (Status: {res.status})")


async def _handle_cancel(args: argparse.Namespace) -> None:
    res = await cancel_instance(
        args.instance_id,
        company_code=args.company_code,
        company_id=args.company_id,
        actor_id=args.actor_id,
    )
    print(f"Cancelled instance {res.id} (Status: {res.status})")


async def _main(argv: list[str] | None = None, *, dispose: bool = True) -> int:
    args_list = sys.argv[1:] if argv is None else argv
    parser = _build_parser()
    try:
        args = parser.parse_args(args_list)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1

    handlers = {
        "list": _handle_list,
        "get": _handle_get,
        "define": _handle_define,
        "update": _handle_update,
        "deactivate": _handle_deactivate,
        "list-instances": _handle_list_instances,
        "get-instance": _handle_get_instance,
        "submit": _handle_submit,
        "approve": _handle_approve,
        "reject": _handle_reject,
        "cancel": _handle_cancel,
    }

    try:
        handler = handlers.get(args.subcommand)
        if handler is None:
            print(f"Unknown subcommand: {args.subcommand}", file=sys.stderr)
            return 1
        await handler(args)
    except (
        ConflictError,
        DomainValidationError,
        ValidationError,
        NotFoundError,
        ActionDeniedError,
        ValueError,
    ) as exc:
        print(f"manage_workflows error: {exc}", file=sys.stderr)
        return 1
    finally:
        if dispose:
            await dispose_engine()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
