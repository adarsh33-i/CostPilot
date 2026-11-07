# ADR 0006: Validated dataset uploads activate content-addressed snapshots

- Status: accepted
- Date: 2026-11-07
- Deciders: engineering, cost-accounting advisor

## Context

A demo dataset proves the engine, not the product. A controller needs to load the
actual month-end extract and ask questions against it, without a migration script and
without the risk that a half-filled workbook silently changes a number. The sample
must remain a sample, not a hidden constant that leaks into uploaded answers.

## Decision

An upload is a schema-controlled transaction, not a file copy.

1. **Two accepted shapes.** A single `.xlsx` with one sheet per canonical table, or a
   `.zip` of the same CSVs. Sheet names are matched case-insensitively and normalised
   (`Standard Costs` and `standard_costs.csv` resolve to the same table). Extra sheets
   are ignored and reported by name.
2. **Validate before activate.** Extraction checks required tables, required columns,
   and empty required sheets. A blocking defect returns `SCHEMA_MISMATCH` with the
   exact table and column plus a remediation. After loading, any ingestion gate with
   status `block` refuses activation with `E_DATA_QUALITY` and the gate's own
   remediation. In both cases the active snapshot is unchanged.
3. **Content-addressed identity.** The snapshot id is the manifest hash of the
   normalised CSVs (`snap_<hash>`). Uploading identical bytes twice replaces the same
   stored snapshot and one history entry, rather than accumulating duplicates.
4. **Durable and explicit activation.** The normalised dataset is stored under
   `COSTPILOT_DATASETS`, with `active.json` and `index.json` recording the pointer and
   history. Startup loads the active dataset; the bundled sample is used only when no
   uploaded snapshot is active. Activation is service-wide and requires the `run`
   permission.
5. **The whole product follows the snapshot.** Capability, demo defaults, planner
   catalog, gates, and resolution read the active snapshot. No route may fall back to
   fixture values once an upload is active.
6. **Runs keep their pin.** A sealed run stores its snapshot id and manifest hash.
   Loading a new dataset does not rewrite old evidence.
7. **Uploads are untrusted input.** A workbook is data. Formula-injection guards apply
   on export, zip members are size-capped, and no upload can alter rules, permissions,
   or the audit chain.

## Consequences

- An upload that fails a gate is a specific, actionable error rather than a broken
  dashboard; the current dataset keeps serving questions.
- Storage grows with distinct datasets, not with repeated uploads of the same bytes.
- Users can reset to the sample by removing the active pointer or setting
  `COSTPILOT_FIXTURES`; that choice is visible in the dataset drawer.
- A canonical-schema extract is the integration contract. ERP adapters belong in
  ingestion, not in the analysis engine.
