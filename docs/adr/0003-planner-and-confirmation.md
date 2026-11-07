# ADR 0003: The planner proposes, the confirmation sheet commits

- Status: accepted
- Date: 2026-11-07
- Deciders: engineering, cost-accounting advisor

## Context

Natural-language questions hide decisions: which period, which plant, which product,
which basis, whether an open period may be used, whether an activity rate is a driver
or a policy. If a system resolves those silently, two runs of the same sentence can
produce different numbers and neither is reviewable.

## Decision

The planner is a deterministic router, not an arithmetic agent. It produces a `Plan`:

1. **One intent, one tool.** The intent maps to exactly one registered tool and tool
   version. Unknown or multi-intent questions are refused with the supported examples,
   not guessed.
2. **Arguments are decimal strings.** Every numeric argument is a decimal string; the
   plan never carries floats.
3. **Assumptions are objects.** Each resolved value carries its source
   (`user_explicit`, `user_implied`, `registry`, `lookup`, `policy_default`,
   `engine_default`, `demo_default`), the evidence used, a confidence, and whether the
   user must confirm it.
4. **At most one clarification.** If a required value is missing or confidence is
   below 0.60, the plan stops with one question and one remediation. It is never
   confirmed with a guess.
5. **The sheet is the trust boundary.** The confirmation endpoint checks the plan
   version, applies user edits with a reason, bumps the plan version, and only then
   dispatches the engine. High-impact plans require the `confirm` permission; an
   analyst can prepare but not commit one.
6. **Narration is validated.** The rationale template may contain fact slots and
   registered formats only. A template with a literal number, an unknown slot, or an
   unknown format is rejected; the orchestrator records the failed attempt and uses a
   deterministic fallback sentence.

The engine sees only confirmed arguments. It never sees the original question, so
prompt-injection text cannot alter a calculation.

## Consequences

- A plan can be inspected, diffed, and replayed; the audit record stores the confirmed
  arguments, not the sentence.
- A rule-based planner has limited coverage. Coverage is explicit: unsupported
  questions return examples rather than a plausible answer.
- Replacing the router with a model-backed planner changes one module and keeps the
  Plan schema, the sheet, and the engine boundary unchanged.
- An out-of-scope customer or an implausible scenario driver returns a 409 with the
  values that are in scope, so the user has a path forward without editing fixtures.
