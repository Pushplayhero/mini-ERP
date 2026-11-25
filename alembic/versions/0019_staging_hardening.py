"""staging_hardening: VULN-06 secret lengths, Session hardening, and PostgreSQL RLS (ADR-018)

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-11-25 00:00:00.000000

Changes:
1. Widens `webhook_endpoints.signing_secret` and `previous_signing_secret` from
   VARCHAR(255) to VARCHAR(512) to store `enc:v1:` MultiFernet ciphertexts.
2. Enhances `sessions` with `last_active_at`, `ip_address`, `user_agent`, and
   composite index `ix_sessions_user_id_expires_at` for idle timeout, cap
   enforcement, and audit visibility.
3. Creates minimal-privilege runtime role `mini_erp_app` (NOLOGIN NOSUPERUSER
   NOBYPASSRLS), `app_current_company_id()` / `app_begin_rls_tx(text)` SQL
   helpers, and enables + forces Row-Level Security (`tenant_isolation_policy`)
   across all 44 single-tenant PostgreSQL tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3e4f5a6b7c8"
down_revision: str | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Exhaustive inventory of all 44 single-tenant tables:
# - 35 TenantScopedMixin entity/line/fact tables
# - 7 composite-PK gapless sequence tables (*_sequences)
# - 2 composite-PK summary projection tables (stock_summary, product_cost_summaries)
#
# Deliberately excluded (14 global, auth/entitlement, or cross-tenant queue tables):
# companies, currencies, exchange_rates, uom, uom_conversions, users, roles,
# permissions, role_permissions, user_company_roles, sessions, outbox,
# integration_outbox_receipts, webhook_deliveries.
RLS_TENANT_TABLES: tuple[str, ...] = (
    "accounting_periods",
    "accounts",
    "bom_lines",
    "boms",
    "company_plugins",
    "cost_adjustments",
    "cost_moves",
    "costing_sequences",
    "custom_field_definitions",
    "customers",
    "integration_batch_jobs",
    "invoices",
    "journal_entries",
    "journal_lines",
    "ledger_sequences",
    "mrp_sequences",
    "payables_sequences",
    "payment_allocation_commands",
    "payment_allocations",
    "payments",
    "product_cost_summaries",
    "products",
    "purchase_order_lines",
    "purchase_orders",
    "purchase_sequences",
    "receivables_sequences",
    "sales_order_lines",
    "sales_orders",
    "sales_sequences",
    "stock_moves",
    "stock_summary",
    "suppliers",
    "vendor_bills",
    "vendor_payment_allocation_commands",
    "vendor_payment_allocations",
    "vendor_payments",
    "webhook_endpoints",
    "work_order_lines",
    "work_order_transactions",
    "work_orders",
    "workflow_approval_logs",
    "workflow_definitions",
    "workflow_instances",
    "workflow_steps",
)


def upgrade() -> None:
    # 1. Widen webhook signing secret columns for enc:v1: MultiFernet ciphertexts
    op.alter_column(
        "webhook_endpoints",
        "signing_secret",
        existing_type=sa.String(length=255),
        type_=sa.String(length=512),
        existing_nullable=False,
    )
    op.alter_column(
        "webhook_endpoints",
        "previous_signing_secret",
        existing_type=sa.String(length=255),
        type_=sa.String(length=512),
        existing_nullable=True,
    )

    # 2. Enhance sessions table for idle timeout, concurrency cap, and metadata
    op.add_column(
        "sessions",
        sa.Column(
            "last_active_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "sessions",
        sa.Column("ip_address", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("user_agent", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_sessions_user_id_expires_at",
        "sessions",
        ["user_id", "expires_at"],
    )

    # 3. Create minimal-privilege runtime role & RLS GUC helper functions
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mini_erp_app') THEN
                    CREATE ROLE mini_erp_app
                        NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
                END IF;
                EXECUTE format('GRANT mini_erp_app TO %I', current_user);
            END $$;
            """
        )
    )

    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION app_current_company_id() RETURNS uuid
            LANGUAGE sql STABLE AS $$
                SELECT NULLIF(current_setting('app.current_company_id', true), '')::uuid;
            $$;
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION app_begin_rls_tx(p_company_id text) RETURNS void
            LANGUAGE plpgsql AS $$
            BEGIN
                EXECUTE 'SET LOCAL ROLE mini_erp_app';
                PERFORM set_config('app.current_company_id', COALESCE(p_company_id, ''), true);
            END;
            $$;
            """
        )
    )

    op.execute(sa.text("GRANT USAGE ON SCHEMA public TO mini_erp_app"))
    op.execute(
        sa.text(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO mini_erp_app"
        )
    )
    op.execute(sa.text("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO mini_erp_app"))
    op.execute(sa.text("REVOKE ALL ON TABLE alembic_version FROM mini_erp_app"))
    op.execute(sa.text("GRANT EXECUTE ON FUNCTION app_current_company_id() TO mini_erp_app"))
    op.execute(sa.text("GRANT EXECUTE ON FUNCTION app_begin_rls_tx(text) TO PUBLIC"))
    op.execute(
        sa.text(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mini_erp_app"
        )
    )
    op.execute(
        sa.text(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            "GRANT USAGE, SELECT ON SEQUENCES TO mini_erp_app"
        )
    )

    # 4. Enable + Force Row-Level Security across all 44 single-tenant tables
    for table_name in RLS_TENANT_TABLES:
        op.execute(sa.text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation_policy ON {table_name} "
                "FOR ALL "
                "USING (company_id = app_current_company_id()) "
                "WITH CHECK (company_id = app_current_company_id())"
            )
        )


def downgrade() -> None:
    # 1. Drop RLS policies and disable RLS across all 44 single-tenant tables
    for table_name in reversed(RLS_TENANT_TABLES):
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation_policy ON {table_name}"))
        op.execute(sa.text(f"ALTER TABLE {table_name} NO FORCE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY"))

    # 2. Revoke privileges & drop helper functions (keep role cleanup safe for shared clusters)
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mini_erp_app') THEN
                    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM mini_erp_app;
                    REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM mini_erp_app;
                    REVOKE USAGE ON SCHEMA public FROM mini_erp_app;
                    ALTER DEFAULT PRIVILEGES IN SCHEMA public
                        REVOKE ALL ON TABLES FROM mini_erp_app;
                    ALTER DEFAULT PRIVILEGES IN SCHEMA public
                        REVOKE ALL ON SEQUENCES FROM mini_erp_app;
                END IF;
            END $$;
            """
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS app_begin_rls_tx(text)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS app_current_company_id()"))

    # 3. Drop session hardening columns & index
    op.drop_index("ix_sessions_user_id_expires_at", table_name="sessions")
    op.drop_column("sessions", "user_agent")
    op.drop_column("sessions", "ip_address")
    op.drop_column("sessions", "last_active_at")

    # 4. Guard against silent ciphertext truncation before narrowing webhook secret columns
    conn = op.get_bind()
    too_long = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM webhook_endpoints "
            "WHERE length(signing_secret) > 255 "
            "   OR (previous_signing_secret IS NOT NULL AND length(previous_signing_secret) > 255)"
        )
    ).scalar_one()
    if too_long:
        raise RuntimeError(
            "Cannot downgrade 0019_staging_hardening: webhook_endpoints contains secret(s) "
            "longer than 255 characters."
        )

    op.alter_column(
        "webhook_endpoints",
        "previous_signing_secret",
        existing_type=sa.String(length=512),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
    op.alter_column(
        "webhook_endpoints",
        "signing_secret",
        existing_type=sa.String(length=512),
        type_=sa.String(length=255),
        existing_nullable=False,
    )
