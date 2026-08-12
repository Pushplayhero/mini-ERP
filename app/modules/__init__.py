"""Business modules (modular monolith).

Each submodule owns its own `models.py` / `schemas.py` / `service.py` /
`router.py` / `events.py` and must not import another module's `models`/
`service` directly (import-linter enforces this in CI). Cross-module
communication goes through service-layer calls or domain events, never
shared ORM model imports.
"""
