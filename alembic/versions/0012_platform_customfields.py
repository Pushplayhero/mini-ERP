"""platform_customfields (ADR-011 / WEEK11 Slice A)

Creates `custom_field_definitions` table with TenantScopedMixin + TimestampAuditMixin
and seeds `platform.customfield.read` and `platform.customfield.manage` permissions,
granting both to the `admin` role (expanding total permission catalog from 47 to 49).

Revision ID: c5d6e7f8a1b2
Revises: b4c5d6e7f8a1
Create Date: 2026-11-06 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "c5d6e7f8a1b2"
down_revision: str | None = "b4c5d6e7f8a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS_CATALOG: list[dict[str, str]] = [
    {
        "code": "platform.customfield.read",
        "description": "View defined custom fields for the active company.",
    },
    {
        "code": "platform.customfield.manage",
        "description": (
            "Create, update, or deactivate custom field definitions for the active company."
        ),
    },
]

_VALID_ENTITY_TYPES_SQL = (
    "entity_type IN ("
    "'customer', 'product', 'account', 'accounting_period', "
    "'journal_entry', 'sales_order', 'invoice', 'payment'"
    ")"
)

_VALID_FIELD_TYPES_SQL = "field_type IN ('TEXT', 'INTEGER', 'DECIMAL', 'BOOLEAN', 'DATE', 'ENUM')"


def upgrade() -> None:
    op.create_table(
        "custom_field_definitions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("field_key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("field_type", sa.String(length=32), nullable=False),
        sa.Column("is_required", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("default_value", JSONB(), nullable=True),
        sa.Column(
            "validation_rules", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
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
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "company_id", "entity_type", "field_key", name="uq_custom_field_company_entity_key"
        ),
        sa.CheckConstraint(
            "field_key ~ '^[a-z][a-z0-9_]{0,63}$'", name="ck_custom_field_key_format"
        ),
        sa.CheckConstraint(_VALID_ENTITY_TYPES_SQL, name="ck_custom_field_entity_type_valid"),
        sa.CheckConstraint(_VALID_FIELD_TYPES_SQL, name="ck_custom_field_type_valid"),
        sa.CheckConstraint(
            "jsonb_typeof(validation_rules) = 'object'",
            name="ck_custom_field_validation_rules_is_object",
        ),
        sa.CheckConstraint(
            "length(btrim(label)) > 0",
            name="ck_custom_field_label_nonempty",
        ),
    )
    op.create_index(
        "ix_custom_field_definitions_company_entity",
        "custom_field_definitions",
        ["company_id", "entity_type", "is_active"],
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

    op.drop_index(
        "ix_custom_field_definitions_company_entity", table_name="custom_field_definitions"
    )
    op.drop_table("custom_field_definitions")
