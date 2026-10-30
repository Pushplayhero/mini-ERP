"""`platform.permissions` — identity, RBAC, and the authorization port's sole producer.

ADR-009. Owns the six tables migration 0009 created (`users`, `roles`,
`permissions`, `role_permissions`, `user_company_roles`, `sessions`), login/
switch-company/logout, and the concrete `app.core.authorization.ActorContext`
resolution logic. `app.core.authorization` itself never imports this package
— there is exactly one producer of `ActorContext`, wired at the composition
root (`app.main`), not a swappable interface.
"""
