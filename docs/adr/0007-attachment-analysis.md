# ADR 0007: Attached documents are evidence, not instructions

- Status: accepted
- Date: 2026-11-07
- Deciders: engineering, cost-accounting advisor

## Context

Controllers work from a supplier quote, a cost sheet, a board PDF, or a screenshot of
an ERP screen. Asking them to retype those figures into a canonical dataset defeats
the point of the product, and letting a model read the file freely would break the
rule that every number comes from the engine. The chat needs to accept ordinary
office documents and answer questions about them without inventing figures.

## Decision

An attachment is extracted once, analysed by registered rules, and sealed like any
other run.

1. **One read-only extract per upload.** XLSX/CSV become tables; DOCX, PDF, text,
   Markdown and JSON become passage text; images go through OCR. The extract stores
   the file name, kind, byte size, SHA-256, tables, text and warnings. There is no
   write-back to the source and no live link to it.
2. **The extract is data.** Prompt-injection text in a document is never executed or
   followed. The planner receives the attachment names, table names and headers, not
   the full text; only the chosen operation receives the relevant passages.
3. **The engine owns the arithmetic.** Rule catalogue `documents` (DOC-001 to DOC-007)
   covers row count, column sum, average, min/max, total difference, row selection and
   passage quotes. Every step cites the attachment and its SHA-256. The model selects
   an operation and names a table or column; it never supplies a value.
4. **Quoted figures are grounded.** For an open question, passages are copied verbatim
   and each printed figure becomes a fact (`f_doc_fig_N`) with its printed form, unit
   and context. Currency prefixes and lakh/crore scaling are applied once, in the
   engine. Narration may only refer to those fact slots.
5. **Confirmation still applies.** An attachment plan shows the file, operation, table,
   column, compare column and row limit with their sources. Nothing is calculated
   until it is confirmed, and the sealed run records the attachment id, name and hash.
6. **Bounded input.** At most 25 MB per file, 80 MB extracted archive, 400 rows, 40
   columns, 10 tables, 180,000 characters and 40 PDF pages. Exceeding a bound returns a
   typed error with a next step rather than a partial answer.
7. **Private by default.** An attachment belongs to its uploader; other users get
   `E_AUTH_SCOPE`, admins excepted. The extracted text stays on the server; the chat
   receives a preview of at most six rows per table and 1,200 characters.

## Consequences

- A scanned document without a text layer depends on local OCR. If OCR is unavailable
  the upload fails with `E_ATTACHMENT_OCR` and the remediation says so; it does not
  return an empty analysis.
- A spreadsheet row limit of 400 keeps the parameter sheet reviewable. Larger extracts
  should be filtered before upload; the truncation warning is shown.
- Attachment analysis is separate from the canonical dataset. It does not activate a
  snapshot, change demo defaults, or alter the dataset gates.
- The document rules are versioned with the registry, so a sealed run can be
  re-performed against the same catalogue version.
