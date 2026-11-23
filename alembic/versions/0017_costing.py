"""costing: moving weighted average subledger, landed cost, PPV & revaluation (ADR-016 / Week 16)

Revision ID: b1c2d3e4f5a6
Revises: a9b1c2d3e4f5
Create Date: 2026-11-23 00:00:00.000000

Creates:
- `cost_move_type` (`RECEIPT`, `SHIPMENT`, `LANDED_COST`, `PPV`, `REVALUATION`),
  `cost_adjustment_type` (`LANDED_COST`, `PPV`, `REVALUATION`), and
  `cost_adjustment_status` (`POSTED`, `VOIDED`) native PostgreSQL enums
- `costing_sequences`, `product_cost_summaries`, `cost_moves`, `cost_adjustments`
- 6 canonical permissions (`costing.summary.read`, `costing.move.read`,
  `costing.adjustment.create`, `costing.adjustment.read`, `costing.adjustment.void`,
  `costing.report.read`) granted to `admin` (expanding permission catalog from 73 to 79)

Updates:
- `custom_field_definitions` entity_type CHECK constraint (adds `cost_adjustment`)
- `workflow_definitions` and `workflow_instances` entity_type/transition CHECK constraints
  (adds `('cost_adjustment', 'void')`)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a9b1c2d3e4f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS_CATALOG: list[dict[str, str]] = [
    {
        "code": "costing.summary.read",
        "description": "View product moving-average cost summaries in the active company.",
    },
    {
        "code": "costing.move.read",
        "description": "View append-only inventory cost move audit trails in the active company.",
    },
    {
        "code": "costing.adjustment.create",
        "description": (
            "Post landed cost, PPV, and inventory revaluation adjustments and sync standard cost."
        ),
    },
    {
        "code": "costing.adjustment.read",
        "description": "View cost adjustments in the active company.",
    },
    {
        "code": "costing.adjustment.void",
        "description": "Void posted cost adjustments in the active company.",
    },
    {
        "code": "costing.report.read",
        "description": (
            "Read inventory valuation and standard-cost variance reports in the active company."
        ),
    },
]

_OLD_CF_ENTITY_TYPES_SQL = (
    "entity_type IN ("
    "'customer', 'supplier', 'product', 'account', 'accounting_period', "
    "'journal_entry', 'sales_order', 'purchase_order', 'invoice', 'payment', "
    "'vendor_bill', 'vendor_payment'"
    ")"
)

_NEW_CF_ENTITY_TYPES_SQL = (
    "entity_type IN ("
    "'customer', 'supplier', 'product', 'account', 'accounting_period', "
    "'journal_entry', 'sales_order', 'purchase_order', 'invoice', 'payment', "
    "'vendor_bill', 'vendor_payment', 'cost_adjustment'"
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
    "(entity_type = 'cost_adjustment' AND transition = 'void') OR "
    "(entity_type = 'journal_entry' AND transition = 'reverse')"
    ")"
)


def upgrade() -> None:
    # 1. Seed 6 new permissions and grant all to admin (73 -> 79)
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

    # 2. costing_sequences
    op.create_table(
        "costing_sequences",
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("next_no", sa.Integer(), server_default="1", nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("company_id", "year"),
    )

    # 3. product_cost_summaries (composite PK: (company_id, product_id))
    op.create_table(
        "product_cost_summaries",
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("product_id", sa.UUID(), nullable=False),
        sa.Column(
            "tracked_qty",
            sa.Numeric(precision=20, scale=6),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "total_cost_value",
            sa.Numeric(precision=20, scale=6),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "moving_avg_cost",
            sa.Numeric(precision=20, scale=6),
            server_default="0",
            nullable=False,
        ),
        sa.CheckConstraint("tracked_qty >= 0", name="ck_product_cost_summaries_qty_nonneg"),
        sa.CheckConstraint("total_cost_value >= 0", name="ck_product_cost_summaries_value_nonneg"),
        sa.CheckConstraint(
            "moving_avg_cost >= 0", name="ck_product_cost_summaries_avg_cost_nonneg"
        ),
        sa.CheckConstraint(
            "tracked_qty != 0 OR total_cost_value = 0",
            name="ck_product_cost_summaries_zero_qty_zero_value",
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("company_id", "product_id"),
    )

    # 4. cost_moves (append-only cost valuation audit ledger)
    op.create_table(
        "cost_moves",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("product_id", sa.UUID(), nullable=False),
        sa.Column(
            "move_type",
            sa.Enum(
                "RECEIPT",
                "SHIPMENT",
                "LANDED_COST",
                "PPV",
                "REVALUATION",
                name="cost_move_type",
            ),
            nullable=False,
        ),
        sa.Column("qty_delta", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("value_delta", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("unit_cost", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("resulting_qty", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("resulting_value", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("resulting_avg_cost", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=True),
        sa.Column("source_id", sa.UUID(), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.CheckConstraint("unit_cost >= 0", name="ck_cost_moves_unit_cost_nonneg"),
        sa.CheckConstraint("resulting_qty >= 0", name="ck_cost_moves_resulting_qty_nonneg"),
        sa.CheckConstraint("resulting_value >= 0", name="ck_cost_moves_resulting_value_nonneg"),
        sa.CheckConstraint(
            "resulting_avg_cost >= 0", name="ck_cost_moves_resulting_avg_cost_nonneg"
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_cost_moves_company_id"), "cost_moves", ["company_id"], unique=False)
    op.create_index("ix_cost_moves_product_id", "cost_moves", ["product_id"], unique=False)
    op.create_index(
        "uq_cost_moves_source",
        "cost_moves",
        ["company_id", "source_type", "source_id", "product_id"],
        unique=True,
        postgresql_where=sa.text("source_type IS NOT NULL"),
    )

    # 5. cost_adjustments
    op.create_table(
        "cost_adjustments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("adjustment_no", sa.String(length=32), nullable=False),
        sa.Column(
            "adjustment_type",
            sa.Enum("LANDED_COST", "PPV", "REVALUATION", name="cost_adjustment_type"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("POSTED", "VOIDED", name="cost_adjustment_status"),
            nullable=False,
        ),
        sa.Column("product_id", sa.UUID(), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("adjustment_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
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
        sa.CheckConstraint("amount <> 0", name="ck_cost_adjustments_amount_nonzero"),
        sa.CheckConstraint(
            "adjustment_type != 'LANDED_COST' OR amount > 0",
            name="ck_cost_adjustments_landed_cost_positive",
        ),
        sa.CheckConstraint(
            "adjustment_type NOT IN ('LANDED_COST', 'PPV') OR order_id IS NOT NULL",
            name="ck_cost_adjustments_po_required",
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) > 0",
            name="ck_cost_adjustments_reason_nonempty",
        ),
        sa.CheckConstraint(
            "(status = 'VOIDED') = (voided_at IS NOT NULL)",
            name="ck_cost_adjustments_voided_at_consistency",
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["order_id"], ["purchase_orders.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "adjustment_no", name="uq_cost_adjustments_company_no"),
    )
    op.create_index(
        op.f("ix_cost_adjustments_company_id"), "cost_adjustments", ["company_id"], unique=False
    )
    op.create_index(
        "ix_cost_adjustments_product_id", "cost_adjustments", ["product_id"], unique=False
    )

    # 6. Extend customfields & workflow CHECK constraints to allow cost_adjustment
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

    op.drop_index("ix_cost_adjustments_product_id", table_name="cost_adjustments")
    op.drop_index(op.f("ix_cost_adjustments_company_id"), table_name="cost_adjustments")
    op.drop_table("cost_adjustments")
    sa.Enum(name="cost_adjustment_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="cost_adjustment_type").drop(op.get_bind(), checkfirst=True)

    op.drop_index("uq_cost_moves_source", table_name="cost_moves")
    op.drop_index("ix_cost_moves_product_id", table_name="cost_moves")
    op.drop_index(op.f("ix_cost_moves_company_id"), table_name="cost_moves")
    op.drop_table("cost_moves")
    sa.Enum(name="cost_move_type").drop(op.get_bind(), checkfirst=True)

    op.drop_table("product_cost_summaries")
    op.drop_table("costing_sequences")

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
