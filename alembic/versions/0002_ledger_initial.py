"""ledger initial

Revision ID: 7d3f1a9c2b6e
Revises: 4aaa64567798
Create Date: 2026-08-13 00:00:00.000000

ADR-005: accounting_periods, ledger_sequences, journal_entries,
journal_lines, plus the two pieces of DB-level enforcement that make the
ADR's invariants hold against *any* writer, not just this service:

- a deferred constraint trigger on `journal_lines` that rejects an
  unbalanced entry (SUM(debit) != SUM(credit), functional currency only) at
  commit time (Decision 2 / R1);
- unconditional `BEFORE UPDATE OR DELETE` triggers on `journal_entries` and
  `journal_lines` that make both tables immutable (Decision 3).

Both are plain SQL (`op.execute`), since triggers/functions are not
representable via Alembic's table-building ops.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "7d3f1a9c2b6e"
down_revision: str | None = "4aaa64567798"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # accounting_periods
    # ------------------------------------------------------------------
    op.create_table(
        "accounting_periods",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("OPEN", "CLOSED", name="period_status"),
            nullable=False,
            server_default="OPEN",
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
        sa.Column(
            "custom_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.CheckConstraint("month >= 1 AND month <= 12", name="ck_accounting_periods_month_range"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "year", "month", name="uq_accounting_periods_company_ym"),
    )
    op.create_index(
        op.f("ix_accounting_periods_company_id"), "accounting_periods", ["company_id"], unique=False
    )

    # ------------------------------------------------------------------
    # ledger_sequences (ADR-005 R2 — gapless entry_no counter)
    # ------------------------------------------------------------------
    op.create_table(
        "ledger_sequences",
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("next_no", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("company_id", "year"),
    )

    # ------------------------------------------------------------------
    # journal_entries
    # ------------------------------------------------------------------
    op.create_table(
        "journal_entries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("entry_no", sa.String(length=32), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("period_id", sa.UUID(), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=True),
        sa.Column("source_id", sa.UUID(), nullable=True),
        sa.Column("reversal_of_id", sa.UUID(), nullable=True),
        sa.Column(
            "posted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column(
            "custom_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["period_id"], ["accounting_periods.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reversal_of_id"], ["journal_entries.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "entry_no", name="uq_journal_entries_company_entry_no"),
        sa.UniqueConstraint("reversal_of_id", name="uq_journal_entries_reversal_of_id"),
    )
    op.create_index(
        op.f("ix_journal_entries_company_id"), "journal_entries", ["company_id"], unique=False
    )
    op.create_index("ix_journal_entries_period_id", "journal_entries", ["period_id"], unique=False)

    # ------------------------------------------------------------------
    # journal_lines (dual-currency per ADR-005 R1)
    # ------------------------------------------------------------------
    op.create_table(
        "journal_lines",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("entry_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column(
            "txn_debit", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False
        ),
        sa.Column(
            "txn_credit", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False
        ),
        sa.Column("debit", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False),
        sa.Column("credit", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False),
        sa.Column(
            "exchange_rate", sa.Numeric(precision=20, scale=10), server_default="1", nullable=False
        ),
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "txn_debit >= 0 AND txn_credit >= 0", name="ck_journal_lines_txn_nonneg"
        ),
        sa.CheckConstraint("debit >= 0 AND credit >= 0", name="ck_journal_lines_fn_nonneg"),
        sa.CheckConstraint(
            "txn_debit = 0 OR txn_credit = 0", name="ck_journal_lines_txn_one_side_only"
        ),
        sa.CheckConstraint("debit = 0 OR credit = 0", name="ck_journal_lines_fn_one_side_only"),
        sa.CheckConstraint(
            "(txn_debit > 0) = (debit > 0) AND (txn_credit > 0) = (credit > 0)",
            name="ck_journal_lines_sides_agree",
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["entry_id"], ["journal_entries.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["currency_code"], ["currencies.code"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entry_id", "line_no", name="uq_journal_lines_entry_line_no"),
    )
    op.create_index(
        op.f("ix_journal_lines_company_id"), "journal_lines", ["company_id"], unique=False
    )
    op.create_index("ix_journal_lines_entry_id", "journal_lines", ["entry_id"], unique=False)
    op.create_index("ix_journal_lines_account_id", "journal_lines", ["account_id"], unique=False)

    # ------------------------------------------------------------------
    # Balance constraint trigger (ADR-005 Decision 2 / R1)
    #
    # Deferred: fires once per affected row, but only actually *runs* right
    # before COMMIT, by which point every line of the entry has already
    # been inserted in the same transaction — so it sees the final state
    # regardless of insertion order. Sums the functional-currency pair
    # (debit/credit), never the transaction-currency pair, per R1.
    # ERRCODE 23514 (check_violation) so SQLAlchemy/asyncpg classify the
    # failure as IntegrityError, which `service._commit_or_conflict` (and
    # masterdata's equivalent) already know how to turn into a 409.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE FUNCTION ledger_check_entry_balance() RETURNS trigger AS $$
        DECLARE
            v_entry_id uuid;
            v_debit numeric(20, 6);
            v_credit numeric(20, 6);
        BEGIN
            IF TG_OP = 'DELETE' THEN
                v_entry_id := OLD.entry_id;
            ELSE
                v_entry_id := NEW.entry_id;
            END IF;

            SELECT COALESCE(SUM(debit), 0), COALESCE(SUM(credit), 0)
              INTO v_debit, v_credit
              FROM journal_lines
             WHERE entry_id = v_entry_id;

            IF v_debit <> v_credit THEN
                RAISE EXCEPTION
                    'journal entry % is not balanced in functional currency: debit=% credit=%',
                    v_entry_id, v_debit, v_credit
                    USING ERRCODE = '23514';
            END IF;

            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_journal_lines_balance
        AFTER INSERT ON journal_lines
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION ledger_check_entry_balance();
        """
    )

    # ------------------------------------------------------------------
    # Immutability triggers (ADR-005 Decision 3)
    #
    # Unconditional: no exemption for the app's own service layer or for
    # migrations. A `TRUNCATE` (used by tests/conftest.py's per-test
    # cleanup) does NOT fire row-level UPDATE/DELETE triggers, so this does
    # not block test isolation — only UPDATE/DELETE statements are blocked,
    # which is exactly the point.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE FUNCTION ledger_block_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'journal entries/lines are immutable (table: %); '
                'use POST /journal-entries/{id}/reverse instead of UPDATE/DELETE',
                TG_TABLE_NAME;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_journal_entries_immutable
        BEFORE UPDATE OR DELETE ON journal_entries
        FOR EACH ROW EXECUTE FUNCTION ledger_block_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_journal_lines_immutable
        BEFORE UPDATE OR DELETE ON journal_lines
        FOR EACH ROW EXECUTE FUNCTION ledger_block_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_journal_lines_immutable ON journal_lines")
    op.execute("DROP TRIGGER IF EXISTS trg_journal_entries_immutable ON journal_entries")
    op.execute("DROP FUNCTION IF EXISTS ledger_block_mutation()")
    op.execute("DROP TRIGGER IF EXISTS trg_journal_lines_balance ON journal_lines")
    op.execute("DROP FUNCTION IF EXISTS ledger_check_entry_balance()")

    op.drop_index("ix_journal_lines_account_id", table_name="journal_lines")
    op.drop_index("ix_journal_lines_entry_id", table_name="journal_lines")
    op.drop_index(op.f("ix_journal_lines_company_id"), table_name="journal_lines")
    op.drop_table("journal_lines")

    op.drop_index("ix_journal_entries_period_id", table_name="journal_entries")
    op.drop_index(op.f("ix_journal_entries_company_id"), table_name="journal_entries")
    op.drop_table("journal_entries")

    op.drop_table("ledger_sequences")

    op.drop_index(op.f("ix_accounting_periods_company_id"), table_name="accounting_periods")
    op.drop_table("accounting_periods")

    # Manual fix-up mirroring 0001's account_type handling: autogenerate
    # does not emit DROP TYPE for native Postgres enums.
    sa.Enum(name="period_status").drop(op.get_bind(), checkfirst=True)
