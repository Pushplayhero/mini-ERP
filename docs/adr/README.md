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
| [ADR-009](ADR-009-platform-permissions.md) | `platform.permissions` — identity, RBAC, and the authorization port | Accepted |
| [ADR-010](ADR-010-platform-plugins.md) | `platform.plugins` — plugin loader, tenant enablement governance, and public-surface isolation | Accepted |
| [ADR-011](ADR-011-platform-customfields.md) | `platform.customfields` — tenant-governed custom field engine, strong type normalization, and public port isolation | Accepted |
| [ADR-012](ADR-012-platform-workflow.md) | `platform.workflow` — configurable approval chains, amount-threshold routing, and state-transition gate | Accepted |
| [ADR-013](ADR-013-platform-integration.md) | `platform.integration` — webhooks, outbox worker, batch import/export, and SSRF defense | Accepted |
| [ADR-014](ADR-014-purchase-and-supplier-masterdata.md) | `purchase` + `masterdata.Supplier` — procurement lifecycle, goods receipt intake, and two-stage GR/IR accrual | Accepted |
| [ADR-015](ADR-015-payables.md) | `payables` — vendor bills, GR/IR clearing, outgoing supplier payments, and AP aging | Accepted |
| [ADR-016](ADR-016-costing.md) | `costing` — moving weighted average valuation, landed cost, PPV clearing, and revaluation | Accepted |
| [ADR-017](ADR-017-mrp.md) | `mrp` — multi-level BOM, work orders, `1310` WIP subledger, and topological MRP planning | Accepted |
| [ADR-018](ADR-018-staging-security-hardening.md) | Pre-Phase 4 staging security hardening — VULN-06 `MultiFernet` encryption, PostgreSQL RLS (`44` tables), session hardening & CORS / secret scan | Accepted |

See also:
- [Phase 2 Architecture Overview](PHASE2-platform-architecture-overview.md)
- [Phase 3 Suites Architecture Overview](PHASE3-suites-architecture-overview.md)

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
