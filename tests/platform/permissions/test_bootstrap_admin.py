"""tests/platform/permissions/test_bootstrap_admin.py — `app.cli.bootstrap_admin`.

ADR-009 Decision 6 / Rule 10b.

`bootstrap_admin()` opens its own session via `get_session_factory()`, so
these tests rely on the same ephemeral test database `db_session` also
points at (both share `app.core.db`'s engine singleton, reset per test by
the autouse `db_engine` fixture) — the same pattern
`tests/inventory/test_rebuild_cli.py` already establishes for CLI-vs-fixture
database sharing.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli.bootstrap_admin import BootstrapRefusedError, bootstrap_admin
from app.modules.masterdata import service as masterdata_service
from app.platform.permissions import service as permissions_service
from tests.platform.permissions._helpers import create_company


async def _remove_reference_data(session: AsyncSession) -> None:
    """Undo the autouse `_seed_reference_data` fixture's TWD/EA rows.

    Codex diff-review finding (Slice B round 3): without this, the rollback
    and concurrency tests below never actually exercise
    `_ensure_reference_data()`'s `session.add(...)` path at all — it's a
    no-op get-or-create against rows that already exist, so a regression
    that reintroduced an intermediate commit *inside* that function
    specifically would go undetected by these tests.

    `currencies`/`uom` are never truncated between tests (deliberately —
    see `tests/conftest.py`'s `_clean_tenant_tables` docstring: "global,
    read-mostly data shared by design"), so an *unrelated* earlier test in
    the same full-suite run may have left `exchange_rates`/
    `uom_conversions` rows referencing this session's TWD/EA — deleting
    those first (also never truncated, same reasoning) avoids a spurious
    FK-violation failure that has nothing to do with what this test
    actually checks.
    """
    await session.execute(
        text(
            "DELETE FROM exchange_rates "
            "WHERE from_currency_code = 'TWD' OR to_currency_code = 'TWD'"
        )
    )
    await session.execute(
        text(
            "DELETE FROM uom_conversions "
            "WHERE from_uom_id IN (SELECT id FROM uom WHERE code = 'EA') "
            "OR to_uom_id IN (SELECT id FROM uom WHERE code = 'EA')"
        )
    )
    await session.execute(text("DELETE FROM currencies WHERE code = 'TWD'"))
    await session.execute(text("DELETE FROM uom WHERE code = 'EA'"))
    await session.commit()


@pytest.mark.asyncio
async def test_bootstrap_admin_creates_first_user_and_grants_admin_role(
    db_session: AsyncSession,
) -> None:
    user_id = await bootstrap_admin(
        email="firstadmin@example.com",
        password="pw12345",
        company_code="BOOT1",
        company_name="Bootstrap Co",
    )
    assert user_id is not None

    result = await permissions_service.login(db_session, "firstadmin@example.com", "pw12345", None)
    assert result.user_id == user_id
    assert (
        "session.manage"
        in (
            await permissions_service.resolve_actor(db_session, result.raw_token)
        ).actor_context.permission_codes
    )


@pytest.mark.asyncio
async def test_bootstrap_admin_refuses_second_run(db_session: AsyncSession) -> None:
    await bootstrap_admin(
        email="first@example.com",
        password="pw12345",
        company_code="BOOT2",
        company_name="Boot Co 2",
    )

    with pytest.raises(BootstrapRefusedError):
        await bootstrap_admin(
            email="second@example.com",
            password="pw12345",
            company_code="BOOT2B",
            company_name="Boot Co 2B",
        )

    # The refusal must be a genuine no-op — no second user created.
    count = await permissions_service.count_users(db_session)
    assert count == 1


@pytest.mark.asyncio
async def test_bootstrap_admin_reuses_matching_existing_company(db_session: AsyncSession) -> None:
    existing_company_id = await create_company(db_session, "PREEXIST")

    await bootstrap_admin(
        email="reuse@example.com",
        password="pw12345",
        company_code="PREEXIST",
        company_name="Preexist Co",
    )

    result = await permissions_service.login(db_session, "reuse@example.com", "pw12345", None)
    assert result.active_company_id == existing_company_id


@pytest.mark.asyncio
async def test_bootstrap_admin_refuses_mismatched_existing_company(
    db_session: AsyncSession,
) -> None:
    await create_company(db_session, "PREEXIST")

    with pytest.raises(BootstrapRefusedError, match="do not match"):
        await bootstrap_admin(
            email="mismatch@example.com",
            password="pw12345",
            company_code="DIFFERENT",
            company_name="Different Co",
        )


@pytest.mark.asyncio
async def test_bootstrap_admin_refuses_incompatible_currency_company(
    db_session: AsyncSession,
) -> None:
    from app.modules.masterdata.models import Company

    # Bypass service validator to create a non-TWD company
    company = Company(code="NON_TWD_CO", name="Non-TWD Co", functional_currency_code="USD")
    db_session.add(company)
    await db_session.commit()

    with pytest.raises(BootstrapRefusedError, match="unsupported functional currency"):
        await bootstrap_admin(
            email="incompat@example.com",
            password="pw12345",
            company_code="NON_TWD_CO",
            company_name="Non-TWD Co",
        )


@pytest.mark.asyncio
async def test_bootstrap_admin_rolls_back_everything_on_failure(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex diff-review finding (Slice B round 2): a failure after

    reference-data/company creation but before the final commit must roll
    back the WHOLE attempt — not leave orphaned currency/uom/company rows
    with no user, which a later retry (now allowed again, since
    `count_users()` would still see zero) could collide with or duplicate.

    Round 3 addition: TWD/EA are deleted first so `_ensure_reference_data()`
    genuinely inserts them (not a no-op get-or-create against the autouse
    fixture's already-seeded rows) — otherwise this test would never touch
    that function's own `session.add(...)` calls at all, and a regression
    reintroducing a commit specifically inside it would go undetected.
    """
    await _remove_reference_data(db_session)

    async def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated failure before the final commit")

    monkeypatch.setattr(permissions_service, "add_user_with_grant", _boom)

    with pytest.raises(RuntimeError, match="simulated failure"):
        await bootstrap_admin(
            email="rollback@example.com",
            password="pw12345",
            company_code="ROLLBACK",
            company_name="Rollback Co",
        )

    currencies = await masterdata_service.list_currencies(db_session)
    uoms = await masterdata_service.list_uoms(db_session)
    assert not any(c.code == "TWD" for c in currencies)
    assert not any(u.code == "EA" for u in uoms)
    companies = await masterdata_service.list_companies_unchecked(db_session)
    assert not any(c.code == "ROLLBACK" for c in companies)
    count = await permissions_service.count_users(db_session)
    assert count == 0

    # A retry after the rollback must succeed cleanly (proving nothing was
    # left half-committed for it to collide with) — undo the patch first,
    # or this retry would hit the same simulated failure.
    monkeypatch.undo()
    user_id = await bootstrap_admin(
        email="retry@example.com",
        password="pw12345",
        company_code="ROLLBACK",
        company_name="Rollback Co",
    )
    assert user_id is not None


@pytest.mark.asyncio
async def test_bootstrap_admin_concurrent_runs_only_one_succeeds(
    db_session: AsyncSession,
) -> None:
    """Codex diff-review finding (Slice B round 1/2): the advisory lock

    held for the whole transaction (not released early by an intermediate
    commit) must serialize two concurrent invocations racing to become
    "the first admin" — exactly one succeeds, the other is refused, never
    both.

    Round 3 addition: TWD/EA are deleted first so both racers' locked
    critical sections genuinely include the reference-data insert path,
    not just company/user creation — the exact section round 2 found
    still vulnerable to an early-releasing intermediate commit.
    """
    await _remove_reference_data(db_session)

    results = await asyncio.gather(
        bootstrap_admin(
            email="racer1@example.com",
            password="pw12345",
            company_code="RACE",
            company_name="Race Co",
        ),
        bootstrap_admin(
            email="racer2@example.com",
            password="pw12345",
            company_code="RACE",
            company_name="Race Co",
        ),
        return_exceptions=True,
    )

    successes = [r for r in results if isinstance(r, uuid.UUID)]
    refusals = [r for r in results if isinstance(r, BootstrapRefusedError)]
    assert len(successes) == 1, results
    assert len(refusals) == 1, results

    count = await permissions_service.count_users(db_session)
    assert count == 1

    # The winner's reference-data insert must not have raced into a
    # duplicate — exactly one TWD/one EA, not zero (never inserted) or
    # two (inserted twice by both racers before either committed).
    currencies = await masterdata_service.list_currencies(db_session)
    uoms = await masterdata_service.list_uoms(db_session)
    assert sum(1 for c in currencies if c.code == "TWD") == 1
    assert sum(1 for u in uoms if u.code == "EA") == 1
