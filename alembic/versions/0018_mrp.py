"""mrp: multi-level BOM, work orders, WIP tracking, and MRP net requirements (ADR-017 / Week 17)

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-11-24 00:00:00.000000

Creates:
- `work_order_status` (`DRAFT`, `CONFIRMED`, `IN_PROGRESS`, `COMPLETED`, `CANCELLED`) and
  `work_order_tx_type` (`MATERIAL_ISSUE`, `MATERIAL_RETURN`, `PRODUCTION_RECEIPT`) native enums
- `mrp_sequences`, `boms`, `bom_lines`, `work_orders`, `work_order_lines`, `work_order_transactions`
- 11 canonical permissions (`mrp.bom.{create,read,update}`,
  `mrp.work_order.{create,read,update,confirm,cancel,issue,receive}`, `mrp.plan.read`)
  granted to `admin` (expanding permission catalog from 79 to 90)

Updates:
- `custom_field_definitions` entity_type CHECK constraint (adds `bom`, `work_order`)
- `workflow_definitions` and `workflow_instances` entity_type/transition CHECK constraints
  (adds `('work_order', 'confirm')`, `('work_order', 'cancel')`)
- Guarded `ALTER TYPE stock_move_type ADD VALUE` & `ALTER TYPE cost_move_type ADD VALUE`
  for `PRODUCTION_ISSUE`, `PRODUCTION_RETURN`, `PRODUCTION_RECEIPT` at end of `upgrade()`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c2d3e4f5a6b7"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS_CATALOG: list[dict[str, str]] = [
    {
        "code": "mrp.bom.create",
        "description": "Create bills of materials in the active company.",
    },
    {
        "code": "mrp.bom.read",
        "description": "View bills of materials and multi-level explosions in the active company.",
    },
    {
        "code": "mrp.bom.update",
        "description": "Update bills of materials in the active company.",
    },
    {
        "code": "mrp.work_order.create",
        "description": "Create draft work orders in the active company.",
    },
    {
        "code": "mrp.work_order.read",
        "description": "View work orders, lines, and transactions in the active company.",
    },
    {
        "code": "mrp.work_order.update",
        "description": "Update draft work orders in the active company.",
    },
    {
        "code": "mrp.work_order.confirm",
        "description": "Confirm work orders in the active company.",
    },
    {
        "code": "mrp.work_order.cancel",
        "description": "Cancel work orders without posted shop-floor activity.",
    },
    {
        "code": "mrp.work_order.issue",
        "description": "Issue and return materials on confirmed/in-progress work orders.",
    },
    {
        "code": "mrp.work_order.receive",
        "description": "Receive finished goods from confirmed/in-progress work orders.",
    },
    {
        "code": "mrp.plan.read",
        "description": "Run and view MRP net-requirements planning in the active company.",
    },
]

_OLD_CF_ENTITY_TYPES_SQL = (
    "entity_type IN ("
    "'customer', 'supplier', 'product', 'account', 'accounting_period', "
    "'journal_entry', 'sales_order', 'purchase_order', 'invoice', 'payment', "
    "'vendor_bill', 'vendor_payment', 'cost_adjustment'"
    ")"
)

_NEW_CF_ENTITY_TYPES_SQL = (
    "entity_type IN ("
    "'customer', 'supplier', 'product', 'account', 'accounting_period', "
    "'journal_entry', 'sales_order', 'purchase_order', 'invoice', 'payment', "
    "'vendor_bill', 'vendor_payment', 'cost_adjustment', 'bom', 'work_order'"
    ")"
)

_OLD_WF_TRANSITIONS_SQL = (
    "("
    "(entity_type = 'sales_order' AND transition IN ('confirm', 'cancel')) OR "
    "(entity_type = 'purchase_order' AND transition IN ('confirm', 'cancel')) OR "
    "(entity_type = 'invoice' AND transition = 'void') OR "
    "(entity_type = 'payment' AND transition = 'void') OR "
    "(entity_type = 'vendor_bill' AND transition = 'void') OR "
    "(entity_type = 'vendor_payment' AND transition = 'void') OR "
    "(entity_type = 'cost_adjustment' AND transition = 'void') OR "
    "(entity_type = 'journal_entry' AND transition = 'reverse')"
    ")"
)

_NEW_WF_TRANSITIONS_SQL = (
    "("
    "(entity_type = 'sales_order' AND transition IN ('confirm', 'cancel')) OR "
    "(entity_type = 'purchase_order' AND transition IN ('confirm', 'cancel')) OR "
    "(entity_type = 'work_order' AND transition IN ('confirm', 'cancel')) OR "
    "(entity_type = 'invoice' AND transition = 'void') OR "
    "(entity_type = 'payment' AND transition = 'void') OR "
    "(entity_type = 'vendor_bill' AND transition = 'void') OR "
    "(entity_type = 'vendor_payment' AND transition = 'void') OR "
    "(entity_type = 'cost_adjustment' AND transition = 'void') OR "
    "(entity_type = 'journal_entry' AND transition = 'reverse')"
    ")"
)


def _guarded_add_enum_value(enum_name: str, value: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_enum
                JOIN pg_type ON pg_enum.enumtypid = pg_type.oid
                WHERE pg_type.typname = '{enum_name}' AND pg_enum.enumlabel = '{value}'
            ) THEN
                ALTER TYPE {enum_name} ADD VALUE '{value}';
            END IF;
        END$$;
        """
    )


def upgrade() -> None:
    # 1. Seed 11 new permissions and grant all to admin (79 -> 90)
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

    # 2. mrp_sequences
    op.create_table(
        "mrp_sequences",
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("next_no", sa.Integer(), server_default="1", nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("company_id", "year"),
    )

    # 3. boms
    op.create_table(
        "boms",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("product_id", sa.UUID(), nullable=False),
        sa.Column("uom_id", sa.UUID(), nullable=False),
        sa.Column("output_qty", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("notes", sa.String(length=500), nullable=True),
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
        sa.CheckConstraint("output_qty > 0", name="ck_boms_output_qty_positive"),
        sa.CheckConstraint("length(btrim(code)) > 0", name="ck_boms_code_nonempty"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["uom_id"], ["uom.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "code", name="uq_boms_company_code"),
    )
    op.create_index(op.f("ix_boms_company_id"), "boms", ["company_id"], unique=False)
    op.create_index("ix_boms_product_id", "boms", ["product_id"], unique=False)
    op.create_index(
        "uq_boms_company_product_active",
        "boms",
        ["company_id", "product_id"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )

    # 4. bom_lines
    op.create_table(
        "bom_lines",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("bom_id", sa.UUID(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("component_product_id", sa.UUID(), nullable=False),
        sa.Column("qty_per", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("uom_id", sa.UUID(), nullable=False),
        sa.Column(
            "scrap_rate",
            sa.Numeric(precision=20, scale=6),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("line_no >= 1", name="ck_bom_lines_line_no_positive"),
        sa.CheckConstraint("qty_per > 0", name="ck_bom_lines_qty_per_positive"),
        sa.CheckConstraint(
            "scrap_rate >= 0 AND scrap_rate < 1", name="ck_bom_lines_scrap_rate_bounds"
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["bom_id"], ["boms.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["component_product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["uom_id"], ["uom.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bom_id", "line_no", name="uq_bom_lines_bom_line_no"),
        sa.UniqueConstraint("bom_id", "component_product_id", name="uq_bom_lines_bom_component"),
    )
    op.create_index(op.f("ix_bom_lines_company_id"), "bom_lines", ["company_id"], unique=False)
    op.create_index("ix_bom_lines_bom_id", "bom_lines", ["bom_id"], unique=False)
    op.create_index(
        "ix_bom_lines_component_product_id", "bom_lines", ["component_product_id"], unique=False
    )

    # 5. work_orders
    op.create_table(
        "work_orders",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("wo_no", sa.String(length=32), nullable=False),
        sa.Column("bom_id", sa.UUID(), nullable=False),
        sa.Column("product_id", sa.UUID(), nullable=False),
        sa.Column("uom_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "DRAFT",
                "CONFIRMED",
                "IN_PROGRESS",
                "COMPLETED",
                "CANCELLED",
                name="work_order_status",
            ),
            server_default="DRAFT",
            nullable=False,
        ),
        sa.Column("planned_qty", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column(
            "completed_qty",
            sa.Numeric(precision=20, scale=6),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "wip_balance",
            sa.Numeric(precision=20, scale=6),
            server_default="0",
            nullable=False,
        ),
        sa.Column("snapshot_sku", sa.String(length=64), nullable=True),
        sa.Column("snapshot_product_name", sa.String(length=255), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.String(length=500), nullable=True),
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
        sa.CheckConstraint("planned_qty > 0", name="ck_work_orders_planned_qty_positive"),
        sa.CheckConstraint(
            "completed_qty >= 0 AND completed_qty <= planned_qty",
            name="ck_work_orders_completed_qty_bounds",
        ),
        sa.CheckConstraint("wip_balance >= 0", name="ck_work_orders_wip_balance_nonneg"),
        sa.CheckConstraint(
            "status::text != 'COMPLETED' OR (wip_balance = 0 AND completed_at IS NOT NULL)",
            name="ck_work_orders_completed_zero_wip",
        ),
        sa.CheckConstraint(
            "status::text != 'CANCELLED' "
            "OR (wip_balance = 0 AND completed_qty = 0 AND cancelled_at IS NOT NULL)",
            name="ck_work_orders_cancelled_clean",
        ),
        sa.CheckConstraint(
            "status::text NOT IN ('CONFIRMED', 'IN_PROGRESS', 'COMPLETED') "
            "OR (snapshot_sku IS NOT NULL AND snapshot_product_name IS NOT NULL "
            "AND confirmed_at IS NOT NULL)",
            name="ck_work_orders_confirmed_has_snapshot",
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["bom_id"], ["boms.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["uom_id"], ["uom.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "wo_no", name="uq_work_orders_company_wo_no"),
    )
    op.create_index(op.f("ix_work_orders_company_id"), "work_orders", ["company_id"], unique=False)
    op.create_index("ix_work_orders_bom_id", "work_orders", ["bom_id"], unique=False)
    op.create_index("ix_work_orders_product_id", "work_orders", ["product_id"], unique=False)

    # 6. work_order_lines
    op.create_table(
        "work_order_lines",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("work_order_id", sa.UUID(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("component_product_id", sa.UUID(), nullable=False),
        sa.Column("uom_id", sa.UUID(), nullable=False),
        sa.Column("required_qty", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column(
            "issued_qty", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False
        ),
        sa.Column(
            "returned_qty", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False
        ),
        sa.Column(
            "issued_cost", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False
        ),
        sa.Column(
            "returned_cost", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False
        ),
        sa.Column(
            "estimated_unit_cost",
            sa.Numeric(precision=20, scale=6),
            server_default="0",
            nullable=False,
        ),
        sa.Column("snapshot_sku", sa.String(length=64), nullable=True),
        sa.Column("snapshot_product_name", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("line_no >= 1", name="ck_work_order_lines_line_no_positive"),
        sa.CheckConstraint("required_qty > 0", name="ck_work_order_lines_required_qty_positive"),
        sa.CheckConstraint(
            "issued_qty >= 0 AND returned_qty >= 0 AND returned_qty <= issued_qty",
            name="ck_work_order_lines_issued_return_bounds",
        ),
        sa.CheckConstraint(
            "issued_cost >= 0 AND returned_cost >= 0 AND returned_cost <= issued_cost "
            "AND estimated_unit_cost >= 0",
            name="ck_work_order_lines_cost_bounds",
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["work_order_id"], ["work_orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["component_product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["uom_id"], ["uom.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("work_order_id", "line_no", name="uq_work_order_lines_wo_line_no"),
        sa.UniqueConstraint(
            "work_order_id", "component_product_id", name="uq_work_order_lines_wo_component"
        ),
    )
    op.create_index(
        op.f("ix_work_order_lines_company_id"), "work_order_lines", ["company_id"], unique=False
    )
    op.create_index(
        "ix_work_order_lines_wo_id", "work_order_lines", ["work_order_id"], unique=False
    )
    op.create_index(
        "ix_work_order_lines_component_id",
        "work_order_lines",
        ["component_product_id"],
        unique=False,
    )

    # 7. work_order_transactions (append-only shop-floor execution log)
    op.create_table(
        "work_order_transactions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("work_order_id", sa.UUID(), nullable=False),
        sa.Column(
            "tx_type",
            sa.Enum(
                "MATERIAL_ISSUE",
                "MATERIAL_RETURN",
                "PRODUCTION_RECEIPT",
                name="work_order_tx_type",
            ),
            nullable=False,
        ),
        sa.Column("request_ref", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "total_cost", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False
        ),
        sa.Column("received_qty", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column(
            "lines_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.CheckConstraint("total_cost >= 0", name="ck_work_order_tx_total_cost_nonneg"),
        sa.CheckConstraint(
            "length(btrim(request_ref)) > 0", name="ck_work_order_tx_request_ref_nonempty"
        ),
        sa.CheckConstraint(
            "received_qty IS NULL OR received_qty > 0",
            name="ck_work_order_tx_received_qty_positive",
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["work_order_id"], ["work_orders.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id", "work_order_id", "request_ref", name="uq_work_order_tx_wo_request_ref"
        ),
    )
    op.create_index(
        op.f("ix_work_order_transactions_company_id"),
        "work_order_transactions",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_work_order_transactions_wo_id",
        "work_order_transactions",
        ["work_order_id"],
        unique=False,
    )

    # 8. Extend customfields & workflow CHECK constraints to allow bom & work_order
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

    # 9. Extend stock_move_type and cost_move_type enums at end of upgrade()
    for val in ("PRODUCTION_ISSUE", "PRODUCTION_RETURN", "PRODUCTION_RECEIPT"):
        _guarded_add_enum_value("stock_move_type", val)
        _guarded_add_enum_value("cost_move_type", val)


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

    op.drop_index("ix_work_order_transactions_wo_id", table_name="work_order_transactions")
    op.drop_index(
        op.f("ix_work_order_transactions_company_id"), table_name="work_order_transactions"
    )
    op.drop_table("work_order_transactions")
    sa.Enum(name="work_order_tx_type").drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_work_order_lines_component_id", table_name="work_order_lines")
    op.drop_index("ix_work_order_lines_wo_id", table_name="work_order_lines")
    op.drop_index(op.f("ix_work_order_lines_company_id"), table_name="work_order_lines")
    op.drop_table("work_order_lines")

    op.drop_index("ix_work_orders_product_id", table_name="work_orders")
    op.drop_index("ix_work_orders_bom_id", table_name="work_orders")
    op.drop_index(op.f("ix_work_orders_company_id"), table_name="work_orders")
    op.drop_table("work_orders")
    sa.Enum(name="work_order_status").drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_bom_lines_component_product_id", table_name="bom_lines")
    op.drop_index("ix_bom_lines_bom_id", table_name="bom_lines")
    op.drop_index(op.f("ix_bom_lines_company_id"), table_name="bom_lines")
    op.drop_table("bom_lines")

    op.drop_index("uq_boms_company_product_active", table_name="boms")
    op.drop_index("ix_boms_product_id", table_name="boms")
    op.drop_index(op.f("ix_boms_company_id"), table_name="boms")
    op.drop_table("boms")

    op.drop_table("mrp_sequences")

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
