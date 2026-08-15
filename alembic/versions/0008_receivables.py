"""receivables: invoices, payments, allocations, aging; sales_orders.shipped_at;
accounts.is_control; customers.payment_terms_days

Revision ID: d2e4824a329a
Revises: c7d2e6f1a840
Create Date: 2026-08-15 00:00:02.000000

ADR-008 (consensus review v5 APPROVED 2026-08-15). Closes the Phase 1 O2C
line: invoices issued off shipped orders, payment application (沖帳), AR
aging. See that ADR's "Consensus Revisions" for the full review history
(R1-R17) behind every non-obvious choice below.

**Ordering / one-transaction discipline (read before touching this file)**:
this project's `alembic/env.py` runs the whole `upgrade head` sequence in
ONE transaction. On a fresh database, migration 0005's
`ALTER TYPE sales_order_status ADD VALUE 'SHIPPED'` is therefore still
"not yet committed" from this migration's point of view, so — same R3
discipline as migration 0006 — any comparison against the literal
`'SHIPPED'` in this file must use `status::text = 'SHIPPED'`, never a typed
comparison. This does NOT apply to `invoice_status`/`payment_status`
below: those are brand-new types `CREATE`d in this same migration (via the
inline `sa.Enum(...)` on `op.create_table`), and Postgres's "new value not
yet usable" restriction is specific to `ALTER TYPE ... ADD VALUE` on an
*existing* type — a freshly created type's values are usable immediately,
including within CHECK constraints and partial-index WHERE clauses created
later in this same migration.

**shipped_at backfill (R13/R17)**: `sales_orders.shipped_at` is added
nullable (only `SHIPPED` orders ever have a shipment moment). Before the
state-conditional CHECK is added, any pre-existing `SHIPPED` row missing
`shipped_at` is backfilled from its own `sales.goods_shipped` outbox
payload (the value was always there, just never persisted on the order) —
recoverable with certainty because exactly one such event exists per
shipped order (ADR-007's idempotent-posting invariant). A diagnostic query
runs after the backfill and raises loudly (naming the offending order ids)
if any row still lacks `shipped_at`, rather than letting the CHECK
constraint below fail with a bare constraint-violation traceback (v5
accepted recommendation).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d2e4824a329a"
down_revision: str | None = "c7d2e6f1a840"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # ------------------------------------------------------------------
    # sales_orders.shipped_at (ADR-008 R13/R17)
    # ------------------------------------------------------------------
    op.add_column(
        "sales_orders", sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=True)
    )

    # v5 accepted recommendation, tightened by Codex diff review (2026-08-15,
    # finding 4): assert EXACTLY ONE matching `sales.goods_shipped` outbox
    # payload per shipped order *before* the backfill UPDATE runs, not just
    # "not zero" after it. `UPDATE ... FROM` join semantics are unspecified
    # when a target row matches more than one source row — PostgreSQL is
    # free to pick any one of them — so if the idempotent-posting invariant
    # (ADR-007) were ever actually violated (a bug, a manual data fix, a
    # future regression), the old post-hoc "still NULL?" check would not
    # catch a row that matched 2+ events: it would just silently backfill
    # from an arbitrary one instead of raising. A preflight `LEFT JOIN ...
    # GROUP BY ... HAVING count(ob.id) <> 1` catches both the zero-match and
    # multi-match cases loudly, before any row is written.
    mismatched = bind.execute(
        sa.text(
            "SELECT so.id, count(ob.id) AS match_count "
            "FROM sales_orders so "
            "LEFT JOIN outbox ob "
            "  ON ob.event_type = 'sales.goods_shipped' "
            "  AND ob.payload->>'source_id' = so.id::text "
            "WHERE so.status::text = 'SHIPPED' AND so.shipped_at IS NULL "
            "GROUP BY so.id "
            "HAVING count(ob.id) <> 1"
        )
    ).fetchall()
    if mismatched:
        detail = ", ".join(f"{row[0]} (matches={row[1]})" for row in mismatched)
        raise RuntimeError(
            "migration 0008: cannot backfill sales_orders.shipped_at — order id(s) "
            f"{detail} do not have exactly one matching sales.goods_shipped outbox "
            "payload. This should be impossible under the idempotent-posting invariant "
            "(ADR-007); investigate before retrying this migration."
        )

    # Backfill pre-existing SHIPPED rows from their own outbox payload —
    # now provably safe: the preflight above guarantees exactly one
    # matching row per order, so the join in this UPDATE cannot pick an
    # arbitrary payload among several.
    bind.execute(
        sa.text(
            "UPDATE sales_orders so SET shipped_at = (ob.payload->>'shipped_at')::timestamptz "
            "FROM outbox ob "
            "WHERE so.status::text = 'SHIPPED' AND so.shipped_at IS NULL "
            "AND ob.event_type = 'sales.goods_shipped' "
            "AND ob.payload->>'source_id' = so.id::text"
        )
    )

    # Diagnostic backstop: name any row the backfill still could not fix,
    # before the CHECK constraint below would reject it with an opaque
    # constraint-violation error instead. Should be unreachable given the
    # preflight above; kept as defense-in-depth.
    missing = bind.execute(
        sa.text("SELECT id FROM sales_orders WHERE status::text = 'SHIPPED' AND shipped_at IS NULL")
    ).fetchall()
    if missing:
        missing_ids = ", ".join(str(row[0]) for row in missing)
        raise RuntimeError(
            "migration 0008: cannot backfill sales_orders.shipped_at for order id(s) "
            f"{missing_ids} — no matching sales.goods_shipped outbox payload was found. "
            "This should be impossible under the idempotent-posting invariant (ADR-007); "
            "investigate before retrying this migration."
        )

    op.create_check_constraint(
        "ck_sales_orders_shipped_has_shipped_at",
        "sales_orders",
        "status::text != 'SHIPPED' OR shipped_at IS NOT NULL",
    )

    # ------------------------------------------------------------------
    # accounts.is_control (ADR-008 R5/R11)
    # ------------------------------------------------------------------
    op.add_column(
        "accounts",
        sa.Column("is_control", sa.Boolean(), server_default="false", nullable=False),
    )

    # ------------------------------------------------------------------
    # customers.payment_terms_days (ADR-008 Decision 1 / R9)
    # ------------------------------------------------------------------
    op.add_column(
        "customers",
        sa.Column("payment_terms_days", sa.Integer(), server_default="30", nullable=False),
    )
    op.create_check_constraint(
        "ck_customers_payment_terms_days_range", "customers", "payment_terms_days BETWEEN 0 AND 365"
    )

    # ------------------------------------------------------------------
    # invoices (ADR-008 Decision 1 / Decision 3)
    # ------------------------------------------------------------------
    op.create_table(
        "invoices",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("invoice_no", sa.String(length=32), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("OPEN", "PARTIAL", "PAID", "VOIDED", name="invoice_status"),
            nullable=False,
        ),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("order_shipped_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invoice_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("total", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column(
            "settled_amount", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False
        ),
        sa.Column("snapshot_customer_code", sa.String(length=32), nullable=False),
        sa.Column("snapshot_customer_name", sa.String(length=255), nullable=False),
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
        sa.CheckConstraint("total > 0", name="ck_invoices_total_positive"),
        sa.CheckConstraint(
            "settled_amount >= 0 AND settled_amount <= total",
            name="ck_invoices_settled_amount_bounds",
        ),
        # Exhaustive status<->settled_amount invariant (R7): every enum
        # state enumerated, no "settled at implementation" gap.
        sa.CheckConstraint(
            "(status = 'OPEN' AND settled_amount = 0) "
            "OR (status = 'PARTIAL' AND settled_amount > 0 AND settled_amount < total) "
            "OR (status = 'PAID' AND settled_amount = total) "
            "OR (status = 'VOIDED' AND settled_amount = 0)",
            name="ck_invoices_status_settled_amount_consistency",
        ),
        sa.CheckConstraint(
            "(status = 'VOIDED') = (voided_at IS NOT NULL)",
            name="ck_invoices_voided_at_consistency",
        ),
        sa.CheckConstraint(
            "due_date >= invoice_date", name="ck_invoices_due_date_after_invoice_date"
        ),
        # R13: the earliest an invoice can legally date to is the day goods
        # actually left — keeps Decision 1's "revenue recognized at/after
        # delivery" claim enforced, not aspirational.
        sa.CheckConstraint(
            "invoice_date >= order_shipped_at::date", name="ck_invoices_invoice_date_after_shipment"
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["order_id"], ["sales_orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["currency_code"], ["currencies.code"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "invoice_no", name="uq_invoices_company_invoice_no"),
    )
    op.create_index(op.f("ix_invoices_company_id"), "invoices", ["company_id"], unique=False)
    op.create_index("ix_invoices_customer_id", "invoices", ["customer_id"], unique=False)
    # At most one *live* (non-voided) invoice per order (Decision 1's
    # double-invoice race). Partial unique index is safe to reference
    # 'VOIDED' here — see module docstring: invoice_status is a brand-new
    # type created above, not subject to the ADD VALUE restriction.
    op.create_index(
        "uq_invoices_order_live",
        "invoices",
        ["company_id", "order_id"],
        unique=True,
        postgresql_where=sa.text("status != 'VOIDED'"),
    )

    # ------------------------------------------------------------------
    # payments (ADR-008 Decision 2 / R2)
    # ------------------------------------------------------------------
    op.create_table(
        "payments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("payment_no", sa.String(length=32), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.Enum("RECEIVED", "VOIDED", name="payment_status"), nullable=False),
        sa.Column("external_ref", sa.String(length=128), nullable=False),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column(
            "allocated_amount",
            sa.Numeric(precision=20, scale=6),
            server_default="0",
            nullable=False,
        ),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        sa.CheckConstraint(
            "allocated_amount >= 0 AND allocated_amount <= amount",
            name="ck_payments_allocated_amount_bounds",
        ),
        sa.CheckConstraint(
            "status != 'VOIDED' OR allocated_amount = 0", name="ck_payments_voided_zero_allocated"
        ),
        sa.CheckConstraint(
            "(status = 'VOIDED') = (voided_at IS NOT NULL)",
            name="ck_payments_voided_at_consistency",
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["currency_code"], ["currencies.code"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "payment_no", name="uq_payments_company_payment_no"),
        # R2: client-supplied idempotency key — a retried POST /payments
        # hits this constraint and 409s instead of double-posting Cash/AR.
        sa.UniqueConstraint("company_id", "external_ref", name="uq_payments_company_external_ref"),
    )
    op.create_index(op.f("ix_payments_company_id"), "payments", ["company_id"], unique=False)
    op.create_index("ix_payments_customer_id", "payments", ["customer_id"], unique=False)

    # ------------------------------------------------------------------
    # payment_allocation_commands (ADR-008 R14 — command-level idempotency)
    # ------------------------------------------------------------------
    op.create_table(
        "payment_allocation_commands",
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
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        # R14: one row per distinct allocation command per payment — an
        # exact retry (same ref, same body) is detected by comparing
        # `request_fingerprint` at the application layer; a reused ref with
        # a different body hits this constraint and 409s as the contract
        # violation it always should have been.
        sa.UniqueConstraint(
            "company_id",
            "payment_id",
            "request_ref",
            name="uq_payment_allocation_commands_payment_request_ref",
        ),
    )
    op.create_index(
        op.f("ix_payment_allocation_commands_company_id"),
        "payment_allocation_commands",
        ["company_id"],
        unique=False,
    )

    # ------------------------------------------------------------------
    # payment_allocations (ADR-008 Decision 3 — append-only fact table)
    # ------------------------------------------------------------------
    op.create_table(
        "payment_allocations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("payment_id", sa.UUID(), nullable=False),
        sa.Column("invoice_id", sa.UUID(), nullable=False),
        sa.Column("command_id", sa.UUID(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name="ck_payment_allocations_amount_positive"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["command_id"], ["payment_allocation_commands.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_payment_allocations_company_id"),
        "payment_allocations",
        ["company_id"],
        unique=False,
    )
    op.create_index("ix_payment_allocations_payment_id", "payment_allocations", ["payment_id"])
    op.create_index("ix_payment_allocations_invoice_id", "payment_allocations", ["invoice_id"])

    # ------------------------------------------------------------------
    # receivables_sequences (invoice_no / payment_no allocation — NOT
    # required to be gapless, same doctrine as sales_sequences)
    # ------------------------------------------------------------------
    op.create_table(
        "receivables_sequences",
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("doc_type", sa.String(length=16), nullable=False),
        sa.Column("next_no", sa.Integer(), server_default="1", nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("company_id", "year", "doc_type"),
    )


def downgrade() -> None:
    op.drop_table("receivables_sequences")

    op.drop_index("ix_payment_allocations_invoice_id", table_name="payment_allocations")
    op.drop_index("ix_payment_allocations_payment_id", table_name="payment_allocations")
    op.drop_index(op.f("ix_payment_allocations_company_id"), table_name="payment_allocations")
    op.drop_table("payment_allocations")

    op.drop_index(
        op.f("ix_payment_allocation_commands_company_id"),
        table_name="payment_allocation_commands",
    )
    op.drop_table("payment_allocation_commands")

    op.drop_index("ix_payments_customer_id", table_name="payments")
    op.drop_index(op.f("ix_payments_company_id"), table_name="payments")
    op.drop_table("payments")
    sa.Enum(name="payment_status").drop(op.get_bind(), checkfirst=True)

    op.drop_index("uq_invoices_order_live", table_name="invoices")
    op.drop_index("ix_invoices_customer_id", table_name="invoices")
    op.drop_index(op.f("ix_invoices_company_id"), table_name="invoices")
    op.drop_table("invoices")
    sa.Enum(name="invoice_status").drop(op.get_bind(), checkfirst=True)

    op.drop_constraint("ck_customers_payment_terms_days_range", "customers", type_="check")
    op.drop_column("customers", "payment_terms_days")

    op.drop_column("accounts", "is_control")

    op.drop_constraint("ck_sales_orders_shipped_has_shipped_at", "sales_orders", type_="check")
    op.drop_column("sales_orders", "shipped_at")
