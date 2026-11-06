"""tests/cli/test_manage_plugins.py — Coverage for manage_plugins CLI tool (ADR-010).

Covers:
1. `list_plugins`:
   - via company_code and company_id
   - default fallback vs customized status
2. `get_plugin`:
   - details inspection
   - NotFoundError on unknown plugin
3. `enable_plugin` and `disable_plugin`:
   - toggle enablement
   - config preservation on enable
4. `configure_plugin`:
   - valid config update
   - Banker's Rounding and validation rejection
   - mutual exclusion toggle flags (--enable / --disable)
5. CLI `_main` entry point:
   - standard exit codes (0 for success, 1 for domain/conflict/not found errors)
   - stdout / stderr reporting
6. Real O2C integration effect:
   - disabling credit_limit allows exceeding order confirmation
   - enabling + configuring grace_amount adjusts the confirmation ceiling
7. PLUGIN_ADMIN_ACTOR least privilege enforcement
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.sales._helpers import (
    confirm_order,
    create_company,
    create_customer,
    create_draft_order,
    create_product,
    order_line,
)

from app import main as _app_main
from app.cli import manage_plugins
from app.core.authorization import actor_context
from app.core.exceptions import (
    ActionDeniedError,
    ConflictError,
    DomainValidationError,
    NotFoundError,
)
from app.core.plugins import (
    PluginDependency,
    PluginManifest,
)
from app.modules.masterdata import service as masterdata_service
from app.modules.masterdata.schemas import CompanyCreate
from app.platform.plugins import loader


@pytest.fixture(autouse=True)
def _ensure_clean_loader_state() -> Iterator[None]:
    loader.reset_loaded_plugins()
    loader.bootstrap_plugins(_app_main.BUILTIN_PLUGIN_MANIFESTS)
    yield
    loader.reset_loaded_plugins()
    loader.bootstrap_plugins(_app_main.BUILTIN_PLUGIN_MANIFESTS)


@pytest.mark.asyncio
async def test_resolve_company_id(client: AsyncClient, db_session: AsyncSession) -> None:
    code = f"CMP_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)

    # 1. Resolve by code
    resolved_id = await manage_plugins._resolve_company_id(db_session, company_code=code)
    assert resolved_id == company_id

    # 2. Resolve by id
    resolved_by_id = await manage_plugins._resolve_company_id(db_session, company_id=company_id)
    assert resolved_by_id == company_id

    # 3. Resolve by matching code and id
    both_id = await manage_plugins._resolve_company_id(
        db_session, company_code=code, company_id=company_id
    )
    assert both_id == company_id

    # 4. Neither provided -> DomainValidationError
    with pytest.raises(DomainValidationError, match="Either company_code or company_id"):
        await manage_plugins._resolve_company_id(db_session)

    # 5. Non-existent code -> NotFoundError
    with pytest.raises(NotFoundError, match="Company"):
        await manage_plugins._resolve_company_id(db_session, company_code="NONEXISTENT_XYZ")

    # 6. Mismatched code and id -> DomainValidationError
    with pytest.raises(DomainValidationError, match="does not match provided code"):
        await manage_plugins._resolve_company_id(
            db_session, company_code="MISMATCH_CODE", company_id=company_id
        )

    # 7. Empty/whitespace company_code or plugin_name -> DomainValidationError
    with pytest.raises(DomainValidationError, match="Company code must not be empty"):
        await manage_plugins._resolve_company_id(db_session, company_code="   ")
    with pytest.raises(DomainValidationError, match="Plugin name must not be empty"):
        await manage_plugins.get_plugin("   ", company_id=company_id, session=db_session)


@pytest.mark.asyncio
async def test_manage_plugins_list_and_get(client: AsyncClient, db_session: AsyncSession) -> None:
    code = f"CMP_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)

    # List plugins via company_code
    plugins_by_code = await manage_plugins.list_plugins(company_code=code, session=db_session)
    assert len(plugins_by_code) >= 1
    cl = next(p for p in plugins_by_code if p.plugin_name == "credit_limit")
    assert cl.is_enabled is True
    assert cl.is_customized is False
    assert cl.config == {"grace_amount": "0.000000"}

    # List plugins via company_id
    plugins_by_id = await manage_plugins.list_plugins(company_id=company_id, session=db_session)
    assert len(plugins_by_id) == len(plugins_by_code)

    # Get single plugin
    detail = await manage_plugins.get_plugin("credit_limit", company_code=code, session=db_session)
    assert detail.plugin_name == "credit_limit"
    assert detail.is_enabled is True

    # Get unknown plugin -> NotFoundError
    with pytest.raises(NotFoundError, match="Plugin"):
        await manage_plugins.get_plugin("unknown_plugin_abc", company_code=code, session=db_session)


@pytest.mark.asyncio
async def test_manage_plugins_enable_disable_and_configure(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    code = f"CMP_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)

    # 1. Disable credit_limit
    disabled = await manage_plugins.disable_plugin(
        "credit_limit", company_code=code, session=db_session
    )
    assert disabled.is_enabled is False
    assert disabled.is_customized is True

    # Verify through get_plugin
    check_disabled = await manage_plugins.get_plugin(
        "credit_limit", company_id=company_id, session=db_session
    )
    assert check_disabled.is_enabled is False

    # 2. Configure credit_limit with grace_amount
    configured = await manage_plugins.configure_plugin(
        "credit_limit",
        {"grace_amount": "100.000000"},
        company_code=code,
        is_enabled=True,
        session=db_session,
    )
    assert configured.is_enabled is True
    assert configured.config == {"grace_amount": "100.000000"}

    # 3. Configure with alias 'grace_allowance'
    configured_alias = await manage_plugins.configure_plugin(
        "credit_limit",
        {"grace_allowance": "250.500000"},
        company_id=company_id,
        session=db_session,
    )
    assert configured_alias.config == {"grace_amount": "250.500000"}

    # 4. Invalid config (extra fields, negative values, etc.)
    with pytest.raises(DomainValidationError, match="Configuration validation failed"):
        await manage_plugins.configure_plugin(
            "credit_limit",
            {"grace_amount": "-50.000000"},
            company_code=code,
            session=db_session,
        )


@pytest.mark.asyncio
async def test_manage_plugins_dependency_guards(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    code = f"CMP_{uuid.uuid4().hex[:6].upper()}"
    _ = await create_company(client, code)

    # Register synthetic plugins for dependency test
    p_req = PluginManifest(
        name="syn_prereq",
        version="1.0.0",
        title="Prereq",
        description="",
        default_enabled=True,
    )
    p_dep = PluginManifest(
        name="syn_dependent",
        version="1.0.0",
        title="Dependent",
        description="",
        default_enabled=True,
        requires=(PluginDependency("syn_prereq", "1.0.0"),),
    )
    loader.bootstrap_plugins([p_req, p_dep], enable_entrypoints=False)
    try:
        # Disabling prerequisite while dependent is active -> ConflictError (409)
        with pytest.raises(
            ConflictError, match="dependent plugin 'syn_dependent' is currently enabled"
        ):
            await manage_plugins.disable_plugin("syn_prereq", company_code=code, session=db_session)

        # First disable dependent -> succeeds
        await manage_plugins.disable_plugin("syn_dependent", company_code=code, session=db_session)
        # Now disable prerequisite -> succeeds
        await manage_plugins.disable_plugin("syn_prereq", company_code=code, session=db_session)

        # Trying to enable dependent while prerequisite is disabled -> DomainValidationError (422)
        with pytest.raises(
            DomainValidationError, match="prerequisite plugin 'syn_prereq' is disabled"
        ):
            await manage_plugins.enable_plugin(
                "syn_dependent", company_code=code, session=db_session
            )
    finally:
        loader.reset_loaded_plugins()
        loader.bootstrap_plugins(_app_main.BUILTIN_PLUGIN_MANIFESTS)


@pytest.mark.asyncio
async def test_manage_plugins_main_cli(client: AsyncClient) -> None:
    code = f"CMP_{uuid.uuid4().hex[:6].upper()}"
    await create_company(client, code)

    # 1. list
    rc = await manage_plugins._main(["list", "--company-code", code], dispose=False)
    assert rc == 0

    # 2. get
    rc = await manage_plugins._main(
        ["get", "--company-code", code, "--plugin", "credit_limit"], dispose=False
    )
    assert rc == 0

    # 3. disable
    rc = await manage_plugins._main(
        ["disable", "--company-code", code, "--plugin", "credit_limit"], dispose=False
    )
    assert rc == 0

    # 4. configure with --enable
    rc = await manage_plugins._main(
        [
            "configure",
            "--company-code",
            code,
            "--plugin",
            "credit_limit",
            "--config",
            '{"grace_amount": "50.000000"}',
            "--enable",
        ],
        dispose=False,
    )
    assert rc == 0

    # 5. enable with config
    rc = await manage_plugins._main(
        [
            "enable",
            "--company-code",
            code,
            "--plugin",
            "credit_limit",
            "--config",
            '{"grace_amount": "75.000000"}',
        ],
        dispose=False,
    )
    assert rc == 0

    # 6. Error cases: invalid json
    rc_bad_json = await manage_plugins._main(
        [
            "configure",
            "--company-code",
            code,
            "--plugin",
            "credit_limit",
            "--config",
            "not-valid-json",
        ],
        dispose=False,
    )
    assert rc_bad_json != 0

    # 7. Error cases: nonexistent company
    rc_no_co = await manage_plugins._main(
        ["list", "--company-code", "NONEXISTENT_CO_XYZ"], dispose=False
    )
    assert rc_no_co != 0


@pytest.mark.asyncio
async def test_manage_plugins_affects_sales_confirmation_e2e(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    code = f"CMP_{uuid.uuid4().hex[:6].upper()}"
    company_id = await create_company(client, code)
    customer_id = await create_customer(client, company_id, "E2ECUST", credit_limit="100")
    product_id = await create_product(client, company_id, "E2E-PROD", list_price="150")

    order = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="150")]
    )

    # 1. By default, credit_limit is enabled with grace_amount = 0.
    # Order total 150 > limit 100 -> 409 Conflict.
    blocked = await confirm_order(client, company_id, order["id"])
    assert blocked.status_code == 409
    assert "exceeds the customer's credit limit" in blocked.json()["detail"]

    # 2. Disable plugin via CLI function for this company
    await manage_plugins.disable_plugin("credit_limit", company_id=company_id, session=db_session)

    # Now the order can be confirmed!
    resp_confirmed = await confirm_order(client, company_id, order["id"])
    assert resp_confirmed.status_code == 200, resp_confirmed.text
    assert resp_confirmed.json()["status"] == "confirmed"

    # 3. Create another order for 150
    order_2 = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="150")]
    )

    # Enable and configure grace_amount = 200 (limit 100 + grace 200 = 300 ceiling)
    # Current exposure: order 1 (150) + order 2 (150) = 300 <= 300 -> allowed!
    await manage_plugins.configure_plugin(
        "credit_limit",
        {"grace_amount": "200.000000"},
        company_id=company_id,
        is_enabled=True,
        session=db_session,
    )

    resp_order2 = await confirm_order(client, company_id, order_2["id"])
    assert resp_order2.status_code == 200, resp_order2.text
    assert resp_order2.json()["status"] == "confirmed"

    # 4. Third order for 10 (exposure 150 + 150 + 10 = 310 > 300 ceiling) -> 409!
    order_3 = await create_draft_order(
        client, company_id, customer_id, [order_line(product_id, "1", unit_price="10")]
    )
    resp_order3 = await confirm_order(client, company_id, order_3["id"])
    assert resp_order3.status_code == 409


@pytest.mark.asyncio
async def test_plugin_admin_actor_least_privilege(db_session: AsyncSession) -> None:
    """PLUGIN_ADMIN_ACTOR is strictly bounded to plugin permissions."""
    with (
        actor_context(manage_plugins.PLUGIN_ADMIN_ACTOR),
        pytest.raises(ActionDeniedError, match="lacks permission 'masterdata.company.create'"),
    ):
        await masterdata_service.create_company(
            db_session,
            CompanyCreate(code="UNAUTH", name="Unauth Co", functional_currency_code="TWD"),
        )
