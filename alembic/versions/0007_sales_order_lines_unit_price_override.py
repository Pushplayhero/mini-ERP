"""sales_order_lines: unit_price_is_override flag

Revision ID: c7d2e6f1a840
Revises: b1c4a7e93f52
Create Date: 2026-08-15 00:00:01.000000

Diff-review fix (Week 2-4 consensus review, ADR-006 Decision 3): the
service layer had no persisted way to tell "client explicitly set
`unit_price`" (a manual quote override) apart from "service defaulted it
from `product.list_price`" once a line was written — so `confirm_order`
could not honor ADR-006's own text, "a draft that sat for a week confirms
at *current* prices unless lines carried manual overrides", for a
defaulted line whose product's price changed after the line was created.

Backfill: every pre-existing line defaults to `false` (not an override) —
the honest, conservative choice; a line written before this column existed
carries no record of whether its price was manual, and treating it as "not
an override" means it will simply reprice to current cost the next time its
order is confirmed, which is the safer of the two possible wrong guesses
(silently freezing a stale price forever is the alternative, and worse).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d2e6f1a840"
down_revision: str | None = "b1c4a7e93f52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sales_order_lines",
        sa.Column(
            "unit_price_is_override",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("sales_order_lines", "unit_price_is_override")
