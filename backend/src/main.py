"""FastAPI entrypoint for the deployable Deep Research application."""

from __future__ import annotations

import base64
import json
import os
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event, Lock
from typing import Any, Dict, Iterator, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field

from agent import DeepResearchAgent, ResearchCancelledError
from config import Configuration, SearchAPI
from runtime import (
    ResearchGate,
    ResearchLimitExceeded,
    RuntimeSettings,
    research_configuration_errors,
)


logger.remove()
logger.add(
    sys.stderr,
    level=os.getenv("LOG_LEVEL", "INFO"),
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
        "<cyan>{name}:{function}:{line}</cyan> | <level>{message}</level>"
    ),
    colorize=sys.stderr.isatty(),
)

runtime_settings = RuntimeSettings.from_env()
research_gate = ResearchGate(runtime_settings)
active_jobs: dict[str, Event] = {}
active_jobs_lock = Lock()


class ResearchRequest(BaseModel):
    """Payload for starting a research job."""

    topic: str = Field(..., min_length=2, max_length=2000)
    search_api: SearchAPI | None = None
    job_id: str = Field(
        default_factory=lambda: uuid4().hex,
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class ResearchResponse(BaseModel):
    """Completed non-streaming research result."""

    job_id: str
    report_markdown: str
    todo_items: list[dict[str, Any]] = Field(default_factory=list)


def _mask_secret(value: Optional[str], visible: int = 4) -> str:
    if not value:
        return "unset"
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}...{value[-visible:]}"


def _build_config(payload: ResearchRequest) -> Configuration:
    overrides: Dict[str, Any] = {}
    if payload.search_api is not None:
        overrides["search_api"] = payload.search_api
    return Configuration.from_env(overrides=overrides)


def _client_id(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        # A trusted reverse proxy appends the actual client address. Using the
        # last value avoids accepting a caller-controlled first hop.
        return forwarded.rsplit(",", 1)[-1].strip()
    return request.client.host if request.client else "unknown"


def _consume_research_budget(request: Request) -> None:
    try:
        research_gate.consume(_client_id(request))
    except ResearchLimitExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc


def _validated_config(payload: ResearchRequest) -> Configuration:
    try:
        config = _build_config(payload)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"运行配置无效：{exc}") from exc

    errors = research_configuration_errors(config)
    if errors:
        raise HTTPException(status_code=503, detail="；".join(errors))
    return config


def _register_job(job_id: str) -> Event:
    with active_jobs_lock:
        if job_id in active_jobs:
            raise HTTPException(status_code=409, detail="任务编号已存在，请重新发起研究。")
        event = Event()
        active_jobs[job_id] = event
        return event


def _remove_job(job_id: str) -> None:
    with active_jobs_lock:
        active_jobs.pop(job_id, None)


def _decode_basic_password(header: str) -> str | None:
    if not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    _, separator, password = decoded.partition(":")
    return password if separator else None


@asynccontextmanager
async def app_lifespan(_: FastAPI):
    try:
        config = Configuration.from_env()
        errors = research_configuration_errors(config)
        logger.info(
            "Research runtime provider={} model={} base_url={} search_api={} "
            "access_password={} daily_budget={} readiness={}",
            config.llm_provider,
            config.resolved_model() or "unset",
            config.llm_base_url or config.ollama_base_url or "unset",
            config.search_api.value,
            "configured" if runtime_settings.access_password else "disabled",
            runtime_settings.daily_research_budget,
            "ready" if not errors else "blocked: " + "; ".join(errors),
        )
        logger.info("LLM key status={}", _mask_secret(config.llm_api_key))
    except Exception as exc:
        logger.error("Runtime configuration is invalid: {}", exc)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="HelloAgents Deep Researcher",
        version="0.2.0",
        lifespan=app_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(runtime_settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
    )

    @app.middleware("http")
    async def basic_access_guard(request: Request, call_next):
        password = runtime_settings.access_password
        if not password or request.url.path in {"/healthz", "/readyz"}:
            return await call_next(request)

        supplied = _decode_basic_password(request.headers.get("authorization", ""))
        if supplied is None or not secrets.compare_digest(supplied, password):
            return JSONResponse(
                status_code=401,
                content={"detail": "请输入演示访问密码。"},
                headers={"WWW-Authenticate": 'Basic realm="Deep Research Demo"'},
            )
        return await call_next(request)

    @app.get("/healthz")
    def health_check() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readiness_check() -> JSONResponse:
        try:
            config = Configuration.from_env()
            errors = research_configuration_errors(config)
        except Exception as exc:
            errors = [str(exc)]
        status_code = 200 if not errors else 503
        return JSONResponse(
            status_code=status_code,
            content={
                "status": "ready" if not errors else "blocked",
                "errors": errors,
                "budget": research_gate.snapshot(),
            },
        )

    @app.post("/research", response_model=ResearchResponse)
    def run_research(payload: ResearchRequest, request: Request) -> ResearchResponse:
        config = _validated_config(payload)
        _consume_research_budget(request)
        cancel_event = _register_job(payload.job_id)
        try:
            agent = DeepResearchAgent(config=config, cancel_event=cancel_event)
            result = agent.run(payload.topic)
        except ResearchCancelledError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Research job {} failed", payload.job_id)
            raise HTTPException(
                status_code=502,
                detail=f"研究任务执行失败：{str(exc)[:300]}",
            ) from exc
        finally:
            _remove_job(payload.job_id)

        todo_payload = [
            {
                "id": item.id,
                "title": item.title,
                "intent": item.intent,
                "query": item.query,
                "status": item.status,
                "summary": item.summary,
                "sources_summary": item.sources_summary,
                "note_id": item.note_id,
                "note_path": item.note_path,
            }
            for item in result.todo_items
        ]
        return ResearchResponse(
            job_id=payload.job_id,
            report_markdown=result.report_markdown or result.running_summary or "",
            todo_items=todo_payload,
        )

    @app.post("/research/stream")
    def stream_research(payload: ResearchRequest, request: Request) -> StreamingResponse:
        config = _validated_config(payload)
        _consume_research_budget(request)
        cancel_event = _register_job(payload.job_id)

        def event_iterator() -> Iterator[str]:
            try:
                agent = DeepResearchAgent(config=config, cancel_event=cancel_event)
                yield f"data: {json.dumps({'type': 'job', 'job_id': payload.job_id}, ensure_ascii=False)}\n\n"
                for event in agent.run_stream(payload.topic):
                    event.setdefault("job_id", payload.job_id)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except ResearchCancelledError:
                event = {"type": "cancelled", "job_id": payload.job_id, "message": "研究任务已取消"}
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception as exc:
                logger.exception("Streaming research job {} failed", payload.job_id)
                event = {
                    "type": "error",
                    "job_id": payload.job_id,
                    "detail": f"研究任务执行失败：{str(exc)[:300]}",
                }
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            finally:
                cancel_event.set()
                _remove_job(payload.job_id)

        return StreamingResponse(
            event_iterator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/research/{job_id}/cancel")
    def cancel_research(job_id: str) -> dict[str, str]:
        with active_jobs_lock:
            cancel_event = active_jobs.get(job_id)
        if cancel_event is None:
            raise HTTPException(status_code=404, detail="任务不存在或已经结束。")
        cancel_event.set()
        return {"status": "cancellation_requested", "job_id": job_id}

    frontend_dir = runtime_settings.frontend_dist_dir
    assets_dir = frontend_dir / "assets"
    index_file = frontend_dir / "index.html"
    if assets_dir.is_dir() and index_file.is_file():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="frontend-assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def serve_frontend(full_path: str) -> FileResponse:
            requested = (frontend_dir / full_path).resolve()
            if requested.is_file() and frontend_dir.resolve() in requested.parents:
                return FileResponse(requested)
            return FileResponse(index_file)
    else:
        logger.warning("Frontend build not found at {}", Path(frontend_dir))

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=False,
        proxy_headers=True,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
