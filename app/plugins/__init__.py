"""app.plugins — trusted, admin-installed extension points (ADR-006 Decision 2).

Plugins MAY import `app.core` and any `app.modules.*` freely. The reverse is
forbidden: `app.core` and `app.modules` must never import `app.plugins` —
enforced by two `import-linter` "forbidden" contracts in `pyproject.toml`
(`core must not import plugins`, `business modules must not import
plugins`). That one-way dependency direction is the entire point: business
modules fire named hook points (`app.core.hooks`) or publish events
(`app.core.events`) without knowing or caring whether anything is
listening; plugins are the thing that listens, living entirely outside both
the kernel and the modules it customizes.

See `README.md` in this directory for what a plugin is, the one shipped
here (`credit_limit.py`), and how to write your own.
"""

from __future__ import annotations
