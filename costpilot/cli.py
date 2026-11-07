"""Command line for CostPilot: serve the workspace, plan, run, and verify."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any


def _service():
    from costpilot.service import ToolService

    return ToolService.from_env()


def _print(payload: dict) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _apply_env(args: argparse.Namespace) -> None:
    if getattr(args, "fixtures", None):
        os.environ["COSTPILOT_FIXTURES"] = args.fixtures
    if getattr(args, "audit_db", None):
        os.environ["COSTPILOT_AUDIT_DB"] = args.audit_db
    if getattr(args, "platform_db", None):
        os.environ["COSTPILOT_PLATFORM_DB"] = args.platform_db


def _orchestrator():
    from costpilot.platform.orchestrator import Orchestrator

    return Orchestrator(_service())


def _user(orchestrator, user_id: str):
    password = os.environ.get("COSTPILOT_DEMO_PASSWORD", "costpilot")
    return orchestrator.auth.login(user_id, password).user


def _edits(pairs: list[str]) -> list[dict[str, str]]:
    edits = []
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key.strip():
            raise SystemExit(f"Invalid --edit {pair!r}. Use key=value, for example product_id=PRD-A.")
        edits.append(
            {
                "key": key.strip(),
                "value": value.strip(),
                "reason": "Edited from the command line",
            }
        )
    return edits


def _plan_summary(payload: dict[str, Any]) -> None:
    plan = payload.get("plan") or {}
    print(f"Status: {payload.get('status')}")
    print(f"Plan:   {payload.get('plan_id')}  ({plan.get('intent')}, confidence {plan.get('confidence')})")
    print(f"Tool:   {plan.get('tool')} {plan.get('tool_version')}")
    if payload.get("clarification"):
        print(f"Question: {payload['clarification']}")
    sheet = payload.get("parameter_sheet") or []
    if sheet:
        print("Parameters:")
        for row in sheet:
            print(f"  {row.get('key'):<22} {row.get('display') or row.get('value'):>18}  [{row.get('source')}]")
    assumptions = payload.get("assumptions") or []
    if assumptions:
        print("Assumptions:")
        for row in assumptions:
            print(f"  {row.get('key'):<22} {row.get('display') or row.get('value')}  [{row.get('source')}]")
    missing = payload.get("missing") or []
    if missing:
        print("Missing: " + ", ".join(str(item) for item in missing))
    for action in payload.get("next_actions") or []:
        print(f"Next:   {action.get('action')} — {action.get('label') or action.get('hint') or ''}")


def _card_summary(result: dict[str, Any]) -> None:
    card = result["card"]
    header = card.get("header") or {}
    print(f"Run:      {result.get('run_id')}  ({card.get('intent_label') or card.get('intent')})")
    print(f"Snapshot: {header.get('snapshot_id')}  {header.get('dataset')}")
    print(f"Period:   {header.get('period_id')}  Plant: {header.get('plant_id')}")
    print(f"Verdict:  {card['verdict']['sentence']}")
    facts = card.get("facts") or []
    if facts:
        print("Facts:")
        for fact in facts:
            label = fact.get("label") or fact.get("name") or fact.get("fact_id")
            print(f"  {label}: {fact.get('display') or fact.get('value')}")
    for table in card.get("tables") or []:
        print(f"Table:    {table.get('title')} ({len(table.get('rows') or [])} rows)")
    print(f"Trace:    {card['audit']['trace']}")
    print(f"Export:   {card['exports']['xlsx']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="costpilot",
        description="Deterministic management-accounting copilot with a sealed audit chain.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_storage(target: argparse.ArgumentParser) -> None:
        target.add_argument("--fixtures", default=None, help="Fixture directory (default: ./fixtures).")
        target.add_argument("--audit-db", default=None, help="SQLite audit path (default: ./data/audit.db).")
        target.add_argument(
            "--platform-db",
            default=None,
            help="SQLite platform path (default: ./data/platform.db).",
        )

    serve = sub.add_parser("serve", help="Run the API and the workspace UI.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    add_storage(serve)

    for name, help_text in (
        ("ask", "Plan a question without running it."),
        ("run", "Plan, confirm, and run a question."),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("question", help="Natural-language question, quoted.")
        command.add_argument("--user", default="controller", help="Demo user id (default: controller).")
        command.add_argument("--json", action="store_true", help="Emit the full payload as JSON.")
        if name == "run":
            command.add_argument(
                "--edit",
                action="append",
                default=[],
                metavar="KEY=VALUE",
                help="Override a plan argument before confirmation. Repeatable.",
            )
        add_storage(command)

    runs = sub.add_parser("runs", help="List recent sealed runs.")
    runs.add_argument("--limit", type=int, default=10)
    runs.add_argument("--user", default="controller")
    runs.add_argument("--json", action="store_true")
    add_storage(runs)

    verify = sub.add_parser("verify", help="Verify the audit hash chain.")
    verify.add_argument("--json", action="store_true")
    add_storage(verify)

    connectors = sub.add_parser("connectors", help="Show the read-only connector catalog.")
    connectors.add_argument("--json", action="store_true")
    add_storage(connectors)

    demo = sub.add_parser("demo", help="Print the canonical special-order case.")
    demo.add_argument("--json", action="store_true", help="Emit the full result payload.")
    add_storage(demo)

    sub.add_parser("capability", help="Print the dataset capability card.")
    sub.add_parser("check", help="Run ingestion gates and exit non-zero on a blocking gate.")

    args = parser.parse_args(argv)
    _apply_env(args)

    if args.command == "serve":
        import uvicorn

        uvicorn.run("costpilot.api.main:app", host=args.host, port=args.port)
        return 0

    if args.command in {"ask", "run"}:
        orchestrator = _orchestrator()
        user = _user(orchestrator, args.user)
        session_id = orchestrator.create_session(user)["session"]["session_id"]
        payload = orchestrator.post_message(user, session_id, args.question)
        if args.command == "ask":
            if args.json:
                _print(payload)
            else:
                _plan_summary(payload)
            return 0 if payload.get("status") != "needs_clarification" else 2
        if payload.get("status") == "needs_clarification":
            if args.json:
                _print(payload)
            else:
                _plan_summary(payload)
                print("Nothing was run. Answer the clarification or rephrase the question.")
            return 2
        try:
            result = orchestrator.confirm_plan(
                user, payload["plan_id"], edits=_edits(args.edit)
            )
        except Exception as exc:  # DomainError carries the remediation
            remediation = getattr(exc, "remediation", None)
            print(f"{getattr(exc, 'code', 'E_CLI')}: {getattr(exc, 'message', str(exc))}")
            if remediation:
                print(f"Remediation: {remediation}")
            print("The plan was not confirmed; nothing was written to the audit chain.")
            return 3
        if args.json:
            _print(result)
        else:
            _card_summary(result)
        return 0

    service = _service()

    if args.command == "capability":
        _print(service.capability())
        return 0

    if args.command == "check":
        gates = [gate.to_dict() for gate in service.snapshot.gates]
        _print({"snapshot_id": service.snapshot.snapshot_id, "gates": gates})
        return 1 if any(gate["status"] == "block" for gate in gates) else 0

    if args.command == "runs":
        orchestrator = _orchestrator()
        user = _user(orchestrator, args.user)
        rows = orchestrator.list_runs(user, limit=args.limit)
        if args.json:
            _print({"runs": rows})
        else:
            for row in rows:
                print(
                    f"{row.get('run_id')}  {row.get('created_at')}  "
                    f"{row.get('intent'):<22} {row.get('question')}"
                )
        return 0

    if args.command == "verify":
        chain = service.verify_chain()
        if args.json:
            _print(chain)
        else:
            state = "ok" if chain.get("ok") else "BROKEN"
            print(f"Audit chain: {state}, {chain.get('checked', 0)} events checked.")
            if not chain.get("ok"):
                print(f"First break: {chain.get('first_break')} ({chain.get('reason')})")
        return 0 if chain.get("ok") else 1

    if args.command == "connectors":
        catalog = service.connector_catalog()
        if args.json:
            _print(catalog)
        else:
            for row in catalog["connectors"]:
                print(
                    f"{row['name']:<10} {row['system']:<12} {row['mode']:<18} "
                    f"contract v{row['contract_version']}  write_back={catalog['write_back']}"
                )
        return 0

    envelope = service.run_differential(
        decision_type="special_order",
        arguments={},
        confirmation={"confirmed": True},
        record=False,
    )
    if args.json:
        _print(envelope)
        return 0
    result = envelope["result"]
    print(f"Verdict: {result['verdict']}")
    print(f"Incremental profit: {result['display']}")
    print(result["rationale"])
    print("Trace:")
    for step in result["steps"]:
        print(f"  {step['step_id']:>2}  {step['rule_id']:<10} {step['display']:>14}  {step['label']}")
    print(f"Snapshot: {service.snapshot.snapshot_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
