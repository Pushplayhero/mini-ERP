"""tests/platform/test_customfields_concurrency.py — Concurrency proof and E2E lifecycle

testing for platform.customfields (ADR-011 Slice D / Week 11).

Covers:
1. Multi-session concurrency proof on `masterdata_service.lock_company_unchecked`:
   - Create-Create serialization: Proves that concurrent definition creation of the
     same field_key is strictly serialized by the company root lock, preventing duplicate
     definitions and ensuring clean ConflictError (409) handling.
   - Update-Update write-skew prevention: Proves bidirectional serialization when
     one transaction tightens validation_rules while another updates default_value,
     asserting that the illegal state (default_value violating validation_rules) can
     never occur.
   - Cross-tenant lock independence: Proves that locking Company A does not block
     definition mutations in Company B.
2. End-to-end lifecycle, deactivation, reactivation, and Banker's Rounding:
   - Full HTTP lifecycle: creation, default population, Banker's Rounding normalization,
     soft deactivation (is_active = False), and reactivation.
   - Verifies fail-closed rejection of deactivated custom fields on entity creation/update.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.authorization import actor_context
from app.core.customfields import (
    CustomFieldEntityType,
    CustomFieldType,
    CustomFieldValidationError,
)
from app.core.exceptions import ConflictError
from app.core.tenancy import company_context
from app.platform.customfields import service as cf_service
from app.platform.customfields.models import CustomFieldDefinition
from app.platform.customfields.schemas import (
    CustomFieldDefinitionCreate,
    CustomFieldDefinitionUpdate,
)
from tests._auth_helpers import setup_company_with_admin
from tests.conftest import TEST_ADMIN_ACTOR

# ---------------------------------------------------------------------------
# 1. Concurrency: Create-Create Serialization
# ---------------------------------------------------------------------------


async def _run_serialized_create_pair(
    session_factory: async_sessionmaker[AsyncSession],
    company_id: uuid.UUID,
    data: CustomFieldDefinitionCreate,
) -> dict[str, str]:
    """Runs two concurrent create_custom_field_definition calls for the same field."""
    from app.modules.masterdata import service as masterdata_service

    orig_lock = masterdata_service.lock_company_unchecked
    call_count = 0
    first_locked, second_entering_lock, second_acquired_lock, release_first = (
        asyncio.Event() for _ in range(4)
    )

    async def instrumented_lock(session: AsyncSession, cid: uuid.UUID) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            row = await orig_lock(session, cid)
            first_locked.set()
            await release_first.wait()
            return row
        second_entering_lock.set()
        row = await orig_lock(session, cid)
        second_acquired_lock.set()
        return row

    results: dict[str, str] = {}

    async def run_create(label: str, *, wait_first: bool) -> None:
        if wait_first:
            await first_locked.wait()
        async with session_factory() as session:
            with actor_context(TEST_ADMIN_ACTOR), company_context(company_id):
                try:
                    await cf_service.create_custom_field_definition(session, data)
                    results[label] = "success"
                except ConflictError as exc:
                    await session.rollback()
                    results[label] = f"ConflictError: {exc}"
                except Exception as exc:
                    await session.rollback()
                    results[label] = f"unexpected: {exc}"

    masterdata_service.lock_company_unchecked = instrumented_lock  # type: ignore[assignment]
    try:
        t_1 = asyncio.create_task(run_create("first", wait_first=False))
        await first_locked.wait()

        t_2 = asyncio.create_task(run_create("second", wait_first=True))
        await second_entering_lock.wait()

        await asyncio.sleep(0.05)
        assert (
            not second_acquired_lock.is_set()
        ), "Transaction 2 must block inside lock_company_unchecked while Tx 1 holds lock"
        assert not t_2.done()

        release_first.set()
        await asyncio.gather(t_1, t_2)
        assert second_acquired_lock.is_set()
    finally:
        masterdata_service.lock_company_unchecked = orig_lock

    return results


@pytest.mark.asyncio
async def test_concurrent_create_same_field_key_serialized_by_company_lock(
    client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Proves ADR-011 §5: concurrent creation of identical custom field definitions

    is strictly serialized by `masterdata_service.lock_company_unchecked`.
    """
    co = await setup_company_with_admin(client, code="CCCREATE")
    company_id = co.company_id
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    data = CustomFieldDefinitionCreate(
        entity_type=CustomFieldEntityType.CUSTOMER,
        field_key="tax_reg_id",
        label="Tax Registration ID",
        field_type=CustomFieldType.TEXT,
    )
    results = await _run_serialized_create_pair(session_factory, company_id, data)

    assert results["first"] == "success"
    assert "ConflictError" in results["second"]
    assert "already defined" in results["second"]

    # Verify database state has exactly 1 row
    async with session_factory() as session:
        with company_context(company_id):
            res = await session.execute(
                select(func.count())
                .select_from(CustomFieldDefinition)
                .where(
                    CustomFieldDefinition.company_id == company_id,
                    CustomFieldDefinition.field_key == "tax_reg_id",
                )
            )
            assert res.scalar_one() == 1


# ---------------------------------------------------------------------------
# 2. Concurrency: Update-Update Write-Skew Prevention (Bidirectional)
# ---------------------------------------------------------------------------


async def _run_serialized_update_pair(
    session_factory: async_sessionmaker[AsyncSession],
    company_id: uuid.UUID,
    field_id: uuid.UUID,
    first_update: CustomFieldDefinitionUpdate,
    second_update: CustomFieldDefinitionUpdate,
) -> dict[str, str]:
    """Runs two concurrent `update_custom_field_definition` calls on the same definition,

    verifying serialization via `masterdata_service.lock_company_unchecked`.
    """
    from app.modules.masterdata import service as masterdata_service

    orig_lock = masterdata_service.lock_company_unchecked
    call_count = 0
    first_locked, second_entering_lock, second_acquired_lock, release_first = (
        asyncio.Event() for _ in range(4)
    )

    async def instrumented_lock(session: AsyncSession, cid: uuid.UUID) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            row = await orig_lock(session, cid)
            first_locked.set()
            await release_first.wait()
            return row
        second_entering_lock.set()
        row = await orig_lock(session, cid)
        second_acquired_lock.set()
        return row

    results: dict[str, str] = {}

    async def run_update(
        label: str, payload: CustomFieldDefinitionUpdate, *, wait_first: bool
    ) -> None:
        if wait_first:
            await first_locked.wait()
        async with session_factory() as session:
            with actor_context(TEST_ADMIN_ACTOR), company_context(company_id):
                try:
                    await cf_service.update_custom_field_definition(session, field_id, payload)
                    results[label] = "success"
                except CustomFieldValidationError as exc:
                    await session.rollback()
                    results[label] = f"CustomFieldValidationError: {exc}"
                except Exception as exc:
                    await session.rollback()
                    results[label] = f"unexpected: {exc}"

    masterdata_service.lock_company_unchecked = instrumented_lock  # type: ignore[assignment]
    try:
        t_1 = asyncio.create_task(run_update("first", first_update, wait_first=False))
        await first_locked.wait()

        t_2 = asyncio.create_task(run_update("second", second_update, wait_first=True))
        await second_entering_lock.wait()

        await asyncio.sleep(0.05)
        assert (
            not second_acquired_lock.is_set()
        ), "Transaction 2 must block on lock_company_unchecked while Transaction 1 holds the lock"
        assert not t_2.done()

        release_first.set()
        await asyncio.gather(t_1, t_2)
        assert second_acquired_lock.is_set()
    finally:
        masterdata_service.lock_company_unchecked = orig_lock

    return results


@pytest.mark.asyncio
async def test_concurrent_update_rules_and_default_value_write_skew_prevented(
    client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Proves ADR-011 §5: write-skew between `validation_rules` mutations and

    `default_value` mutations is eliminated in both concurrency directions:

    Direction 1:
    - Initial: min_value=1, max_value=10, default_value=2
    - Tx 1 updates validation_rules -> max_value=5.
    - Tx 2 concurrently attempts to update default_value -> 8.
    - Tx 2 blocks on lock_company_unchecked. Once Tx 1 commits, Tx 2 sees the
      new max_value=5 rule, and setting default_value=8 is rejected with 422.

    Direction 2:
    - Initial: min_value=1, max_value=10, default_value=2
    - Tx 1 updates default_value -> 8.
    - Tx 2 concurrently attempts to update validation_rules -> max_value=5.
    - Tx 2 blocks on lock_company_unchecked. Once Tx 1 commits, Tx 2 re-validates
      the existing default_value=8 against its new max_value=5 rule and is rejected with 422.

    In neither direction can (max_value=5, default_value=8) ever be persisted.
    """
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # Direction 1
    co1 = await setup_company_with_admin(client, code="WSKEW1")
    async with session_factory() as session:
        with actor_context(TEST_ADMIN_ACTOR), company_context(co1.company_id):
            def1 = await cf_service.create_custom_field_definition(
                session,
                CustomFieldDefinitionCreate(
                    entity_type=CustomFieldEntityType.CUSTOMER,
                    field_key="priority_level",
                    label="Priority Level",
                    field_type=CustomFieldType.INTEGER,
                    validation_rules={"min_value": 1, "max_value": 10},
                    default_value=2,
                ),
            )
            field1_id = def1.id

    res1 = await _run_serialized_update_pair(
        session_factory,
        co1.company_id,
        field1_id,
        first_update=CustomFieldDefinitionUpdate(validation_rules={"min_value": 1, "max_value": 5}),
        second_update=CustomFieldDefinitionUpdate(default_value=8),
    )
    assert res1["first"] == "success"
    assert "CustomFieldValidationError" in res1["second"]
    assert "exceeds max_value 5" in res1["second"]

    # Verify persisted state: rules tightened to max=5, default remained 2
    async with session_factory() as session:
        with actor_context(TEST_ADMIN_ACTOR), company_context(co1.company_id):
            reread1 = await cf_service.get_custom_field_definition(session, field1_id)
            assert reread1.validation_rules == {"min_value": 1, "max_value": 5}
            assert reread1.default_value == 2

    # Direction 2
    co2 = await setup_company_with_admin(client, code="WSKEW2")
    async with session_factory() as session:
        with actor_context(TEST_ADMIN_ACTOR), company_context(co2.company_id):
            def2 = await cf_service.create_custom_field_definition(
                session,
                CustomFieldDefinitionCreate(
                    entity_type=CustomFieldEntityType.CUSTOMER,
                    field_key="priority_level",
                    label="Priority Level",
                    field_type=CustomFieldType.INTEGER,
                    validation_rules={"min_value": 1, "max_value": 10},
                    default_value=2,
                ),
            )
            field2_id = def2.id

    res2 = await _run_serialized_update_pair(
        session_factory,
        co2.company_id,
        field2_id,
        first_update=CustomFieldDefinitionUpdate(default_value=8),
        second_update=CustomFieldDefinitionUpdate(
            validation_rules={"min_value": 1, "max_value": 5}
        ),
    )
    assert res2["first"] == "success"
    assert "CustomFieldValidationError" in res2["second"]
    assert "exceeds max_value 5" in res2["second"]

    # Verify persisted state: default updated to 8, rules remained max=10
    async with session_factory() as session:
        with actor_context(TEST_ADMIN_ACTOR), company_context(co2.company_id):
            reread2 = await cf_service.get_custom_field_definition(session, field2_id)
            assert reread2.validation_rules == {"min_value": 1, "max_value": 10}
            assert reread2.default_value == 8


# ---------------------------------------------------------------------------
# 3. Concurrency: Cross-Tenant Lock Independence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_definition_mutations_do_not_block_each_other(
    client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Proves ADR-011 §5 multi-tenant concurrency performance:

    Holding `lock_company_unchecked` for Company A does NOT block definition mutations
    in Company B.
    """
    from app.modules.masterdata import service as masterdata_service

    co_a = await setup_company_with_admin(client, code="TENANTA")
    co_b = await setup_company_with_admin(client, code="TENANTB")
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    orig_lock = masterdata_service.lock_company_unchecked
    co_a_locked = asyncio.Event()
    release_co_a = asyncio.Event()

    async def instrumented_lock(session: AsyncSession, cid: uuid.UUID) -> Any:
        row = await orig_lock(session, cid)
        if cid == co_a.company_id:
            co_a_locked.set()
            await release_co_a.wait()
        return row

    co_b_completed = False

    async def run_company_a() -> None:
        async with session_factory() as session:
            with actor_context(TEST_ADMIN_ACTOR), company_context(co_a.company_id):
                await cf_service.create_custom_field_definition(
                    session,
                    CustomFieldDefinitionCreate(
                        entity_type=CustomFieldEntityType.CUSTOMER,
                        field_key="field_a",
                        label="Field A",
                        field_type=CustomFieldType.TEXT,
                    ),
                )

    async def run_company_b() -> None:
        nonlocal co_b_completed
        await co_a_locked.wait()
        # Company A is currently locked in its critical section.
        # Company B MUST succeed immediately without waiting for release_co_a!
        async with session_factory() as session:
            with actor_context(TEST_ADMIN_ACTOR), company_context(co_b.company_id):
                await cf_service.create_custom_field_definition(
                    session,
                    CustomFieldDefinitionCreate(
                        entity_type=CustomFieldEntityType.CUSTOMER,
                        field_key="field_b",
                        label="Field B",
                        field_type=CustomFieldType.TEXT,
                    ),
                )
                co_b_completed = True

    masterdata_service.lock_company_unchecked = instrumented_lock  # type: ignore[assignment]
    try:
        t_a = asyncio.create_task(run_company_a())
        await co_a_locked.wait()

        t_b = asyncio.create_task(run_company_b())
        # Give t_b time to complete
        await asyncio.wait_for(t_b, timeout=2.0)
        assert (
            co_b_completed is True
        ), "Company B must complete while Company A is still holding its lock"

        # Now release Company A
        release_co_a.set()
        await t_a
    finally:
        masterdata_service.lock_company_unchecked = orig_lock

    # Verify both definitions exist in their respective companies
    async with session_factory() as session:
        with actor_context(TEST_ADMIN_ACTOR), company_context(co_a.company_id):
            defs_a = await cf_service.list_custom_field_definitions(session)
            assert [d.field_key for d in defs_a] == ["field_a"]
        with actor_context(TEST_ADMIN_ACTOR), company_context(co_b.company_id):
            defs_b = await cf_service.list_custom_field_definitions(session)
            assert [d.field_key for d in defs_b] == ["field_b"]


# ---------------------------------------------------------------------------
# 3b. Concurrency: Deactivate vs Update Serialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_deactivate_and_update_serialized_by_company_lock(
    client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Proves ADR-011 §5: `deactivate_custom_field_definition` acquires

    `lock_company_unchecked` and serializes against concurrent `update_custom_field_definition`.
    """
    from app.modules.masterdata import service as masterdata_service

    co = await setup_company_with_admin(client, code="DEACTLCK")
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with session_factory() as session:
        with actor_context(TEST_ADMIN_ACTOR), company_context(co.company_id):
            field_id = (
                await cf_service.create_custom_field_definition(
                    session,
                    CustomFieldDefinitionCreate(
                        entity_type=CustomFieldEntityType.CUSTOMER,
                        field_key="region_code",
                        label="Region Code",
                        field_type=CustomFieldType.TEXT,
                    ),
                )
            ).id

    orig_lock = masterdata_service.lock_company_unchecked
    call_count = 0
    first_locked, second_entering_lock, second_acquired_lock, release_first = (
        asyncio.Event() for _ in range(4)
    )

    async def instrumented_lock(session: AsyncSession, cid: uuid.UUID) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            row = await orig_lock(session, cid)
            first_locked.set()
            await release_first.wait()
            return row
        second_entering_lock.set()
        row = await orig_lock(session, cid)
        second_acquired_lock.set()
        return row

    async def run_deactivate() -> None:
        async with session_factory() as session:
            with actor_context(TEST_ADMIN_ACTOR), company_context(co.company_id):
                await cf_service.deactivate_custom_field_definition(session, field_id)

    async def run_update_label() -> None:
        await first_locked.wait()
        async with session_factory() as session:
            with actor_context(TEST_ADMIN_ACTOR), company_context(co.company_id):
                await cf_service.update_custom_field_definition(
                    session,
                    field_id,
                    CustomFieldDefinitionUpdate(label="Updated Region Code"),
                )

    masterdata_service.lock_company_unchecked = instrumented_lock  # type: ignore[assignment]
    try:
        t_deact = asyncio.create_task(run_deactivate())
        await first_locked.wait()

        t_upd = asyncio.create_task(run_update_label())
        await second_entering_lock.wait()

        await asyncio.sleep(0.05)
        assert (
            not second_acquired_lock.is_set()
        ), "Concurrent update must block while deactivate holds company row lock"
        assert not t_upd.done()

        release_first.set()
        await asyncio.gather(t_deact, t_upd)
        assert second_acquired_lock.is_set()
    finally:
        masterdata_service.lock_company_unchecked = orig_lock

    async with session_factory() as session:
        with actor_context(TEST_ADMIN_ACTOR), company_context(co.company_id):
            final_def = await cf_service.get_custom_field_definition(session, field_id)
            assert final_def.is_active is False
            assert final_def.label == "Updated Region Code"


# ---------------------------------------------------------------------------
# 4. E2E Custom Field Lifecycle, Banker's Rounding, and Deactivation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_custom_field_lifecycle_deactivation_reactivation_and_decimal_rounding(
    client: AsyncClient,
) -> None:
    """E2E lifecycle test via HTTP API:

    1. Creates DECIMAL custom field with Banker's Rounding default and rules.
    2. Exercises Banker's Rounding (ROUND_HALF_EVEN) on Customer entity creation:
       - 10.0000005 -> 10.000000 (rounds to even 0)
       - 10.0000015 -> 10.000002 (rounds to even 2)
    3. Soft-deactivates the field via DELETE /api/v1/custom-fields/{id}.
    4. Proves fail-closed behavior: passing the deactivated field on customer creation
       fails with 422 (Unknown custom field).
    5. Reactivates field via PATCH /api/v1/custom-fields/{id} (is_active = True).
    6. Confirms customer creation succeeds again with the field active.
    """
    co = await setup_company_with_admin(client, code="E2ELIFE")

    # 1. Define DECIMAL custom field
    resp_def = await client.post(
        "/api/v1/custom-fields",
        json={
            "entity_type": "customer",
            "field_key": "royalty_rate",
            "label": "Royalty Rate",
            "field_type": "DECIMAL",
            "is_required": False,
            "default_value": "5.000000",
            "validation_rules": {"min_value": "0.000000", "max_value": "100.000000"},
        },
        headers=co.headers,
    )
    assert resp_def.status_code == 201, resp_def.text
    field_id = resp_def.json()["id"]

    # 2. Banker's Rounding verification on Customer creation
    # Case A: 10.0000005 rounds to even -> 10.000000
    resp_cust1 = await client.post(
        "/api/v1/customers",
        json={
            "code": "CUST-ROUND1",
            "name": "Customer Round 1",
            "currency_code": "TWD",
            "custom_data": {"royalty_rate": "10.0000005"},
        },
        headers=co.headers,
    )
    assert resp_cust1.status_code == 201, resp_cust1.text
    assert resp_cust1.json()["custom_data"]["royalty_rate"] == "10.000000"

    # Case B: 10.0000015 rounds to even -> 10.000002
    resp_cust2 = await client.post(
        "/api/v1/customers",
        json={
            "code": "CUST-ROUND2",
            "name": "Customer Round 2",
            "currency_code": "TWD",
            "custom_data": {"royalty_rate": "10.0000015"},
        },
        headers=co.headers,
    )
    assert resp_cust2.status_code == 201, resp_cust2.text
    assert resp_cust2.json()["custom_data"]["royalty_rate"] == "10.000002"

    # Case C: Exceeding max_value 100.000000 is rejected with 422
    resp_cust_exceed = await client.post(
        "/api/v1/customers",
        json={
            "code": "CUST-EXCEED",
            "name": "Customer Exceed",
            "currency_code": "TWD",
            "custom_data": {"royalty_rate": "150.000000"},
        },
        headers=co.headers,
    )
    assert resp_cust_exceed.status_code == 422
    assert "exceeds max_value" in resp_cust_exceed.text

    # 3. Soft-deactivate field definition
    resp_del = await client.delete(f"/api/v1/custom-fields/{field_id}", headers=co.headers)
    assert resp_del.status_code == 200, resp_del.text
    assert resp_del.json()["is_active"] is False

    # 4. Proves fail-closed: submitting deactivated field raises 422 Unknown custom field
    resp_cust_deact = await client.post(
        "/api/v1/customers",
        json={
            "code": "CUST-DEACT",
            "name": "Customer Deactivated",
            "currency_code": "TWD",
            "custom_data": {"royalty_rate": "10.000000"},
        },
        headers=co.headers,
    )
    assert resp_cust_deact.status_code == 422
    assert "Unknown custom field 'royalty_rate'" in resp_cust_deact.text

    # Partial update on existing customer with deactivated field key also fails closed (422),
    # while partial update without touching the deactivated key preserves historical custom_data
    cust1_id = resp_cust1.json()["id"]
    resp_cust1_patch_fail = await client.patch(
        f"/api/v1/customers/{cust1_id}",
        json={"custom_data": {"royalty_rate": "20.000000"}},
        headers=co.headers,
    )
    assert resp_cust1_patch_fail.status_code == 422
    assert "Unknown custom field 'royalty_rate'" in resp_cust1_patch_fail.text

    resp_cust1_patch_preserve = await client.patch(
        f"/api/v1/customers/{cust1_id}",
        json={"name": "Customer Round 1 Renamed", "custom_data": {}},
        headers=co.headers,
    )
    assert resp_cust1_patch_preserve.status_code == 200, resp_cust1_patch_preserve.text
    assert resp_cust1_patch_preserve.json()["name"] == "Customer Round 1 Renamed"
    assert resp_cust1_patch_preserve.json()["custom_data"]["royalty_rate"] == "10.000000"

    # Customers created without the deactivated field succeed
    resp_cust_clean = await client.post(
        "/api/v1/customers",
        json={
            "code": "CUST-CLEAN",
            "name": "Customer Clean",
            "currency_code": "TWD",
            "custom_data": {},
        },
        headers=co.headers,
    )
    assert resp_cust_clean.status_code == 201
    assert "royalty_rate" not in resp_cust_clean.json()["custom_data"]

    # 5. Reactivate field definition via PATCH
    resp_patch = await client.patch(
        f"/api/v1/custom-fields/{field_id}",
        json={"is_active": True},
        headers=co.headers,
    )
    assert resp_patch.status_code == 200, resp_patch.text
    assert resp_patch.json()["is_active"] is True

    # 6. Once reactivated, submitting royalty_rate succeeds again
    resp_cust_react = await client.post(
        "/api/v1/customers",
        json={
            "code": "CUST-REACT",
            "name": "Customer Reactivated",
            "currency_code": "TWD",
            "custom_data": {"royalty_rate": "25.000000"},
        },
        headers=co.headers,
    )
    assert resp_cust_react.status_code == 201, resp_cust_react.text
    assert resp_cust_react.json()["custom_data"]["royalty_rate"] == "25.000000"
