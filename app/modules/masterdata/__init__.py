"""masterdata — companies, customers, products, UoM, accounts, currencies.

Phase 1 / Week 1 module. See README section "Design Decisions" for the
multi-company isolation mechanism and the global-vs-tenant-scoped table
split (uom/currencies/exchange_rates are shared reference data; customers/
products/accounts are tenant-scoped).
"""
