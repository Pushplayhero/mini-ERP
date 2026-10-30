"""`app.platform` — Phase 2 platform modules (ADR-009 §7.1 Q3, PHASE2 overview).

`permissions` is the first module (ADR-009). Business modules
(`app.modules.*`) are contractually forbidden from importing anything under
this package (import-linter, WEEK9 Slice C) — they depend only on
`app.core.authorization`, the port. Only `app.main` (the composition root)
is allowed to import both and wire them together.
"""
