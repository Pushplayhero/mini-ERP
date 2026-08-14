"""sales_orders: confirmed/shipped orders must carry customer snapshots

Revision ID: b1c4a7e93f52
Revises: e6b1a4c9d0f7
Create Date: 2026-08-15 00:00:00.000000

Diff-review fix (Week 2-4 consensus review, ADR-006 finding): ADR-006
Decision 3 promises that a `CONFIRMED` order's `snapshot_customer_code`/
`snapshot_customer_name` are frozen at confirm time — `sales.service
.confirm_order` correctly does this on every real write path, but nothing
below the application layer enforced it. `snapshot_customer_code`/
`snapshot_customer_name` stayed nullable with no constraint tying them to
`status`, so a bypass writer (anything skipping `confirm_order`) could
persist a `CONFIRMED` order with no snapshot at all — same class of gap
ADR-005's DB-level invariants (balance, immutability) exist to close, just
missed here. This is a plain single-row `CHECK` (both columns live on
`sales_orders` itself, unlike the cross-table case below), so no trigger is
needed.

**`SHIPPED` is covered too (diff-review fix, round 2)**: the state machine
only reaches `SHIPPED` by first passing through `CONFIRMED`
(`draft -> confirmed -> shipped`), so a `SHIPPED` order with no snapshot is
exactly the same integrity violation, reachable by the same class of bypass
writer — checking only `CONFIRMED` would leave that writer free to persist
a `SHIPPED` order directly, defeating this constraint's whole purpose.

**R3 applies across migration files too (learned the hard way here)**:
`SHIPPED` was added to `sales_order_status` by migration 0005's guarded
`ALTER TYPE ... ADD VALUE`, whose own R3 doctrine says nothing in *that*
migration may reference the literal afterward — but this project's
`alembic/env.py` runs the *entire* `upgrade head` sequence in one
transaction (no `transaction_per_migration`), so a fresh environment
applying 0001..0007 in one go has 0005's `ADD VALUE` and this migration's
use of `'SHIPPED'` in the *same* transaction regardless of the file
boundary — `asyncpg.exceptions.UnsafeNewEnumValueUsageError` confirmed this
empirically. The fix: compare the column's *text* representation
(`status::text`) against plain string literals instead of casting the
literals to `sales_order_status` — Postgres's "new value not yet
committed" restriction is about typed-enum comparison operators, not text
equality, so casting the column (not the literal) side-steps it entirely.

**Scope note**: `sales_order_lines.snapshot_sku`/`snapshot_product_name`
have the same gap in principle, but proving "this line's parent order is
CONFIRMED/SHIPPED" from a `sales_order_lines` row requires a cross-table
check, which plain `CHECK` cannot express (would need a trigger, mirroring
`ledger_check_entry_balance` in migration 0002). Deliberately deferred,
not silently dropped: the order-level check below already covers the
"confirmed/shipped order has no snapshot at all" case ADR-006 wording was
implied to guard the primary blast radius (unmatchable/blank customer
name/code appearing on a confirmed order) for the manageable cost of one
constraint; line-level snapshot enforcement is a future revisit if a real
data-integrity incident ever motivates the added trigger complexity.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c4a7e93f52"
down_revision: str | None = "e6b1a4c9d0f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_sales_orders_confirmed_has_snapshot",
        "sales_orders",
        "status::text NOT IN ('CONFIRMED', 'SHIPPED') "
        "OR (snapshot_customer_code IS NOT NULL AND snapshot_customer_name IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_sales_orders_confirmed_has_snapshot", "sales_orders", type_="check")
