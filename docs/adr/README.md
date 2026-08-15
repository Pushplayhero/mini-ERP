# Architecture Decision Records

| ADR | Title | Status |
|---|---|---|
| [ADR-001](ADR-001-modular-monolith.md) | Modular monolith, not microservices | Accepted (retroactively documented) |
| [ADR-002](ADR-002-append-only-ledgers.md) | Append-only fact tables + rebuildable summaries | Accepted (retroactively documented) |
| [ADR-003](ADR-003-posting-engine.md) | Posting engine | Accepted |
| [ADR-004](ADR-004-event-bus.md) | Event bus | Accepted |
| [ADR-005](ADR-005-ledger-journal-design.md) | Ledger/journal design | Accepted |
| [ADR-006](ADR-006-sales-and-hook-registry.md) | Sales module, hook registry | Accepted |
| [ADR-007](ADR-007-inventory-and-shipping.md) | Inventory, shipping | Accepted |
| [ADR-008](ADR-008-receivables.md) | Receivables — invoicing, payments, AR aging | Accepted |

ADR-003 through ADR-008 each passed a real `codex` CLI architecture
consensus review before implementation (see each ADR's "Consensus
Revisions" section for the review history) — not a Claude self-review; see
HANDOFF.md for why that distinction matters on this project. ADR-001 and
ADR-002 document decisions already made and shipped by Week 1/2
respectively (see each ADR's "Documentation note" / "Scope note"); they
received a routine-tier Codex diff review for factual accuracy instead of
a pre-implementation consensus review, since there was no implementation
left to gate.

This index, plus `docs/adr/WEEK7-phase1-hardening-brief.md` (the
implementation brief that produced ADR-001/002 and everything else in
Week 7), are the project's Phase-1-closing documentation set.

`docs/adr/WEEK8-phase1-polish-brief.md` is a separate, later brief
(presentation/infrastructure polish on top of the now-complete Phase 1 —
a `v0.1.0` tag, a real GitHub remote + CI, a coverage badge, a demo GIF
script/handoff, and one small application-code rounding fix) — it went
through five real Codex plan-consensus review rounds before APPROVED,
same discipline as every ADR and brief above.
