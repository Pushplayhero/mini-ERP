"""platform_plugins (ADR-010 / WEEK10 Slice B)

Creates `company_plugins` table with TenantScopedMixin + TimestampAuditMixin
and seeds `platform.plugin.read` and `platform.plugin.manage` permissions,
granting both to the `admin` role (expanding total permission catalog from 45 to 47).

Revision ID: b4c5d6e7f8a1
Revises: e7b8c9d0a1f2
Create Date: 2026-08-23 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "b4c5d6e7f8a1"
down_revision: str | None = "e7b8c9d0a1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS_CATALOG: list[dict[str, str]] = [
    {
        "code": "platform.plugin.read",
        "description": (
            "View discovered plugins and their effective state/config for the active company."
        ),
    },
    {
        "code": "platform.plugin.manage",
        "description": "Enable, disable, or configure plugins for the active company.",
    },
]


def upgrade() -> None:
    op.create_table(
        "company_plugins",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("plugin_name", sa.String(length=100), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("config", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
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
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("company_id", "plugin_name", name="uq_company_plugins_company_plugin"),
    )
    op.create_index(
        "ix_company_plugins_company_id",
        "company_plugins",
        ["company_id"],
    )

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


def downgrade() -> None:
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

    op.drop_index("ix_company_plugins_company_id", table_name="company_plugins")
    op.drop_table("company_plugins")
