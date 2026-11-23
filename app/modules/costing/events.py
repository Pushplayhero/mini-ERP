"""Domain events published by costing (ADR-016).

Only the `event_type` string constants live here — the payload schema
(`CostAdjustmentPostingPayload`) lives in `app.modules.ledger.posting`, next to
the posting rules that consume it, matching `sales.events` / `purchase.events` /
`payables.events`.
"""

from __future__ import annotations

COSTING_LANDED_COST_POSTED_EVENT_TYPE = "costing.landed_cost_posted"
COSTING_LANDED_COST_VOIDED_EVENT_TYPE = "costing.landed_cost_voided"
COSTING_PPV_UNFAVORABLE_POSTED_EVENT_TYPE = "costing.ppv_unfavorable_posted"
COSTING_PPV_UNFAVORABLE_VOIDED_EVENT_TYPE = "costing.ppv_unfavorable_voided"
COSTING_PPV_FAVORABLE_POSTED_EVENT_TYPE = "costing.ppv_favorable_posted"
COSTING_PPV_FAVORABLE_VOIDED_EVENT_TYPE = "costing.ppv_favorable_voided"
COSTING_REVALUATION_UP_POSTED_EVENT_TYPE = "costing.revaluation_up_posted"
COSTING_REVALUATION_UP_VOIDED_EVENT_TYPE = "costing.revaluation_up_voided"
COSTING_REVALUATION_DOWN_POSTED_EVENT_TYPE = "costing.revaluation_down_posted"
COSTING_REVALUATION_DOWN_VOIDED_EVENT_TYPE = "costing.revaluation_down_voided"
