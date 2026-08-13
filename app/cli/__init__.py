"""Standalone operational scripts (not part of the FastAPI app process).

`app/cli/` is a new top-level package, sibling to `app/core` and
`app/modules`, for entry points that drive the running system from outside
an HTTP request — this week, `replay_outbox`. Placed here rather than e.g.
`app/modules/ledger/cli.py` because a replay driver is infrastructure that
dispatches to *whichever* handlers are registered on the bus; it has no
ledger-specific knowledge of its own (that all lives behind
`app.core.events.redispatch`), so it does not belong inside any one business
module.
"""

from __future__ import annotations
