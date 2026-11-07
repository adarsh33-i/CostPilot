"""FastAPI surface for the M0-M3 demo."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from costpilot.engine.errors import DomainError
from costpilot.service import ToolService

STATIC_DIR = Path(__file__).parent / "static"


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


def create_app(service: ToolService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.service = service or ToolService.from_env()
        yield

    app = FastAPI(
        title="CostPilot",
        version="0.3.0",
        description=(
            "Deterministic management-accounting engine. Every number comes from a rule "
            "with a full trace; the demo covers M0-M3 (no LLM planner yet)."
        ),
        lifespan=lifespan,
    )

    def get_service(request: Request) -> ToolService:
        return request.app.state.service

    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        remediation = [exc.remediation] if exc.remediation else []
        return JSONResponse(
            status_code=422,
            content={
                "error": {"code": exc.code, "message": exc.message},
                "remediation": remediation,
            },
        )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health(svc: ToolService = Depends(get_service)) -> dict[str, Any]:
        chain = svc.verify_chain()
        return {
            "ok": True,
            "snapshot_id": svc.snapshot.snapshot_id,
            "dataset": svc.snapshot.dataset_name,
            "engine_version": svc.pins()["engine_version"],
            "rulebook_version": svc.pins()["rulebook_version"],
            "audit_chain": chain,
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
                {"name": "describe_dataset", "method": "GET", "path": "/api/capability", "milestone": "M1"},
                {"name": "run_differential_analysis", "method": "POST", "path": "/api/tools/run_differential_analysis", "milestone": "M3"},
                {"name": "run_variance_analysis", "method": "POST", "path": "/api/tools/run_variance_analysis", "milestone": "M2"},
                {"name": "run_costing_bridge", "method": "POST", "path": "/api/tools/run_costing_bridge", "milestone": "M2"},
                {"name": "explain_run", "method": "GET", "path": "/api/runs/{run_id}", "milestone": "M4 (trace only)"},
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
    def run_variance(
        payload: VarianceRequest, svc: ToolService = Depends(get_service)
    ) -> dict[str, Any]:
        return svc.run_variance(
            product_id=payload.product_id,
            plant_id=payload.plant_id,
            period_id=payload.period_id,
            question=payload.question,
        )

    @app.post("/api/tools/run_costing_bridge")
    def run_bridge(
        payload: BridgeRequest, svc: ToolService = Depends(get_service)
    ) -> dict[str, Any]:
        return svc.run_bridge(
            product_id=payload.product_id,
            plant_id=payload.plant_id,
            period_id=payload.period_id,
            variable_operating_income=payload.variable_operating_income,
            question=payload.question,
        )

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, svc: ToolService = Depends(get_service)) -> JSONResponse:
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

    @app.post("/api/runs/{run_id}/reperform")
    def reperform(run_id: str, svc: ToolService = Depends(get_service)) -> dict[str, Any]:
        return svc.reperform(run_id)

    @app.get("/api/audit/verify")
    def verify_audit(svc: ToolService = Depends(get_service)) -> dict[str, Any]:
        return svc.verify_chain()

    return app


app = create_app()
