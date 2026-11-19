"""purchase and suppliers (ADR-014 / Week 14)

Revision ID: f8a1b2c3d4e5
Revises: e7f8a1b2c3d4
Create Date: 2026-11-18 00:00:00.000000

Creates:
- `suppliers` table (`masterdata` supplier masterdata)
- `purchase_sequences`, `purchase_orders`, `purchase_order_lines` tables
- 10 canonical permissions (`masterdata.supplier.*` x4, `purchase.order.*` x6) granted to `admin`
  (expanding permission catalog from 55 to 65)

Updates:
- `custom_field_definitions` entity_type CHECK constraint (adds `supplier`, `purchase_order`)
- `workflow_definitions` and `workflow_instances` entity_type/transition CHECK constraints
  (adds `('purchase_order', 'confirm')` and `('purchase_order', 'cancel')`)
- Guarded `ALTER TYPE stock_move_type ADD VALUE 'RECEIPT'` at end of `upgrade()`
  (per Migration 0005 R3, nothing after ADD VALUE in this migration references the new value).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f8a1b2c3d4e5"
down_revision: str | None = "e7f8a1b2c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS_CATALOG: list[dict[str, str]] = [
    {
        "code": "masterdata.supplier.create",
        "description": "Create suppliers in the active company.",
    },
    {
        "code": "masterdata.supplier.read",
        "description": "View suppliers in the active company.",
    },
    {
        "code": "masterdata.supplier.update",
        "description": "Update suppliers in the active company.",
    },
    {
        "code": "masterdata.supplier.delete",
        "description": "Delete suppliers in the active company.",
    },
    {
        "code": "purchase.order.create",
        "description": "Create draft purchase orders in the active company.",
    },
    {
        "code": "purchase.order.read",
        "description": "View purchase orders and lines in the active company.",
    },
    {
        "code": "purchase.order.update",
        "description": "Update draft purchase orders in the active company.",
    },
    {
        "code": "purchase.order.confirm",
        "description": "Confirm purchase orders in the active company.",
    },
    {
        "code": "purchase.order.cancel",
        "description": "Cancel draft or confirmed purchase orders in the active company.",
    },
    {
        "code": "purchase.order.receive",
        "description": "Receive goods on confirmed purchase orders in the active company.",
    },
]

_OLD_CF_ENTITY_TYPES_SQL = (
    "entity_type IN ("
    "'customer', 'product', 'account', 'accounting_period', "
    "'journal_entry', 'sales_order', 'invoice', 'payment'"
    ")"
)

_NEW_CF_ENTITY_TYPES_SQL = (
    "entity_type IN ("
    "'customer', 'supplier', 'product', 'account', 'accounting_period', "
    "'journal_entry', 'sales_order', 'purchase_order', 'invoice', 'payment'"
    ")"
)

_OLD_WF_TRANSITIONS_SQL = (
    "("
    "(entity_type = 'sales_order' AND transition IN ('confirm', 'cancel')) OR "
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
    "(entity_type = 'journal_entry' AND transition = 'reverse')"
    ")"
)


def upgrade() -> None:
    # 1. Seed 10 new permissions and grant all to admin (55 -> 65)
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

    # 2. suppliers (masterdata)
    op.create_table(
        "suppliers",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column(
            "payment_terms_days",
            sa.Integer(),
            server_default="30",
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
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
        sa.Column(
            "custom_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.CheckConstraint(
            "payment_terms_days BETWEEN 0 AND 365",
            name="ck_suppliers_payment_terms_days_range",
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["currency_code"], ["currencies.code"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "code", name="uq_suppliers_company_code"),
    )
    op.create_index(op.f("ix_suppliers_company_id"), "suppliers", ["company_id"], unique=False)

    # 3. purchase_sequences (PO numbering counter)
    op.create_table(
        "purchase_sequences",
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("next_no", sa.Integer(), server_default="1", nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("company_id", "year"),
    )

    # 4. purchase_orders
    op.create_table(
        "purchase_orders",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("order_no", sa.String(length=32), nullable=False),
        sa.Column("supplier_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "DRAFT",
                "CONFIRMED",
                "CANCELLED",
                "RECEIVED",
                name="purchase_order_status",
            ),
            server_default="DRAFT",
            nullable=False,
        ),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("total", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False),
        sa.Column("snapshot_supplier_code", sa.String(length=32), nullable=True),
        sa.Column("snapshot_supplier_name", sa.String(length=255), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column(
            "custom_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.CheckConstraint("total >= 0", name="ck_purchase_orders_total_nonneg"),
        sa.CheckConstraint(
            "status::text NOT IN ('CONFIRMED', 'RECEIVED') "
            "OR (snapshot_supplier_code IS NOT NULL AND snapshot_supplier_name IS NOT NULL)",
            name="ck_purchase_orders_confirmed_has_snapshot",
        ),
        sa.CheckConstraint(
            "status::text != 'RECEIVED' OR received_at IS NOT NULL",
            name="ck_purchase_orders_received_has_received_at",
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["currency_code"], ["currencies.code"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "order_no", name="uq_purchase_orders_company_order_no"),
    )
    op.create_index(
        op.f("ix_purchase_orders_company_id"), "purchase_orders", ["company_id"], unique=False
    )
    op.create_index(
        "ix_purchase_orders_supplier_id", "purchase_orders", ["supplier_id"], unique=False
    )

    # 5. purchase_order_lines
    op.create_table(
        "purchase_order_lines",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.UUID(), nullable=False),
        sa.Column("qty", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("uom_id", sa.UUID(), nullable=False),
        sa.Column("unit_cost", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column(
            "unit_cost_is_override",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("snapshot_sku", sa.String(length=64), nullable=True),
        sa.Column("snapshot_product_name", sa.String(length=255), nullable=True),
        sa.Column("amount", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("qty > 0", name="ck_purchase_order_lines_qty_positive"),
        sa.CheckConstraint("unit_cost >= 0", name="ck_purchase_order_lines_unit_cost_nonneg"),
        sa.CheckConstraint("amount >= 0", name="ck_purchase_order_lines_amount_nonneg"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["order_id"], ["purchase_orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["uom_id"], ["uom.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", "line_no", name="uq_purchase_order_lines_order_line_no"),
    )
    op.create_index(
        op.f("ix_purchase_order_lines_company_id"),
        "purchase_order_lines",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_order_lines_order_id",
        "purchase_order_lines",
        ["order_id"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_order_lines_product_id",
        "purchase_order_lines",
        ["product_id"],
        unique=False,
    )

    # 6. Extend customfields & workflow CHECK constraints to allow supplier & purchase_order
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

    # 7. Extend stock_move_type enum with RECEIPT (guarded; nothing below references 'RECEIPT')
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_enum
                JOIN pg_type ON pg_enum.enumtypid = pg_type.oid
                WHERE pg_type.typname = 'stock_move_type' AND pg_enum.enumlabel = 'RECEIPT'
            ) THEN
                ALTER TYPE stock_move_type ADD VALUE 'RECEIPT';
            END IF;
        END$$;
        """
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

    op.drop_index("ix_purchase_order_lines_product_id", table_name="purchase_order_lines")
    op.drop_index("ix_purchase_order_lines_order_id", table_name="purchase_order_lines")
    op.drop_index(op.f("ix_purchase_order_lines_company_id"), table_name="purchase_order_lines")
    op.drop_table("purchase_order_lines")

    op.drop_index("ix_purchase_orders_supplier_id", table_name="purchase_orders")
    op.drop_index(op.f("ix_purchase_orders_company_id"), table_name="purchase_orders")
    op.drop_table("purchase_orders")

    op.drop_table("purchase_sequences")
    sa.Enum(name="purchase_order_status").drop(op.get_bind(), checkfirst=True)

    op.drop_index(op.f("ix_suppliers_company_id"), table_name="suppliers")
    op.drop_table("suppliers")

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
