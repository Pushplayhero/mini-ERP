"""posting engine: partial unique index on journal_entries source

Revision ID: 9c4d2f7a1b3e
Revises: 7d3f1a9c2b6e
Create Date: 2026-08-13 00:00:00.000000

ADR-003 Decision 3 / Consensus Revision R2: idempotent posting. Once the bus
(ADR-004) can deliver an event more than once (at-least-once, once replay
exists), the same `(company_id, source_type, source_id)` must never produce
two journal entries. This partial unique index makes that a DB-enforced
invariant, not a code-level promise alone — mirroring the project's
established doctrine (see ADR-005) of putting invariants at the DB layer so
they hold against *any* writer, not just this service.

Partial (`WHERE source_type IS NOT NULL`) because Week 2's manually-created
entries, and reversals (`ledger.service._post_reversal` — diff-review fix:
reversals set `source_type`/`source_id` to `NULL`, NEVER copy them from the
original entry being reversed; copying would collide with that very entry
under this index and make every reversal of an event-sourced entry fail),
have no source event of their own and must not collide with each other on
`(company_id, NULL, NULL)`.

`ledger.posting.handle_posting_event` catches the resulting `IntegrityError`
inside a `SAVEPOINT` (ADR-003 R2) and treats it as "already posted, skip" —
see that module for the read side of this contract.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9c4d2f7a1b3e"
down_revision: str | None = "7d3f1a9c2b6e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_journal_entries_source",
        "journal_entries",
        ["company_id", "source_type", "source_id"],
        unique=True,
        postgresql_where=sa.text("source_type IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_journal_entries_source", table_name="journal_entries")
