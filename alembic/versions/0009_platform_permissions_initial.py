"""platform.permissions initial (ADR-009 / WEEK9 Slice A)

Six RBAC tables per ADR-009 Decision 1: `users`, `roles`, `permissions`,
`role_permissions`, `user_company_roles`, `sessions`. `user_company_roles`
is deliberately NOT tenant-scoped (`TenantScopedMixin`) — see ADR-009
Decision 1's "Options Considered" entry: entitlement resolution determines
*which* company to bind, so it must be queryable before any company context
exists, the same reasoning that keeps `Company` itself un-tenant-scoped.

Seeds a **minimal placeholder catalog only** (WEEK9 brief Decision 2/round 2
finding 4): the `admin` role and the single baseline `session.manage`
permission code, granted to `admin`. The full by-endpoint permission-code
catalog is intentionally deferred to migration 0010 (Slice D) — see the
WEEK9 brief for why (classifying routes requires enumerating them first).
`session.manage` must exist and be granted here, not deferred, because the
very first bootstrap admin (Slice B) needs to call `/auth/switch-company`/
`/auth/logout` immediately after Slice D's cutover, with no lockout gap.

Revision ID: f19b7d4e2c85
Revises: d2e4824a329a
Create Date: 2026-08-17 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f19b7d4e2c85"
down_revision: str | None = "d2e4824a329a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
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
        sa.PrimaryKeyConstraint("id"),
    )
    # Case-insensitive uniqueness (ADR-009 Decision 1: "login identifier =
    # email, unique, case-insensitive") via a functional index — Postgres
    # has no built-in citext dependency this project otherwise needs.
    op.create_index(
        "uq_users_email_lower",
        "users",
        [sa.text("lower(email)")],
        unique=True,
    )

    op.create_table(
        "roles",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("code"),
    )

    op.create_table(
        "permissions",
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("code"),
    )

    op.create_table(
        "role_permissions",
        sa.Column("role_code", sa.String(length=64), nullable=False),
        sa.Column("permission_code", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(["role_code"], ["roles.code"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["permission_code"], ["permissions.code"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("role_code", "permission_code"),
    )

    # Entitlement table (ADR-009 Decision 1) — deliberately NOT
    # TenantScopedMixin, see module docstring above.
    op.create_table(
        "user_company_roles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("role_code", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_code"], ["roles.code"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "company_id", "role_code", name="uq_user_company_role"),
    )
    op.create_index(
        "ix_user_company_roles_user_company",
        "user_company_roles",
        ["user_id", "company_id"],
    )

    # ADR-009 Decision 3: opaque bearer token, stored as its SHA-256 hash
    # (never Argon2id — a fast deterministic hash is required for an
    # indexed per-request lookup; Argon2id's slowness is for password
    # brute-force resistance, a different problem).
    op.create_table(
        "sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("active_company_id", sa.UUID(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["active_company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
    )
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])

    # --- Minimal placeholder seed catalog (WEEK9 brief Decision 2) ---
    roles_table = sa.table(
        "roles",
        sa.column("code", sa.String),
        sa.column("name", sa.String),
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

    op.bulk_insert(roles_table, [{"code": "admin", "name": "Administrator"}])
    op.bulk_insert(
        permissions_table,
        [
            {
                "code": "session.manage",
                "description": (
                    "Switch active company or log out one's own session "
                    "(self-scoped, granted to every seeded role)."
                ),
            }
        ],
    )
    op.bulk_insert(
        role_permissions_table,
        [{"role_code": "admin", "permission_code": "session.manage"}],
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_user_company_roles_user_company", table_name="user_company_roles")
    op.drop_table("user_company_roles")
    op.drop_table("role_permissions")
    op.drop_table("permissions")
    op.drop_table("roles")
    op.drop_index("uq_users_email_lower", table_name="users")
    op.drop_table("users")
