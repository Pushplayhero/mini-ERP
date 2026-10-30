"""platform_permissions_full_catalog (ADR-009 / WEEK9 Slice D)

Adds 44 endpoint-covering permission codes across masterdata, sales, ledger,
inventory, and receivables modules, and grants all 44 to the `admin` role
(the 45th route permission, `session.manage`, was seeded in migration 0009).

Revision ID: e7b8c9d0a1f2
Revises: f19b7d4e2c85
Create Date: 2026-08-22 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7b8c9d0a1f2"
down_revision: str | None = "f19b7d4e2c85"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS_CATALOG: list[dict[str, str]] = [
    {"code": "masterdata.company.create", "description": "Create companies"},
    {"code": "masterdata.company.read", "description": "Read and list companies"},
    {"code": "masterdata.customer.create", "description": "Create customer accounts"},
    {"code": "masterdata.customer.read", "description": "Read and list customer accounts"},
    {"code": "masterdata.customer.update", "description": "Update customer accounts"},
    {"code": "masterdata.customer.delete", "description": "Delete customer accounts"},
    {"code": "masterdata.product.create", "description": "Create products"},
    {"code": "masterdata.product.read", "description": "Read and list products"},
    {"code": "masterdata.product.update", "description": "Update products"},
    {"code": "masterdata.product.delete", "description": "Delete products"},
    {"code": "masterdata.account.create", "description": "Create chart of accounts"},
    {"code": "masterdata.account.read", "description": "Read and list chart of accounts"},
    {"code": "masterdata.account.update", "description": "Update chart of accounts"},
    {"code": "masterdata.account.delete", "description": "Delete chart of accounts"},
    {"code": "masterdata.uom.create", "description": "Create units of measure"},
    {
        "code": "masterdata.uom_conversion.create",
        "description": "Create unit of measure conversions",
    },
    {
        "code": "masterdata.uom_conversion.read",
        "description": "Read and list unit of measure conversions",
    },
    {"code": "masterdata.currency.create", "description": "Create currencies"},
    {"code": "masterdata.exchange_rate.create", "description": "Create exchange rates"},
    {"code": "masterdata.exchange_rate.read", "description": "Read and list exchange rates"},
    {"code": "sales.order.create", "description": "Create sales orders"},
    {"code": "sales.order.read", "description": "Read and list sales orders"},
    {"code": "sales.order.update", "description": "Update draft sales orders"},
    {"code": "sales.order.confirm", "description": "Confirm sales orders"},
    {"code": "sales.order.cancel", "description": "Cancel sales orders"},
    {"code": "sales.order.ship", "description": "Ship confirmed sales orders"},
    {"code": "ledger.period.create", "description": "Create fiscal periods"},
    {"code": "ledger.period.read", "description": "Read and list fiscal periods"},
    {"code": "ledger.period.close", "description": "Close fiscal periods"},
    {"code": "ledger.journal_entry.create", "description": "Create journal entries"},
    {"code": "ledger.journal_entry.read", "description": "Read and list journal entries"},
    {"code": "ledger.journal_entry.reverse", "description": "Reverse journal entries"},
    {"code": "ledger.report.read", "description": "Read financial reports and trial balance"},
    {"code": "inventory.adjustment.create", "description": "Create inventory stock adjustments"},
    {"code": "inventory.summary.read", "description": "Read inventory stock summary"},
    {"code": "inventory.move.read", "description": "Read inventory stock moves"},
    {"code": "receivables.invoice.create", "description": "Create and issue invoices"},
    {"code": "receivables.invoice.read", "description": "Read and list invoices"},
    {"code": "receivables.invoice.void", "description": "Void open invoices"},
    {"code": "receivables.payment.create", "description": "Create customer payments"},
    {"code": "receivables.payment.read", "description": "Read and list customer payments"},
    {"code": "receivables.payment.void", "description": "Void customer payments"},
    {"code": "receivables.payment.allocate", "description": "Allocate payments against invoices"},
    {"code": "receivables.report.read", "description": "Read accounts receivable aging reports"},
]


def upgrade() -> None:
    permissions_table = sa.table(
        "permissions",
        sa.column("code", sa.String),
        sa.column("description", sa.String),
    )
    role_permissions_table = sa.table(
        "role_permissions",
        sa.column("role_code", sa.String),
        sa.column("permission_code", sa.String),
    )

    op.bulk_insert(permissions_table, PERMISSIONS_CATALOG)
    role_grants = [
        {"role_code": "admin", "permission_code": item["code"]} for item in PERMISSIONS_CATALOG
    ]
    op.bulk_insert(role_permissions_table, role_grants)


def downgrade() -> None:
    role_permissions_table = sa.table(
        "role_permissions",
        sa.column("role_code", sa.String),
        sa.column("permission_code", sa.String),
    )
    permissions_table = sa.table(
        "permissions",
        sa.column("code", sa.String),
    )

    codes = [item["code"] for item in PERMISSIONS_CATALOG]
    op.execute(
        role_permissions_table.delete().where(role_permissions_table.c.permission_code.in_(codes))
    )
    op.execute(permissions_table.delete().where(permissions_table.c.code.in_(codes)))
