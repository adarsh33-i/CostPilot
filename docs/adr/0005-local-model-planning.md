# ADR 0005: Free local models plan and narrate; the engine calculates

- Status: accepted
- Date: 2026-11-07
- Deciders: engineering, cost-accounting advisor

## Context

A language model is useful for reading an accountant's question and explaining a
result. It is not a trustworthy calculator: token-level arithmetic drifts, cannot be
re-performed, and leaves no rule id behind. Hosted models also raise a data-residency
question that a controller cannot answer for month-end cost data.

## Decision

The model is an interpreter, not a calculator.

1. **Free and local.** Planning uses Ollama on the same machine. The catalog exposes
   `free: true`, `local: true`, and the prompt contains no credentials. There is no
   hosted fallback, so no accounting row leaves the host by default.
2. **The model returns a plan, not a result.** One request asks for JSON with an intent
   from the registered catalog, decimal-string slots, and a confidence. The model may
   not invent an intent, identifier, or number outside the dataset catalog; those
   replies are rejected.
3. **The engine calculates.** The plan is resolved against the snapshot, shown on the
   confirmation sheet with a source per value, and only then dispatched to a registered
   tool. Every fact in the card comes from a calc step with a formula and inputs.
4. **Narration reuses facts.** A narration may reference `{{f_profit}}`-style slots and
   registered formats. A reply with a literal digit that is not a calculated fact is
   rejected, and a deterministic sentence is used instead.
5. **Fallback is visible.** If Ollama is offline, slow, or returns a malformed plan, the
   deterministic planner answers and the reply carries a `planner_note`. A question
   never dead-ends because a model is missing.
6. **The planner is recorded.** The plan stores `planner` (`llm:<model>` or
   `deterministic`) and the chosen model, so a reviewer can see how the question was
   read without trusting the model for arithmetic.

## Consequences

- Plan quality varies with the installed model. The boundary does not: the same engine,
  rules, gates, and audit chain run whether a model or the router planned the question.
- Tool calling is not required. A JSON-format completion is enough, so smaller free
  models work and no vendor SDK is needed.
- Model errors become a note and a fallback, not an exception; operators can still see
  the failure in the trace.
- A hosted model could be added behind the same `LLMClient` interface, but it would
  need a separate decision about data residency.
