"""tests/platform/test_plugins_router.py — HTTP endpoints for /api/v1/plugins (ADR-010 / Slice B).

Covers:
1. GET /api/v1/plugins:
   - 401 unauthenticated.
   - 403 unauthorized (lacking platform.plugin.read).
   - 200 admin returns list of plugins including default state for credit_limit.
2. GET /api/v1/plugins/{plugin_name}:
   - 401 unauthenticated.
   - 403 unauthorized.
   - 404 for nonexistent plugin.
   - 200 for registered plugin.
3. PATCH /api/v1/plugins/{plugin_name}:
   - 401 unauthenticated.
   - 403 unauthorized (has read, lacks platform.plugin.manage).
   - 404 nonexistent plugin.
   - 422 empty body (both fields None).
   - 422 extra attributes / invalid config schema.
   - 200 enable with valid config (Decimal normalized).
   - 200 partial update: toggle is_enabled while retaining existing config.
   - 200 partial update: change config while retaining existing is_enabled state.
4. Dependency invariants via HTTP:
   - 422 when enabling dependent plugin while prerequisite is disabled.
   - 409 when disabling prerequisite while dependent is active.
5. Multi-tenant isolation:
   - Plugin config and enablement in Company A do not leak or affect Company B.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from httpx import AsyncClient
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events, hooks
from app.core.plugins import (
    PluginDependency,
    PluginManifest,
    get_plugin_runtime_gate,
    set_plugin_runtime_gate,
)
from app.platform.permissions.models import Role, RolePermission
from app.platform.plugins import loader
from tests._auth_helpers import setup_company_with_admin
from tests.platform.permissions._helpers import (
    create_company,
    create_role_without_permissions,
    create_user_with_role,
)


class CreditLimitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    threshold_amount: Decimal = Decimal("50000.000000")
    currency: str = "TWD"


@pytest.fixture(autouse=True)
def clean_plugin_loader() -> Iterator[None]:
    saved_gate = get_plugin_runtime_gate()
    saved_loaded = dict(loader._loaded_plugins)
    saved_hook_specs = dict(hooks._hook_specs)
    saved_handlers = {k: list(v) for k, v in hooks._handlers.items()}
    saved_event_schemas = dict(events._schemas)
    saved_event_handlers = {k: list(v) for k, v in events._handlers.items()}
    loader.reset_loaded_plugins()
    # Bootstrap credit_limit for router testing
    credit_limit_manifest = PluginManifest(
        name="credit_limit",
        version="0.1.0",
        title="Sales Order Credit Limit Check",
        description="Validates credit limit on order confirmation",
        default_enabled=False,
        config_schema=CreditLimitConfig,
    )
    loader.bootstrap_plugins([credit_limit_manifest], enable_entrypoints=False)
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


@pytest.mark.asyncio
async def test_list_plugins_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/plugins")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_list_plugins_requires_permission(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    role_code = f"unprivileged_{uuid.uuid4().hex[:6]}"
    company_id = await create_company(db_session, "PLUGROUTER1")
    await create_role_without_permissions(db_session, role_code)
    await create_user_with_role(
        db_session,
        email="unprivileged@example.com",
        password="password123",
        company_id=company_id,
        role_code=role_code,
    )
    login_resp = await client.post(
        "/auth/login",
        json={"email": "unprivileged@example.com", "password": "password123"},
    )
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]

    response = await client.get(
        "/api/v1/plugins",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_list_plugins_success(client: AsyncClient) -> None:
    auth = await setup_company_with_admin(client, code="PLUGROUTER2")
    response = await client.get("/api/v1/plugins", headers=auth.headers)
    assert response.status_code == 200

    items = response.json()
    assert isinstance(items, list)
    plugin_names = [p["plugin_name"] for p in items]
    assert "credit_limit" in plugin_names

    cl = next(p for p in items if p["plugin_name"] == "credit_limit")
    assert cl["version"] == "0.1.0"
    assert cl["is_enabled"] is False
    assert cl["is_customized"] is False
    assert cl["requires"] == []
    assert cl["config"]["threshold_amount"] == "50000.000000"
    assert cl["config"]["currency"] == "TWD"


@pytest.mark.asyncio
async def test_get_plugin_requires_auth_and_permission(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    # 401 unauthenticated
    resp_unauth = await client.get("/api/v1/plugins/credit_limit")
    assert resp_unauth.status_code == 401

    # 403 unprivileged
    role_code = f"empty_{uuid.uuid4().hex[:6]}"
    company_id = await create_company(db_session, "PLUGROUTER3")
    await create_role_without_permissions(db_session, role_code)
    await create_user_with_role(
        db_session,
        email="empty_user@example.com",
        password="password123",
        company_id=company_id,
        role_code=role_code,
    )
    login_resp = await client.post(
        "/auth/login",
        json={"email": "empty_user@example.com", "password": "password123"},
    )
    token = login_resp.json()["access_token"]
    resp_forbidden = await client.get(
        "/api/v1/plugins/credit_limit",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_forbidden.status_code == 403


@pytest.mark.asyncio
async def test_get_plugin_not_found(client: AsyncClient) -> None:
    auth = await setup_company_with_admin(client, code="PLUGROUTER4")
    response = await client.get(
        "/api/v1/plugins/nonexistent_plugin_xyz",
        headers=auth.headers,
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_plugin_success(client: AsyncClient) -> None:
    auth = await setup_company_with_admin(client, code="PLUGROUTER5")
    response = await client.get(
        "/api/v1/plugins/credit_limit",
        headers=auth.headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["plugin_name"] == "credit_limit"
    assert data["is_enabled"] is False
    assert data["is_customized"] is False
    assert data["config"]["threshold_amount"] == "50000.000000"


@pytest.mark.asyncio
async def test_patch_plugin_requires_auth_and_manage_permission(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    # 401 unauthenticated
    resp_unauth = await client.patch(
        "/api/v1/plugins/credit_limit",
        json={"is_enabled": True},
    )
    assert resp_unauth.status_code == 401

    # User with platform.plugin.read but lacking platform.plugin.manage -> 403
    role_code = f"reader_{uuid.uuid4().hex[:6]}"
    company_id = await create_company(db_session, "PLUGROUTER6")
    db_session.add(Role(code=role_code, name="Reader Role"))
    db_session.add(RolePermission(role_code=role_code, permission_code="platform.plugin.read"))
    await create_user_with_role(
        db_session,
        email="reader@example.com",
        password="password123",
        company_id=company_id,
        role_code=role_code,
    )
    login_resp = await client.post(
        "/auth/login",
        json={"email": "reader@example.com", "password": "password123"},
    )
    token = login_resp.json()["access_token"]
    resp_manage_forbidden = await client.patch(
        "/api/v1/plugins/credit_limit",
        json={"is_enabled": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_manage_forbidden.status_code == 403


@pytest.mark.asyncio
async def test_patch_plugin_validation_errors(client: AsyncClient) -> None:
    auth = await setup_company_with_admin(client, code="PLUGROUTER7")

    # 404 on nonexistent plugin
    resp_404 = await client.patch(
        "/api/v1/plugins/nonexistent",
        json={"is_enabled": True},
        headers=auth.headers,
    )
    assert resp_404.status_code == 404

    # 422 on empty body (both is_enabled and config are None)
    resp_empty = await client.patch(
        "/api/v1/plugins/credit_limit",
        json={},
        headers=auth.headers,
    )
    assert resp_empty.status_code == 422

    # 422 on extra attributes (model_config extra='forbid')
    resp_extra = await client.patch(
        "/api/v1/plugins/credit_limit",
        json={"is_enabled": True, "extra_field": "disallowed"},
        headers=auth.headers,
    )
    assert resp_extra.status_code == 422

    # 422 on invalid schema for credit_limit config (e.g. invalid decimal or extra field in config)
    resp_bad_config = await client.patch(
        "/api/v1/plugins/credit_limit",
        json={
            "config": {
                "threshold_amount": "not_a_valid_number",
                "currency": "TWD",
            }
        },
        headers=auth.headers,
    )
    assert resp_bad_config.status_code == 422


@pytest.mark.asyncio
async def test_patch_plugin_success_and_partial_updates(client: AsyncClient) -> None:
    auth = await setup_company_with_admin(client, code="PLUGROUTER8")

    # 1. Enable with valid config
    resp = await client.patch(
        "/api/v1/plugins/credit_limit",
        json={
            "is_enabled": True,
            "config": {
                "threshold_amount": "50000.00",
                "currency": "TWD",
            },
        },
        headers=auth.headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_enabled"] is True
    assert data["is_customized"] is True
    assert Decimal(data["config"]["threshold_amount"]) == Decimal("50000.00")
    assert data["config"]["currency"] == "TWD"

    # 2. Partial update: disable without providing config (should retain existing config)
    resp_toggle = await client.patch(
        "/api/v1/plugins/credit_limit",
        json={"is_enabled": False},
        headers=auth.headers,
    )
    assert resp_toggle.status_code == 200
    data_toggle = resp_toggle.json()
    assert data_toggle["is_enabled"] is False
    assert Decimal(data_toggle["config"]["threshold_amount"]) == Decimal("50000.00")

    # 3. Partial update: change config without providing is_enabled (should retain is_enabled=False)
    resp_config = await client.patch(
        "/api/v1/plugins/credit_limit",
        json={
            "config": {
                "threshold_amount": "80000.00",
                "currency": "TWD",
            }
        },
        headers=auth.headers,
    )
    assert resp_config.status_code == 200
    data_config = resp_config.json()
    assert data_config["is_enabled"] is False
    assert Decimal(data_config["config"]["threshold_amount"]) == Decimal("80000.00")


@pytest.mark.asyncio
async def test_patch_plugin_dependency_invariants(client: AsyncClient) -> None:
    auth = await setup_company_with_admin(client, code="PLUGROUTER9")

    # Register synthetic plugins: plugin_base and plugin_dep
    p_base = PluginManifest(
        name="synthetic_base",
        version="1.0.0",
        title="Base Plugin",
        description="Prerequisite",
        default_enabled=False,
    )
    p_dep = PluginManifest(
        name="synthetic_dep",
        version="1.0.0",
        title="Dependent Plugin",
        description="Requires base",
        default_enabled=False,
        requires=(PluginDependency("synthetic_base", "1.0.0"),),
    )
    loader.bootstrap_plugins([p_base, p_dep], enable_entrypoints=False)

    # 1. Trying to enable dependent while base is disabled -> 422
    resp_fail = await client.patch(
        "/api/v1/plugins/synthetic_dep",
        json={"is_enabled": True},
        headers=auth.headers,
    )
    assert resp_fail.status_code == 422
    assert "is disabled" in resp_fail.json()["detail"]

    # 2. Enable base plugin first -> 200
    resp_base_ok = await client.patch(
        "/api/v1/plugins/synthetic_base",
        json={"is_enabled": True},
        headers=auth.headers,
    )
    assert resp_base_ok.status_code == 200
    assert resp_base_ok.json()["is_enabled"] is True

    # 3. Now enable dependent plugin -> 200
    resp_dep_ok = await client.patch(
        "/api/v1/plugins/synthetic_dep",
        json={"is_enabled": True},
        headers=auth.headers,
    )
    assert resp_dep_ok.status_code == 200
    assert resp_dep_ok.json()["is_enabled"] is True

    # 4. Try to disable base plugin while dependent is still active -> 409 Conflict
    resp_conflict = await client.patch(
        "/api/v1/plugins/synthetic_base",
        json={"is_enabled": False},
        headers=auth.headers,
    )
    assert resp_conflict.status_code == 409
    assert "dependent plugin" in resp_conflict.json()["detail"]


@pytest.mark.asyncio
async def test_tenant_isolation_via_router(client: AsyncClient) -> None:
    auth_a = await setup_company_with_admin(client, code="PLUGISOLA")
    auth_b = await setup_company_with_admin(client, code="PLUGISOLB")

    # Company A enables credit_limit with custom config
    resp_a = await client.patch(
        "/api/v1/plugins/credit_limit",
        json={
            "is_enabled": True,
            "config": {
                "threshold_amount": "99999.00",
                "currency": "TWD",
            },
        },
        headers=auth_a.headers,
    )
    assert resp_a.status_code == 200
    assert resp_a.json()["is_enabled"] is True
    assert Decimal(resp_a.json()["config"]["threshold_amount"]) == Decimal("99999.00")

    # Company B inspects credit_limit: must see default state (is_enabled=False, default config)
    resp_b_get = await client.get(
        "/api/v1/plugins/credit_limit",
        headers=auth_b.headers,
    )
    assert resp_b_get.status_code == 200
    data_b = resp_b_get.json()
    assert data_b["is_enabled"] is False
    assert data_b["is_customized"] is False
    assert Decimal(data_b["config"]["threshold_amount"]) == Decimal("50000.00")

    # Company B configures different credit_limit
    resp_b_patch = await client.patch(
        "/api/v1/plugins/credit_limit",
        json={
            "is_enabled": True,
            "config": {
                "threshold_amount": "12345.00",
                "currency": "TWD",
            },
        },
        headers=auth_b.headers,
    )
    assert resp_b_patch.status_code == 200
    assert Decimal(resp_b_patch.json()["config"]["threshold_amount"]) == Decimal("12345.00")

    # Company A's configuration remains completely intact
    resp_a_verify = await client.get(
        "/api/v1/plugins/credit_limit",
        headers=auth_a.headers,
    )
    assert resp_a_verify.status_code == 200
    assert Decimal(resp_a_verify.json()["config"]["threshold_amount"]) == Decimal("99999.00")
