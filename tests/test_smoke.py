"""Smoke test: the app boots and the health endpoint responds.

Deliberately minimal — this is the "does anything work at all" check the
brief asks for; the substantive coverage lives in tests/masterdata/.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
