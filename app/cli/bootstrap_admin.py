"""The full fresh-install sequence (ADR-009 Decision 6, WEEK9 Slice B).

Entry point: `uv run python -m app.cli.bootstrap_admin --email a@b.com --password ...`
(or set `BOOTSTRAP_ADMIN_EMAIL`/`BOOTSTRAP_ADMIN_PASSWORD` — credentials are
never hardcoded here, per Decision 6).

**Exempt from `require_permission()` under Rule 10b (ADR-009 Decision 10)**
— not Rule 10a: this creates real new business facts (the first `Company`/
`User`/credentials/entitlement), but runs at a point where the role/
permission catalog is already seeded (migration 0009) while no user/
credential/entitlement capable of authenticating an actor exists yet —
requiring a permission check here would be circular (checking whether the
operator creating the *first* administrator is themselves an
administrator).

Rule 10b is inherently first-run-only (unlike Rule 10a's reconstruction
tools, which stay exempt on every run): this script itself refuses to
create a second user once any `users` row exists (enforced below, in code
— not merely documented), since re-running it to add *additional* users
would be exactly the "creates new business facts" case Rule 10a excludes,
and this tool has no exemption covering repeat runs.

Ordering mirrors `seed_demo.py`'s `_ensure_reference_data` ->
`_ensure_company` sequence: a freshly-migrated database has empty
`currencies`/`uom` tables, and `Company.functional_currency_code`'s FK
needs a real currency row to reference before a company can be created.
No tenant-scoped write happens anywhere in this script (`Company`,
`Currency`/`Uom`, `User`, `user_company_roles` are all deliberately
*not* `TenantScopedMixin` — see their own docstrings), so unlike
`seed_demo.py`, this script never needs to bind `company_context(...)`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import dispose_engine, get_session_factory
from app.modules.masterdata import service as masterdata_service
from app.modules.masterdata.models import Company, Currency, Uom
from app.platform.permissions import service as permissions_service

_ADMIN_ROLE_CODE = "admin"

# Arbitrary but fixed forever (changing it would silently stop new callers
# from contending with locks taken under the old key) — a global, not
# per-company, `app.core.locking`-style advisory lock key: bootstrap has no
# company yet when it needs to serialize against a concurrent run. Chosen
# well within `bigint` range (Postgres advisory lock keys are signed 64-bit).
_BOOTSTRAP_LOCK_KEY = 20260817_009


class BootstrapRefusedError(RuntimeError):
    """Raised when a `users` row already exists — Rule 10b is first-run-only."""


async def _ensure_reference_data(session: AsyncSession) -> None:
    """Non-committing — see `bootstrap_admin()`'s docstring note (Codex

    diff-review finding, Slice B round 2): going through
    `masterdata_service`'s self-committing `create_currency`/`create_uom`
    would each end the whole bootstrap transaction early, releasing the
    advisory lock before the critical section it protects is even half
    done. `session.add(...)` + a single `flush()` here, never `commit()`.
    """
    currencies = await masterdata_service.list_currencies(session)
    if not any(c.code == "TWD" for c in currencies):
        session.add(Currency(code="TWD", name="New Taiwan Dollar", decimal_places=0))
    uoms = await masterdata_service.list_uoms(session)
    if not any(u.code == "EA" for u in uoms):
        session.add(Uom(code="EA", name="Each"))
    await session.flush()


async def _ensure_company(session: AsyncSession, *, code: str, name: str) -> uuid.UUID:
    """Non-committing — see `_ensure_reference_data`'s docstring."""
    existing = await masterdata_service.list_companies(session)
    if existing:
        return existing[0].id

    company = Company(code=code, name=name, functional_currency_code="TWD")
    session.add(company)
    await session.flush()
    return company.id


async def bootstrap_admin(
    *, email: str, password: str, company_code: str, company_name: str
) -> uuid.UUID:
    """Runs the entire fresh-install sequence as ONE transaction, committing

    exactly once at the end (Codex diff-review finding, Slice B round 2 —
    round 1's fix only merged the user+grant writes into one commit, missing
    that the reference-data/company steps before them also each committed
    independently through `masterdata_service`'s normal self-committing
    functions, ending the advisory-lock-protected transaction early every
    time). Every step below is non-committing (`add`/`flush` only); a
    failure anywhere rolls the whole attempt back via the session context
    manager's own rollback-on-exception behavior, leaving nothing partial
    to retry against.
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        # A fixed, global (not per-company — there is no company yet)
        # transaction-scoped advisory lock, released automatically at
        # commit/rollback, mirroring `app.core.locking`'s existing pattern.
        # Serializes concurrent bootstrap invocations: the second waits
        # here until the first's single commit (or rollback) below.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _BOOTSTRAP_LOCK_KEY}
        )

        existing_user_count = await permissions_service.count_users(session)
        if existing_user_count > 0:
            raise BootstrapRefusedError(
                f"Refusing to run: {existing_user_count} user(s) already exist. "
                "bootstrap_admin.py is a first-install-only tool (ADR-009 Rule 10b) — "
                "create additional users through the normal admin flow instead."
            )

        await _ensure_reference_data(session)
        company_id = await _ensure_company(session, code=company_code, name=company_name)
        user = await permissions_service.add_user_with_grant(
            session,
            email=email,
            password=password,
            company_id=company_id,
            role_code=_ADMIN_ROLE_CODE,
        )
        await session.commit()
        await session.refresh(user)
        return user.id


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ADR-009 first-install bootstrap (Rule 10b).")
    parser.add_argument("--email", default=os.environ.get("BOOTSTRAP_ADMIN_EMAIL"))
    parser.add_argument("--password", default=os.environ.get("BOOTSTRAP_ADMIN_PASSWORD"))
    parser.add_argument(
        "--company-code", default=os.environ.get("BOOTSTRAP_ADMIN_COMPANY_CODE", "MAIN")
    )
    parser.add_argument(
        "--company-name", default=os.environ.get("BOOTSTRAP_ADMIN_COMPANY_NAME", "Main Company")
    )
    args = parser.parse_args(argv)
    if not args.email or not args.password:
        parser.error(
            "--email/--password are required (or set BOOTSTRAP_ADMIN_EMAIL/"
            "BOOTSTRAP_ADMIN_PASSWORD) — credentials are never hardcoded (ADR-009 Decision 6)."
        )
    return args


async def _main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        user_id = await bootstrap_admin(
            email=args.email,
            password=args.password,
            company_code=args.company_code,
            company_name=args.company_name,
        )
    except BootstrapRefusedError as exc:
        print(f"bootstrap_admin: {exc}", file=sys.stderr)
        await dispose_engine()
        return 1

    print(f"bootstrap_admin: created user {user_id} with role '{_ADMIN_ROLE_CODE}'")
    await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
