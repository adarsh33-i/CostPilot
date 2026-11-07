"""FastAPI surface: authenticated v1 product API plus the legacy /api demo routes."""

from __future__ import annotations

import csv
import io
import json
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from costpilot import ENGINE_VERSION, RULEBOOK_VERSION
from costpilot.data import uploads as dataset_uploads
from costpilot.data.loader import REQUIRED_FILES, load_snapshot
from costpilot.engine.errors import DomainError
from costpilot.platform.auth import AuthService, User
from costpilot.platform.exports import ExportError, export_working_paper
from costpilot.platform.llm import model_catalog
from costpilot.platform.orchestrator import Orchestrator
from costpilot.service import ToolService

STATIC_DIR = Path(__file__).parent / "static"

AUTH_CODES = {"E_AUTH_REQUIRED", "E_AUTH_INVALID", "E_AUTH_EXPIRED"}
NOT_FOUND = {
    "RUN_NOT_FOUND",
    "E_PLAN_NOT_FOUND",
    "E_SESSION_NOT_FOUND",
    "E_ATTACHMENT_NOT_FOUND",
    "REGISTRY_PROPOSAL_UNKNOWN",
    "MAPPING_NOT_FOUND",
    "REGISTRY_ACCOUNT_UNKNOWN",
}
CONFLICT = {
    "CONFIRMATION_REQUIRED",
    "E_ASSUMPTION_UNCONFIRMED",
    "E_DATA_MISSING",
    "E_DATA_QUALITY",
    "E_PLAN_COMPLETED",
    "E_PLAN_CANCELLED",
    "E_IDEMPOTENCY_CONFLICT",
    "REGISTRY_PROPOSAL_STATE",
}
UNPROCESSABLE = {
    "E_PLAN_SCHEMA",
    "E_PLAN_EDIT",
    "E_PLAN_UNSUPPORTED",
    "E_ENGINE_DOMAIN",
    "REPERFORM_UNSUPPORTED",
    "REGISTRY_VALUE",
    "REGISTRY_FIELD",
    "REGISTRY_EMPTY",
    "REGISTRY_REASON_REQUIRED",
    "REGISTRY_REVENUE_BEHAVIOR",
    "MAPPING_TARGET_UNKNOWN",
    "MAPPING_SOURCE_EMPTY",
    "CONNECTOR_UNKNOWN",
    "MON_METRIC_UNKNOWN",
    "MON_THRESHOLD_MISSING",
    "E_MODEL_UNAVAILABLE",
    "E_DATASET_FORMAT",
    "SCHEMA_MISMATCH",
    "E_ATTACHMENT_FORMAT",
    "E_ATTACHMENT_EMPTY",
    "E_ATTACHMENT_TOO_LARGE",
    "E_ATTACHMENT_TABLE",
    "E_ATTACHMENT_COLUMN",
    "E_ATTACHMENT_NO_NUMBERS",
    "E_ATTACHMENT_OPERATION",
    "E_ATTACHMENT_QUERY",
    "E_ATTACHMENT_NO_MATCH",
    "E_ATTACHMENT_OCR",
    "DOC_VALIDATION",
}
ACTIONS = {
    "E_AUTH_REQUIRED": ["login"],
    "E_AUTH_INVALID": ["retry_login"],
    "E_AUTH_EXPIRED": ["login"],
    "E_AUTH_SCOPE": ["request_access", "change_scope"],
    "E_ATTACHMENT_NOT_FOUND": ["attach_file"],
    "E_ATTACHMENT_FORMAT": ["attach_file", "download_template"],
    "E_ATTACHMENT_EMPTY": ["attach_file"],
    "E_ATTACHMENT_TOO_LARGE": ["attach_file", "split_file"],
    "E_ATTACHMENT_TABLE": ["choose_table"],
    "E_ATTACHMENT_COLUMN": ["choose_column"],
    "E_ATTACHMENT_NO_NUMBERS": ["choose_column"],
    "E_ATTACHMENT_QUERY": ["ask_again"],
    "E_ATTACHMENT_NO_MATCH": ["ask_again", "choose_column"],
    "E_ATTACHMENT_OCR": ["attach_image", "install_ocr"],
    "E_PLAN_AMBIGUOUS": ["choose_interpretation", "select_dimension"],
    "E_PLAN_UNSUPPORTED": ["show_supported", "nearest_analysis"],
    "E_PLAN_SCHEMA": ["retry_plan", "open_form"],
    "E_ASSUMPTION_UNCONFIRMED": ["open_parameter_sheet"],
    "E_DATA_MISSING": ["upload_template", "exclude_scope"],
    "E_DATA_QUALITY": ["fix_account_tag", "view_gate", "reload_file"],
    "E_ENGINE_DOMAIN": ["review_inputs", "run_alternative_view"],
    "E_EXPORT_FAILED": ["retry", "alternate_format"],
    "E_EXPORT_FORMAT": ["choose_format"],
    "CUSTOMER_NOT_IN_SCOPE": ["choose_customer"],
    "BUD_SCENARIO_RANGE": ["open_parameter_sheet"],
    "E_RATE_LIMIT": ["retry_after", "queue"],
    "E_PLAN_EDIT": ["open_parameter_sheet", "retry_plan"],
    "REGISTRY_PROPOSAL_STATE": ["refresh_registry"],
    "MON_METRIC_UNKNOWN": ["open_alert_builder"],
    "MON_THRESHOLD_MISSING": ["open_alert_builder"],
    "E_DATASET_FORMAT": ["download_template", "choose_file"],
    "SCHEMA_MISMATCH": ["download_template", "fix_columns", "reupload"],
}
DEFAULT_ACTIONS = ["retry"]


class LoginRequest(BaseModel):
    user_id: str
    password: str


class MessageRequest(BaseModel):
    text: str = ""
    model: str | None = None
    attachment_ids: list[str] = Field(default_factory=list)


class ConfirmRequest(BaseModel):
    edits: list[dict[str, Any]] = Field(default_factory=list)
    idempotency_key: str | None = None
    plan_version: int | None = None


class CancelRequest(BaseModel):
    reason: str | None = None


class MappingRequest(BaseModel):
    source_system: str
    source_account: str
    source_name: str = ""
    target_account: str


class RegistryRequest(BaseModel):
    changes: dict[str, Any] = Field(default_factory=dict)
    reason: str


class AlertRuleRequest(BaseModel):
    text: str
    period_id: str | None = None
    materiality: str = "50000"


class ConnectorRunRequest(BaseModel):
    name: str


class DifferentialRequest(BaseModel):
    decision_type: str = "special_order"
    arguments: dict[str, Any] = Field(default_factory=dict)
    confirmation: dict[str, Any] | None = None
    question: str | None = None


class VarianceRequest(BaseModel):
    product_id: str | None = None
    plant_id: str | None = None
    period_id: str | None = None
    question: str | None = None


class BridgeRequest(BaseModel):
    product_id: str | None = None
    plant_id: str | None = None
    period_id: str | None = None
    variable_operating_income: str | None = None
    question: str | None = None


def create_app(service: ToolService | None = None, auth: AuthService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.service = service or ToolService.from_env()
        app.state.auth = auth or AuthService()
        app.state.orchestrator = Orchestrator(app.state.service, app.state.auth)
        yield

    app = FastAPI(
        title="CostPilot",
        version="1.0.0",
        description=(
            "Deterministic management-accounting platform. The engine owns every number; "
            "the planner proposes a typed plan; the orchestrator requires confirmation; "
            "every run is sealed in an append-only audit chain."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def get_service(request: Request) -> ToolService:
        return request.app.state.service

    def get_orchestrator(request: Request) -> Orchestrator:
        return request.app.state.orchestrator

    def token_from(request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        token = request.headers.get("x-costpilot-token")
        if token:
            return token
        return request.query_params.get("token")

    def current_user(request: Request) -> User:
        return request.app.state.auth.resolve(token_from(request))

    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        if exc.code in AUTH_CODES:
            status = 401
        elif exc.code == "E_AUTH_SCOPE":
            status = 403
        elif exc.code in NOT_FOUND:
            status = 404
        elif exc.code in CONFLICT:
            status = 409
        elif exc.code in {"E_RATE_LIMIT"}:
            status = 429
        elif exc.code in UNPROCESSABLE | {"E_EXPORT_FORMAT"}:
            status = 422
        elif exc.code in {"E_PLAN_AMBIGUOUS", "CUSTOMER_NOT_IN_SCOPE", "BUD_SCENARIO_RANGE"}:
            status = 409
        elif exc.code in {"E_EXPORT_FAILED"}:
            status = 500
        elif exc.code in {"E_PLAN_UNSUPPORTED"}:
            status = 422
        else:
            status = 422
        remediation = [exc.remediation] if exc.remediation else []
        return JSONResponse(
            status_code=status,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "checked": exc.code,
                },
                "remediation": remediation,
                "actions": [{"action": action, "label": action.replace("_", " ").title()} for action in ACTIONS.get(exc.code, DEFAULT_ACTIONS)],
            },
        )

    # ---------- static shell ----------

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    # ---------- auth ----------

    @app.post("/v1/auth/login")
    def login(payload: LoginRequest, request: Request) -> dict[str, Any]:
        session = request.app.state.auth.login(payload.user_id, payload.password)
        return {
            "token": session.token,
            "user": session.user.public(),
            "created_at": session.created_at.isoformat(),
            "expires_at": session.expires_at.isoformat(),
        }

    @app.post("/v1/auth/logout")
    def logout(request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
        request.app.state.auth.logout(token_from(request) or "")
        return {"status": "logged_out", "user_id": user.user_id}

    @app.get("/v1/auth/me")
    def me(user: User = Depends(current_user)) -> dict[str, Any]:
        return {"user": user.public()}

    # ---------- capabilities and sessions ----------

    @app.get("/v1/capabilities")
    def capabilities(
        user: User = Depends(current_user), svc: ToolService = Depends(get_service)
    ) -> dict[str, Any]:
        payload = svc.capability()
        payload["limits"] = {"max_tool_calls": 3, "max_clarifications": 1}
        payload["roles"] = _roles()
        return payload

    @app.get("/v1/models")
    def models(
        user: User = Depends(current_user), orch: Orchestrator = Depends(get_orchestrator)
    ) -> dict[str, Any]:
        catalog = model_catalog(orch.llm)
        catalog["planner"] = "AI selects the registered analysis; the rule engine calculates every number."
        return catalog

    @app.get("/v1/registry/rules")
    def registry_rules(
        user: User = Depends(current_user), svc: ToolService = Depends(get_service)
    ) -> dict[str, Any]:
        return svc.rules()

    @app.post("/v1/sessions")
    def create_session(
        payload: dict[str, Any] | None = None,
        user: User = Depends(current_user),
        orch: Orchestrator = Depends(get_orchestrator),
    ) -> dict[str, Any]:
        return orch.create_session(user, (payload or {}).get("dataset_id"))

    @app.get("/v1/sessions")
    def list_sessions(
        limit: int = 20,
        user: User = Depends(current_user),
        orch: Orchestrator = Depends(get_orchestrator),
    ) -> dict[str, Any]:
        return {"sessions": orch.list_sessions(user, max(1, min(limit, 100)))}

    @app.get("/v1/sessions/{session_id}")
    def get_session(
        session_id: str,
        user: User = Depends(current_user),
        orch: Orchestrator = Depends(get_orchestrator),
    ) -> dict[str, Any]:
        return orch.session_detail(user, session_id)

    @app.get("/v1/sessions/{session_id}/messages")
    def list_messages(
        session_id: str,
        user: User = Depends(current_user),
        orch: Orchestrator = Depends(get_orchestrator),
    ) -> dict[str, Any]:
        return orch.session_detail(user, session_id)

    @app.post("/v1/sessions/{session_id}/messages")
    def post_message(
        session_id: str,
        payload: MessageRequest,
        user: User = Depends(current_user),
        orch: Orchestrator = Depends(get_orchestrator),
    ) -> dict[str, Any]:
        return orch.post_message(
            user,
            session_id,
            payload.text,
            model=payload.model,
            attachment_ids=payload.attachment_ids,
        )

    @app.post("/v1/attachments")
    async def upload_attachment(
        file: UploadFile = File(...),
        session_id: str | None = Form(None),
        user: User = Depends(current_user),
        orch: Orchestrator = Depends(get_orchestrator),
    ) -> dict[str, Any]:
        raw = await file.read()
        return orch.upload_attachment(
            user, file.filename or "attachment", raw, session_id=session_id
        )

    @app.get("/v1/attachments/{attachment_id}")
    def get_attachment(
        attachment_id: str,
        user: User = Depends(current_user),
        orch: Orchestrator = Depends(get_orchestrator),
    ) -> dict[str, Any]:
        return orch.attachment(user, attachment_id)

    @app.post("/v1/sessions/{session_id}/close")
    def close_session(
        session_id: str,
        user: User = Depends(current_user),
        orch: Orchestrator = Depends(get_orchestrator),
    ) -> dict[str, Any]:
        return orch.close_session(user, session_id)

    # ---------- plans and runs ----------

    @app.get("/v1/plans/{plan_id}")
    def get_plan(
        plan_id: str,
        user: User = Depends(current_user),
        orch: Orchestrator = Depends(get_orchestrator),
    ) -> dict[str, Any]:
        plan = orch.store.get_plan(plan_id)
        if plan is None:
            raise DomainError("E_PLAN_NOT_FOUND", f"Plan {plan_id} was not found.", "Ask the question again to create a plan.")
        if plan.get("user_id") and plan["user_id"] != user.user_id and not user.can("admin"):
            raise DomainError("E_AUTH_SCOPE", "This plan belongs to another user.", "Create your own plan.")
        return {"plan": plan, "parameter_sheet": orch.parameter_sheet(plan)}

    @app.post("/v1/plans/{plan_id}/confirm")
    def confirm_plan(
        plan_id: str,
        payload: ConfirmRequest,
        request: Request,
        user: User = Depends(current_user),
        orch: Orchestrator = Depends(get_orchestrator),
    ) -> dict[str, Any]:
        idempotency = request.headers.get("idempotency-key") or payload.idempotency_key
        return orch.confirm_plan(
            user,
            plan_id,
            edits=payload.edits,
            idempotency_key=idempotency,
            plan_version=payload.plan_version,
        )

    @app.post("/v1/plans/{plan_id}/cancel")
    def cancel_plan(
        plan_id: str,
        payload: CancelRequest,
        user: User = Depends(current_user),
        orch: Orchestrator = Depends(get_orchestrator),
    ) -> dict[str, Any]:
        return orch.cancel_plan(user, plan_id, payload.reason)

    @app.get("/v1/runs")
    def list_runs(
        limit: int = 30,
        user: User = Depends(current_user),
        orch: Orchestrator = Depends(get_orchestrator),
    ) -> dict[str, Any]:
        return {"runs": orch.list_runs(user, max(1, min(limit, 200)))}

    @app.get("/v1/runs/{run_id}")
    def get_run(
        run_id: str, user: User = Depends(current_user), orch: Orchestrator = Depends(get_orchestrator)
    ) -> dict[str, Any]:
        return orch.get_run(user, run_id)

    @app.get("/v1/runs/{run_id}/trace")
    def get_trace(
        run_id: str, user: User = Depends(current_user), orch: Orchestrator = Depends(get_orchestrator)
    ) -> dict[str, Any]:
        return orch.trace(user, run_id)

    @app.post("/v1/runs/{run_id}/reperform")
    def reperform(
        run_id: str, user: User = Depends(current_user), orch: Orchestrator = Depends(get_orchestrator)
    ) -> dict[str, Any]:
        return orch.reperform(user, run_id)

    @app.get("/v1/runs/{run_id}/events")
    def run_events(
        run_id: str,
        request: Request,
        user: User = Depends(current_user),
        orch: Orchestrator = Depends(get_orchestrator),
    ) -> StreamingResponse:
        last = int(request.headers.get("last-event-id") or request.query_params.get("last_event_id") or 0)
        events = orch.run_events(user, run_id, last)

        def stream():
            for event in events:
                sequence = int(event.get("sequence", 0))
                yield f"id: {sequence}\n"
                yield f"event: {event.get('event', 'message')}\n"
                yield "data: " + json.dumps(event.get("data", {}), default=str) + "\n\n"
            yield "event: done\ndata: {\"ok\": true}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/v1/runs/{run_id}/export")
    def export_run(
        run_id: str,
        format: str = "xlsx",
        user: User = Depends(current_user),
        orch: Orchestrator = Depends(get_orchestrator),
        svc: ToolService = Depends(get_service),
    ) -> Response:
        if format not in {"xlsx", "pdf", "json"}:
            raise DomainError(
                "E_EXPORT_FORMAT",
                f"Export format {format!r} is not supported.",
                "Use xlsx, pdf, or json.",
            )
        payload = orch.export_payload(user, run_id)
        payload["quality"] = svc.capability().get("data_quality", {})
        try:
            body, filename, media_type = export_working_paper(payload, format)
        except ExportError as exc:
            raise DomainError("E_EXPORT_FAILED", str(exc), "Retry, or choose JSON as the alternate format.") from exc
        return Response(
            content=body,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-CostPilot-Run": run_id,
            },
        )

    # ---------- datasets and quality ----------

    @app.get("/v1/datasets")
    def datasets(user: User = Depends(current_user), svc: ToolService = Depends(get_service)) -> dict[str, Any]:
        return {
            "datasets": svc.datasets(),
            "current": svc.dataset_info(),
            "accepted": [".xlsx", ".zip"],
            "template": "/v1/datasets/workbook-template",
        }

    @app.get("/v1/datasets/{dataset_id}/quality")
    def dataset_quality(
        dataset_id: str,
        user: User = Depends(current_user),
        svc: ToolService = Depends(get_service),
    ) -> dict[str, Any]:
        snapshot = svc.snapshot
        if dataset_id not in {snapshot.snapshot_id, "current"}:
            raise DomainError(
                "E_DATA_MISSING",
                f"Dataset {dataset_id} is not loaded in this session.",
                "Use GET /v1/datasets to list the current snapshot.",
            )
        capability = svc.capability()
        return {
            "dataset_id": snapshot.snapshot_id,
            "name": snapshot.dataset_name,
            "row_counts": snapshot.row_counts,
            "quality": capability.get("data_quality", {}),
            "gates": [
                gate.to_dict() if hasattr(gate, "to_dict") else gate for gate in snapshot.gates
            ],
            "remediation": _gate_remediation(snapshot.gates),
        }

    @app.get("/v1/datasets/templates")
    def list_templates(user: User = Depends(current_user)) -> dict[str, Any]:
        return {
            "templates": [
                {
                    "file": name,
                    "columns": columns,
                    "download": f"/v1/datasets/templates/{name}",
                }
                for name, columns in REQUIRED_FILES.items()
            ]
        }

    @app.get("/v1/datasets/templates/{name}")
    def download_template(name: str, user: User = Depends(current_user)) -> Response:
        columns = REQUIRED_FILES.get(name)
        if columns is None:
            raise DomainError(
                "E_DATA_MISSING",
                f"No template named {name!r}.",
                "List templates at GET /v1/datasets/templates.",
            )
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        return Response(
            content=buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.post("/v1/datasets/validate")
    async def validate_dataset(
        file: UploadFile = File(...),
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        name = Path(file.filename or "").name
        expected = REQUIRED_FILES.get(name)
        if expected is None:
            raise DomainError(
                "SCHEMA_MISMATCH",
                f"{name!r} is not a canonical CostPilot template.",
                "Download a template from GET /v1/datasets/templates and retry.",
            )
        raw = await file.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DomainError(
                "SCHEMA_MISMATCH",
                "The file is not UTF-8 text.",
                "Save the template as UTF-8 CSV and retry.",
            ) from exc
        reader = csv.DictReader(io.StringIO(text))
        header = reader.fieldnames or []
        missing = [column for column in expected if column not in header]
        rows = sum(1 for _ in reader)
        return {
            "file": name,
            "rows": rows,
            "columns_found": len(header),
            "columns_expected": len(expected),
            "missing": missing,
            "ok": not missing,
            "applies_to_runs": False,
            "remediation": [] if not missing else [
                f"Add the missing columns to {name}: {', '.join(missing)}.",
                "Re-run the validation, then load the complete snapshot through the ingestion worker.",
            ],
        }

    @app.get("/v1/datasets/workbook-template")
    def workbook_template(user: User = Depends(current_user)) -> Response:
        return Response(
            content=dataset_uploads.workbook_bytes(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="costpilot-dataset-template.xlsx"'},
        )

    @app.post("/v1/datasets/upload")
    async def upload_dataset(
        file: UploadFile = File(...),
        name: str | None = Form(None),
        user: User = Depends(current_user),
        svc: ToolService = Depends(get_service),
    ) -> dict[str, Any]:
        if not user.can("run"):
            raise DomainError(
                "E_AUTH_SCOPE",
                "This role cannot load a dataset.",
                "Ask an analyst or controller to upload the workbook, or sign in as analyst.",
            )
        raw = await file.read()
        workdir = Path(tempfile.mkdtemp(prefix="costpilot-upload-"))
        try:
            try:
                summary = dataset_uploads.extract(raw, file.filename or "", workdir)
            except ValueError as exc:
                raise DomainError(
                    "E_DATASET_FORMAT",
                    str(exc),
                    "Download the workbook template at GET /v1/datasets/workbook-template, "
                    "fill one sheet per table, and upload the .xlsx or a .zip of CSVs.",
                ) from exc
            blocking = (
                summary["missing_required"]
                or summary["missing_columns"]
                or summary["empty_required"]
            )
            if blocking:
                details = []
                if summary["missing_required"]:
                    details.append(
                        "Missing tables: " + ", ".join(summary["missing_required"]) + "."
                    )
                for table, columns in (summary["missing_columns"] or {}).items():
                    details.append(f"{table} is missing columns: {', '.join(columns)}.")
                if summary["empty_required"]:
                    details.append(
                        "No header row: " + ", ".join(summary["empty_required"]) + "."
                    )
                raise DomainError(
                    "SCHEMA_MISMATCH",
                    "The workbook is missing required data. " + " ".join(details),
                    " ".join(dataset_uploads.remediation(summary)),
                )
            dataset_name = (name or Path(file.filename or "Uploaded dataset").stem).strip()
            dataset_name = dataset_name[:120] or "Uploaded dataset"
            try:
                snapshot = load_snapshot(workdir, dataset_name=dataset_name)
            except DomainError as exc:
                raise DomainError(
                    exc.code,
                    exc.message,
                    " ".join(dataset_uploads.remediation(summary)),
                ) from exc
            blocked = [gate for gate in snapshot.gates if gate.status == "block"]
            if blocked:
                details = " ".join(
                    f"{gate.id} {gate.name}: {gate.detail}." for gate in blocked
                )
                fixes = " ".join(
                    gate.remediation for gate in blocked if gate.remediation
                )
                raise DomainError(
                    "E_DATA_QUALITY",
                    "The workbook loaded but failed a blocking ingestion gate, so it was "
                    "not activated. " + details,
                    fixes or "Correct the rows named above and upload the workbook again.",
                )
            info = svc.activate_snapshot(
                snapshot,
                path=workdir,
                uploaded_by=user.user_id,
                summary={
                    "files": summary["files"],
                    "rows": summary["row_counts"],
                    "ignored_sheets": summary["ignored_sheets"],
                },
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        capability = svc.capability()
        gates = [
            gate.to_dict() if hasattr(gate, "to_dict") else gate for gate in svc.snapshot.gates
        ]
        return {
            "activated": True,
            "dataset": svc.dataset_info(),
            "previous": info.get("previous"),
            "activated_at": info.get("uploaded_at"),
            "gates": gates,
            "remediation": _gate_remediation(svc.snapshot.gates),
            "capability": {
                "analysis_ready": capability.get("analysis_ready", []),
                "analysis_unavailable": capability.get("analysis_unavailable", []),
                "demo_defaults": capability.get("demo_defaults", {}),
            },
            "next": "Ask a question in the chat to analyse the new snapshot.",
        }

    # ---------- connectors and mappings ----------

    @app.get("/v1/connectors")
    def connectors(user: User = Depends(current_user), svc: ToolService = Depends(get_service)) -> dict[str, Any]:
        return svc.connector_catalog()

    @app.post("/v1/connectors/{name}/preview")
    def connector_preview(
        name: str,
        user: User = Depends(current_user),
        orch: Orchestrator = Depends(get_orchestrator),
    ) -> dict[str, Any]:
        orch.auth.require(user, "run")
        payload = orch.service.run_connector_tool(name=name, question=f"Preview {name} connector", user=user)
        return payload

    @app.get("/v1/mappings")
    def mappings(
        system: str | None = None,
        user: User = Depends(current_user),
        svc: ToolService = Depends(get_service),
    ) -> dict[str, Any]:
        return {"mappings": svc.mappings.all(system), "write_back": False}

    @app.post("/v1/mappings")
    def propose_mapping(
        payload: MappingRequest,
        user: User = Depends(current_user),
        svc: ToolService = Depends(get_service),
    ) -> dict[str, Any]:
        if not user.can("run"):
            raise DomainError(
                "E_AUTH_SCOPE",
                "This role cannot propose an account mapping.",
                "Ask an analyst or controller to propose the mapping.",
            )
        return svc.propose_mapping(
            source_system=payload.source_system,
            source_account=payload.source_account,
            source_name=payload.source_name,
            target_account=payload.target_account,
            user=user,
        )

    @app.post("/v1/mappings/{proposal_id}/approve")
    def approve_mapping(
        proposal_id: str,
        user: User = Depends(current_user),
        svc: ToolService = Depends(get_service),
    ) -> dict[str, Any]:
        if not user.can("approve"):
            raise DomainError(
                "E_AUTH_SCOPE",
                "Only a controller or administrator can approve a mapping.",
                "Ask a controller to approve this mapping.",
            )
        return svc.approve_mapping(proposal_id, user)

    # ---------- alerts ----------

    @app.get("/v1/alerts/rules")
    def alert_rules(user: User = Depends(current_user), svc: ToolService = Depends(get_service)) -> dict[str, Any]:
        return {"rules": svc.list_alert_rules()}

    @app.post("/v1/alerts/rules")
    def create_alert_rule(
        payload: AlertRuleRequest,
        user: User = Depends(current_user),
        svc: ToolService = Depends(get_service),
    ) -> dict[str, Any]:
        if not user.can("run"):
            raise DomainError(
                "E_AUTH_SCOPE",
                "This role cannot create alert rules.",
                "Use an analyst, controller, or administrator account.",
            )
        return svc.save_alert_rule(
            text=payload.text,
            created_by=user.user_id,
            period_id=payload.period_id,
            materiality=payload.materiality,
        )

    @app.delete("/v1/alerts/rules/{rule_id}")
    def delete_alert_rule(
        rule_id: str, user: User = Depends(current_user), svc: ToolService = Depends(get_service)
    ) -> dict[str, Any]:
        if not user.can("configure"):
            raise DomainError(
                "E_AUTH_SCOPE",
                "Only an administrator can delete an alert rule.",
                "Ask an administrator to remove this rule.",
            )
        return {"deleted": svc.delete_alert_rule(rule_id), "rule_id": rule_id}

    @app.get("/v1/alerts/digests")
    def alert_digests(user: User = Depends(current_user), svc: ToolService = Depends(get_service)) -> dict[str, Any]:
        return {"digests": svc.alert_digests()}

    # ---------- audit and registry ----------

    @app.get("/v1/audit/verify")
    def audit_verify(user: User = Depends(current_user), svc: ToolService = Depends(get_service)) -> dict[str, Any]:
        return svc.verify_chain()

    @app.get("/v1/registry/accounts")
    def registry_accounts(
        period_id: str | None = None,
        plant_id: str | None = None,
        behavior: str | None = None,
        statement: str | None = None,
        q: str | None = None,
        limit: int = 200,
        user: User = Depends(current_user),
        svc: ToolService = Depends(get_service),
    ) -> dict[str, Any]:
        rows = svc.registry.accounts(
            period_id=period_id,
            plant_id=plant_id,
            behavior=behavior,
            statement=statement,
            query=q,
            limit=max(1, min(limit, 500)),
        )
        return {"accounts": rows, "registry_version": svc.registry.version()}

    @app.patch("/v1/registry/accounts/{account_id}")
    def propose_registry_change(
        account_id: str,
        payload: RegistryRequest,
        user: User = Depends(current_user),
        svc: ToolService = Depends(get_service),
    ) -> dict[str, Any]:
        if not user.can("run"):
            raise DomainError(
                "E_AUTH_SCOPE",
                "This role cannot propose registry changes.",
                "Use an analyst, controller, or administrator account.",
            )
        return svc.registry.propose(user, account_id, payload.changes, payload.reason)

    @app.get("/v1/registry/proposals")
    def registry_proposals(
        status: str | None = None,
        user: User = Depends(current_user),
        svc: ToolService = Depends(get_service),
    ) -> dict[str, Any]:
        return {"proposals": svc.registry.proposals(status), "registry_version": svc.registry.version()}

    @app.post("/v1/registry/proposals/{proposal_id}/approve")
    def approve_registry_change(
        proposal_id: str,
        user: User = Depends(current_user),
        svc: ToolService = Depends(get_service),
    ) -> dict[str, Any]:
        if not user.can("configure"):
            raise DomainError(
                "E_AUTH_SCOPE",
                "Only an administrator can approve a registry version.",
                "Ask an administrator to approve this change.",
            )
        return svc.registry.approve(user, proposal_id)

    # ---------- legacy structured tool routes ----------

    @app.get("/api/health")
    def health(svc: ToolService = Depends(get_service)) -> dict[str, Any]:
        chain = svc.verify_chain()
        return {
            "ok": True,
            "snapshot_id": svc.snapshot.snapshot_id,
            "dataset": svc.snapshot.dataset_name,
            "engine_version": ENGINE_VERSION,
            "rulebook_version": RULEBOOK_VERSION,
            "audit_chain": chain,
            "pins": svc.pins(),
        }

    @app.get("/api/capability")
    def capability(svc: ToolService = Depends(get_service)) -> dict[str, Any]:
        return svc.capability()

    @app.get("/api/rules")
    def rules(svc: ToolService = Depends(get_service)) -> dict[str, Any]:
        return svc.rules()

    @app.get("/api/tools")
    def tools(svc: ToolService = Depends(get_service)) -> dict[str, Any]:
        return {
            "tools": [
                {"name": "describe_dataset", "method": "GET", "path": "/v1/capabilities", "milestone": "M1"},
                {"name": "run_differential_analysis", "method": "POST", "path": "/api/tools/run_differential_analysis", "milestone": "M3"},
                {"name": "run_variance_analysis", "method": "POST", "path": "/api/tools/run_variance_analysis", "milestone": "M2"},
                {"name": "run_costing_bridge", "method": "POST", "path": "/api/tools/run_costing_bridge", "milestone": "M2"},
                {"name": "run_abc_profitability", "method": "POST", "path": "/v1/sessions/{id}/messages", "milestone": "M7"},
                {"name": "run_budget_chain", "method": "POST", "path": "/v1/sessions/{id}/messages", "milestone": "M9"},
                {"name": "run_performance_eval", "method": "POST", "path": "/v1/sessions/{id}/messages", "milestone": "M10"},
                {"name": "explain_run", "method": "GET", "path": "/v1/runs/{id}/trace", "milestone": "M4"},
            ],
            "supported_decisions": svc.capability()["supported_decisions"],
        }

    @app.post("/api/tools/preview_differential_analysis")
    def preview_differential(
        payload: DifferentialRequest, svc: ToolService = Depends(get_service)
    ) -> dict[str, Any]:
        return svc.preview_differential(
            decision_type=payload.decision_type,
            arguments=payload.arguments,
        )

    @app.post("/api/tools/run_differential_analysis")
    def run_differential(
        payload: DifferentialRequest, svc: ToolService = Depends(get_service)
    ) -> dict[str, Any]:
        return svc.run_differential(
            decision_type=payload.decision_type,
            arguments=payload.arguments,
            confirmation=payload.confirmation,
            question=payload.question,
        )

    @app.post("/api/tools/run_variance_analysis")
    def run_variance(payload: VarianceRequest, svc: ToolService = Depends(get_service)) -> dict[str, Any]:
        return svc.run_variance(
            product_id=payload.product_id,
            plant_id=payload.plant_id,
            period_id=payload.period_id,
            question=payload.question,
        )

    @app.post("/api/tools/run_costing_bridge")
    def run_bridge(payload: BridgeRequest, svc: ToolService = Depends(get_service)) -> dict[str, Any]:
        return svc.run_bridge(
            product_id=payload.product_id,
            plant_id=payload.plant_id,
            period_id=payload.period_id,
            variable_operating_income=payload.variable_operating_income,
            question=payload.question,
        )

    @app.get("/api/runs/{run_id}")
    def get_run_legacy(run_id: str, svc: ToolService = Depends(get_service)) -> JSONResponse:
        run = svc.get_run(run_id)
        if run is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {"code": "RUN_NOT_FOUND", "message": f"Run {run_id} was not found."},
                    "remediation": ["Open a run from the audit drawer or list recent runs."],
                },
            )
        return JSONResponse(content=run)

    @app.get("/api/audit/recent")
    def audit_recent(limit: int = 20, svc: ToolService = Depends(get_service)) -> dict[str, Any]:
        return {"runs": svc.audit.recent_runs(max(1, min(limit, 200)))}

    @app.post("/api/runs/{run_id}/reperform")
    def reperform_legacy(run_id: str, svc: ToolService = Depends(get_service)) -> dict[str, Any]:
        return svc.reperform(run_id)

    @app.get("/api/audit/verify")
    def verify_audit(svc: ToolService = Depends(get_service)) -> dict[str, Any]:
        return svc.verify_chain()

    return app


def _roles() -> dict[str, list[str]]:
    from costpilot.platform.auth import ROLE_PERMISSIONS

    return {role: sorted(permissions) for role, permissions in ROLE_PERMISSIONS.items()}


def _gate_remediation(gates: list[Any]) -> list[str]:
    actions = []
    for gate in gates:
        payload = gate.to_dict() if hasattr(gate, "to_dict") else gate
        if isinstance(payload, dict) and payload.get("remediation") and payload.get("status") != "pass":
            actions.append(f"{payload.get('id')}: {payload.get('remediation')}")
    return actions


app = create_app()
