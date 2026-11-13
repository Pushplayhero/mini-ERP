"""CLI tool for platform integrations, webhooks, and batch data — ADR-013 / Week 13 Slice C.

Subcommands:
- webhooks: list, add, rotate-secret, delete
- deliveries: list, retry
- batch: import, export

Usage examples:
    uv run python -m app.cli.manage_integrations webhooks list --company-code MAIN
    uv run python -m app.cli.manage_integrations webhooks add --company-code MAIN ...
    uv run python -m app.cli.manage_integrations batch import --company-code MAIN ...
    uv run python -m app.cli.manage_integrations batch export --company-code MAIN ...
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import main as _app_main  # noqa: F401
from app.core.authorization import ActorContext, actor_context
from app.core.db import dispose_engine, get_session_factory
from app.core.integration import BatchDataFormat, BatchEntityType
from app.core.tenancy import company_context
from app.modules.masterdata.models import Company
from app.platform.integration import batch, service
from app.platform.integration.schemas import (
    WebhookEndpointCreate,
)

INTEGRATION_CLI_ACTOR = ActorContext(
    user_id=uuid.UUID("00000000-0000-0000-0000-000000000015"),
    permission_codes=frozenset(
        {
            "platform.integration.manage",
            "platform.integration.read",
            "platform.integration.import",
        }
    ),
    is_system_actor=True,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage_integrations",
        description="Manage tenant webhooks, delivery history, and batch import/export",
    )
    subparsers = parser.add_subparsers(dest="group", required=True)

    # 1. webhooks group
    wh_parser = subparsers.add_parser("webhooks", help="Manage webhook endpoints")
    wh_subs = wh_parser.add_subparsers(dest="action", required=True)

    wh_list = wh_subs.add_parser("list", help="List webhook endpoints")
    wh_list.add_argument("--company-code", required=True, help="Target company code")
    wh_list.add_argument("--active-only", action="store_true", help="Filter active endpoints only")

    wh_add = wh_subs.add_parser("add", help="Add a new webhook endpoint")
    wh_add.add_argument("--company-code", required=True, help="Target company code")
    wh_add.add_argument("--name", required=True, help="Endpoint identifier name")
    wh_add.add_argument("--url", required=True, help="Destination target URL")
    wh_add.add_argument(
        "--events",
        default="",
        help="Comma-separated event types (empty or '*' for all)",
    )
    wh_add.add_argument(
        "--timeout",
        type=Decimal,
        default=Decimal("10.00"),
        help="Timeout in seconds",
    )
    wh_add.add_argument(
        "--max-attempts",
        type=int,
        default=5,
        help="Maximum retry attempts",
    )

    wh_rot = wh_subs.add_parser("rotate-secret", help="Rotate signing secret")
    wh_rot.add_argument("--company-code", required=True, help="Target company code")
    wh_rot.add_argument("--endpoint-id", type=uuid.UUID, required=True, help="Endpoint UUID")

    wh_del = wh_subs.add_parser("delete", help="Soft-deactivate a webhook endpoint")
    wh_del.add_argument("--company-code", required=True, help="Target company code")
    wh_del.add_argument("--endpoint-id", type=uuid.UUID, required=True, help="Endpoint UUID")

    # 2. deliveries group
    deliv_parser = subparsers.add_parser("deliveries", help="Inspect and retry webhook deliveries")
    deliv_subs = deliv_parser.add_subparsers(dest="action", required=True)

    deliv_list = deliv_subs.add_parser("list", help="List delivery history")
    deliv_list.add_argument("--company-code", required=True, help="Target company code")
    deliv_list.add_argument("--endpoint-id", type=uuid.UUID, default=None)
    deliv_list.add_argument("--status", default=None)
    deliv_list.add_argument("--limit", type=int, default=50)

    deliv_retry = deliv_subs.add_parser("retry", help="Retry a dead delivery")
    deliv_retry.add_argument("--company-code", required=True, help="Target company code")
    deliv_retry.add_argument("--delivery-id", type=uuid.UUID, required=True)

    # 3. batch group
    batch_parser = subparsers.add_parser("batch", help="Batch import and export")
    batch_subs = batch_parser.add_subparsers(dest="action", required=True)

    b_imp = batch_subs.add_parser("import", help="Import entity records from file")
    b_imp.add_argument("--company-code", required=True, help="Target company code")
    b_imp.add_argument(
        "--entity",
        required=True,
        type=BatchEntityType,
        choices=list(BatchEntityType),
        help="Entity type to import",
    )
    b_imp.add_argument("--file", required=True, help="Input data file path")
    b_imp.add_argument(
        "--format",
        type=BatchDataFormat,
        default=BatchDataFormat.CSV,
        choices=list(BatchDataFormat),
    )
    b_imp.add_argument("--dry-run", action="store_true", help="Validate without persisting")

    b_exp = batch_subs.add_parser("export", help="Export entity records to file")
    b_exp.add_argument("--company-code", required=True, help="Target company code")
    b_exp.add_argument(
        "--entity",
        required=True,
        type=BatchEntityType,
        choices=list(BatchEntityType),
        help="Entity type to export",
    )
    b_exp.add_argument("--output", required=True, help="Output file path")
    b_exp.add_argument(
        "--format",
        type=BatchDataFormat,
        default=BatchDataFormat.CSV,
        choices=list(BatchDataFormat),
    )
    b_exp.add_argument("--include-bom", action="store_true", help="Include UTF-8 BOM in CSV")

    return parser


async def _handle_webhooks(session: AsyncSession, args: argparse.Namespace) -> int:
    if args.action == "list":
        is_active = True if args.active_only else None
        endpoints = await service.list_webhook_endpoints(session, is_active=is_active)
        for ep in endpoints:
            print(
                f"ID: {ep.id} | Name: {ep.name} | Active: {ep.is_active} | "
                f"URL: {ep.url} | Secret Prefix: {ep.secret_prefix}"
            )
        return 0

    if args.action == "add":
        event_types = (
            [e.strip() for e in args.events.split(",") if e.strip()] if args.events else []
        )
        create_in = WebhookEndpointCreate(
            name=args.name,
            url=args.url,
            event_types=event_types,
            timeout_seconds=args.timeout,
            max_attempts=args.max_attempts,
        )
        ep, secret = await service.create_webhook_endpoint(session, create_in)
        print(f"Endpoint created successfully: {ep.id}")
        print(f"Signing Secret (SAVE NOW): {secret}")
        return 0

    if args.action == "rotate-secret":
        ep, secret = await service.rotate_webhook_endpoint_secret(session, args.endpoint_id)
        print(f"Secret rotated for endpoint: {ep.id}")
        print(f"New Signing Secret (SAVE NOW): {secret}")
        return 0

    if args.action == "delete":
        await service.deactivate_webhook_endpoint(session, args.endpoint_id)
        print(f"Deactivated endpoint: {args.endpoint_id}")
        return 0

    return 1


async def _handle_deliveries(session: AsyncSession, args: argparse.Namespace) -> int:
    if args.action == "list":
        deliveries = await service.list_webhook_deliveries(
            session,
            endpoint_id=args.endpoint_id,
            status=args.status,
            limit=args.limit,
        )
        for d in deliveries:
            print(
                f"ID: {d.id} | Status: {d.status} | Attempts: {d.attempts}/{d.max_attempts} | "
                f"Next: {d.next_attempt_at} | Error: {d.last_error}"
            )
        return 0

    if args.action == "retry":
        retried = await service.retry_webhook_delivery(session, args.delivery_id)
        print(f"Delivery {retried.id} requeued to status: {retried.status}")
        return 0

    return 1


async def _handle_batch(session: AsyncSession, args: argparse.Namespace) -> int:
    if args.action == "import":
        path = Path(args.file)
        if not path.is_file():
            print(f"Error: File not found: {path}", file=sys.stderr)
            return 1
        content = path.read_text(encoding="utf-8")
        job, errors = await batch.import_batch_data(
            session,
            args.entity,
            content,
            data_format=args.format,
            dry_run=args.dry_run,
        )
        print(
            f"Import Job {job.id} Status: {job.status} (Dry: {job.dry_run}) | "
            f"Total: {job.total_rows}, Succeeded: {job.succeeded_rows}, Failed: {job.failed_rows}"
        )
        if errors:
            print(f"Errors encountered ({len(errors)}):")
            for err in errors[:10]:
                print(f"  Row {err['row']}: {err['error']}")
        return 0 if job.status in ("COMPLETED", "DRY_RUN") else 1

    if args.action == "export":
        content, job = await batch.export_batch_data(
            session,
            args.entity,
            data_format=args.format,
            include_bom=args.include_bom,
        )
        out_path = Path(args.output)
        out_path.write_text(content, encoding="utf-8")
        print(
            f"Export Job {job.id} Status: {job.status} | "
            f"Exported {job.total_rows} rows to {out_path}"
        )
        return 0

    return 1


async def _amain(argv: list[str] | None = None, *, dispose: bool = True) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    session_factory = get_session_factory()
    try:
        async with session_factory() as session:
            company = await session.scalar(select(Company).where(Company.code == args.company_code))
            if company is None:
                print(f"Error: Company '{args.company_code}' not found", file=sys.stderr)
                return 1

            with company_context(company.id), actor_context(INTEGRATION_CLI_ACTOR):
                if args.group == "webhooks":
                    return await _handle_webhooks(session, args)
                if args.group == "deliveries":
                    return await _handle_deliveries(session, args)
                if args.group == "batch":
                    return await _handle_batch(session, args)

        return 0
    finally:
        if dispose:
            await dispose_engine()


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
