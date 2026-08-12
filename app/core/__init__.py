"""Cross-cutting kernel: settings, DB session management, tenancy, exceptions.

Rule enforced by import-linter: `app.core` must never import from `app.modules.*`.
Core is the foundation business modules build on, never the other way round.
"""
