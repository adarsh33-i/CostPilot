# CostPilot

CostPilot is a deterministic management-accounting copilot for manufacturing, with a
ChatGPT-style chat workspace. A controller asks a question in plain language, reviews
the resolved parameters and their sources, confirms, and receives an analysis card
where every number carries a rule ID, a formula, its inputs, and a source reference.
The card is sealed into an append-only SHA-256 audit chain and can be exported to
XLSX, PDF, or JSON.

The design rule is simple: **the language model never performs arithmetic.** A free
local model served by Ollama reads the question and selects one registered analysis;
the plan carries identifiers and decimal-string arguments only. The engine does the
arithmetic with `Decimal`, and narration may only reference fact slots such as
`{{f_profit}}`, never literal digits. If Ollama is offline or the model returns
something outside the contract, the deterministic planner takes over and the chat says
so. The XLSX export shows the same calc steps as the card, so a reviewer can re-add
every number by hand.

## What is implemented

| Area | Capability |
| --- | --- |
| Engine | Decimal money policy, typed calc steps, versioned rule registry, slot-only narration, result validation |
| Data | Canonical snapshot loader, manifest hash, ten ingestion gates, account registry, capability card, templates, validated XLSX/CSV upload with activation and history |
| Variance | Material, labour, overhead, flexible budget, account flux, variable-to-absorption bridge |
| Decisions | Seven workflows: special order, make-or-buy, add-or-drop, sell-or-process, shutdown, product mix, CVP |
| Profitability | Activity-based costing, time-driven ABC with unused capacity, customer profitability with cost to serve |
| Budget | Annual budget chain, driver scenarios with range guard, rolling forecast, forecast-quality scoring |
| Performance | Division performance scorecard, ROI / residual income, investment conflict detection |
| Monitoring | Metric aliases, alert rules, materiality thresholds, digest generation |
| Group | Entity consolidation and group reporting across divisions |
| Platform | Free local-model planner over Ollama, deterministic fallback, confirmation sheet, orchestrator, RBAC and row-level scope, session privacy |
| Trust | Append-only audit chain, re-performance, trace, SSE run events, tamper verification |
| Integrations | Read-only NetSuite, SAP, and flat-file extracts; mapping proposals with human approval; no write-back |
| Delivery | FastAPI v1 API, single-file chat workspace, XLSX/PDF/JSON exports with formula-injection guards |

The full contracts are in [docs/CostPilot_Blueprint.pdf](docs/CostPilot_Blueprint.pdf),
with the [Phase 1](docs/CostPilot_Phase_A_Plan.pdf) and
[Phase 2](docs/CostPilot_Phase_B_Plan.pdf) plans and the
[white paper](docs/CostPilot_WhitePaper.pdf).

## Run it

All paths are absolute so the commands work from any directory. Step 0 tells you
whether the service is already running.

**0. Check whether the service is already running.**

```bash
curl -s http://127.0.0.1:8000/api/health
```

If that prints JSON with `"ok": true`, skip to step 5. If it fails, continue.

**1. Go to the repository.**

```bash
cd /Users/adarshahebbar/Documents/CostPilot
```

**2. Activate the virtual environment.** Create it only if it does not exist.

```bash
source /Users/adarshahebbar/Documents/CostPilot/.venv/bin/activate
```

```bash
# Only when /Users/adarshahebbar/Documents/CostPilot/.venv is missing:
uv venv --python 3.13 /Users/adarshahebbar/Documents/CostPilot/.venv
source /Users/adarshahebbar/Documents/CostPilot/.venv/bin/activate
```

**3. Install the package and development tools.**

```bash
uv pip install -e "/Users/adarshahebbar/Documents/CostPilot[dev]" --python /Users/adarshahebbar/Documents/CostPilot/.venv/bin/python
```

**4. Run without a server.** Each command is independent:

```bash
costpilot check
costpilot capability
costpilot demo
costpilot models
costpilot ask "Variance for PRD-B at PLT-01 in 2026-09"
costpilot run "Customer profitability for PLT-01 in 2026-09 for CUS-05"
costpilot runs --limit 10
costpilot verify
costpilot connectors
```

`check` runs the ten ingestion gates and exits non-zero on a blocking gate. `demo`
prints the canonical special-order trace. `models` lists the free local models that
Ollama serves and marks the default. `ask` prepares a plan and parameter sheet without
running it. `run` plans, confirms, and prints the card summary. `verify` checks the
audit hash chain. Add `--json` to any planning or reporting command for the full
payload.

`ask` and `run` use the deterministic planner by default so they work with no model
running. Pass a model id to let the local model plan instead:

```bash
costpilot ask "Variance for PRD-B at PLT-01 in 2026-09" --model qwen3:8b
costpilot run "Special order for PRD-B at PLT-01: 4000 units at 380 each" --model qwen3:8b --edit order_units=3500
```

`--model auto` uses the configured default model, or the first reachable model when
the default is not installed. Point the client at another Ollama with
`COSTPILOT_OLLAMA_URL`; change the default model with `COSTPILOT_LLM_MODEL`. Models
must be free and local; there is no hosted API key.

**5. Start the workspace.** Two variants, same result:

```bash
costpilot serve --port 8000
```

```bash
python -m uvicorn costpilot.api.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. API docs are at <http://127.0.0.1:8000/docs>.

Ollama is optional. If it is running at <http://127.0.0.1:11434>, the model picker
lists its models and the chat plans with them; otherwise the deterministic planner
answers and the picker offers only that option. Check with:

```bash
costpilot models
```

**Variant: different fixture directory or database files.**

```bash
costpilot serve --port 8000 --fixtures /absolute/path/to/fixtures --audit-db /absolute/path/to/audit.db --platform-db /absolute/path/to/platform.db
```

```bash
COSTPILOT_FIXTURES=/absolute/path/to/fixtures COSTPILOT_AUDIT_DB=/absolute/path/to/audit.db COSTPILOT_PLATFORM_DB=/absolute/path/to/platform.db python -m uvicorn costpilot.api.main:app --port 8000
```

**Variant: another port.**

```bash
costpilot serve --port 8080
```

**6. Sign in.** Demo accounts all use the password `costpilot`, or the value of
`COSTPILOT_DEMO_PASSWORD`:

| User | Role | Scope |
| --- | --- | --- |
| `viewer` | viewer | PLT-01 only, read and export |
| `analyst` | analyst | both plants, can run but cannot approve |
| `controller` | controller | both entities, can confirm and approve |
| `admin` | admin | full configuration and governance |

Set `COSTPILOT_USERS` to a JSON array to replace the demo accounts.

**7. Stop the service** with `Ctrl+C` in the terminal that started it.

## The workspace

The workspace is one chat thread. The topbar carries the model picker and the dataset
name; the thread carries the conversation.

1. **Ask.** Type a question. The selected model reads it and proposes one registered
   analysis; the reply shows the plan, the assumptions with their sources, and any
   values that need confirmation.
2. **Review and confirm.** Edit a parameter inline if it is wrong, then confirm. A
   plan requiring approval needs the `confirm` permission.
3. **Read the card.** The answer card carries the verdict, the facts, the waterfall,
   and the calc steps; every number links to a rule ID, formula, inputs, and source.
4. **Inspect.** Trace shows the sealed run, its pins, and its steps; re-performance
   re-runs the engine and compares exact values; export links produce XLSX, PDF, or
   JSON.

Side drawers cover the rest: the dataset drawer shows quality, gates, upload history,
and the upload control; the runs drawer lists sealed runs with an inspector; the trust
drawer shows the audit chain; the sources drawer previews read-only connector extracts
and mapping proposals. The uploaded dataset is activated for the whole service, so a
new question uses it immediately.

### Bring your own data

The bundled Northline dataset is a sample, not a requirement. In the dataset drawer,
download `costpilot-dataset-template.xlsx`, fill one sheet per canonical table, and
upload it. A ZIP of the same CSVs works too. The upload is validated before it is
activated: missing tables, missing columns, empty required sheets, and blocking
ingestion gates are refused with a specific remediation. Each accepted upload becomes
a content-addressed snapshot (`snap_<hash>`) with its own history entry, and the
special-order case, capability card, and demo defaults come from the active snapshot,
not from constants.

### Models

The picker lists the models your local Ollama serves, plus the deterministic planner.
Nothing is sent anywhere; Ollama runs on the same machine. A model that lacks tool
calling still works because CostPilot asks for a JSON plan, not a tool call. If the
model times out or returns a malformed plan, the chat reports the fallback and the
deterministic planner answers instead, so a question never dead-ends.

## API

The v1 flow is login, session, message, plan, confirm, run. The confirmation sheet is
the trust boundary: nothing is calculated until it is confirmed.

```bash
curl -s -X POST http://127.0.0.1:8000/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"user_id":"controller","password":"costpilot"}'
```

Copy the `token` from the response into `TOKEN`, then:

```bash
export TOKEN='paste-token-here'
```

```bash
curl -s -X POST http://127.0.0.1:8000/v1/sessions \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{}'
```

The session binds to the current snapshot; no dataset argument is needed.

Copy the `session_id`, then:

```bash
export SESSION='paste-session-id-here'
```

```bash
curl -s -X POST "http://127.0.0.1:8000/v1/sessions/$SESSION/messages" \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"text":"Variance for PRD-B at PLT-01 in 2026-09","model":"qwen3:8b"}'
```

Omit `model`, or send `"deterministic"`, to use the rule-based planner. Send `"auto"`
for the configured default. If the named model is not installed or does not answer,
the plan says so in `planner_note` and the deterministic planner handles the question,
so the request still returns a usable plan.

Copy `plan_id` from the response, then:

```bash
export PLAN='paste-plan-id-here'
```

```bash
curl -s -X POST "http://127.0.0.1:8000/v1/plans/$PLAN/confirm" \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"plan_version":1}'
```

The response carries the sealed card, `run_id`, trace and export links. Useful
endpoints: `GET /v1/models`, `GET /v1/capabilities`, `GET /v1/runs`, `GET /v1/runs/{id}`,
`GET /v1/runs/{id}/trace`, `POST /v1/runs/{id}/reperform`, `GET /v1/runs/{id}/events`,
`GET /v1/runs/{id}/export?format=xlsx|pdf|json`, `GET /v1/audit/verify`,
`GET /v1/connectors`, `GET /v1/alerts/rules`, `GET /v1/registry/accounts`.

Dataset endpoints:

- `GET /v1/datasets` — active snapshot, history, accepted formats, template link
- `GET /v1/datasets/{id}/quality` — row counts, gates, quality, remediation
- `GET /v1/datasets/templates` and `GET /v1/datasets/templates/{name}` — CSV header templates
- `GET /v1/datasets/workbook-template` — XLSX template, one sheet per table plus notes
- `POST /v1/datasets/validate` — check a file without activating it
- `POST /v1/datasets/upload` — validate, load, activate, and return gates and capability

```bash
curl -s -X POST http://127.0.0.1:8000/v1/datasets/upload \
  -H "authorization: Bearer $TOKEN" \
  -F 'file=@/absolute/path/to/costpilot-dataset.xlsx' \
  -F 'name=Northline FY27'
```

Upload requires the `run` permission. The new snapshot is active for every session;
existing runs keep their original snapshot pin.

The legacy `/api` surface remains for the structured tools:
`GET /api/health`, `POST /api/tools/preview_differential_analysis`,
`POST /api/tools/run_differential_analysis`, `POST /api/tools/run_variance_analysis`,
`POST /api/tools/run_costing_bridge`, `GET /api/runs/{run_id}`, and
`POST /api/runs/{run_id}/reperform`.

## Supported questions

The planner — the selected local model, or the deterministic router when no model is
chosen — maps natural-language questions to registered tools. Examples:

- `Variance for PRD-B at PLT-01 in 2026-09`
- `Account flux for PLT-01`
- `Costing bridge for PRD-A at PLT-02`
- `Special order for PRD-B at PLT-01: 1200 units at 940 each`
- `ABC profitability at PLT-01 in 2026-09`
- `TDABC capacity for PLT-01`
- `Customer profitability for PLT-01 in 2026-09 for CUS-05`
- `Budget for PLT-01 in 2026-09`
- `Scenario: material price up 3 percent for PLT-01`
- `Rolling forecast for PLT-01`
- `Forecast quality for PLT-01`
- `Division performance for DIV-01`
- `Monitor material price variance at PLT-01 with materiality 75000`
- `Group report for 2026-09`

An out-of-scope customer returns an explicit 409 with the customers that are in scope.
A scenario driver expressed as a percentage is converted to a fraction; a raw driver
above 100% is rejected rather than silently multiplied by 100.

## Verify

```bash
cd /Users/adarshahebbar/Documents/CostPilot
source /Users/adarshahebbar/Documents/CostPilot/.venv/bin/activate
ruff check /Users/adarshahebbar/Documents/CostPilot
pytest /Users/adarshahebbar/Documents/CostPilot/tests
```

The suite covers the golden variance and decision numbers, every variance formula, the
ten gates, hash-chain integrity and re-performance, all Phase 2 engines, the planner,
narration validation, RBAC and scope, the v1 HTTP flow, exports, connectors, uploads,
the local-model contract (with the HTTP call stubbed, so no network is needed), and the
command line.

## Layout

```
costpilot/engine/       money, trace, narration, validation, rulebook
costpilot/engine/variance/   material, labour, overhead, flexible budget, flux
costpilot/engine/decisions/  seven workflows
costpilot/engine/costing/    variable-to-absorption bridge
costpilot/engine/abc.py      ABC, TDABC, customer profitability
costpilot/engine/budgets.py  budget chain, scenarios, rolling, quality
costpilot/engine/performance.py, monitoring.py, group.py
costpilot/data/         loader, gates, capability, upload extraction
costpilot/audit.py      append-only SQLite chain and re-performance
costpilot/service.py    resolution, confirmation, activation, tool envelopes
costpilot/connectors/   read-only extracts and mapping registry
costpilot/platform/     auth, llm, planner, orchestrator, store, registry, exports
costpilot/api/          FastAPI surface and the single-file chat workspace
fixtures/               deterministic Northline Industries dataset and connector extracts
scripts/                fixture generator
tests/                  golden corpus and regression suite
docs/                   blueprint, phase plans, white paper, ADRs
```

## The dataset

The bundled sample is Northline Industries: three products (PRD-A, PRD-B, PRD-C), two
plants (Pune, Coimbatore), three periods (Jul and Aug 2026 closed, Sep 2026 open), two
divisions, six customers, INR. The canonical special-order case is PRD-B at Pune in
September: 2,000 units at ₹380, variable manufacturing cost ₹300, ₹40,000 incremental
fixed cost, 2,200 idle units, regular price ₹500.

Nothing in the product is hard-coded to those values. The sample is loaded at startup
only when no uploaded dataset is active. An upload replaces it, and the capability
card, demo defaults, and chat answers follow the new snapshot. Regenerate the sample
with:

```bash
python /Users/adarshahebbar/Documents/CostPilot/scripts/generate_fixture.py
```

The generator is deterministic. Editing any fixture file changes the snapshot ID,
because the ID is derived from the manifest hash of all files. An upload works the same
way: identical bytes produce the same snapshot ID, so loading the same workbook twice
does not create a duplicate.

## Limits

- Planning uses free models on local Ollama only; there is no hosted-model path and no
  API key. Plan quality depends on the installed model. The model receives the dataset
  catalog (tables, periods, plants, products, customers) and the question, not ledger
  rows, and it never returns a number that reaches a result.
- The planner selects one registered analysis. A question spanning two analyses is
  split by the user, not chained by the model. At most one clarification is asked.
- Uploads accept `.xlsx` (one sheet per canonical table) or a `.zip` of CSVs, up to
  25 MB per file. They must match the canonical schema; extra sheets are ignored and
  reported. Accepted snapshots are stored under `COSTPILOT_DATASETS` (default
  `/Users/adarshahebbar/Documents/CostPilot/data/datasets`), with an active pointer and
  an index, and survive restarts. Uploads need the `run` permission.
- One currency, one scarce resource per product-mix run, no FX translation.
- Account flux is level B: no price/usage split is fabricated, and the residual is
  labelled unexplained.
- Connectors read packaged extracts. There is no live ERP session, no credential
  broker, and no write-back path by design.
- The audit database and platform store are local SQLite. The chain is append-only by
  trigger, not distributed.

## Documents

- [Build blueprint](docs/CostPilot_Blueprint.pdf) — contracts, data model, rulebook, delivery gates
- [Phase 1 plan](docs/CostPilot_Phase_A_Plan.pdf) and [Phase 2 plan](docs/CostPilot_Phase_B_Plan.pdf)
- [White paper](docs/CostPilot_WhitePaper.pdf)
- [ADR 0001: trust boundary](docs/adr/0001-trust-boundary.md)
- [ADR 0002: canonical data model](docs/adr/0002-canonical-data-model.md)
- [ADR 0003: planner and confirmation sheet](docs/adr/0003-planner-and-confirmation.md)
- [ADR 0004: read-only connectors](docs/adr/0004-read-only-connectors.md)
- [ADR 0005: local models plan and narrate](docs/adr/0005-local-model-planning.md)
- [ADR 0006: validated dataset uploads](docs/adr/0006-validated-dataset-uploads.md)
