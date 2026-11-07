# CostPilot

CostPilot is a deterministic management-accounting copilot for manufacturing. A
controller asks a question in plain language, reviews the resolved parameters and
their sources, confirms, and receives an analysis card where every number carries a
rule ID, a formula, its inputs, and a source reference. The card is sealed into an
append-only SHA-256 audit chain and can be exported to XLSX, PDF, or JSON.

The design rule is simple: **the language model never performs arithmetic.** A planner
turns a question into one registered tool call with decimal-string arguments. The
engine does the arithmetic with `Decimal`, and narration may only reference fact slots
such as `{{f_profit}}`, never literal digits. If narration fails validation, a
deterministic fallback sentence is used and the attempt is recorded.

## What is implemented

| Area | Capability |
| --- | --- |
| Engine | Decimal money policy, typed calc steps, versioned rule registry, slot-only narration, result validation |
| Data | Canonical snapshot loader, manifest hash, ten ingestion gates, account registry, capability card, templates |
| Variance | Material, labour, overhead, flexible budget, account flux, variable-to-absorption bridge |
| Decisions | Seven workflows: special order, make-or-buy, add-or-drop, sell-or-process, shutdown, product mix, CVP |
| Profitability | Activity-based costing, time-driven ABC with unused capacity, customer profitability with cost to serve |
| Budget | Annual budget chain, driver scenarios with range guard, rolling forecast, forecast-quality scoring |
| Performance | Division performance scorecard, ROI / residual income, investment conflict detection |
| Monitoring | Metric aliases, alert rules, materiality thresholds, digest generation |
| Group | Entity consolidation and group reporting across divisions |
| Platform | Natural-language planner, confirmation sheet, orchestrator, RBAC and row-level scope, session privacy |
| Trust | Append-only audit chain, re-performance, trace, SSE run events, tamper verification |
| Integrations | Read-only NetSuite, SAP, and flat-file extracts; mapping proposals with human approval; no write-back |
| Delivery | FastAPI v1 API, single-file workspace with ten tabs, XLSX/PDF/JSON exports with formula-injection guards |

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
costpilot ask "Variance for PRD-B at PLT-01 in 2026-09"
costpilot run "Customer profitability for PLT-01 in 2026-09 for CUS-05"
costpilot runs --limit 10
costpilot verify
costpilot connectors
```

`check` runs the ten ingestion gates and exits non-zero on a blocking gate. `demo`
prints the canonical special-order trace. `ask` prepares a plan and parameter sheet
without running it. `run` plans, confirms, and prints the card summary. `verify`
checks the audit hash chain. Add `--json` to any planning or reporting command for the
full payload.

**5. Start the workspace.** Two variants, same result:

```bash
costpilot serve --port 8000
```

```bash
python -m uvicorn costpilot.api.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. API docs are at <http://127.0.0.1:8000/docs>.

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

Ten tabs: Workspace (chat, plan sheet, result card, waterfall, trace drawer), Runs and
audit, Profitability, Budget, Performance, Alerts, Group, Connectors, Registry, and
Trust. Every card exposes its export links; every run exposes a trace and a
re-performance button.

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
  -d '{"text":"Variance for PRD-B at PLT-01 in 2026-09"}'
```

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
endpoints: `GET /v1/runs`, `GET /v1/runs/{id}`, `GET /v1/runs/{id}/trace`,
`POST /v1/runs/{id}/reperform`, `GET /v1/runs/{id}/events`,
`GET /v1/runs/{id}/export?format=xlsx|pdf|json`, `GET /v1/audit/verify`,
`GET /v1/connectors`, `GET /v1/alerts/rules`, `GET /v1/registry/accounts`.

The legacy `/api` surface remains for the structured tools:
`GET /api/health`, `POST /api/tools/preview_differential_analysis`,
`POST /api/tools/run_differential_analysis`, `POST /api/tools/run_variance_analysis`,
`POST /api/tools/run_costing_bridge`, `GET /api/runs/{run_id}`, and
`POST /api/runs/{run_id}/reperform`.

## Supported questions

The planner routes natural-language questions to registered tools. Examples:

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
narration validation, RBAC and scope, the v1 HTTP flow, exports, connectors, and the
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
costpilot/data/         loader, gates, capability
costpilot/audit.py      append-only SQLite chain and re-performance
costpilot/service.py    resolution, confirmation, tool envelopes
costpilot/connectors.py read-only extracts and mapping registry
costpilot/platform/     auth, planner, orchestrator, store, registry, exports
costpilot/api/          FastAPI surface and the single-file workspace
fixtures/               deterministic Northline Industries dataset
scripts/                fixture generator
tests/                  golden corpus and regression suite
docs/                   blueprint, phase plans, white paper, ADRs
```

## The dataset

Northline Industries: three products (PRD-A, PRD-B, PRD-C), two plants (Pune,
Coimbatore), three periods (Jul and Aug 2026 closed, Sep 2026 open), two divisions, six
customers, INR. The canonical special-order case is PRD-B at Pune in September: 2,000
units at ₹380, variable manufacturing cost ₹300, ₹40,000 incremental fixed cost, 2,200
idle units, regular price ₹500. Regenerate the files with:

```bash
python /Users/adarshahebbar/Documents/CostPilot/scripts/generate_fixture.py
```

The generator is deterministic. Editing any fixture file changes the snapshot ID,
because the ID is derived from the manifest hash of all files.

## Limits

- The planner is rule-based, not a hosted language model. It is deliberately small,
  auditable, and offline; the contracts allow a model-backed planner to replace it
  without changing the engine boundary.
- One currency, one scarce resource per product-mix run, no FX translation.
- Account flux is level B: no price/usage split is fabricated, and the residual is
  labelled unexplained.
- Snapshots are immutable in memory but there is no durable snapshot store;
  re-ingesting a corrected file produces a new snapshot ID and leaves the files on
  disk untouched.
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
