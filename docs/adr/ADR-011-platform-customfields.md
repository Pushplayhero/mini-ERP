# ADR-011: `platform.customfields` — Tenant-Governed Custom Field Engine, Strong Type Normalization, and Public Port Isolation

**Status:** **ACCEPTED**
**Date:** 2026-11-06
**Deciders:** Project Owner, Platform Architecture Gate
**Depends on:**
- `docs/adr/PHASE2-platform-architecture-overview.md` §2.3, §4, §5.1, §7.2 Q4 (ACCEPTED)
- `docs/open-erp-master-plan.md` §2.4, §3, §10.2
- `docs/adr/ADR-009-platform-permissions.md` Decisions 4, 7, 9, 10 (ACCEPTED)
- `docs/adr/ADR-010-platform-plugins.md` Decisions 1, 2, 6 (ACCEPTED)

> Numbering note: ADR-011 is the third Phase 2 ADR, following ADR-009 (`platform.permissions`)
> and ADR-010 (`platform.plugins`) per `docs/adr/PHASE2-platform-architecture-overview.md` §4 / §7.2.

---

## Context

Phase 1 laid the schema foundation for extensible business data by hanging a `custom_data JSONB` column on primary transactional entities via `CustomDataMixin` (`app/core/db.py`). Eight tenant-scoped transactional models currently inherit this mixin:
1. `app.modules.masterdata.models.Customer`
2. `app.modules.masterdata.models.Product`
3. `app.modules.masterdata.models.Account`
4. `app.modules.ledger.models.AccountingPeriod`
5. `app.modules.ledger.models.JournalEntry`
6. `app.modules.sales.models.SalesOrder`
7. `app.modules.receivables.models.Invoice`
8. `app.modules.receivables.models.Payment`

In Phase 1 and early Phase 2, `custom_data` payloads are passed transparently as unvalidated dictionaries (`dict[str, Any] = Field(default_factory=dict)`). This leaves five critical architectural and operational gaps:

1. **No tenant-level governance or field schema registry (`PHASE2-platform-architecture-overview.md` §2.3).**
   There is no definition table where a company administrator can register custom fields (e.g. `einvoice_carrier_id`, `custom_tax_category`, `delivery_condition_code`), set descriptive labels, enforce data types, or specify requiredness.
2. **Data pollution and lack of fail-closed validation.**
   Any API caller can inject arbitrary, nested, or misspelled keys (e.g. `{"carrir_id": 123}` instead of `carrier_id`). Without a fail-closed validator, `custom_data` degenerates into an uncontrolled schemaless dump, breaking downstream report extraction and integrations.
3. **Absence of Banker's Rounding and numeric bounds on decimal fields.**
   When custom fields represent monetary amounts, surcharges, or rate thresholds, Phase 1's precision invariant (`NUMERIC(20, 6)` + `ROUND_HALF_EVEN`) is completely unguarded. Floating-point inaccuracy or arbitrary-precision strings can slip into the JSONB column.
4. **Direct dependency violation risk under `import-linter` (`PHASE2-platform-architecture-overview.md` §5.1).**
   The five business modules must validate `custom_data` upon creation and update without importing concrete platform packages. If `app.modules.sales` imported `app.platform.customfields`, the modular-monolith independence contract would be broken.
5. **No concurrency protection against TOCTOU race conditions during field schema changes.**
   If an administrator adds a required custom field while high-concurrency order creation is executing, uncoordinated reads of field definitions lead to inconsistent validation outcomes unless synchronized via tenant root locks.

---

## Decision

### 1. Two-boundary model: tenant field registry vs. core validation port

Following the pattern established by `app/core/authorization.py` (ADR-009) and `app/core/plugins.py` (ADR-010):
- **Core Port (`app/core/customfields.py`)**: Defines domain types (`CustomFieldType`, `CustomFieldEntityType`), `FIELD_KEY_PATTERN`, exceptions (`CustomFieldValidationError`), and the runtime validation hook protocol (`CustomDataValidator` / `CustomDataValidatorPort`). Business modules call only this lightweight port.
- **Platform Implementation (`app/platform/customfields/`)**: Owns the `custom_field_definitions` persistence table, administrative APIs (`/api/v1/custom-fields`), Pydantic schema validation, and Banker's Rounding normalization.
- **Composition Root Wiring (`app/main.py`)**: Registers the concrete validator implementation with `app.core.customfields.set_custom_data_validator` during lifespan startup.

---

### 2. Supported entity targets and field types

#### 2.1 Entity targets (`CustomFieldEntityType`)
Strict enum restricted to the 8 primary tenant-scoped entities carrying `CustomDataMixin`:
- `customer`
- `product`
- `account`
- `accounting_period`
- `journal_entry`
- `sales_order`
- `invoice`
- `payment`

#### 2.2 Field types (`CustomFieldType`)
- `TEXT`: String, constrained by optional `min_length`, `max_length`, and `regex`.
- `INTEGER`: 64-bit integer, constrained by optional `min_value` and `max_value`.
- `DECIMAL`: Normalized `NUMERIC(20, 6)` string Banker's Rounded (`ROUND_HALF_EVEN`), bounded within `[0, 10^14)`.
- `BOOLEAN`: Strict boolean (`True` / `False`).
- `DATE`: ISO-8601 date string (`YYYY-MM-DD`).
- `ENUM`: String matching a defined list of allowable `choices`.

---

### 3. Schema and database persistence (`custom_field_definitions`)

Migration `0012_platform_customfields.py` creates table `custom_field_definitions`:

```sql
CREATE TABLE custom_field_definitions (
    id UUID PRIMARY KEY,
    company_id UUID NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
    entity_type VARCHAR(50) NOT NULL,
    field_key VARCHAR(64) NOT NULL,
    label VARCHAR(120) NOT NULL,
    field_type VARCHAR(32) NOT NULL,
    is_required BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    default_value JSONB NULL,
    validation_rules JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by UUID NULL,
    updated_by UUID NULL,
    CONSTRAINT uq_custom_field_company_entity_key UNIQUE (company_id, entity_type, field_key),
    CONSTRAINT ck_custom_field_key_format CHECK (field_key ~ '^[a-z][a-z0-9_]{0,63}$'),
    CONSTRAINT ck_custom_field_entity_type_valid CHECK (
        entity_type IN (
            'customer', 'product', 'account', 'accounting_period',
            'journal_entry', 'sales_order', 'invoice', 'payment'
        )
    ),
    CONSTRAINT ck_custom_field_type_valid CHECK (
        field_type IN ('TEXT', 'INTEGER', 'DECIMAL', 'BOOLEAN', 'DATE', 'ENUM')
    ),
    CONSTRAINT ck_custom_field_validation_rules_is_object CHECK (
        jsonb_typeof(validation_rules) = 'object'
    ),
    CONSTRAINT ck_custom_field_label_nonempty CHECK (length(btrim(label)) > 0)
);

CREATE INDEX ix_custom_field_definitions_company_entity
ON custom_field_definitions (company_id, entity_type, is_active);
```

---

### 4. Validation, default population, and fail-closed extra-field policy

When an entity is created or updated, `validate_and_normalize_custom_data` runs within the active transaction:

1. **Load active definitions**: Fetches all rows from `custom_field_definitions` where `company_id = current_company` and `entity_type = target_entity` and `is_active = TRUE`.
2. **Fail-closed on unknown fields (`extra="forbid"`)**:
   Any key in `raw_custom_data` that is **not** defined in active definitions raises `DomainValidationError(422)`:
   `"Unknown custom field '{key}' for entity '{entity_type}'"`.
3. **Required fields & default values**:
   - On **creation** (`is_create=True`): If an active field with `is_required=True` is missing from `raw_custom_data`, and it has a non-null `default_value`, the default value is automatically populated. If no default exists, raises `DomainValidationError(422)`.
   - On **update** (`is_create=False`): Existing keys are merged. Missing keys in a partial update keep their existing values. If an explicit `null` is passed for a required field, raises `DomainValidationError(422)`.
4. **Type normalization & Banker's Rounding**:
   - `DECIMAL`: Quantized via `Decimal(val).quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)`. Stored as formatted string with 6 decimal places.
   - `DATE`: Verified via `datetime.date.fromisoformat(val)`.
   - `ENUM`: Verified that `val in validation_rules["choices"]`.
   - `INTEGER` / `TEXT` / `BOOLEAN`: Type-checked and range-checked against `min_value`/`max_value`/`min_length`/`max_length`.

---

### 5. Concurrency protection on definition changes

Mutations to `custom_field_definitions` (create, update, activate/deactivate) serialize by acquiring the tenant root lock:
```python
await masterdata_service.lock_company_unchecked(session, company_id)
```
This guarantees serial execution against concurrent definition changes for the same company, completely eliminating TOCTOU race conditions.

---

### 6. Permissions and administrative API

Migration `0012` registers 2 new permissions in `permissions` and grants them to the `admin` role:
- `platform.customfield.read`: List and read field definitions.
- `platform.customfield.manage`: Create, update, or deactivate field definitions.

The catalog expands from 47 to **49 permissions**.

Endpoints under `/api/v1/custom-fields`:
- `GET /api/v1/custom-fields?entity_type={entity_type}` (`platform.customfield.read`)
- `POST /api/v1/custom-fields` (`platform.customfield.manage`)
- `GET /api/v1/custom-fields/{field_id}` (`platform.customfield.read`)
- `PATCH /api/v1/custom-fields/{field_id}` (`platform.customfield.manage`)
- `DELETE /api/v1/custom-fields/{field_id}` (`platform.customfield.manage` — soft deactivate `is_active = False`)

---

### 7. Import-linter contracts

`pyproject.toml` updates:
- `app.modules.*` must **NOT** import `app.platform.customfields`.
- `app.core` must **NOT** import `app.platform.customfields`.
- `app.platform.customfields` may depend on `app.core` and `app.modules.masterdata` (for `lock_company_unchecked`) only.

---

## Action Items (Week 11 Implementation Sequence)

1. **Slice A — Core customfields port, Migration 0012, and import-linter contracts:**
   - Create `app/core/customfields.py` (`CustomFieldType`, `CustomFieldEntityType`, `FIELD_KEY_PATTERN`, `CustomDataValidator` / `CustomDataValidatorPort`, `set_custom_data_validator`, `validate_and_normalize_custom_data`).
   - Add Alembic migration `0012_platform_customfields.py` (`custom_field_definitions` table + 2 new permissions `platform.customfield.read` & `platform.customfield.manage` granted to `admin`).
   - Update `pyproject.toml` import-linter contracts.
   - Unit test core port and migration upgrade/downgrade.
2. **Slice B — `platform.customfields` service, models, schemas, and management API:**
   - Create `app/platform/customfields/{models,schemas,service,router}.py`.
   - Wire router and runtime validator in `app/main.py`.
   - Router and service tests covering field creation, validation rule enforcement, and Banker's Rounding defaults.
3. **Slice C — Business module integration and CLI tooling:**
   - Wire `validate_and_normalize_custom_data` into `Customer`, `Product`, `Account`, `AccountingPeriod`, `JournalEntry`, `SalesOrder`, `Invoice`, and `Payment` creation/update paths.
   - Create CLI `app/cli/manage_custom_fields.py` (`CUSTOM_FIELD_ADMIN_ACTOR`, list, define, update, deactivate).
   - Integration tests for business entities rejecting unknown/invalid custom fields.
4. **Slice D — End-to-end tenant governance, concurrency proof, and documentation closure:**
   - Comprehensive tests for multi-tenant custom field isolation, concurrency lock on field definition mutations, and decimal banker's rounding.
   - Update `threat-model.md`, `README.md`, `README.zh-TW.md`, `HANDOFF.md`, and D.5 security gate manifest.
