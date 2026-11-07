# ADR 0001: The model never performs arithmetic

- Status: accepted
- Date: 2026-11-07
- Deciders: engineering, cost-accounting advisor

## Context

A finance answer is only usable if a controller can re-perform it. Language models
produce plausible numbers that cannot be reproduced, cannot be traced to a rule, and
change between runs. Prompt instructions such as "do not hallucinate numbers" are not
enforceable.

## Decision

Separate the system into two halves with a hard boundary:

1. **Deterministic engine.** Typed inputs, `Decimal` arithmetic, one versioned rule per
   calculation, a `CalcStep` for every value with formula, inputs, unit, and source
   references. The engine returns a `CalcResult`.
2. **Narration.** A rationale template may contain fact slots only
   (`{{f_profit}}`); it may not contain literal digits, percentages, or currency
   symbols. The renderer substitutes display values from facts that already point at
   engine steps. Validation rejects any template that breaks the slot rule.

The LLM, when it arrives in M4, is restricted to intent extraction, parameter filling,
and choosing a registered tool. It never emits a number. A confirmation sheet exposes
the resolved parameters and their sources before the engine runs.

Three enforcement points make the boundary mechanical rather than aspirational:

- `validate_result` checks schema, unit declaration, fact-to-step coverage, provenance,
  and slot-only narration on every engine result.
- `StepBuilder` refuses duplicate step IDs and resolves the rule version at build time.
- Every run is appended to a SHA-256 hash chain; re-performance compares stored step
  values with a fresh execution of the pinned rules.

## Consequences

- More code per calculation than a prompt. Accepted: the trace is the product.
- Answers are reproducible bit for bit, given the snapshot, registry, and engine
  versions pinned in the run.
- The model cannot answer a question with no registered tool. It must offer the upload
  or confirmation path instead of estimating; that is the intended failure mode.
- Phase 2 workflows must add rules and steps, not prose formulas, before they can ship.
