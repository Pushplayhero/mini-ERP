"""platform_workflow (ADR-012 / WEEK12 Slice A)

Creates `workflow_definitions`, `workflow_steps`, `workflow_instances`, and
`workflow_approval_logs` tables with composite tenant-safe foreign keys, check constraints,
and active instance partial unique index.
Seeds `platform.workflow.read`, `platform.workflow.manage`, and `platform.workflow.approve`
permissions, granting all three to the `admin` role (expanding total permission catalog
from 49 to 52).

Revision ID: d6e7f8a1b2c3
Revises: c5d6e7f8a1b2
Create Date: 2026-11-09 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "d6e7f8a1b2c3"
down_revision: str | None = "c5d6e7f8a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS_CATALOG: list[dict[str, str]] = [
    {
        "code": "platform.workflow.read",
        "description": "View workflow definitions, steps, instances, and audit logs.",
    },
    {
        "code": "platform.workflow.manage",
        "description": "Create, update, or deactivate workflow definitions and steps.",
    },
    {
        "code": "platform.workflow.approve",
        "description": (
            "Submit approval requests, approve/reject pending steps, and cancel workflows."
        ),
    },
]

_VALID_ENTITY_TRANSITIONS_SQL = (
    "("
    "(entity_type = 'sales_order' AND transition IN ('confirm', 'cancel')) OR "
    "(entity_type = 'invoice' AND transition = 'void') OR "
    "(entity_type = 'payment' AND transition = 'void') OR "
    "(entity_type = 'journal_entry' AND transition = 'reverse')"
    ")"
)


def upgrade() -> None:
    # 1. Seed permissions (before workflow_steps FK to permissions.code)
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

    # 2. workflow_definitions
    op.create_table(
        "workflow_definitions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("transition", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "allow_self_approval", sa.Boolean(), nullable=False, server_default=sa.text("false")
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
        sa.UniqueConstraint("company_id", "id", name="uq_workflow_def_company_id"),
        sa.UniqueConstraint(
            "company_id",
            "entity_type",
            "transition",
            name="uq_workflow_def_company_entity_transition",
        ),
        sa.CheckConstraint(
            _VALID_ENTITY_TRANSITIONS_SQL, name="ck_workflow_def_entity_transition_valid"
        ),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_workflow_def_name_nonempty"),
    )

    # 2. workflow_steps
    op.create_table(
        "workflow_steps",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("definition_id", sa.UUID(), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("step_name", sa.String(length=120), nullable=False),
        sa.Column(
            "min_amount",
            sa.Numeric(precision=20, scale=6),
            nullable=False,
            server_default=sa.text("0.000000"),
        ),
        sa.Column("required_permission", sa.String(length=100), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["company_id", "definition_id"],
            ["workflow_definitions.company_id", "workflow_definitions.id"],
            ondelete="CASCADE",
            name="fk_workflow_step_company_def",
        ),
        sa.ForeignKeyConstraint(["required_permission"], ["permissions.code"], ondelete="RESTRICT"),
        sa.UniqueConstraint("definition_id", "step_order", name="uq_workflow_step_def_order"),
        sa.CheckConstraint("step_order >= 1", name="ck_workflow_step_order_positive"),
        sa.CheckConstraint("min_amount >= 0", name="ck_workflow_step_min_amount_nonnegative"),
        sa.CheckConstraint("length(btrim(step_name)) > 0", name="ck_workflow_step_name_nonempty"),
    )

    # 3. workflow_instances
    op.create_table(
        "workflow_instances",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("definition_id", sa.UUID(), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("transition", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("target_amount", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("current_step_order", sa.Integer(), nullable=True),
        sa.Column("steps_snapshot", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("requested_by", sa.UUID(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by", sa.UUID(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["company_id", "definition_id"],
            ["workflow_definitions.company_id", "workflow_definitions.id"],
            ondelete="RESTRICT",
            name="fk_workflow_instance_company_def",
        ),
        sa.UniqueConstraint("company_id", "id", name="uq_workflow_instance_company_id"),
        sa.CheckConstraint(
            _VALID_ENTITY_TRANSITIONS_SQL, name="ck_workflow_instance_entity_transition_valid"
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED', 'CONSUMED')",
            name="ck_workflow_instance_status_valid",
        ),
        sa.CheckConstraint(
            "target_amount >= 0", name="ck_workflow_instance_target_amount_nonnegative"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(steps_snapshot) = 'array'",
            name="ck_workflow_instance_steps_snapshot_is_array",
        ),
        sa.CheckConstraint(
            "(status = 'PENDING') = (current_step_order IS NOT NULL)",
            name="ck_workflow_instance_pending_has_step",
        ),
        sa.CheckConstraint(
            "(status = 'CONSUMED') = (consumed_at IS NOT NULL)",
            name="ck_workflow_instance_consumed_timestamp",
        ),
    )

    op.create_index(
        "uq_workflow_instance_active",
        "workflow_instances",
        ["company_id", "entity_type", "entity_id", "transition"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'APPROVED')"),
    )
    op.create_index(
        "ix_workflow_instances_lookup",
        "workflow_instances",
        ["company_id", "entity_type", "entity_id", "transition", "status"],
    )

    # 4. workflow_approval_logs
    op.create_table(
        "workflow_approval_logs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("instance_id", sa.UUID(), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("step_name", sa.String(length=120), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=False),
        sa.Column("comment", sa.String(length=1000), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["company_id", "instance_id"],
            ["workflow_instances.company_id", "workflow_instances.id"],
            ondelete="RESTRICT",
            name="fk_workflow_log_company_instance",
        ),
        sa.UniqueConstraint("instance_id", "step_order", name="uq_workflow_log_instance_step"),
        sa.CheckConstraint("step_order >= 1", name="ck_workflow_log_step_order_positive"),
        sa.CheckConstraint(
            "length(btrim(step_name)) > 0", name="ck_workflow_log_step_name_nonempty"
        ),
        sa.CheckConstraint(
            "decision IN ('APPROVE', 'REJECT')", name="ck_workflow_log_decision_valid"
        ),
    )


def downgrade() -> None:
    op.drop_table("workflow_approval_logs")
    op.drop_index("ix_workflow_instances_lookup", table_name="workflow_instances")
    op.drop_index("uq_workflow_instance_active", table_name="workflow_instances")
    op.drop_table("workflow_instances")
    op.drop_table("workflow_steps")
    op.drop_table("workflow_definitions")

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
