"""tests/platform/test_integration_router.py — HTTP endpoints for /api/v1/integrations.

ADR-013 / Week 13 Slice B.

Covers:
1. Authentication & authorization (401 unauthenticated, 403 unauthorized).
2. Webhook endpoints:
   - GET /api/v1/integrations/webhooks (platform.integration.read)
   - POST /api/v1/integrations/webhooks (platform.integration.manage, 201 Created)
   - GET /api/v1/integrations/webhooks/{id}
   - PATCH /api/v1/integrations/webhooks/{id}
   - POST /api/v1/integrations/webhooks/{id}/rotate-secret
   - DELETE /api/v1/integrations/webhooks/{id} (200 OK soft deactivation)
3. One-time signing secret exposure contract (present on create/rotate, absent on read).
4. Delivery endpoints & retry:
   - GET /api/v1/integrations/deliveries
   - GET /api/v1/integrations/deliveries/{id}
   - POST /api/v1/integrations/deliveries/{id}/retry
5. Batch import/export & jobs endpoints:
   - POST /api/v1/integrations/imports
   - POST /api/v1/integrations/exports
   - GET /api/v1/integrations/jobs
   - GET /api/v1/integrations/jobs/{id}
6. Cross-company tenant isolation on HTTP layer.
"""

from __future__ import annotations

import socket
import uuid
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.platform.integration.models import WebhookDelivery
from tests._auth_helpers import setup_company_with_admin
from tests.platform.permissions._helpers import (
    create_company,
    create_role_without_permissions,
    create_user_with_role,
)


async def _login_unpriv_headers(
    client: AsyncClient,
    db_session: AsyncSession,
    company_id: uuid.UUID,
    email: str,
) -> dict[str, str]:
    unpriv_role = f"unpriv_{uuid.uuid4().hex[:6]}"
    await create_role_without_permissions(db_session, unpriv_role)
    await create_user_with_role(
        db_session,
        email=email,
        password="password123",
        company_id=company_id,
        role_code=unpriv_role,
    )
    login_resp = await client.post(
        "/auth/login",
        json={"email": email, "password": "password123"},
    )
    return {"Authorization": f"Bearer {login_resp.json()['access_token']}"}


@pytest.mark.asyncio
async def test_webhook_endpoints_http_auth_guards(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    # 1. 401 unauthenticated
    resp_unauth = await client.get("/api/v1/integrations/webhooks")
    assert resp_unauth.status_code == 401

    # 2. 403 unauthorized for user with zero permissions
    co_id = await create_company(db_session, "INT_ROUT_CO1")
    unpriv_headers = await _login_unpriv_headers(
        client, db_session, co_id, "unpriv_integ@example.com"
    )
    resp_forbidden = await client.get(
        "/api/v1/integrations/webhooks",
        headers=unpriv_headers,
    )
    assert resp_forbidden.status_code == 403


@pytest.mark.asyncio
async def test_webhook_endpoints_http_lifecycle(
    client: AsyncClient,
) -> None:
    # Admin user creates webhook endpoint
    admin_auth = await setup_company_with_admin(client, code="INT_ROUT_CO2")
    headers = admin_auth.headers

    fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    with patch("socket.getaddrinfo", return_value=fake_addrinfo):
        create_payload = {
            "name": "Audit Dispatcher",
            "url": "https://api.thirdparty.com/webhook",
            "event_types": ["sales_order.confirmed"],
            "timeout_seconds": "12.50",
            "max_attempts": 4,
        }
        create_resp = await client.post(
            "/api/v1/integrations/webhooks",
            json=create_payload,
            headers=headers,
        )
        assert create_resp.status_code == 201
        created_data = create_resp.json()
        endpoint_id = created_data["id"]
        assert created_data["name"] == "Audit Dispatcher"
        assert created_data["url"] == "https://api.thirdparty.com/webhook"
        assert created_data["event_types"] == ["sales_order.confirmed"]
        assert "signing_secret" in created_data
        assert created_data["signing_secret"].startswith("whsec_")
        assert created_data["secret_prefix"] == created_data["signing_secret"][:12]

        # 4. GET endpoint by ID — signing_secret MUST NOT be exposed
        get_resp = await client.get(
            f"/api/v1/integrations/webhooks/{endpoint_id}",
            headers=headers,
        )
        assert get_resp.status_code == 200
        get_data = get_resp.json()
        assert get_data["id"] == endpoint_id
        assert "signing_secret" not in get_data
        assert get_data["secret_prefix"] == created_data["secret_prefix"]

        # 5. List endpoints — signing_secret MUST NOT be exposed
        list_resp = await client.get("/api/v1/integrations/webhooks", headers=headers)
        assert list_resp.status_code == 200
        items = list_resp.json()
        assert len(items) == 1
        assert "signing_secret" not in items[0]

        # 6. PATCH endpoint
        patch_resp = await client.patch(
            f"/api/v1/integrations/webhooks/{endpoint_id}",
            json={"name": "Renamed Dispatcher", "is_active": False},
            headers=headers,
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["name"] == "Renamed Dispatcher"
        assert patch_resp.json()["is_active"] is False

        # 7. Rotate secret — returns new plaintext signing_secret once and exposes overlap expiry
        rotate_resp = await client.post(
            f"/api/v1/integrations/webhooks/{endpoint_id}/rotate-secret?overlap_seconds=300",
            headers=headers,
        )
        assert rotate_resp.status_code == 200
        rotate_data = rotate_resp.json()
        assert rotate_data["signing_secret"] != created_data["signing_secret"]
        assert rotate_data["signing_secret"].startswith("whsec_")
        assert rotate_data["secret_prefix"] == rotate_data["signing_secret"][:12]
        assert rotate_data["previous_secret_expires_at"] is not None
        assert "previous_signing_secret" not in rotate_data

        # 8. DELETE endpoint (soft deactivation returns 200 OK)
        del_resp = await client.delete(
            f"/api/v1/integrations/webhooks/{endpoint_id}",
            headers=headers,
        )
        assert del_resp.status_code == 200
        del_data = del_resp.json()
        assert del_data["is_active"] is False
        assert "signing_secret" not in del_data

        # 9. GET after deactivation still returns endpoint with is_active=False
        get_after = await client.get(
            f"/api/v1/integrations/webhooks/{endpoint_id}",
            headers=headers,
        )
        assert get_after.status_code == 200
        assert get_after.json()["is_active"] is False


@pytest.mark.asyncio
async def test_webhook_cross_tenant_isolation(
    client: AsyncClient,
) -> None:
    admin1 = await setup_company_with_admin(client, code="INT_ISO_CO1")
    admin2 = await setup_company_with_admin(client, code="INT_ISO_CO2")

    fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    with patch("socket.getaddrinfo", return_value=fake_addrinfo):
        create_resp = await client.post(
            "/api/v1/integrations/webhooks",
            json={
                "name": "Co1 Hook",
                "url": "https://api.co1.com/webhook",
            },
            headers=admin1.headers,
        )
        assert create_resp.status_code == 201
        ep_id = create_resp.json()["id"]

        # Admin 2 cannot read Admin 1's endpoint
        get_other = await client.get(
            f"/api/v1/integrations/webhooks/{ep_id}",
            headers=admin2.headers,
        )
        assert get_other.status_code == 404

        # Admin 2 cannot update Admin 1's endpoint
        patch_other = await client.patch(
            f"/api/v1/integrations/webhooks/{ep_id}",
            json={"name": "Hacked"},
            headers=admin2.headers,
        )
        assert patch_other.status_code == 404

        # Admin 2 cannot delete Admin 1's endpoint
        del_other = await client.delete(
            f"/api/v1/integrations/webhooks/{ep_id}",
            headers=admin2.headers,
        )
        assert del_other.status_code == 404


@pytest.mark.asyncio
async def test_webhook_deliveries_http_api(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    admin = await setup_company_with_admin(client, code="INT_DELIV_CO")
    headers = admin.headers
    co_id = admin.company_id

    # Create dummy endpoint and delivery directly
    fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    with patch("socket.getaddrinfo", return_value=fake_addrinfo):
        ep_resp = await client.post(
            "/api/v1/integrations/webhooks",
            json={
                "name": "Delivery Test Hook",
                "url": "https://api.co.com/webhook",
            },
            headers=headers,
        )
        ep_id = uuid.UUID(ep_resp.json()["id"])

    outbox_id = uuid.uuid4()
    await db_session.execute(
        insert(events.OUTBOX_TABLE).values(
            id=outbox_id,
            event_type="test.event",
            payload={"company_id": str(co_id), "k": "v"},
        )
    )
    await db_session.flush()

    delivery = WebhookDelivery(
        company_id=co_id,
        endpoint_id=ep_id,
        outbox_event_id=outbox_id,
        event_type="test.event",
        payload={"k": "v"},
        status="DEAD",
        attempts=5,
        max_attempts=5,
        last_error="502 Bad Gateway",
    )
    db_session.add(delivery)
    await db_session.commit()

    # GET /api/v1/integrations/deliveries
    list_resp = await client.get("/api/v1/integrations/deliveries", headers=headers)
    assert list_resp.status_code == 200
    deliveries = list_resp.json()
    assert len(deliveries) >= 1
    assert deliveries[0]["id"] == str(delivery.id)
    assert deliveries[0]["status"] == "DEAD"

    # GET /api/v1/integrations/deliveries/{id}
    get_resp = await client.get(
        f"/api/v1/integrations/deliveries/{delivery.id}",
        headers=headers,
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "DEAD"

    # POST /api/v1/integrations/deliveries/{id}/retry
    retry_resp = await client.post(
        f"/api/v1/integrations/deliveries/{delivery.id}/retry",
        headers=headers,
    )
    assert retry_resp.status_code == 200
    assert retry_resp.json()["status"] == "PENDING"
    assert retry_resp.json()["attempts"] == 0

    # Filter deliveries by event_type
    filtered_resp = await client.get(
        "/api/v1/integrations/deliveries?event_type=test.event",
        headers=headers,
    )
    assert filtered_resp.status_code == 200
    assert len(filtered_resp.json()) >= 1


@pytest.mark.asyncio
async def test_batch_import_export_and_jobs_http_api(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    admin1 = await setup_company_with_admin(client, code="INT_BATCH_CO1")
    admin2 = await setup_company_with_admin(client, code="INT_BATCH_CO2")

    # Unprivileged user gets 403 on import, export, and jobs routes
    unpriv_headers = await _login_unpriv_headers(
        client, db_session, admin1.company_id, "unpriv_batch@example.com"
    )
    assert (
        await client.post(
            "/api/v1/integrations/imports",
            json={"entity_type": "customer", "format": "CSV", "content": "code,name\nC1,Co"},
            headers=unpriv_headers,
        )
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/integrations/exports",
            json={"entity_type": "customer", "format": "JSON"},
            headers=unpriv_headers,
        )
    ).status_code == 403
    assert (
        await client.get("/api/v1/integrations/jobs", headers=unpriv_headers)
    ).status_code == 403

    # 1. Dry-run CSV import validates without persisting
    csv_body = "code,name,credit_limit,currency_code\nC-HTTP-01,=SUM(1+1),5000.00,TWD\n"
    dry_resp = await client.post(
        "/api/v1/integrations/imports",
        json={
            "entity_type": "customer",
            "format": "CSV",
            "dry_run": True,
            "content": csv_body,
        },
        headers=admin1.headers,
    )
    assert dry_resp.status_code == 200
    dry_data = dry_resp.json()
    assert dry_data["job"]["dry_run"] is True
    assert dry_data["job"]["status"] == "DRY_RUN"
    assert dry_data["job"]["total_rows"] == 1
    assert dry_data["job"]["failed_rows"] == 0
    assert dry_data["job"]["succeeded_rows"] == 0
    assert dry_data["errors"] == []

    # 2. Live CSV import persists customer (with literal '=SUM(1+1)' intact in DB)
    live_resp = await client.post(
        "/api/v1/integrations/imports",
        json={
            "entity_type": "customer",
            "format": "CSV",
            "dry_run": False,
            "content": csv_body,
        },
        headers=admin1.headers,
    )
    assert live_resp.status_code == 200
    live_data = live_resp.json()
    import_job_id = live_data["job"]["id"]
    assert live_data["job"]["status"] == "COMPLETED"
    assert live_data["job"]["succeeded_rows"] == 1

    # 3. Export CSV (default include_bom=False -> no UTF-8 BOM, formula neutralized)
    export_csv_resp = await client.post(
        "/api/v1/integrations/exports",
        json={"entity_type": "customer", "format": "CSV"},
        headers=admin1.headers,
    )
    assert export_csv_resp.status_code == 200
    export_csv_data = export_csv_resp.json()
    assert not export_csv_data["content"].startswith("\ufeff")
    assert "'=SUM(1+1)" in export_csv_data["content"]

    # 4. Jobs history & cross-tenant isolation
    jobs_resp = await client.get(
        "/api/v1/integrations/jobs?job_type=IMPORT", headers=admin1.headers
    )
    assert jobs_resp.status_code == 200
    job_ids = {item["id"] for item in jobs_resp.json()}
    assert import_job_id in job_ids

    job_detail = await client.get(
        f"/api/v1/integrations/jobs/{import_job_id}",
        headers=admin1.headers,
    )
    assert job_detail.status_code == 200
    assert job_detail.json()["id"] == import_job_id

    other_job_detail = await client.get(
        f"/api/v1/integrations/jobs/{import_job_id}",
        headers=admin2.headers,
    )
    assert other_job_detail.status_code == 404


@pytest.mark.asyncio
async def test_batch_import_malformed_csv_field_and_oversized_payload_return_422(
    client: AsyncClient,
) -> None:
    admin = await setup_company_with_admin(client, code="INT_BATCH_ERR_CO")

    # Field > 131,072 bytes triggers csv.Error -> translated to 422 instead of 500
    oversized_field_resp = await client.post(
        "/api/v1/integrations/imports",
        json={
            "entity_type": "customer",
            "format": "CSV",
            "content": "code,name\nC1," + ("X" * 150_000) + "\n",
        },
        headers=admin.headers,
    )
    assert oversized_field_resp.status_code == 422

    # Payload exceeding Pydantic max_length=1_048_576 fails fast with 422
    too_large_resp = await client.post(
        "/api/v1/integrations/imports",
        json={
            "entity_type": "customer",
            "format": "CSV",
            "content": "a" * 1_048_577,
        },
        headers=admin.headers,
    )
    assert too_large_resp.status_code == 422
