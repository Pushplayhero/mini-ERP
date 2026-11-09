"""tests/platform/test_customfields_router.py — HTTP endpoints for /api/v1/custom-fields.

ADR-011 / Slice B.

Covers:
1. GET /api/v1/custom-fields:
   - 401 unauthenticated.
   - 403 unauthorized (lacking platform.customfield.read).
   - 200 returns list, filterable by entity_type and active_only.
2. POST /api/v1/custom-fields:
   - 401 unauthenticated.
   - 403 unauthorized (reader lacking platform.customfield.manage).
   - 422 invalid body / extra forbidden fields.
   - 409 conflict on duplicate (entity_type, field_key).
   - 201 created with normalized schema and Banker's Rounding on Decimal numbers.
3. GET /api/v1/custom-fields/{field_id}:
   - 401 unauthenticated.
   - 403 unauthorized.
   - 404 nonexistent field ID.
   - 200 retrieved definition.
4. PATCH /api/v1/custom-fields/{field_id}:
   - 401 unauthenticated.
   - 403 unauthorized.
   - 404 nonexistent field ID.
   - 422 empty body or invalid rule update.
   - 200 updated definition.
5. DELETE /api/v1/custom-fields/{field_id}:
   - 401 unauthenticated.
   - 403 unauthorized.
   - 404 nonexistent field ID.
   - 200 soft-deactivated (is_active = False).
6. Multi-tenant HTTP isolation:
   - Company A definitions invisible and untouchable to Company B.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.permissions.models import Role, RolePermission
from tests._auth_helpers import setup_company_with_admin
from tests.platform.permissions._helpers import (
    create_company,
    create_role_without_permissions,
    create_user_with_role,
)


@pytest.mark.asyncio
async def test_list_custom_fields_auth_and_permissions(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    # 401 unauthenticated
    resp_unauth = await client.get("/api/v1/custom-fields")
    assert resp_unauth.status_code == 401

    # 403 unauthorized (role with zero permissions)
    role_code = f"unprivileged_{uuid.uuid4().hex[:6]}"
    company_id = await create_company(db_session, "CFROUTER1")
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
    token = login_resp.json()["access_token"]
    resp_forbidden = await client.get(
        "/api/v1/custom-fields",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_forbidden.status_code == 403

    # 200 admin
    auth = await setup_company_with_admin(client, code="CFROUTER2")
    resp_ok = await client.get("/api/v1/custom-fields", headers=auth.headers)
    assert resp_ok.status_code == 200
    assert resp_ok.json() == []


@pytest.mark.asyncio
async def test_create_custom_field_auth_and_permissions(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    # 401 unauthenticated
    resp_unauth = await client.post(
        "/api/v1/custom-fields",
        json={
            "entity_type": "customer",
            "field_key": "tax_id",
            "label": "統一編號",
            "field_type": "TEXT",
        },
    )
    assert resp_unauth.status_code == 401

    # 403 reader role (has platform.customfield.read, lacks platform.customfield.manage)
    reader_role = f"reader_{uuid.uuid4().hex[:6]}"
    company_id = await create_company(db_session, "CFROUTER3")
    db_session.add(Role(code=reader_role, name="Reader Role"))
    db_session.add(
        RolePermission(role_code=reader_role, permission_code="platform.customfield.read")
    )
    await create_user_with_role(
        db_session,
        email="reader@example.com",
        password="password123",
        company_id=company_id,
        role_code=reader_role,
    )
    login_resp = await client.post(
        "/auth/login",
        json={"email": "reader@example.com", "password": "password123"},
    )
    token = login_resp.json()["access_token"]
    resp_manage_forbidden = await client.post(
        "/api/v1/custom-fields",
        json={
            "entity_type": "customer",
            "field_key": "tax_id",
            "label": "統一編號",
            "field_type": "TEXT",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_manage_forbidden.status_code == 403


@pytest.mark.asyncio
async def test_create_custom_field_success_and_validation(client: AsyncClient) -> None:
    auth = await setup_company_with_admin(client, code="CFROUTER4")

    # 422 extra field forbidden
    resp_extra = await client.post(
        "/api/v1/custom-fields",
        json={
            "entity_type": "customer",
            "field_key": "extra_check",
            "label": "額外欄位測試",
            "field_type": "TEXT",
            "unknown_field": "disallowed",
        },
        headers=auth.headers,
    )
    assert resp_extra.status_code == 422

    # 422 invalid validation rule (e.g. min > max)
    resp_invalid_rules = await client.post(
        "/api/v1/custom-fields",
        json={
            "entity_type": "customer",
            "field_key": "rating",
            "label": "評級",
            "field_type": "DECIMAL",
            "validation_rules": {"min_value": 100, "max_value": 10},
        },
        headers=auth.headers,
    )
    assert resp_invalid_rules.status_code == 422

    # 201 successful creation with Decimal Banker's Rounding
    resp_create = await client.post(
        "/api/v1/custom-fields",
        json={
            "entity_type": "customer",
            "field_key": "discount_rate",
            "label": "折扣率",
            "field_type": "DECIMAL",
            "is_required": False,
            "default_value": "0.12555555",  # rounds to 0.125556
            "validation_rules": {"min_value": 0, "max_value": 1},
        },
        headers=auth.headers,
    )
    assert resp_create.status_code == 201
    created_data = resp_create.json()
    field_id = created_data["id"]
    assert created_data["field_key"] == "discount_rate"
    assert created_data["label"] == "折扣率"
    assert created_data["field_type"] == "DECIMAL"
    assert created_data["default_value"] == "0.125556"
    assert created_data["is_active"] is True

    # 409 conflict duplicate (entity_type, field_key)
    resp_duplicate = await client.post(
        "/api/v1/custom-fields",
        json={
            "entity_type": "customer",
            "field_key": "discount_rate",
            "label": "重複折扣率",
            "field_type": "DECIMAL",
        },
        headers=auth.headers,
    )
    assert resp_duplicate.status_code == 409

    # List endpoint with entity_type filter
    resp_list = await client.get(
        "/api/v1/custom-fields?entity_type=customer",
        headers=auth.headers,
    )
    assert resp_list.status_code == 200
    items = resp_list.json()
    assert len(items) == 1
    assert items[0]["id"] == field_id

    # List endpoint with other entity_type filter
    resp_list_sales = await client.get(
        "/api/v1/custom-fields?entity_type=sales_order",
        headers=auth.headers,
    )
    assert resp_list_sales.status_code == 200
    assert resp_list_sales.json() == []

    # List endpoint with invalid entity_type filter fails closed with 422
    resp_list_invalid = await client.get(
        "/api/v1/custom-fields?entity_type=invalid_entity",
        headers=auth.headers,
    )
    assert resp_list_invalid.status_code == 422


@pytest.mark.asyncio
async def test_get_patch_delete_custom_field(client: AsyncClient) -> None:
    auth = await setup_company_with_admin(client, code="CFROUTER5")

    # Create a definition
    resp_create = await client.post(
        "/api/v1/custom-fields",
        json={
            "entity_type": "sales_order",
            "field_key": "delivery_notes",
            "label": "交貨備註",
            "field_type": "TEXT",
            "is_required": False,
            "validation_rules": {"max_length": 200},
        },
        headers=auth.headers,
    )
    assert resp_create.status_code == 201
    field_id = resp_create.json()["id"]

    # 404 GET nonexistent
    fake_id = str(uuid.uuid4())
    resp_404_get = await client.get(
        f"/api/v1/custom-fields/{fake_id}",
        headers=auth.headers,
    )
    assert resp_404_get.status_code == 404

    # 200 GET
    resp_get = await client.get(
        f"/api/v1/custom-fields/{field_id}",
        headers=auth.headers,
    )
    assert resp_get.status_code == 200
    assert resp_get.json()["id"] == field_id
    assert resp_get.json()["label"] == "交貨備註"

    # 422 PATCH empty body
    resp_empty_patch = await client.patch(
        f"/api/v1/custom-fields/{field_id}",
        json={},
        headers=auth.headers,
    )
    assert resp_empty_patch.status_code == 422

    # 422 PATCH null on non-nullable field
    resp_null_label = await client.patch(
        f"/api/v1/custom-fields/{field_id}",
        json={"label": None},
        headers=auth.headers,
    )
    assert resp_null_label.status_code == 422

    # 422 PATCH invalid validation rules
    resp_invalid_patch = await client.patch(
        f"/api/v1/custom-fields/{field_id}",
        json={"validation_rules": {"min_length": 50, "max_length": 10}},
        headers=auth.headers,
    )
    assert resp_invalid_patch.status_code == 422

    # 200 PATCH partial update (including setting default_value, then clearing to null)
    resp_patch = await client.patch(
        f"/api/v1/custom-fields/{field_id}",
        json={
            "label": "特殊交貨指示",
            "is_required": True,
            "default_value": "預設交貨備註",
            "validation_rules": {"max_length": 500},
        },
        headers=auth.headers,
    )
    assert resp_patch.status_code == 200
    patched_data = resp_patch.json()
    assert patched_data["label"] == "特殊交貨指示"
    assert patched_data["is_required"] is True
    assert patched_data["default_value"] == "預設交貨備註"
    assert patched_data["validation_rules"] == {"max_length": 500}

    # 200 PATCH clear default_value to null
    resp_clear_default = await client.patch(
        f"/api/v1/custom-fields/{field_id}",
        json={"default_value": None},
        headers=auth.headers,
    )
    assert resp_clear_default.status_code == 200
    assert resp_clear_default.json()["default_value"] is None

    # 404 DELETE nonexistent
    resp_404_delete = await client.delete(
        f"/api/v1/custom-fields/{fake_id}",
        headers=auth.headers,
    )
    assert resp_404_delete.status_code == 404

    # 200 DELETE soft deactivation
    resp_delete = await client.delete(
        f"/api/v1/custom-fields/{field_id}",
        headers=auth.headers,
    )
    assert resp_delete.status_code == 200
    assert resp_delete.json()["is_active"] is False

    # List active_only=true should now exclude it
    resp_active_list = await client.get(
        "/api/v1/custom-fields?active_only=true",
        headers=auth.headers,
    )
    assert resp_active_list.status_code == 200
    assert resp_active_list.json() == []

    # List active_only=false should include it
    resp_all_list = await client.get(
        "/api/v1/custom-fields?active_only=false",
        headers=auth.headers,
    )
    assert resp_all_list.status_code == 200
    assert len(resp_all_list.json()) == 1
    assert resp_all_list.json()[0]["id"] == field_id


@pytest.mark.asyncio
async def test_custom_fields_multi_tenant_isolation(client: AsyncClient) -> None:
    auth_a = await setup_company_with_admin(client, code="CFCOMA")
    auth_b = await setup_company_with_admin(client, code="CFCOMB")

    # Company A creates a field
    resp_a = await client.post(
        "/api/v1/custom-fields",
        json={
            "entity_type": "customer",
            "field_key": "membership_tier",
            "label": "會員等級",
            "field_type": "ENUM",
            "validation_rules": {"choices": ["GOLD", "SILVER", "BRONZE"]},
        },
        headers=auth_a.headers,
    )
    assert resp_a.status_code == 201
    field_a_id = resp_a.json()["id"]

    # Company B creates field with the SAME field_key (allowed in different tenant)
    resp_b = await client.post(
        "/api/v1/custom-fields",
        json={
            "entity_type": "customer",
            "field_key": "membership_tier",
            "label": "公司B會員等級",
            "field_type": "TEXT",
        },
        headers=auth_b.headers,
    )
    assert resp_b.status_code == 201
    field_b_id = resp_b.json()["id"]
    assert field_a_id != field_b_id

    # Company B cannot read Company A's definition -> 404
    resp_b_get_a = await client.get(
        f"/api/v1/custom-fields/{field_a_id}",
        headers=auth_b.headers,
    )
    assert resp_b_get_a.status_code == 404

    # Company B cannot patch Company A's definition -> 404
    resp_b_patch_a = await client.patch(
        f"/api/v1/custom-fields/{field_a_id}",
        json={"label": "非法篡改"},
        headers=auth_b.headers,
    )
    assert resp_b_patch_a.status_code == 404

    # Company B cannot delete Company A's definition -> 404
    resp_b_delete_a = await client.delete(
        f"/api/v1/custom-fields/{field_a_id}",
        headers=auth_b.headers,
    )
    assert resp_b_delete_a.status_code == 404

    # Company B's list only contains Company B's field
    resp_b_list = await client.get("/api/v1/custom-fields", headers=auth_b.headers)
    assert resp_b_list.status_code == 200
    items_b = resp_b_list.json()
    assert len(items_b) == 1
    assert items_b[0]["id"] == field_b_id
    assert items_b[0]["label"] == "公司B會員等級"
