"""Command line for the M0-M3 demo: serve the workspace or print a case."""

from __future__ import annotations

import argparse
import json
import os


def _service():
    from costpilot.service import ToolService

    return ToolService.from_env()


def _print(payload: dict) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="costpilot",
        description="Deterministic management-accounting engine (M0-M3 demo).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the API and analysis-card workspace.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--fixtures",
        default=None,
        help="Fixture directory (default: ./fixtures, or COSTPILOT_FIXTURES).",
    )
    serve.add_argument(
        "--audit-db",
        default=None,
        help="SQLite audit path (default: ./data/audit.db, or COSTPILOT_AUDIT_DB).",
    )

    demo = sub.add_parser("demo", help="Print the canonical special-order case.")
    demo.add_argument("--json", action="store_true", help="Emit the full result payload.")

    sub.add_parser("capability", help="Print the dataset capability card.")
    sub.add_parser("check", help="Run ingestion gates and exit non-zero on a blocking gate.")

    args = parser.parse_args(argv)

    if args.command == "serve":
        if args.fixtures:
            os.environ["COSTPILOT_FIXTURES"] = args.fixtures
        if args.audit_db:
            os.environ["COSTPILOT_AUDIT_DB"] = args.audit_db
        import uvicorn

        uvicorn.run("costpilot.api.main:app", host=args.host, port=args.port)
        return 0

    service = _service()

    if args.command == "capability":
        _print(service.capability())
        return 0

    if args.command == "check":
        gates = [gate.to_dict() for gate in service.snapshot.gates]
        _print({"snapshot_id": service.snapshot.snapshot_id, "gates": gates})
        return 1 if any(gate["status"] == "block" for gate in gates) else 0

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
