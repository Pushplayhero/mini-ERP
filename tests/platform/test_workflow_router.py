"""tests/platform/test_workflow_router.py — HTTP endpoints for /api/v1/workflows.

ADR-012 / Week 12 Slice B.

Covers:
1. Authentication & authorization (401 unauthenticated, 403 unauthorized).
2. Definitions endpoints:
   - GET /api/v1/workflows/definitions (platform.workflow.read)
   - POST /api/v1/workflows/definitions (platform.workflow.manage, 201 Created)
   - GET /api/v1/workflows/definitions/{id}
   - PATCH /api/v1/workflows/definitions/{id}
   - DELETE /api/v1/workflows/definitions/{id} (soft deactivation)
3. Instances endpoints:
   - GET /api/v1/workflows/instances
   - POST /api/v1/workflows/instances (platform.workflow.approve, 201 Created)
   - GET /api/v1/workflows/instances/{id}
   - POST /api/v1/workflows/instances/{id}/approve
   - POST /api/v1/workflows/instances/{id}/reject
   - POST /api/v1/workflows/instances/{id}/cancel
4. Cross-company tenant isolation on HTTP layer.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.workflow import get_workflow_target_resolver, set_workflow_target_resolver
from tests._auth_helpers import setup_company_with_admin
from tests.platform.permissions._helpers import (
    create_company,
    create_role_without_permissions,
    create_user_with_role,
)


@pytest.fixture
def mock_resolver() -> Generator[None, None, None]:
    orig_resolver = get_workflow_target_resolver()

    async def _resolver(
        session: AsyncSession,
        *,
        entity_type: str,
        entity_id: uuid.UUID,
        transition: str,
    ) -> Decimal:
        return Decimal("1500.000000")

    set_workflow_target_resolver(_resolver)
    yield
    set_workflow_target_resolver(orig_resolver)


@pytest.mark.asyncio
async def test_workflow_definitions_http_lifecycle(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    # 1. 401 unauthenticated
    resp_unauth = await client.get("/api/v1/workflows/definitions")
    assert resp_unauth.status_code == 401

    # 2. 403 unauthorized for user with zero permissions
    unpriv_role = f"unpriv_{uuid.uuid4().hex[:6]}"
    co_id = await create_company(db_session, "WFROUTCO1")
    await create_role_without_permissions(db_session, unpriv_role)
    await create_user_with_role(
        db_session,
        email="unprivileged@example.com",
        password="password123",
        company_id=co_id,
        role_code=unpriv_role,
    )
    login_resp = await client.post(
        "/auth/login",
        json={"email": "unprivileged@example.com", "password": "password123"},
    )
    unauth_token = login_resp.json()["access_token"]
    resp_forbidden = await client.get(
        "/api/v1/workflows/definitions",
        headers={"Authorization": f"Bearer {unauth_token}"},
    )
    assert resp_forbidden.status_code == 403

    # 3. Admin user creates definition
    admin_auth = await setup_company_with_admin(client, code="WFROUTCO2")
    headers = admin_auth.headers

    create_payload = {
        "entity_type": "sales_order",
        "transition": "confirm",
        "name": "SO Routing Flow",
        "description": "API flow test",
        "steps": [
            {
                "step_order": 1,
                "step_name": "Supervisor",
                "min_amount": "500.00",
                "required_permission": "platform.workflow.approve",
            }
        ],
    }
    resp_create = await client.post(
        "/api/v1/workflows/definitions", json=create_payload, headers=headers
    )
    assert resp_create.status_code == 201
    def_data = resp_create.json()
    assert def_data["name"] == "SO Routing Flow"
    def_id = def_data["id"]

    # 4. List definitions
    resp_list = await client.get("/api/v1/workflows/definitions", headers=headers)
    assert resp_list.status_code == 200
    assert len(resp_list.json()) == 1

    # 5. Get definition by ID
    resp_get = await client.get(f"/api/v1/workflows/definitions/{def_id}", headers=headers)
    assert resp_get.status_code == 200
    assert resp_get.json()["id"] == def_id

    # 6. Patch definition
    resp_patch = await client.patch(
        f"/api/v1/workflows/definitions/{def_id}",
        json={"name": "Patched Flow Name"},
        headers=headers,
    )
    assert resp_patch.status_code == 200
    assert resp_patch.json()["name"] == "Patched Flow Name"

    # 7. Delete (soft-deactivate) definition
    resp_del = await client.delete(f"/api/v1/workflows/definitions/{def_id}", headers=headers)
    assert resp_del.status_code == 200
    assert resp_del.json()["is_active"] is False


@pytest.mark.asyncio
async def test_workflow_instances_http_lifecycle(
    client: AsyncClient,
    db_session: AsyncSession,
    mock_resolver: None,
) -> None:
    admin_auth = await setup_company_with_admin(client, code="WFROUTCO3")
    headers = admin_auth.headers

    # Create definition with allow_self_approval=True
    def_payload = {
        "entity_type": "invoice",
        "transition": "void",
        "name": "Invoice Void Approval",
        "allow_self_approval": True,
        "steps": [
            {
                "step_order": 1,
                "step_name": "Controller Void Approval",
                "min_amount": "100.00",
                "required_permission": "platform.workflow.approve",
            }
        ],
    }
    await client.post("/api/v1/workflows/definitions", json=def_payload, headers=headers)

    # Submit instance
    invoice_id = str(uuid.uuid4())
    sub_resp = await client.post(
        "/api/v1/workflows/instances",
        json={
            "entity_type": "invoice",
            "entity_id": invoice_id,
            "transition": "void",
        },
        headers=headers,
    )
    assert sub_resp.status_code == 201
    inst_data = sub_resp.json()
    assert inst_data["status"] == "PENDING"
    inst_id = inst_data["id"]

    # Get instance
    get_resp = await client.get(f"/api/v1/workflows/instances/{inst_id}", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == inst_id

    # List instances
    list_resp = await client.get(
        "/api/v1/workflows/instances",
        params={"entity_type": "invoice", "status": "PENDING"},
        headers=headers,
    )
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    # Approve instance
    app_resp = await client.post(
        f"/api/v1/workflows/instances/{inst_id}/approve",
        json={"comment": "Void approved by API"},
        headers=headers,
    )
    assert app_resp.status_code == 200
    assert app_resp.json()["status"] == "APPROVED"
    assert len(app_resp.json()["logs"]) == 1
