# ADR 0002: A canonical snapshot, not live tables

- Status: accepted
- Date: 2026-11-07
- Deciders: engineering, cost-accounting advisor

## Context

The engine needs stable grain, stable keys, and a reproducible answer. ERP extracts
arrive with unknown accounts, changed dimension members, and control totals that do not
tie. If the engine reads live operational tables, every answer becomes a moving target
and no trace can be re-performed.

## Decision

Analysis reads an immutable **snapshot** built from eleven CSV templates:

`entities`, `plants`, `products`, `periods`, `accounts`, `standard_costs`,
`production_actuals`, `budgets`, `gl_balances`, `inventory`, `control_totals`.

The snapshot grain is period × plant × product for costs and volumes, and
period × plant × account for flux. A manifest hashes every file with SHA-256; the
snapshot ID is derived from the manifest hash, so re-ingesting a corrected file creates
a new snapshot ID and never mutates the previous one.

Ten gates run before a snapshot is trusted:

| Gate | Failure behaviour |
| --- | --- |
| G1 schema | Block on empty or malformed files |
| G2 referential integrity | Block; unknown members go to the mapping queue |
| G3 period completeness | Block when a product-plant-period row is missing |
| G4 duplicate keys | Block |
| G5 control totals | Block when GL rows do not tie to expected amounts |
| G6 classification coverage | Block below 99% of amount-weighted GL |
| G7 currency | Block on mixed currencies; no FX translation in Phase 1 |
| G8 sign anomalies | Warn; keep the sign in drill-down |
| G9 outliers | Warn; never silently adjust the price |
| G10 standard cards and usage | Block when a card or usage row is missing |

Blocking gates set `analysis_ready` to false. Warnings remain visible on the capability
card and in every pack.

## Consequences

- Ingestion is a deliberate step, not an implicit read. Phase 2 connectors must land in
  the same canonical tables before analysis works.
- A blocked load is not a dead end: every gate returns a remediation, and unknown
  dimension members are routed to a mapping queue rather than discarded.
- Snapshot storage is currently in memory. A durable snapshot store is required before
  M6 connectors; the manifest and ID contract already support it.
- The synthetic fixture is deterministic and committed so the golden corpus is stable.
