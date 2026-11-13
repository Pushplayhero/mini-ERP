# ADR-012: `platform.workflow` — Configurable Approval Chains, Amount-Threshold Routing, and State-Transition Gate

**Status:** **ACCEPTED**  
**Date:** 2026-11-09  
**Deciders:** Project Owner, Platform Architecture Gate  
**Depends on:**
- `docs/adr/PHASE2-platform-architecture-overview.md` §2.4, §3b, §4, §5.1, §7.1 Q1, Q3 (ACCEPTED)
- `docs/open-erp-master-plan.md` §2.4, §3, §10.2
- `docs/adr/ADR-009-platform-permissions.md` Decisions 4, 7, 9, 10 (ACCEPTED)
- `docs/adr/ADR-010-platform-plugins.md` Decisions 1, 2, 6 (ACCEPTED)
- `docs/adr/ADR-011-platform-customfields.md` Decisions 1, 5, 7 (ACCEPTED)

> Numbering note: ADR-012 is the fourth Phase 2 ADR, following ADR-009 (`platform.permissions`),
> ADR-010 (`platform.plugins`), and ADR-011 (`platform.customfields`) per `docs/adr/PHASE2-platform-architecture-overview.md` §4 / §7.2.

---

## Context

In Phase 1 and early Phase 2, state transitions across transactional documents were hard-coded directly in service functions. For instance, `sales.service.confirm_order` transitioned a draft sales order to `CONFIRMED` upon checking permissions and running the `sales.order.validate_confirm` hook. Similarly, `sales.service.cancel_order`, `receivables.service.void_invoice`, `receivables.service.void_payment`, and `ledger.service.reverse_journal_entry` transitioned document statuses immediately within a single transaction.

While this straightforward model supported Order-to-Cash core functionality, real-world enterprise operations require tenant-configurable approval governance (`PHASE2-platform-architecture-overview.md` §2.4). Specifically, five architectural requirements emerge:

1. **Configurable approval chains per tenant (`PHASE2-platform-architecture-overview.md` §2.4):**
   Enterprises require multi-level approval workflows before high-impact or irreversible state transitions execute (e.g. large sales orders require sales manager and CFO approval; voiding invoices or reversing posted ledger journals requires controller sign-off). Hard-coding these rules in Python violates the multi-tenant extensibility principle.
2. **Separation of amount-threshold routing and RBAC actor authority (`PHASE2-platform-architecture-overview.md` §3b & §7.1 Q1):**
   Conflating business-policy vetoes (such as credit limit checks), workflow routing (amount-tiered approval steps), and user permissions (RBAC) into an ad-hoc "rules engine" leads to unmaintainable complexity. As established in §7.1 Q1:
   - **Business policy hooks** (e.g. `credit_limit` plugin) inspect domain entities without evaluating user roles.
   - **Workflow routing** inspects the document's monetary amount (`target_amount`) against step thresholds (`min_amount`) to select required approval steps.
   - **RBAC authorization** evaluates whether the approving user possesses the step's `required_permission`.
3. **Atomic state-transition gate and anti-replay protection:**
   When an authorized user confirms a document, the system must guarantee that an approved workflow instance exists, matches the exact document amount, has not been tampered with, and is atomically transitioned to `CONSUMED` in the same database transaction. An approved workflow instance must never be reused or replayed.
4. **Strict modular-monolith boundary isolation (`PHASE2-platform-architecture-overview.md` §5.1 & §7.1 Q3):**
   Under `import-linter`, business modules (`app.modules.*`) must never import `app.platform.workflow`. The dependency direction must run via a lightweight core port (`app/core/workflow.py`), with runtime implementations wired exclusively at the composition root (`app/main.py`).
5. **TOCTOU concurrency protection and invariant enforcement:**
   Concurrent approval decisions, duplicate submissions, and concurrent definition mutations must serialize cleanly via database row locks (`SELECT ... FOR UPDATE`), tenant root locks (`lock_company_unchecked`), and partial unique indexes.

---

## Decision

### 1. Two-boundary model: core workflow port vs. platform workflow engine

Following the architectural pattern established by `app/core/authorization.py` (ADR-009) and `app/core/customfields.py` (ADR-011):

- **Core Port (`app/core/workflow.py`)**:
  - Defines domain enums: `WorkflowEntityType`, `WorkflowTransition`, `WorkflowInstanceStatus`, and `WorkflowDecision`.
  - Defines the canonical transition registry `VALID_ENTITY_TRANSITIONS`.
  - Defines exceptions: `WorkflowApprovalRequiredError(ConflictError)` (maps to HTTP `409 Conflict`).
  - Defines runtime protocols:
    - `WorkflowApprovalGate`: Gating protocol invoked by business modules before executing protected state transitions (`verify_workflow_approval`).
    - `WorkflowTargetResolver`: Lookup protocol invoked by the workflow engine to query authoritative document amounts and state without importing business modules (`resolve_workflow_target`).
  - If no approval gate is registered (e.g. standalone unit tests) or no active workflow is configured for the entity/transition, the gate is a transparent no-op, preserving 100% backward compatibility.
- **Platform Implementation (`app/platform/workflow/`)**:
  - Owns persistence models (`WorkflowDefinition`, `WorkflowStep`, `WorkflowInstance`, `WorkflowApprovalLog`), Pydantic schemas, service layer, and HTTP routes (`/api/v1/workflows`).
  - Evaluates step thresholds, validates user approval permissions, records immutable audit trails, and maintains state transitions.
- **Composition Root Wiring (`app/main.py`)**:
  - Registers the platform workflow gate with `app.core.workflow.set_workflow_approval_gate` during application startup.
  - Registers the target resolver with `app.core.workflow.set_workflow_target_resolver`.

---

### 2. Supported entity transitions

Five high-impact state transitions across sales, receivables, and ledger modules are supported under `WorkflowEntityType` and `WorkflowTransition`:

| `entity_type` | `transition` | Service Intercept Point | Pre-condition Status | Authoritative Amount Field (`NUMERIC(20, 6)`) |
| :--- | :--- | :--- | :--- | :--- |
| `sales_order` | `confirm` | `sales.service.confirm_order` | `DRAFT` (repriced `total > 0`) | Repriced `order.total` |
| `sales_order` | `cancel` | `sales.service.cancel_order` | `DRAFT` or `CONFIRMED` | Snapshot `order.total` |
| `invoice` | `void` | `receivables.service.void_invoice` | `ISSUED` (unallocated) | Snapshot `invoice.total` |
| `payment` | `void` | `receivables.service.void_payment` | `RECEIVED` (unallocated) | Snapshot `payment.amount` |
| `journal_entry` | `reverse` | `ledger.service.reverse_journal_entry` | `POSTED` (unreversed) | Entry debit total `SUM(lines.debit)` |

In `sales.service.confirm_order`, `verify_workflow_approval` executes after row-level locking (`SELECT ... FOR UPDATE`) and price re-calculation, but before committing the status change to `CONFIRMED`. If the order total changed after an approval instance was granted, the gate detects an amount mismatch and raises `WorkflowApprovalRequiredError(409)`.

---

### 3. Amount-threshold routing and RBAC authority separation

#### 3.1 Step filtering and threshold routing
A `WorkflowDefinition` contains one or more `WorkflowStep` records sorted by `step_order` (`1..N` strictly ascending). Each step defines:
- `step_order: int` (`>= 1`)
- `step_name: str` (non-empty descriptive label)
- `min_amount: Decimal` (`NUMERIC(20, 6)`, non-negative, quantized with `ROUND_HALF_EVEN`)
- `required_permission: str` (valid permission code from `permissions` catalog)

When a document is submitted for approval with authoritative amount $A$:
- Steps where `step.min_amount <= A` are selected into the instance's immutable `steps_snapshot` (ordered by `step_order ASC`).
- If one or more steps match: `WorkflowInstance.status` becomes `PENDING`, and `current_step_order` points to the first matching step.
- If no steps match (the document amount is strictly below every step's `min_amount` threshold):
  - **Direct transition path (`verify_workflow_approval`)**: The gate sees zero matching steps for amount $A$ and allows the business transition immediately without requiring a `WorkflowInstance` ( consuming any existing `APPROVED` instance for that document transition if one was pre-submitted).
  - **Explicit submission path (`submit_instance`)**: If a caller explicitly submits the document via `POST /api/v1/workflows/instances`, the instance is created directly in `status = 'APPROVED'` with `current_step_order = NULL` and `steps_snapshot = []`.

#### 3.2 Actor authority and Separation of Duties (SoD)
When approving (`POST /api/v1/workflows/instances/{id}/approve`) or rejecting:
1. The caller must have `platform.workflow.approve` permission.
2. The caller must also possess the active step's `required_permission` (e.g. `sales.order.confirm` or `ledger.journal_entry.reverse`).
3. **Separation of Duties:** `requested_by` is strictly `NOT NULL` (stamped from `require_current_actor().user_id` upon submission). If `definition.allow_self_approval` is `False` (the default), and `actor.user_id == instance.requested_by`, approval is rejected with `ActionDeniedError(403)`. A requester cannot approve their own document; multi-step tier separation across distinct roles is governed by each step's `required_permission`.

---

### 4. Instance lifecycle state machine and anti-replay `CONSUMED` state

```
           +-----------------------+
           |        [Start]        |
           +-----------------------+
                       |
               submit_instance
                       v
         [steps matched > 0?]
             /              \
          Yes                No
           v                  v
     +-----------+      +------------+
     |  PENDING  |      |  APPROVED  |
     +-----------+      +------------+
       |   |   |              |
approve|   |   |cancel        |
(more) |   |   +------+       |
  v    |   |reject    |       |
[Next] |   v          v       |
       | +----------+ +-------------+
       | | REJECTED | |  CANCELLED  |
       | +----------+ +-------------+
       |                      ^
       |approve (last)        | cancel / supersede
       v                      |
     +------------------------+
     |        APPROVED        |
     +------------------------+
                  |
        verify_workflow_approval (inside state transition tx)
                  v
            +------------+
            |  CONSUMED  |
            +------------+
```

1. **Explicit Submission:** Because state-transition endpoints rollback upon raising `409 Conflict`, workflow instances are initiated via `POST /api/v1/workflows/instances`.
2. **Sequential Approval:** Multi-step workflows advance `current_step_order` sequentially. Any rejection immediately marks the instance `REJECTED`.
3. **Single-Use Consumption:** When the document state transition completes, `verify_workflow_approval` locks the `APPROVED` instance via `SELECT ... FOR UPDATE`, validates `target_amount == current_amount`, and sets `status = CONSUMED` with `consumed_at` and `consumed_by`. A consumed workflow cannot be reused.

---

### 5. Schema and database persistence (`0013_platform_workflow.py`)

Migration `0013_platform_workflow.py` adds four tenant-scoped tables:

```sql
CREATE TABLE workflow_definitions (
    id UUID PRIMARY KEY,
    company_id UUID NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
    entity_type VARCHAR(50) NOT NULL,
    transition VARCHAR(50) NOT NULL,
    name VARCHAR(120) NOT NULL,
    description VARCHAR(500) NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    allow_self_approval BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by UUID NULL,
    updated_by UUID NULL,
    CONSTRAINT uq_workflow_def_company_id UNIQUE (company_id, id),
    CONSTRAINT uq_workflow_def_company_entity_transition UNIQUE (company_id, entity_type, transition),
    CONSTRAINT ck_workflow_def_entity_transition_valid CHECK (
        (entity_type = 'sales_order' AND transition IN ('confirm', 'cancel')) OR
        (entity_type = 'invoice' AND transition = 'void') OR
        (entity_type = 'payment' AND transition = 'void') OR
        (entity_type = 'journal_entry' AND transition = 'reverse')
    ),
    CONSTRAINT ck_workflow_def_name_nonempty CHECK (length(btrim(name)) > 0)
);

CREATE TABLE workflow_steps (
    id UUID PRIMARY KEY,
    company_id UUID NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
    definition_id UUID NOT NULL,
    step_order INTEGER NOT NULL,
    step_name VARCHAR(120) NOT NULL,
    min_amount NUMERIC(20, 6) NOT NULL DEFAULT 0.000000,
    required_permission VARCHAR(100) NOT NULL REFERENCES permissions(code) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by UUID NULL,
    updated_by UUID NULL,
    CONSTRAINT fk_workflow_step_company_def FOREIGN KEY (company_id, definition_id)
        REFERENCES workflow_definitions(company_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_workflow_step_def_order UNIQUE (definition_id, step_order),
    CONSTRAINT ck_workflow_step_order_positive CHECK (step_order >= 1),
    CONSTRAINT ck_workflow_step_min_amount_nonnegative CHECK (min_amount >= 0),
    CONSTRAINT ck_workflow_step_name_nonempty CHECK (length(btrim(step_name)) > 0)
);

CREATE TABLE workflow_instances (
    id UUID PRIMARY KEY,
    company_id UUID NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
    definition_id UUID NOT NULL,
    entity_type VARCHAR(50) NOT NULL,
    entity_id UUID NOT NULL,
    transition VARCHAR(50) NOT NULL,
    status VARCHAR(32) NOT NULL,
    target_amount NUMERIC(20, 6) NOT NULL,
    current_step_order INTEGER NULL,
    steps_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,
    requested_by UUID NOT NULL,
    consumed_at TIMESTAMPTZ NULL,
    consumed_by UUID NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by UUID NULL,
    updated_by UUID NULL,
    CONSTRAINT uq_workflow_instance_company_id UNIQUE (company_id, id),
    CONSTRAINT fk_workflow_instance_company_def FOREIGN KEY (company_id, definition_id)
        REFERENCES workflow_definitions(company_id, id) ON DELETE RESTRICT,
    CONSTRAINT ck_workflow_instance_entity_transition_valid CHECK (
        (entity_type = 'sales_order' AND transition IN ('confirm', 'cancel')) OR
        (entity_type = 'invoice' AND transition = 'void') OR
        (entity_type = 'payment' AND transition = 'void') OR
        (entity_type = 'journal_entry' AND transition = 'reverse')
    ),
    CONSTRAINT ck_workflow_instance_status_valid CHECK (
        status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED', 'CONSUMED')
    ),
    CONSTRAINT ck_workflow_instance_target_amount_nonnegative CHECK (target_amount >= 0),
    CONSTRAINT ck_workflow_instance_steps_snapshot_is_array CHECK (
        jsonb_typeof(steps_snapshot) = 'array'
    ),
    CONSTRAINT ck_workflow_instance_pending_has_step CHECK (
        (status = 'PENDING') = (current_step_order IS NOT NULL)
    ),
    CONSTRAINT ck_workflow_instance_consumed_timestamp CHECK (
        (status = 'CONSUMED') = (consumed_at IS NOT NULL)
    )
);

-- Partial unique index: at most one active (PENDING or unconsumed APPROVED) instance
-- per (company_id, entity_type, entity_id, transition)
CREATE UNIQUE INDEX uq_workflow_instance_active
ON workflow_instances (company_id, entity_type, entity_id, transition)
WHERE status IN ('PENDING', 'APPROVED');

CREATE INDEX ix_workflow_instances_lookup
ON workflow_instances (company_id, entity_type, entity_id, transition, status);

CREATE TABLE workflow_approval_logs (
    id UUID PRIMARY KEY,
    company_id UUID NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
    instance_id UUID NOT NULL,
    step_order INTEGER NOT NULL,
    step_name VARCHAR(120) NOT NULL,
    decision VARCHAR(32) NOT NULL,
    actor_id UUID NOT NULL,
    comment VARCHAR(1000) NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by UUID NULL,
    updated_by UUID NULL,
    CONSTRAINT fk_workflow_log_company_instance FOREIGN KEY (company_id, instance_id)
        REFERENCES workflow_instances(company_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_workflow_log_instance_step UNIQUE (instance_id, step_order),
    CONSTRAINT ck_workflow_log_step_order_positive CHECK (step_order >= 1),
    CONSTRAINT ck_workflow_log_step_name_nonempty CHECK (length(btrim(step_name)) > 0),
    CONSTRAINT ck_workflow_log_decision_valid CHECK (decision IN ('APPROVE', 'REJECT'))
);
```

---

### 6. Concurrency and TOCTOU protection

1. **Definition mutation serialization:**
   All mutations to `workflow_definitions` and `workflow_steps` acquire the tenant root lock:
   ```python
   await masterdata_service.lock_company_unchecked(session, company_id)
   ```
   Step updates use the `steps.clear()` + `session.flush()` two-phase flush pattern to avoid unique constraint collisions.
2. **Approval instance serialization:**
   All instance state modifications (`approve`, `reject`, `cancel`) execute `SELECT ... FOR UPDATE` on `WorkflowInstance`. If two managers submit concurrent approval requests for the same step, the second request reads the updated `current_step_order` or `status` and fails cleanly.
3. **Double-submission and dual-active prevention:**
   The partial unique index `uq_workflow_instance_active` (`WHERE status IN ('PENDING', 'APPROVED')`) guarantees at the database level that at most one active (`PENDING` or unconsumed `APPROVED`) workflow instance can exist for the same `(company_id, entity_type, entity_id, transition)`. Re-submitting after a draft edit atomically supersedes (`CANCELLED` + flush) any prior unconsumed `APPROVED` instance under the target document's `FOR UPDATE` lock before inserting the new row.
4. **Amount tampering prevention:**
   `submit_instance` never trusts client-supplied amounts; it reads the authoritative document total directly from the database under lock. During state transition, `verify_workflow_approval` verifies `instance.target_amount == current_amount`.

---

### 7. Permissions and administrative API

Migration `0013` registers 3 new permissions in `permissions` and grants them to the `admin` role:
- `platform.workflow.read`: View workflow definitions, steps, instances, and audit logs.
- `platform.workflow.manage`: Create, update, or deactivate workflow definitions and steps.
- `platform.workflow.approve`: Submit approval requests, approve/reject pending steps, and cancel workflows.

The permission catalog expands from 49 to **52 permissions**.

#### HTTP endpoints under `/api/v1/workflows`:
- `GET /api/v1/workflows/definitions` (`platform.workflow.read`)
- `POST /api/v1/workflows/definitions` (`platform.workflow.manage`)
- `GET /api/v1/workflows/definitions/{definition_id}` (`platform.workflow.read`)
- `PATCH /api/v1/workflows/definitions/{definition_id}` (`platform.workflow.manage`)
- `DELETE /api/v1/workflows/definitions/{definition_id}` (`platform.workflow.manage` — soft deactivation `is_active = False`)
- `GET /api/v1/workflows/instances` (`platform.workflow.read`)
- `POST /api/v1/workflows/instances` (`platform.workflow.approve`)
- `GET /api/v1/workflows/instances/{instance_id}` (`platform.workflow.read`)
- `POST /api/v1/workflows/instances/{instance_id}/approve` (`platform.workflow.approve`)
- `POST /api/v1/workflows/instances/{instance_id}/reject` (`platform.workflow.approve`)
- `POST /api/v1/workflows/instances/{instance_id}/cancel` (`platform.workflow.approve`)

---

### 8. Import-linter contracts

`pyproject.toml` adds Contract 13:
```toml
[[tool.importlinter.contracts]]
name = "platform.workflow may depend on core and masterdata only"
type = "forbidden"
source_modules = ["app.platform.workflow"]
forbidden_modules = [
    "app.modules.sales",
    "app.modules.inventory",
    "app.modules.receivables",
    "app.modules.ledger",
    "app.plugins",
    "app.platform.permissions",
    "app.platform.plugins",
    "app.platform.customfields",
    "app.platform.integration",
    "app.main",
    "app.cli",
]
```
`"app.platform.workflow"` is added to Contract 12's `protected_modules` to prevent bare import traversal.

---

## Action Items (Week 12 Implementation Sequence)

1. [x] **Slice A — Core workflow port, Migration 0013, and import-linter contract:**
   - Create `app/core/workflow.py` (`WorkflowEntityType`, `WorkflowTransition`, `WorkflowInstanceStatus`, `WorkflowDecision`, `VALID_ENTITY_TRANSITIONS`, `WorkflowApprovalRequiredError`, `WorkflowApprovalGate`, `WorkflowTargetResolver`, port registration functions).
   - Add Alembic migration `0013_platform_workflow.py` (4 tables, DDL constraints, partial unique index, 3 permissions granted to `admin`).
   - Add Contract 13 to `pyproject.toml` and update protected modules.
   - Unit tests for core port and migration upgrade/downgrade (`tests/core/test_workflow_port.py`, `tests/test_migrations.py`).
2. [x] **Slice B — `platform.workflow` models, schemas, service, and administrative API:**
   - Create `app/platform/workflow/{models,schemas,service,router}.py`.
   - Wire router, approval gate, and target resolver in `app/main.py`.
   - Unit and router tests for definition CRUD, step threshold matching, SoD enforcement, and approval lifecycle (`tests/platform/test_workflow_service.py`, `tests/platform/test_workflow_router.py`).
3. [x] **Slice C — Business module gate integration, target resolvers, and CLI tooling:**
   - Wire `verify_workflow_approval` into `sales.confirm_order`, `sales.cancel_order`, `receivables.void_invoice`, `receivables.void_payment`, and `ledger.reverse_journal_entry`.
   - Implement `resolve_workflow_target` for the 5 entity types.
   - Create CLI `app/cli/manage_workflows.py` (`WORKFLOW_ADMIN_ACTOR`, list, submit, approve, reject).
   - Cross-module integration tests verifying business transitions fail-closed (409) without approval and succeed once approved (`tests/platform/test_workflow_business_integration.py`, `tests/cli/test_manage_workflows.py`).
4. [x] **Slice D — Multi-tenant concurrency tests, anti-replay proof, and documentation closure:**
   - Concurrency tests: dual-manager approval race serialization (`FOR UPDATE`), duplicate submission rejection (partial unique index), tenant root locking on definition changes, and post-approval price modification & zero-step policy-tightening rejection (`tests/platform/test_workflow_concurrency.py`).
   - Anti-replay test verifying `CONSUMED` instances cannot be reused under concurrent requests.
   - Update `threat-model.md`, `README.md`, `README.zh-TW.md`, `SECURITY.md`, `HANDOFF.md`, and D.5 security gate manifest (224 tracked files).
