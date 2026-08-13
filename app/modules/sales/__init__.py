"""sales — order lifecycle (draft/confirm/cancel), ADR-006.

Week 4 deliverable: `SalesOrder`/`SalesOrderLine` CRUD (draft), the
`confirm`/`cancel` state machine, and the `sales.order_confirmed` event
(no posting subscriber this week — see `events.py`). The hook point
`sales.order.validate_confirm` (see `service.SALES_ORDER_VALIDATE_CONFIRM`)
is where `app.plugins.credit_limit` plugs in without this module knowing it
exists.
"""
