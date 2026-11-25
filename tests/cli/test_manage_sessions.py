"""CLI tests for `app.cli.manage_sessions` (ADR-018)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.platform.permissions._helpers import create_company, create_user_with_role

from app.cli import manage_sessions
from app.platform.permissions import service
from app.platform.permissions.models import Session as SessionModel


@pytest.mark.asyncio
async def test_manage_sessions_purge_expired_and_revoke_user(
    db_session: AsyncSession,
    capsys: pytest.CaptureFixture[str],
) -> None:
    co_id = await create_company(db_session, "SESSCLICO")
    await create_user_with_role(
        db_session, email="u1@example.com", password="pw12345", company_id=co_id
    )
    await create_user_with_role(
        db_session, email="u2@example.com", password="pw12345", company_id=co_id
    )

    s_expired = await service.login(db_session, "u1@example.com", "pw12345", None)
    s_idle = await service.login(db_session, "u1@example.com", "pw12345", None)
    s_active_u1 = await service.login(db_session, "u1@example.com", "pw12345", None)
    s_active_u2 = await service.login(db_session, "u2@example.com", "pw12345", None)

    row_exp = await db_session.get(SessionModel, s_expired.session_id)
    row_idle = await db_session.get(SessionModel, s_idle.session_id)
    assert row_exp is not None and row_idle is not None
    row_exp.expires_at = datetime.now(UTC) - timedelta(minutes=5)
    row_idle.last_active_at = datetime.now(UTC) - timedelta(minutes=120)
    await db_session.commit()

    rc_purge = await manage_sessions._amain(["purge-expired"], dispose=False)
    assert rc_purge == 0
    assert "Purged 2 expired/idle session(s)." in capsys.readouterr().out

    remaining_ids = {row.id for row in (await db_session.scalars(select(SessionModel))).all()}
    assert remaining_ids == {s_active_u1.session_id, s_active_u2.session_id}

    # Revoke user u1 -> u2 stays intact
    rc_rev = await manage_sessions._amain(
        ["revoke-user", "--email", "U1@EXAMPLE.COM"], dispose=False
    )
    assert rc_rev == 0
    assert "Revoked 1 session(s)" in capsys.readouterr().out

    remaining_after_rev = {row.id for row in (await db_session.scalars(select(SessionModel))).all()}
    assert remaining_after_rev == {s_active_u2.session_id}

    # Unknown user returns non-zero
    rc_missing = await manage_sessions._amain(
        ["revoke-user", "--email", "nobody@example.com"], dispose=False
    )
    assert rc_missing == 1
    assert "not found" in capsys.readouterr().err
