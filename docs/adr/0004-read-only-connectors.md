# ADR 0004: Connectors are read-only extracts with human-approved mappings

- Status: accepted
- Date: 2026-11-07
- Deciders: engineering, cost-accounting advisor

## Context

The fastest way to lose trust in a finance copilot is to let it write a journal entry.
The second fastest is to let it silently translate an unknown ERP account into an
existing one. Either failure contaminates the ledger and the audit trail at once.

## Decision

Connectors are extract readers, never writers:

1. **Read-only by construction.** The catalog exposes `write_back: false`. A connector
   reads a packaged GL extract, reconciles its control totals against the snapshot, and
   returns a preview with the gates it passed and the rows it could not map.
2. **Least privilege.** The connector contract has no credential-write or posting
   method. Phase 1 ships packaged extracts; a live deployment supplies a read-only
   extract landed in the same canonical tables, not a live query credential inside the
   analysis process.
3. **Mappings are proposals.** An unknown account or dimension member becomes a
   proposal with evidence and a confidence. It is never auto-applied. Approval requires
   the `configure` permission (admin) and is recorded in the platform store; the
   registry version is bumped when accounts change.
4. **Previews can be recorded.** A preview run can be sealed into the same audit chain
   as an analysis, so the reconciliation evidence is reproducible.
5. **Content is data.** Extract rows, account descriptions, and connector metadata are
   treated as untrusted data. They cannot become instructions to the planner or alter a
   confirmed argument.

## Consequences

- No write-back path exists to test or to disable; the capability is absent rather
  than gated.
- Loading latency and mapping effort move to ingestion, where a controller can review
  them, instead of surfacing as a wrong number in a report.
- A blocked extract is not a dead end: the preview names the failing gate, the
  reconciliation difference, and the remediation.
- Live OIDC and a credential broker are deployment concerns; they do not change the
  connector contract or the engine boundary.
