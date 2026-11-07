# ADR 0008: Financial statements are computed, not narrated into existence

- Status: accepted
- Date: 2026-11-07
- Deciders: engineering, cost-accounting advisor

## Context

A controller may bring a statement workbook, a PDF annual report, a screenshot, or
nothing more than an annual turnover. Each source should still produce a balance
sheet, an income statement, a cash flow statement and ratio analysis, and the model
must not invent any of those numbers. The same question must be answerable from a
printed file and from the turnover alone, with the difference between the two made
explicit.

## Decision

The `financial_statements` intent is served by one deterministic engine with two
modes.

1. **Reported mode reads, never invents.** Canonical line labels are matched from
   standard aliases. A printed subtotal is kept; a missing subtotal is recomputed
   from its accounting identity and marked derived (`FIN-004`). A printed subtotal
   that disagrees with its components raises an informational `FIN_CONFLICT` warning
   rather than being replaced. A missing cash flow raises `FIN_NO_CASHFLOW` and is
   not estimated.
   When a PDF, Word file or OCR'd screenshot carries the lines as prose with no table
   structure, the parser pairs standard labels with the amounts that follow, masks
   dates and years, applies one document-level scale, and reads the current and prior
   columns left to right. A table is always preferred; the prose path is used only
   when the tables yield no statement lines.
2. **Model mode builds a consistent set.** Turnover and explicit assumptions produce
   an income statement, balance sheet and indirect cash flow. Cash is the balancing
   item, so total assets equal total liabilities plus equity and closing cash equals
   opening cash plus the net change by construction. These identities are asserted
   before the result is sealed.
3. **One rulebook.** `FIN-001` to `FIN-013` cover reported lines, derived lines, the
   balance check, the indirect cash flow, profitability, liquidity, leverage,
   efficiency, growth, and the observations. Every line and ratio is a Decimal step
   with a formula, its inputs, a source, and a trace entry.
4. **Scales are applied once.** Lakh, crore, million, billion and thousand markers in
   a header or question become a single multiplier in the parser or planner. The
   engine receives plain decimals. Indian comma formatting is accepted.
5. **Periods are explicit.** The current and prior columns are chosen from the
   stated `FY` period. A period the file does not hold is a typed error with the
   columns that are available, not a silent fallback.
6. **Assumptions are reviewed.** Gross margin, operating-expense ratio, depreciation,
   interest, tax, working-capital days, leverage, payout and capex appear on the
   confirmation sheet with their source. Edits accept a percentage (`25%`) or a raw
   share (`0.25`), and a scaled turnover (`3 crore`) is normalised before the run.
7. **The model narrates after calculation.** Narration references ratio and line facts
   and the deterministic observations only. It cannot introduce a number that the
   engine did not produce, and the same number gates that protect other intents apply.

## Consequences

- Modelled statements are internally consistent management accounts, not a forecast.
  They show what the stated margins, days and gearing imply; they do not predict next
  year.
- Reported mode depends on recognisable row labels. A file with unrecognisable labels
  asks for a turnover, or for the rows to be renamed, instead of guessing a mapping.
- Bank statements and similar non-financial tables are not treated as statements;
  the router needs a statement phrase or a statement file to enter the intent.
- Re-performance is exact because the rulebook and the inputs are sealed with the run.
  Exports carry the three statements, every ratio and the observations.
