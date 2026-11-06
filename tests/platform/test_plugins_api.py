"""tests/platform/test_plugins_api.py — End-to-end tenant plugin governance, concurrency proof,
and replay_outbox compatibility (ADR-010 Action Item 4 / Slice D).

Covers:
1. End-to-end tenant governance with the real built-in `credit_limit` plugin (v1.0.0):
   - Per-company enable/disable and Banker's Rounding `grace_amount` configuration via HTTP API.
   - Live interaction driving real `POST /api/v1/sales-orders/{id}/confirm` decisions.
   - Multi-tenant isolation between Company A and Company B.
2. Runtime dependency guards via HTTP API:
   - 422 Unprocessable Entity when enabling a dependent plugin whose prerequisite is disabled.
   - 409 Conflict when disabling a prerequisite plugin while an active dependent relies on it.
   - 422 Unprocessable Entity on schema violations / unknown extra fields.
3. Multi-session concurrency proof on `masterdata_service.lock_company_unchecked`:
   - Proves ADR-010 §3.3 & Option C write-skew prevention on parent company rows.
   - Verifies serialization in both directions on newly created companies
     (0 rows in `company_plugins`).
   - Asserts the invalid state `(prereq=False, dependent=True)` can never occur.
4. `replay_outbox` compatibility with gated event-subscribing plugins:
   - Proves ADR-009 Rule 10a & ADR-010 Context #5 / §4.2 compatibility.
   - Runs `replay_outbox.replay_outbox()` with NO `ActorContext` bound.
   - Invokes event handler for enabled tenant with its custom config and bypasses disabled tenant.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app import main as _app_main
from app.cli import replay_outbox
from app.core import events, hooks
from app.core.authorization import (
    actor_context,
    current_actor_or_none,
)
from app.core.exceptions import ConflictError, DomainValidationError
from app.core.plugins import (
    EventBinding,
    PluginDependency,
    PluginManifest,
    get_current_plugin_config,
    get_plugin_runtime_gate,
    set_plugin_runtime_gate,
)
from app.core.tenancy import company_context, require_current_company_id
from app.platform.plugins import loader
from app.platform.plugins.models import CompanyPlugin
from app.platform.plugins.schemas import CompanyPluginUpdate
from app.platform.plugins.service import (
    execute_with_plugin_gate_unchecked,
    update_company_plugin,
)
from tests._auth_helpers import setup_company_with_admin
from tests.conftest import TEST_ADMIN_ACTOR
from tests.sales._helpers import (
    confirm_order,
    create_customer,
    create_draft_order,
    create_product,
    order_line,
)


@pytest.fixture(autouse=True)
def clean_builtin_plugins_state() -> Iterator[None]:
    """Ensures clean plugin loader and runtime gate state for each test."""
    saved_gate = get_plugin_runtime_gate()
    saved_loaded = dict(loader._loaded_plugins)
    saved_hook_specs = dict(hooks._hook_specs)
    saved_handlers = {k: list(v) for k, v in hooks._handlers.items()}
    saved_event_schemas = dict(events._schemas)
    saved_event_handlers = {k: list(v) for k, v in events._handlers.items()}

    loader.reset_loaded_plugins()
    loader.bootstrap_plugins(_app_main.BUILTIN_PLUGIN_MANIFESTS, enable_entrypoints=False)
    set_plugin_runtime_gate(execute_with_plugin_gate_unchecked)

    yield

    loader.reset_loaded_plugins()
    loader._loaded_plugins.update(saved_loaded)
    hooks._hook_specs.clear()
    hooks._hook_specs.update(saved_hook_specs)
    hooks._handlers.clear()
    hooks._handlers.update(saved_handlers)
    events._schemas.clear()
    events._schemas.update(saved_event_schemas)
    events._handlers.clear()
    events._handlers.update(saved_event_handlers)
    set_plugin_runtime_gate(saved_gate)


# ---------------------------------------------------------------------------
# 1. End-to-end built-in credit_limit governance via HTTP API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_builtin_credit_limit_governance_and_grace_amount_via_http_api(
    client: AsyncClient,
) -> None:
    """Verifies ADR-010 Action Item 4 end-to-end tenant governance with the real

    built-in credit_limit plugin:
    - Lists plugins and fetches credit_limit default status (enabled, grace_amount=0.000000).
    - Blocks confirm_order over limit with 409.
    - Configures grace_amount via PATCH /api/v1/plugins/credit_limit with Banker's Rounding.
    - Successfully unblocks confirm_order under the new grace ceiling.
    - Proves cross-company isolation: Company B remains blocked until explicitly configured.
    """
    co_a = await setup_company_with_admin(client, code="E2EA")
    co_b = await setup_company_with_admin(client, code="E2EB")

    # 1. Verify plugin discovery and default status for Company A
    list_resp = await client.get("/api/v1/plugins", headers=co_a.headers)
    assert list_resp.status_code == 200, list_resp.text
    plugins = {p["plugin_name"]: p for p in list_resp.json()}
    assert "credit_limit" in plugins
    cl_info = plugins["credit_limit"]
    assert cl_info["version"] == "1.0.0"
    assert cl_info["title"] == "Customer Credit Limit Guard"
    assert cl_info["is_enabled"] is True
    assert cl_info["config"] == {"grace_amount": "0.000000"}

    # 2. Setup master data for Company A and Company B
    cust_a = await create_customer(client, co_a.company_id, "CUST-A", credit_limit="1000")
    cust_b = await create_customer(client, co_b.company_id, "CUST-B", credit_limit="1000")
    prod_a = await create_product(client, co_a.company_id, "SKU-A", list_price="100")
    prod_b = await create_product(client, co_b.company_id, "SKU-B", list_price="100")

    # 3. Create order in Company A for 1200.000000 (12 * 100 > credit_limit 1000)
    order_a = await create_draft_order(
        client, co_a.company_id, cust_a, [order_line(prod_a, "12", unit_price="100")]
    )
    confirm_a_blocked = await confirm_order(client, co_a.company_id, order_a["id"])
    assert confirm_a_blocked.status_code == 409
    assert confirm_a_blocked.json()["detail"] == "Sales order exceeds the customer's credit limit"

    # Verify order stays in draft
    reread_a = await client.get(f"/api/v1/sales-orders/{order_a['id']}", headers=co_a.headers)
    assert reread_a.json()["status"] == "draft"

    # 4. Configure grace_amount for Company A to 250.1234565 via PATCH /api/v1/plugins/credit_limit
    # Banker's Rounding: 250.1234565 -> 250.123456; ceiling = 1000 + 250.123456 >= 1200
    patch_a = await client.patch(
        "/api/v1/plugins/credit_limit",
        json={"config": {"grace_amount": "250.1234565"}},
        headers=co_a.headers,
    )
    assert patch_a.status_code == 200, patch_a.text
    patched_body_a = patch_a.json()
    assert patched_body_a["is_enabled"] is True
    assert patched_body_a["config"]["grace_amount"] == "250.123456"

    # 5. Company A confirm now succeeds under the grace allowance
    confirm_a_success = await confirm_order(client, co_a.company_id, order_a["id"])
    assert confirm_a_success.status_code == 200, confirm_a_success.text
    assert confirm_a_success.json()["status"] == "confirmed"

    # 6. Verify cross-company isolation: Company B remains blocked (grace_amount still 0)
    order_b = await create_draft_order(
        client, co_b.company_id, cust_b, [order_line(prod_b, "12", unit_price="100")]
    )
    confirm_b_blocked = await confirm_order(client, co_b.company_id, order_b["id"])
    assert confirm_b_blocked.status_code == 409
    assert confirm_b_blocked.json()["detail"] == "Sales order exceeds the customer's credit limit"

    # 7. Disable credit_limit plugin entirely in Company B
    patch_b = await client.patch(
        "/api/v1/plugins/credit_limit",
        json={"is_enabled": False},
        headers=co_b.headers,
    )
    assert patch_b.status_code == 200, patch_b.text
    assert patch_b.json()["is_enabled"] is False

    # Company B confirm now succeeds with plugin disabled
    confirm_b_success = await confirm_order(client, co_b.company_id, order_b["id"])
    assert confirm_b_success.status_code == 200, confirm_b_success.text
    assert confirm_b_success.json()["status"] == "confirmed"

    # Verify Company A's plugin status is still enabled and configured
    get_a = await client.get("/api/v1/plugins/credit_limit", headers=co_a.headers)
    assert get_a.status_code == 200
    assert get_a.json()["is_enabled"] is True
    assert get_a.json()["config"]["grace_amount"] == "250.123456"


# ---------------------------------------------------------------------------
# 2. Runtime dependency guards & validation via HTTP API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plugins_api_dependency_guards_and_validation_via_http(
    client: AsyncClient,
) -> None:
    """Verifies runtime dependency validation via HTTP API:

    - 422 when enabling dependent plugin while prerequisite is disabled.
    - 409 when disabling prerequisite while dependent is active.
    - 422 when config violates schema (unknown field or invalid value).
    """
    manifest_prereq = PluginManifest(
        name="prereq_plugin",
        version="1.0.0",
        title="Prerequisite Plugin",
        description="Required base capability",
        default_enabled=False,
    )
    manifest_dep = PluginManifest(
        name="dep_plugin",
        version="1.0.0",
        title="Dependent Plugin",
        description="Requires prereq_plugin",
        default_enabled=False,
        requires=(PluginDependency("prereq_plugin", "1.0.0"),),
    )
    loader.bootstrap_plugins(
        [*_app_main.BUILTIN_PLUGIN_MANIFESTS, manifest_prereq, manifest_dep],
        enable_entrypoints=False,
    )

    co = await setup_company_with_admin(client, code="DEPGUARD")

    # 1. Enabling dep_plugin while prereq_plugin is disabled fails with 422
    resp_enable_dep = await client.patch(
        "/api/v1/plugins/dep_plugin",
        json={"is_enabled": True},
        headers=co.headers,
    )
    assert resp_enable_dep.status_code == 422
    assert (
        "prerequisite plugin 'prereq_plugin' is disabled for this company" in resp_enable_dep.text
    )

    # 2. Enable prereq_plugin first -> succeeds 200
    resp_enable_prereq = await client.patch(
        "/api/v1/plugins/prereq_plugin",
        json={"is_enabled": True},
        headers=co.headers,
    )
    assert resp_enable_prereq.status_code == 200

    # 3. Now enable dep_plugin -> succeeds 200
    resp_enable_dep_ok = await client.patch(
        "/api/v1/plugins/dep_plugin",
        json={"is_enabled": True},
        headers=co.headers,
    )
    assert resp_enable_dep_ok.status_code == 200
    assert resp_enable_dep_ok.json()["is_enabled"] is True

    # 4. Disabling prereq_plugin while dep_plugin is active fails with 409 Conflict
    resp_disable_prereq = await client.patch(
        "/api/v1/plugins/prereq_plugin",
        json={"is_enabled": False},
        headers=co.headers,
    )
    assert resp_disable_prereq.status_code == 409
    assert (
        "dependent plugin 'dep_plugin' is currently enabled for this company"
        in resp_disable_prereq.text
    )

    # 5. Schema validation: extra properties on credit_limit config rejected with 422
    resp_bad_config = await client.patch(
        "/api/v1/plugins/credit_limit",
        json={"config": {"extra_key": "not_allowed"}},
        headers=co.headers,
    )
    assert resp_bad_config.status_code == 422
    assert "Configuration validation failed for plugin 'credit_limit'" in resp_bad_config.text


# ---------------------------------------------------------------------------
# 3. Multi-session concurrency proof on lock_company_unchecked (Write-Skew)
# ---------------------------------------------------------------------------


async def _run_serialized_pair(
    session_factory: async_sessionmaker[AsyncSession],
    company_id: uuid.UUID,
    first_mutation: tuple[str, CompanyPluginUpdate],
    second_mutation: tuple[str, CompanyPluginUpdate],
) -> dict[str, str]:
    """Run two `update_company_plugin` calls concurrently on a company with 0 plugin rows.

    Intercepts `masterdata_service.lock_company_unchecked` so that:
    1. Transaction 1 acquires the PostgreSQL `SELECT ... FOR UPDATE` lock on `companies`
       and pauses at the start of the critical section before reading `company_plugins`.
    2. Transaction 2 enters `lock_company_unchecked` and is proven to block on the DB row lock.
    3. Transaction 1 is released to validate, upsert, and commit, releasing the DB lock.
    4. Transaction 2 wakes up, reads the committed snapshot, and enforces dependency invariants.
    """
    from app.modules.masterdata import service as masterdata_service

    async with session_factory() as check_session:
        with company_context(company_id):
            res = await check_session.execute(
                select(func.count())
                .select_from(CompanyPlugin)
                .where(CompanyPlugin.company_id == company_id)
            )
            assert res.scalar_one() == 0, "New company must have 0 rows in company_plugins"

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

    async def run_tx(
        label: str, plugin_name: str, payload: CompanyPluginUpdate, *, wait_first: bool
    ) -> None:
        if wait_first:
            await first_locked.wait()
        async with session_factory() as session:
            with actor_context(TEST_ADMIN_ACTOR), company_context(company_id):
                try:
                    await update_company_plugin(session, plugin_name, payload)
                    results[label] = "success"
                except (DomainValidationError, ConflictError) as exc:
                    await session.rollback()
                    results[label] = f"{type(exc).__name__}: {exc}"
                except Exception as exc:
                    await session.rollback()
                    results[label] = f"unexpected: {exc}"

    masterdata_service.lock_company_unchecked = instrumented_lock  # type: ignore[assignment]
    try:
        t_1 = asyncio.create_task(
            run_tx("first", first_mutation[0], first_mutation[1], wait_first=False)
        )
        await first_locked.wait()
        t_2 = asyncio.create_task(
            run_tx("second", second_mutation[0], second_mutation[1], wait_first=True)
        )
        await second_entering_lock.wait()

        await asyncio.sleep(0.05)
        assert (
            not second_acquired_lock.is_set()
        ), "Second transaction must block inside lock_company_unchecked while first holds lock"
        assert not t_2.done()

        release_first.set()
        await asyncio.gather(t_1, t_2)
        assert second_acquired_lock.is_set()
    finally:
        masterdata_service.lock_company_unchecked = orig_lock  # type: ignore[assignment]

    return results


async def _assert_concurrency_direction_1(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    co1 = await setup_company_with_admin(client, code="CONC1")
    results = await _run_serialized_pair(
        session_factory,
        co1.company_id,
        first_mutation=("prereq_toggle", CompanyPluginUpdate(is_enabled=False)),
        second_mutation=("dep_toggle", CompanyPluginUpdate(is_enabled=True)),
    )

    assert results["first"] == "success"
    assert "DomainValidationError" in results["second"]
    assert "prerequisite plugin 'prereq_toggle' is disabled for this company" in results["second"]

    async with session_factory() as check_session:
        with company_context(co1.company_id):
            res = await check_session.execute(
                select(CompanyPlugin).where(CompanyPlugin.company_id == co1.company_id)
            )
            persisted = {p.plugin_name: p.is_enabled for p in res.scalars().all()}
            assert persisted.get("prereq_toggle") is False
            assert persisted.get("dep_toggle", False) is False


async def _assert_concurrency_direction_2(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    co2 = await setup_company_with_admin(client, code="CONC2")
    results = await _run_serialized_pair(
        session_factory,
        co2.company_id,
        first_mutation=("dep_toggle", CompanyPluginUpdate(is_enabled=True)),
        second_mutation=("prereq_toggle", CompanyPluginUpdate(is_enabled=False)),
    )

    assert results["first"] == "success"
    assert "ConflictError" in results["second"]
    assert (
        "dependent plugin 'dep_toggle' is currently enabled for this company" in results["second"]
    )

    async with session_factory() as check_session:
        with company_context(co2.company_id):
            res = await check_session.execute(
                select(CompanyPlugin).where(CompanyPlugin.company_id == co2.company_id)
            )
            persisted = {p.plugin_name: p.is_enabled for p in res.scalars().all()}
            assert persisted.get("dep_toggle") is True
            assert persisted.get("prereq_toggle", True) is True


@pytest.mark.asyncio
async def test_concurrent_plugin_toggle_write_skew_serialized_by_lock_company_unchecked(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    """Proves ADR-010 §3.3 & Option C: concurrent write-skew prevention on newly

    created companies with zero rows in `company_plugins`.

    Scenario:
    - `prereq_plugin`: default_enabled=True
    - `dependent_plugin`: default_enabled=False, requires=[prereq_plugin]
    - An empty company table has NO rows in `company_plugins`.
    - If locking was applied on `CompanyPlugin` rows alone, Transaction A (disabling prereq)
      and Transaction B (enabling dependent) would lock zero rows, leading to phantom/write-skew.
    - `lock_company_unchecked` performs `SELECT * FROM companies WHERE id = :cid FOR UPDATE`,
      guaranteeing that Transaction A and Transaction B are strictly serialized.

    We test both concurrency directions:
    1. Direction 1: Transaction A disables prereq first; Transaction B blocks, resumes,
       and fails closed with DomainValidationError (422).
    2. Direction 2: Transaction B enables dependent first; Transaction A blocks, resumes,
       and fails closed with ConflictError (409).
    In neither case can (prereq=False, dependent=True) ever be persisted.
    """
    manifest_prereq = PluginManifest(
        name="prereq_toggle",
        version="1.0.0",
        title="Prereq Toggle",
        description="Default enabled prerequisite",
        default_enabled=True,
    )
    manifest_dep = PluginManifest(
        name="dep_toggle",
        version="1.0.0",
        title="Dependent Toggle",
        description="Requires prereq_toggle",
        default_enabled=False,
        requires=(PluginDependency("prereq_toggle", "1.0.0"),),
    )
    loader.bootstrap_plugins(
        [*_app_main.BUILTIN_PLUGIN_MANIFESTS, manifest_prereq, manifest_dep],
        enable_entrypoints=False,
    )

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    await _assert_concurrency_direction_1(client, session_factory)
    await _assert_concurrency_direction_2(client, session_factory)


# ---------------------------------------------------------------------------
# 4. replay_outbox compatibility with gated event-subscribing plugins
# ---------------------------------------------------------------------------


class AuditEventConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tag: str = "DEFAULT"


_AUDIT_EVENT_LOG: list[tuple[uuid.UUID, str, str]] = []


async def _handle_order_confirmed_audit(session: AsyncSession, payload: Any) -> None:
    del session
    cid = require_current_company_id()
    cfg = get_current_plugin_config(AuditEventConfig)
    tag_value = cfg.tag
    order_id = getattr(payload, "source_id", None) or (
        payload.get("source_id") if isinstance(payload, dict) else None
    )
    _AUDIT_EVENT_LOG.append((cid, tag_value, str(order_id)))


@pytest.mark.asyncio
async def test_replay_outbox_executes_gated_event_plugins_without_actor_context(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    """Proves ADR-009 Rule 10a & ADR-010 Context #5 / §4.2 compatibility:

    - replay_outbox executes background event redispatch with NO ActorContext bound.
    - execute_with_plugin_gate_unchecked succeeds without requiring ActorContext or
      platform.plugin.read permission.
    - Company A (plugin enabled with custom config tag) invokes event handler.
    - Company B (plugin disabled) bypasses event handler without error.
    - Both outbox rows are successfully marked as dispatched_at IS NOT NULL.
    """
    _AUDIT_EVENT_LOG.clear()

    manifest_audit = PluginManifest(
        name="audit_event_plugin",
        version="1.0.0",
        title="Audit Event Subscriber",
        description="Records sales order confirmations to in-memory audit log",
        default_enabled=True,
        config_schema=AuditEventConfig,
        events=(
            EventBinding(
                event_type="sales.order_confirmed",
                handler=_handle_order_confirmed_audit,
            ),
        ),
    )
    loader.bootstrap_plugins(
        [*_app_main.BUILTIN_PLUGIN_MANIFESTS, manifest_audit],
        enable_entrypoints=False,
    )
    set_plugin_runtime_gate(execute_with_plugin_gate_unchecked)

    co_a = await setup_company_with_admin(client, code="REPLA")
    co_b = await setup_company_with_admin(client, code="REPLB")

    # Company A: custom config
    patch_a = await client.patch(
        "/api/v1/plugins/audit_event_plugin",
        json={"is_enabled": True, "config": {"tag": "CUSTOM_TAG_A"}},
        headers=co_a.headers,
    )
    assert patch_a.status_code == 200

    # Company B: disabled
    patch_b = await client.patch(
        "/api/v1/plugins/audit_event_plugin",
        json={"is_enabled": False},
        headers=co_b.headers,
    )
    assert patch_b.status_code == 200

    # Create and confirm one order in Company A and one order in Company B
    cust_a = await create_customer(client, co_a.company_id, "CUST-RA", credit_limit="0")
    cust_b = await create_customer(client, co_b.company_id, "CUST-RB", credit_limit="0")
    prod_a = await create_product(client, co_a.company_id, "SKU-RA", list_price="50")
    prod_b = await create_product(client, co_b.company_id, "SKU-RB", list_price="50")

    order_a = await create_draft_order(
        client, co_a.company_id, cust_a, [order_line(prod_a, "1", unit_price="50")]
    )
    order_b = await create_draft_order(
        client, co_b.company_id, cust_b, [order_line(prod_b, "1", unit_price="50")]
    )

    conf_a = await confirm_order(client, co_a.company_id, order_a["id"])
    assert conf_a.status_code == 200
    conf_b = await confirm_order(client, co_b.company_id, order_b["id"])
    assert conf_b.status_code == 200

    # Clear in-memory log before replay verification
    _AUDIT_EVENT_LOG.clear()

    # Reset dispatched_at to NULL on both outbox rows to simulate replay pending status
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        await session.execute(
            update(events.OUTBOX_TABLE)
            .where(events.OUTBOX_TABLE.c.event_type == "sales.order_confirmed")
            .values(dispatched_at=None)
        )
        await session.commit()

    # Confirm that current execution environment has NO actor context bound (ADR-009 Rule 10a)
    assert current_actor_or_none() is None, "Replay test must run with no active ActorContext"

    # Execute replay_outbox
    summary = await replay_outbox.replay_outbox()

    # Assert no failures during replay
    assert summary.failed == 0
    assert summary.total >= 2

    # Verify event handler invocation log:
    # Company A handler MUST have executed with CUSTOM_TAG_A
    logged_companies = {entry[0] for entry in _AUDIT_EVENT_LOG}
    assert co_a.company_id in logged_companies
    assert (
        co_b.company_id not in logged_companies
    ), "Company B handler must be bypassed because plugin is disabled"

    matching_entry_a = next(entry for entry in _AUDIT_EVENT_LOG if entry[0] == co_a.company_id)
    assert matching_entry_a[1] == "CUSTOM_TAG_A"
    assert matching_entry_a[2] == order_a["id"]

    # Verify both outbox rows in DB are now marked dispatched
    async with session_factory() as session:
        res = await session.execute(
            select(func.count())
            .select_from(events.OUTBOX_TABLE)
            .where(
                events.OUTBOX_TABLE.c.event_type == "sales.order_confirmed",
                events.OUTBOX_TABLE.c.dispatched_at.is_(None),
            )
        )
        assert res.scalar_one() == 0, "All replayed outbox rows must have dispatched_at populated"
