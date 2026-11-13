"""platform_integration (ADR-013 / WEEK13 Slice A)

Creates `webhook_endpoints`, `integration_outbox_receipts`, `webhook_deliveries`, and
`integration_batch_jobs` tables with composite tenant-safe foreign keys, check constraints,
and delivery partial index.
Seeds `platform.integration.read`, `platform.integration.manage`, and `platform.integration.import`
permissions, granting all three to the `admin` role (expanding total permission catalog
from 52 to 55).

Revision ID: e7f8a1b2c3d4
Revises: d6e7f8a1b2c3
Create Date: 2026-11-13 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "e7f8a1b2c3d4"
down_revision: str | None = "d6e7f8a1b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS_CATALOG: list[dict[str, str]] = [
    {
        "code": "platform.integration.read",
        "description": "View webhook endpoints, deliveries, batch job history, and export data.",
    },
    {
        "code": "platform.integration.manage",
        "description": "Create, update, deactivate webhooks, rotate secrets, and retry deliveries.",
    },
    {
        "code": "platform.integration.import",
        "description": "Execute batch master data imports.",
    },
]


def upgrade() -> None:
    # 1. Seed permissions
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

    # 2. webhook_endpoints
    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("signing_secret", sa.String(length=255), nullable=False),
        sa.Column("previous_signing_secret", sa.String(length=255), nullable=True),
        sa.Column("previous_secret_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("secret_prefix", sa.String(length=16), nullable=False),
        sa.Column("event_types", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "timeout_seconds",
            sa.Numeric(precision=5, scale=2),
            nullable=False,
            server_default=sa.text("10.00"),
        ),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("5")),
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
        sa.PrimaryKeyConstraint("id", name="pk_webhook_endpoints"),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="CASCADE",
            name="fk_webhook_endpoints_company",
        ),
        sa.UniqueConstraint("company_id", "id", name="uq_webhook_endpoints_company_id"),
        sa.UniqueConstraint("company_id", "name", name="uq_webhook_endpoints_company_name"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_webhook_endpoints_name_nonempty"),
        sa.CheckConstraint("length(btrim(url)) > 0", name="ck_webhook_endpoints_url_nonempty"),
        sa.CheckConstraint(
            "timeout_seconds >= 1.00 AND timeout_seconds <= 60.00",
            name="ck_webhook_endpoints_timeout",
        ),
        sa.CheckConstraint(
            "max_attempts >= 1 AND max_attempts <= 20",
            name="ck_webhook_endpoints_max_attempts",
        ),
    )

    # 3. integration_outbox_receipts
    op.create_table(
        "integration_outbox_receipts",
        sa.Column("outbox_id", sa.UUID(), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("outbox_id", name="pk_integration_outbox_receipts"),
        sa.ForeignKeyConstraint(
            ["outbox_id"],
            ["outbox.id"],
            ondelete="CASCADE",
            name="fk_integration_outbox_receipts_outbox",
        ),
    )

    # 4. webhook_deliveries
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("endpoint_id", sa.UUID(), nullable=False),
        sa.Column("outbox_event_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("5")),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_http_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_webhook_deliveries"),
        sa.ForeignKeyConstraint(
            ["company_id", "endpoint_id"],
            ["webhook_endpoints.company_id", "webhook_endpoints.id"],
            ondelete="CASCADE",
            name="fk_webhook_deliveries_company_endpoint",
        ),
        sa.ForeignKeyConstraint(
            ["outbox_event_id"],
            ["outbox.id"],
            ondelete="CASCADE",
            name="fk_webhook_deliveries_outbox",
        ),
        sa.UniqueConstraint(
            "company_id",
            "endpoint_id",
            "outbox_event_id",
            name="uq_webhook_deliveries_idempotency",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'IN_PROGRESS', 'DELIVERED', 'DEAD')",
            name="ck_webhook_deliveries_status",
        ),
    )
    op.create_index(
        "ix_webhook_deliveries_due",
        "webhook_deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("status IN ('PENDING', 'IN_PROGRESS')"),
    )
    op.create_index(
        "ix_webhook_deliveries_endpoint",
        "webhook_deliveries",
        ["company_id", "endpoint_id", "created_at"],
    )

    # 5. integration_batch_jobs
    op.create_table(
        "integration_batch_jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("format", sa.String(length=16), nullable=False, server_default="CSV"),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("total_rows", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("succeeded_rows", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed_rows", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_summary", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
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
        sa.PrimaryKeyConstraint("id", name="pk_integration_batch_jobs"),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="CASCADE",
            name="fk_integration_batch_jobs_company",
        ),
        sa.CheckConstraint(
            "job_type IN ('IMPORT', 'EXPORT')",
            name="ck_integration_batch_jobs_type",
        ),
        sa.CheckConstraint(
            "format IN ('CSV', 'JSON')",
            name="ck_integration_batch_jobs_format",
        ),
        sa.CheckConstraint(
            "status IN ('COMPLETED', 'FAILED', 'DRY_RUN')",
            name="ck_integration_batch_jobs_status",
        ),
    )


def downgrade() -> None:
    op.drop_table("integration_batch_jobs")
    op.drop_index("ix_webhook_deliveries_endpoint", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_due", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_table("integration_outbox_receipts")
    op.drop_table("webhook_endpoints")

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
