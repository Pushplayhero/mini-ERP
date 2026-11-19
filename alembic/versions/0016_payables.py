"""payables: vendor bills, outgoing payments, allocations, AP aging (ADR-015 / Week 15)

Revision ID: a9b1c2d3e4f5
Revises: f8a1b2c3d4e5
Create Date: 2026-11-19 00:00:00.000000

Creates:
- `vendor_bill_status` (`OPEN`, `PARTIAL`, `PAID`, `VOIDED`) & `vendor_payment_status`
  (`ISSUED`, `VOIDED`) native PostgreSQL enums
- `payables_sequences`, `vendor_bills`, `vendor_payments`,
  `vendor_payment_allocation_commands`, `vendor_payment_allocations`
- 8 canonical permissions (`payables.bill.*` x3, `payables.payment.*` x4,
  `payables.report.read`) granted to `admin` (expanding permission catalog from 65 to 73)

Updates:
- `custom_field_definitions` entity_type CHECK constraint (adds `vendor_bill`, `vendor_payment`)
- `workflow_definitions` and `workflow_instances` entity_type/transition CHECK constraints
  (adds `('vendor_bill', 'void')` and `('vendor_payment', 'void')`)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a9b1c2d3e4f5"
down_revision: str | None = "f8a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS_CATALOG: list[dict[str, str]] = [
    {
        "code": "payables.bill.create",
        "description": "Issue vendor bills from received purchase orders in the active company.",
    },
    {
        "code": "payables.bill.read",
        "description": "View vendor bills in the active company.",
    },
    {
        "code": "payables.bill.void",
        "description": "Void unsettled vendor bills in the active company.",
    },
    {
        "code": "payables.payment.create",
        "description": "Record outgoing vendor payments in the active company.",
    },
    {
        "code": "payables.payment.read",
        "description": "View outgoing vendor payments in the active company.",
    },
    {
        "code": "payables.payment.void",
        "description": "Void unallocated vendor payments in the active company.",
    },
    {
        "code": "payables.payment.allocate",
        "description": "Allocate vendor payments to vendor bills in the active company.",
    },
    {
        "code": "payables.report.read",
        "description": "Read AP aging and payables reports in the active company.",
    },
]

_OLD_CF_ENTITY_TYPES_SQL = (
    "entity_type IN ("
    "'customer', 'supplier', 'product', 'account', 'accounting_period', "
    "'journal_entry', 'sales_order', 'purchase_order', 'invoice', 'payment'"
    ")"
)

_NEW_CF_ENTITY_TYPES_SQL = (
    "entity_type IN ("
    "'customer', 'supplier', 'product', 'account', 'accounting_period', "
    "'journal_entry', 'sales_order', 'purchase_order', 'invoice', 'payment', "
    "'vendor_bill', 'vendor_payment'"
    ")"
)

_OLD_WF_TRANSITIONS_SQL = (
    "("
    "(entity_type = 'sales_order' AND transition IN ('confirm', 'cancel')) OR "
    "(entity_type = 'purchase_order' AND transition IN ('confirm', 'cancel')) OR "
    "(entity_type = 'invoice' AND transition = 'void') OR "
    "(entity_type = 'payment' AND transition = 'void') OR "
    "(entity_type = 'journal_entry' AND transition = 'reverse')"
    ")"
)

_NEW_WF_TRANSITIONS_SQL = (
    "("
    "(entity_type = 'sales_order' AND transition IN ('confirm', 'cancel')) OR "
    "(entity_type = 'purchase_order' AND transition IN ('confirm', 'cancel')) OR "
    "(entity_type = 'invoice' AND transition = 'void') OR "
    "(entity_type = 'payment' AND transition = 'void') OR "
    "(entity_type = 'vendor_bill' AND transition = 'void') OR "
    "(entity_type = 'vendor_payment' AND transition = 'void') OR "
    "(entity_type = 'journal_entry' AND transition = 'reverse')"
    ")"
)


def upgrade() -> None:
    # 1. Seed 8 new permissions and grant all to admin (65 -> 73)
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

    # 2. payables_sequences
    op.create_table(
        "payables_sequences",
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("doc_type", sa.String(length=16), nullable=False),
        sa.Column("next_no", sa.Integer(), server_default="1", nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("company_id", "year", "doc_type"),
    )

    # 3. vendor_bills
    op.create_table(
        "vendor_bills",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("bill_no", sa.String(length=32), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=False),
        sa.Column("supplier_id", sa.UUID(), nullable=False),
        sa.Column("supplier_invoice_no", sa.String(length=128), nullable=True),
        sa.Column(
            "status",
            sa.Enum("OPEN", "PARTIAL", "PAID", "VOIDED", name="vendor_bill_status"),
            nullable=False,
        ),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("order_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bill_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("total", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column(
            "settled_amount", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False
        ),
        sa.Column("snapshot_supplier_code", sa.String(length=32), nullable=False),
        sa.Column("snapshot_supplier_name", sa.String(length=255), nullable=False),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "custom_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.CheckConstraint("total > 0", name="ck_vendor_bills_total_positive"),
        sa.CheckConstraint(
            "settled_amount >= 0 AND settled_amount <= total",
            name="ck_vendor_bills_settled_amount_bounds",
        ),
        sa.CheckConstraint(
            "(status = 'OPEN' AND settled_amount = 0) "
            "OR (status = 'PARTIAL' AND settled_amount > 0 AND settled_amount < total) "
            "OR (status = 'PAID' AND settled_amount = total) "
            "OR (status = 'VOIDED' AND settled_amount = 0)",
            name="ck_vendor_bills_status_settled_amount_consistency",
        ),
        sa.CheckConstraint(
            "(status = 'VOIDED') = (voided_at IS NOT NULL)",
            name="ck_vendor_bills_voided_at_consistency",
        ),
        sa.CheckConstraint(
            "due_date >= bill_date", name="ck_vendor_bills_due_date_after_bill_date"
        ),
        sa.CheckConstraint(
            "bill_date >= (order_received_at AT TIME ZONE 'UTC')::date",
            name="ck_vendor_bills_bill_date_after_receipt",
        ),
        sa.CheckConstraint(
            "supplier_invoice_no IS NULL OR length(btrim(supplier_invoice_no)) > 0",
            name="ck_vendor_bills_invoice_no_nonempty",
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["order_id"], ["purchase_orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["currency_code"], ["currencies.code"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "bill_no", name="uq_vendor_bills_company_bill_no"),
    )
    op.create_index(
        op.f("ix_vendor_bills_company_id"), "vendor_bills", ["company_id"], unique=False
    )
    op.create_index("ix_vendor_bills_supplier_id", "vendor_bills", ["supplier_id"], unique=False)
    op.create_index(
        "uq_vendor_bills_order_live",
        "vendor_bills",
        ["company_id", "order_id"],
        unique=True,
        postgresql_where=sa.text("status != 'VOIDED'"),
    )
    op.create_index(
        "uq_vendor_bills_supplier_invoice_no_live",
        "vendor_bills",
        ["company_id", "supplier_id", "supplier_invoice_no"],
        unique=True,
        postgresql_where=sa.text("status != 'VOIDED' AND supplier_invoice_no IS NOT NULL"),
    )

    # 4. vendor_payments
    op.create_table(
        "vendor_payments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("payment_no", sa.String(length=32), nullable=False),
        sa.Column("supplier_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("ISSUED", "VOIDED", name="vendor_payment_status"),
            nullable=False,
        ),
        sa.Column("external_ref", sa.String(length=128), nullable=False),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column(
            "allocated_amount",
            sa.Numeric(precision=20, scale=6),
            server_default="0",
            nullable=False,
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "custom_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.CheckConstraint("amount > 0", name="ck_vendor_payments_amount_positive"),
        sa.CheckConstraint(
            "allocated_amount >= 0 AND allocated_amount <= amount",
            name="ck_vendor_payments_allocated_amount_bounds",
        ),
        sa.CheckConstraint(
            "status != 'VOIDED' OR allocated_amount = 0",
            name="ck_vendor_payments_voided_zero_allocated",
        ),
        sa.CheckConstraint(
            "(status = 'VOIDED') = (voided_at IS NOT NULL)",
            name="ck_vendor_payments_voided_at_consistency",
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["currency_code"], ["currencies.code"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id", "payment_no", name="uq_vendor_payments_company_payment_no"
        ),
        sa.UniqueConstraint(
            "company_id", "external_ref", name="uq_vendor_payments_company_external_ref"
        ),
    )
    op.create_index(
        op.f("ix_vendor_payments_company_id"), "vendor_payments", ["company_id"], unique=False
    )
    op.create_index(
        "ix_vendor_payments_supplier_id", "vendor_payments", ["supplier_id"], unique=False
    )

    # 5. vendor_payment_allocation_commands
    op.create_table(
        "vendor_payment_allocation_commands",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("payment_id", sa.UUID(), nullable=False),
        sa.Column("request_ref", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["payment_id"], ["vendor_payments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id",
            "payment_id",
            "request_ref",
            name="uq_vendor_payment_alloc_cmds_payment_request_ref",
        ),
    )
    op.create_index(
        op.f("ix_vendor_payment_allocation_commands_company_id"),
        "vendor_payment_allocation_commands",
        ["company_id"],
        unique=False,
    )

    # 6. vendor_payment_allocations (append-only)
    op.create_table(
        "vendor_payment_allocations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("payment_id", sa.UUID(), nullable=False),
        sa.Column("bill_id", sa.UUID(), nullable=False),
        sa.Column("command_id", sa.UUID(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name="ck_vendor_payment_allocations_amount_positive"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["payment_id"], ["vendor_payments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["bill_id"], ["vendor_bills.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["command_id"], ["vendor_payment_allocation_commands.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_vendor_payment_allocations_company_id"),
        "vendor_payment_allocations",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_vendor_payment_allocations_payment_id",
        "vendor_payment_allocations",
        ["payment_id"],
        unique=False,
    )
    op.create_index(
        "ix_vendor_payment_allocations_bill_id",
        "vendor_payment_allocations",
        ["bill_id"],
        unique=False,
    )

    # 7. Extend customfields & workflow CHECK constraints to allow vendor_bill & vendor_payment
    op.drop_constraint(
        "ck_custom_field_entity_type_valid", "custom_field_definitions", type_="check"
    )
    op.create_check_constraint(
        "ck_custom_field_entity_type_valid",
        "custom_field_definitions",
        _NEW_CF_ENTITY_TYPES_SQL,
    )

    op.drop_constraint(
        "ck_workflow_def_entity_transition_valid", "workflow_definitions", type_="check"
    )
    op.create_check_constraint(
        "ck_workflow_def_entity_transition_valid",
        "workflow_definitions",
        _NEW_WF_TRANSITIONS_SQL,
    )

    op.drop_constraint(
        "ck_workflow_instance_entity_transition_valid", "workflow_instances", type_="check"
    )
    op.create_check_constraint(
        "ck_workflow_instance_entity_transition_valid",
        "workflow_instances",
        _NEW_WF_TRANSITIONS_SQL,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_workflow_instance_entity_transition_valid", "workflow_instances", type_="check"
    )
    op.create_check_constraint(
        "ck_workflow_instance_entity_transition_valid",
        "workflow_instances",
        _OLD_WF_TRANSITIONS_SQL,
    )

    op.drop_constraint(
        "ck_workflow_def_entity_transition_valid", "workflow_definitions", type_="check"
    )
    op.create_check_constraint(
        "ck_workflow_def_entity_transition_valid",
        "workflow_definitions",
        _OLD_WF_TRANSITIONS_SQL,
    )

    op.drop_constraint(
        "ck_custom_field_entity_type_valid", "custom_field_definitions", type_="check"
    )
    op.create_check_constraint(
        "ck_custom_field_entity_type_valid",
        "custom_field_definitions",
        _OLD_CF_ENTITY_TYPES_SQL,
    )

    op.drop_index("ix_vendor_payment_allocations_bill_id", table_name="vendor_payment_allocations")
    op.drop_index(
        "ix_vendor_payment_allocations_payment_id", table_name="vendor_payment_allocations"
    )
    op.drop_index(
        op.f("ix_vendor_payment_allocations_company_id"), table_name="vendor_payment_allocations"
    )
    op.drop_table("vendor_payment_allocations")

    op.drop_index(
        op.f("ix_vendor_payment_allocation_commands_company_id"),
        table_name="vendor_payment_allocation_commands",
    )
    op.drop_table("vendor_payment_allocation_commands")

    op.drop_index("ix_vendor_payments_supplier_id", table_name="vendor_payments")
    op.drop_index(op.f("ix_vendor_payments_company_id"), table_name="vendor_payments")
    op.drop_table("vendor_payments")
    sa.Enum(name="vendor_payment_status").drop(op.get_bind(), checkfirst=True)

    op.drop_index("uq_vendor_bills_supplier_invoice_no_live", table_name="vendor_bills")
    op.drop_index("uq_vendor_bills_order_live", table_name="vendor_bills")
    op.drop_index("ix_vendor_bills_supplier_id", table_name="vendor_bills")
    op.drop_index(op.f("ix_vendor_bills_company_id"), table_name="vendor_bills")
    op.drop_table("vendor_bills")
    sa.Enum(name="vendor_bill_status").drop(op.get_bind(), checkfirst=True)

    op.drop_table("payables_sequences")

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
