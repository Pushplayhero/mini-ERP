"""sales initial

Revision ID: a2f5d8e91c47
Revises: 9c4d2f7a1b3e
Create Date: 2026-08-13 00:00:00.000000

ADR-006: `sales_sequences`, `sales_orders`, `sales_order_lines`.

`sales_sequences` mirrors `ledger_sequences`'s shape exactly (composite
`(company_id, year)` primary key, `next_no` counter) but backs `order_no`
allocation, which ADR-006 Decision 3 explicitly does NOT require to be
gapless — the table exists only so `service._allocate_order_no` has
somewhere to keep a monotonically-increasing counter per company/year; nothing
here enforces "no gaps" the way `ledger_sequences` + its usage together do.

`sales_orders.status`/`sales_order_lines` CHECK constraints put the same
invariants ADR-005 established for ledger (positive quantities, native
Postgres enums, non-negative money) at the DB layer too, so they hold
against any writer, not just this service.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a2f5d8e91c47"
down_revision: str | None = "9c4d2f7a1b3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # sales_sequences (order_no allocation counter — NOT required gapless)
    # ------------------------------------------------------------------
    op.create_table(
        "sales_sequences",
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("next_no", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("company_id", "year"),
    )

    # ------------------------------------------------------------------
    # sales_orders
    # ------------------------------------------------------------------
    op.create_table(
        "sales_orders",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("order_no", sa.String(length=32), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("DRAFT", "CONFIRMED", "CANCELLED", name="sales_order_status"),
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("total", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False),
        sa.Column("snapshot_customer_code", sa.String(length=32), nullable=True),
        sa.Column("snapshot_customer_name", sa.String(length=255), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("total >= 0", name="ck_sales_orders_total_nonneg"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["currency_code"], ["currencies.code"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "order_no", name="uq_sales_orders_company_order_no"),
    )
    op.create_index(
        op.f("ix_sales_orders_company_id"), "sales_orders", ["company_id"], unique=False
    )
    op.create_index("ix_sales_orders_customer_id", "sales_orders", ["customer_id"], unique=False)

    # ------------------------------------------------------------------
    # sales_order_lines
    # ------------------------------------------------------------------
    op.create_table(
        "sales_order_lines",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.UUID(), nullable=False),
        sa.Column("qty", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("uom_id", sa.UUID(), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("snapshot_sku", sa.String(length=64), nullable=True),
        sa.Column("snapshot_product_name", sa.String(length=255), nullable=True),
        sa.Column("amount", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("qty > 0", name="ck_sales_order_lines_qty_positive"),
        sa.CheckConstraint("unit_price >= 0", name="ck_sales_order_lines_unit_price_nonneg"),
        sa.CheckConstraint("amount >= 0", name="ck_sales_order_lines_amount_nonneg"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["order_id"], ["sales_orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["uom_id"], ["uom.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", "line_no", name="uq_sales_order_lines_order_line_no"),
    )
    op.create_index(
        op.f("ix_sales_order_lines_company_id"), "sales_order_lines", ["company_id"], unique=False
    )
    op.create_index(
        "ix_sales_order_lines_order_id", "sales_order_lines", ["order_id"], unique=False
    )
    op.create_index(
        "ix_sales_order_lines_product_id", "sales_order_lines", ["product_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_sales_order_lines_product_id", table_name="sales_order_lines")
    op.drop_index("ix_sales_order_lines_order_id", table_name="sales_order_lines")
    op.drop_index(op.f("ix_sales_order_lines_company_id"), table_name="sales_order_lines")
    op.drop_table("sales_order_lines")

    op.drop_index("ix_sales_orders_customer_id", table_name="sales_orders")
    op.drop_index(op.f("ix_sales_orders_company_id"), table_name="sales_orders")
    op.drop_table("sales_orders")

    op.drop_table("sales_sequences")

    # Manual fix-up mirroring 0001/0002's enum handling: autogenerate does
    # not emit DROP TYPE for native Postgres enums.
    sa.Enum(name="sales_order_status").drop(op.get_bind(), checkfirst=True)
