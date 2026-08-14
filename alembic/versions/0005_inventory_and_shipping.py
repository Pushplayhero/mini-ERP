"""inventory + shipping

Revision ID: e6b1a4c9d0f7
Revises: a2f5d8e91c47
Create Date: 2026-08-13 00:00:00.000000

ADR-007: `stock_moves` (append-only) + `stock_summary` (rebuildable
projection), `products.standard_cost`, and the `SHIPPED` value on
`sales_order_status`. This is the migration that retires Week 3's synthetic
posting event and turns `sales.goods_shipped` into the first real one.

**R3 (normative, read before touching this file again)**: this migration
calls `ALTER TYPE sales_order_status ADD VALUE 'SHIPPED'`. Postgres forbids
using a newly-added enum value anywhere else in the *same* transaction that
added it (a plain `ALTER TYPE ... ADD VALUE` statement, unlike `CREATE TYPE`,
does not finalize the new label until the adding transaction commits). Every
statement in `upgrade()` below is therefore ordered so that nothing after
the `ADD VALUE` call ever references the literal `'SHIPPED'` — if you are
adding a follow-up statement to this file, do not reference `'SHIPPED'` in
it; put that in a later migration instead, once this one has committed.

**R2**: no data-migration statement in this file references `'SHIPPED'`
either, for the same reason — the `outbox` cleanup UPDATE below only
touches `event_type = 'test.synthetic_sale'` rows, unrelated to the enum.

**R1**: `test.synthetic_sale` is retired in this same deploy (see
`app/modules/ledger/posting.py` — the schema/handler/rule are deleted, not
merely superseded). Any `outbox` row of that event_type that has not yet
been replayed (`dispatched_at IS NULL`) can never be replayed successfully
again once that code is gone — it would hit `UnknownEventTypeError` forever.
Marking those rows `dispatched_at = now()` here is the only honest terminal
state for an event whose type no longer exists in the running application;
it is not "pretending they were processed", it is acknowledging they never
can be, by a schema that has been intentionally deleted.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6b1a4c9d0f7"
down_revision: str | None = "a2f5d8e91c47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # products.standard_cost (ADR-007 Decision 3 / Consensus P3)
    # ------------------------------------------------------------------
    op.add_column(
        "products",
        sa.Column(
            "standard_cost",
            sa.Numeric(precision=20, scale=6),
            server_default="0",
            nullable=False,
        ),
    )
    # Diff-review fix: DB-layer backstop mirroring `ProductCreate`/
    # `ProductUpdate`'s `standard_cost: ... = Field(ge=0)` — without this, a
    # negative standard_cost line and a positive one in the same shipment
    # could net `total_cost` to zero and silently trip the zero-cost skip
    # (`ledger.posting`) even though valued goods genuinely moved.
    op.create_check_constraint("ck_products_standard_cost_nonneg", "products", "standard_cost >= 0")

    # ------------------------------------------------------------------
    # stock_moves (append-only — ADR-007 Decision 2 / Decision 4)
    # ------------------------------------------------------------------
    op.create_table(
        "stock_moves",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("product_id", sa.UUID(), nullable=False),
        sa.Column("qty_delta", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column(
            "move_type",
            sa.Enum("SHIPMENT", "ADJUSTMENT", name="stock_move_type"),
            nullable=False,
        ),
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
        sa.CheckConstraint("qty_delta <> 0", name="ck_stock_moves_qty_delta_nonzero"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_stock_moves_company_id"), "stock_moves", ["company_id"], unique=False)
    op.create_index("ix_stock_moves_product_id", "stock_moves", ["product_id"], unique=False)
    # ADR-007 Decision 4 / R2 of ADR-003 applied to stock: the same
    # partial-unique-index + SAVEPOINT idempotency pattern used for
    # `journal_entries` (see migration 0003 / `uq_journal_entries_source`),
    # here scoped down to `product_id` too since one shipment event fans out
    # into one stock_moves row *per line*, all sharing the same
    # (company_id, source_type, source_id). `WHERE source_type IS NOT NULL`
    # excludes manual adjustments (NULL source_type, no replay path, no
    # dedup needed) from the constraint, exactly as manual journal entries
    # are excluded from `uq_journal_entries_source`.
    op.create_index(
        "uq_stock_moves_source",
        "stock_moves",
        ["company_id", "source_type", "source_id", "product_id"],
        unique=True,
        postgresql_where=sa.text("source_type IS NOT NULL"),
    )

    # ------------------------------------------------------------------
    # stock_summary (rebuildable projection — ADR-007 Decision 2)
    # ------------------------------------------------------------------
    op.create_table(
        "stock_summary",
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("product_id", sa.UUID(), nullable=False),
        sa.Column("on_hand", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False),
        sa.CheckConstraint("on_hand >= 0", name="ck_stock_summary_on_hand_nonneg"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("company_id", "product_id"),
    )

    # ------------------------------------------------------------------
    # sales_order_status: add SHIPPED (R3 — see module docstring: nothing
    # below this statement, in this migration, may reference 'SHIPPED')
    #
    # Wrapped in a conditional (checked against the `pg_enum` catalog, not
    # against the enum type itself — this is a metadata read, not a "use" of
    # the new value, so it does not run afoul of R3's same-transaction
    # restriction) rather than a bare `ALTER TYPE ... ADD VALUE`, because
    # `downgrade()` deliberately does NOT remove this label once added (see
    # `downgrade()` below): without this guard, a `downgrade -1` followed by
    # a second `upgrade head` — an entirely reasonable operational sequence,
    # and one this project's own migration test exercises — would re-run
    # this statement against a type that already has the label and fail
    # with `DuplicateObjectError`. This makes re-running the upgrade side of
    # this migration safe regardless of how many times the enum half of it
    # has already been applied.
    # ------------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_enum
                JOIN pg_type ON pg_enum.enumtypid = pg_type.oid
                WHERE pg_type.typname = 'sales_order_status' AND pg_enum.enumlabel = 'SHIPPED'
            ) THEN
                ALTER TYPE sales_order_status ADD VALUE 'SHIPPED';
            END IF;
        END$$;
        """
    )

    # ------------------------------------------------------------------
    # R1: neutralize any un-replayed test.synthetic_sale outbox rows before
    # this deploy deletes the schema/handler that could ever replay them.
    # Does NOT reference 'SHIPPED' — see module docstring.
    # ------------------------------------------------------------------
    op.execute(
        "UPDATE outbox SET dispatched_at = now() "
        "WHERE event_type = 'test.synthetic_sale' AND dispatched_at IS NULL"
    )


def downgrade() -> None:
    # R1's outbox UPDATE is intentionally NOT reversed: there is no way to
    # distinguish, after the fact, which of the now-`dispatched_at`-stamped
    # rows were genuinely already dispatched before this migration ran vs.
    # neutralized by it — reintroducing NULLs here would just resurrect rows
    # that (per this migration's whole justification) can never be replayed
    # successfully again anyway. No-op, deliberately.

    op.drop_table("stock_summary")

    op.drop_index("uq_stock_moves_source", table_name="stock_moves")
    op.drop_index("ix_stock_moves_product_id", table_name="stock_moves")
    op.drop_index(op.f("ix_stock_moves_company_id"), table_name="stock_moves")
    op.drop_table("stock_moves")
    sa.Enum(name="stock_move_type").drop(op.get_bind(), checkfirst=True)

    op.drop_constraint("ck_products_standard_cost_nonneg", "products", type_="check")
    op.drop_column("products", "standard_cost")

    # R3: the SHIPPED enum value is intentionally NOT removed. Postgres has
    # no `ALTER TYPE ... DROP VALUE` — the only way to truly remove a native
    # enum label is to rebuild the type (create a new type, rewrite every
    # dependent column, drop the old type), which is a heavyweight,
    # data-risky operation this project's downgrade path does not attempt on
    # a table (`sales_orders`) that may already hold real `SHIPPED` rows by
    # the time anyone runs `downgrade`. A stray unused enum label surviving
    # a downgrade is harmless (nothing can ever set a row to a value no
    # application code emits); silently corrupting/blocking a downgrade to
    # attempt the removal would not be. This is a documented, deliberate
    # no-op, not an oversight.
