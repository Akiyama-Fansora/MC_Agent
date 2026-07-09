from __future__ import annotations

import argparse
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .agent_message import coerce_message_bool
from .agent_runtime import collection_tools_for_crawler, tool_catalog_prompt, tools_for_agent
from .config import AppConfig, load_config
from .crawler_job_setup_service import CrawlerJobSetupService
from .crawler_llm_planner import plan_crawler_tasks_resilient
from .crawler_planner import plan_crawler_tasks, toolsets_payload
from .event_stream import ThreadedEventStream
from .llm_profiles import profile_by_id, profiles_payload, save_profiles_payload, test_profile_connection
from .session_state import DEFAULT_SESSION_STORE, normalize_session_id
from .web_server import (
    AGENTS,
    STATIC_DIR,
    WEB_DIR,
    _adaptive_preview_k,
    _available_models,
    _chat,
    _chat_impl,
    _delete_session,
    _ingest_after_crawl,
    _job_to_dict,
    _jobs_payload,
    _recent_crawler_manifest_summary,
    _request_job_stop,
    _run_ingest_job,
    _search,
    _send_agent_message,
    _session_summary,
    _start_job,
    _status_payload,
    _user_message_fields,
)


def _stream_chat_events(config: AppConfig, payload: dict[str, Any]) -> Iterator[str]:
    def run(emit: Any) -> None:
        result = _send_agent_message(config, payload, emit=emit, **_user_message_fields(payload))
        emit("response", result)
        emit("done", {"ok": True})

    return ThreadedEventStream(run).sse()


def create_app(config: AppConfig | None = None) -> FastAPI:
    app = FastAPI(
        title="MC_Agent API",
        version="0.1.0",
        description="FastAPI backend for MCagent, CrawlerAgent, local RAG, SSE chat, jobs, and settings.",
    )
    app.state.config = config or load_config(None)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def cfg() -> AppConfig:
        return app.state.config

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})

    @app.get("/index.html", include_in_schema=False)
    def index_html() -> FileResponse:
        return index()

    @app.get("/settings", include_in_schema=False)
    def settings() -> FileResponse:
        return FileResponse(WEB_DIR / "settings.html", media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})

    @app.get("/settings.html", include_in_schema=False)
    def settings_html() -> FileResponse:
        return settings()

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "backend": "fastapi"}

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return _status_payload(cfg())

    @app.get("/api/jobs")
    def jobs() -> dict[str, Any]:
        return _jobs_payload()

    @app.get("/api/models")
    def models() -> dict[str, Any]:
        return {"models": _available_models(cfg())}

    @app.get("/api/llm-profiles")
    def llm_profiles_get() -> dict[str, Any]:
        return profiles_payload(cfg())

    @app.get("/api/agents")
    def agents() -> dict[str, Any]:
        return {"agents": AGENTS}

    @app.get("/api/agents/{agent_id}/tools")
    def agent_tools(agent_id: str) -> dict[str, Any]:
        route_tools = [tool.to_dict() for tool in tools_for_agent(agent_id)]
        collection_tools = [tool.to_dict() for tool in collection_tools_for_crawler()] if agent_id == "crawler_agent" else []
        return {
            "agent": agent_id,
            "route_tools": route_tools,
            "collection_tools": collection_tools,
            "catalog": tool_catalog_prompt(agent_id),
        }

    @app.get("/api/crawler/summary")
    def crawler_summary_get() -> dict[str, Any]:
        return _recent_crawler_manifest_summary(cfg().paths.source_dir, limit=20)

    @app.post("/api/chat")
    async def chat(request: Request) -> dict[str, Any]:
        payload = await request.json()
        return await run_in_threadpool(_chat, cfg(), payload)

    @app.post("/api/agent-message")
    async def agent_message(request: Request) -> dict[str, Any]:
        payload = await request.json()
        return await run_in_threadpool(
            _send_agent_message,
            cfg(),
            payload,
            from_agent=str(payload.get("from_agent") or payload.get("from") or "User"),
            content=str(payload.get("content") or payload.get("message") or payload.get("question") or ""),
            to_agent=str(payload.get("to_agent") or payload.get("to") or payload.get("agent") or "MCagent"),
            intent=str(payload.get("intent") or ""),
            conversation_id=str(payload.get("session_id") or payload.get("conversation_id") or ""),
            reply_to=str(payload.get("reply_to") or ""),
            requires_reply=coerce_message_bool(payload.get("requires_reply"), default=True),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )

    @app.post("/api/chat/stream")
    async def chat_stream(request: Request) -> StreamingResponse:
        payload = await request.json()
        return StreamingResponse(
            _stream_chat_events(cfg(), payload),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/search")
    async def search(request: Request) -> dict[str, Any]:
        payload = await request.json()
        query = str(payload.get("query") or "")
        top_k = int(payload.get("top_k") or _adaptive_preview_k(query))
        return {"results": _search(cfg(), query, top_k)}

    @app.post("/api/crawler/plan")
    async def crawler_plan(request: Request) -> dict[str, Any]:
        payload = await request.json()
        question = str(payload.get("question") or payload.get("query") or "")
        include_completed = bool(payload.get("include_completed"))
        max_tasks = CrawlerJobSetupService().planner_max_tasks(payload)
        session_summary = payload.get("session_summary") if isinstance(payload.get("session_summary"), dict) else None
        if include_completed:
            plan = await run_in_threadpool(plan_crawler_tasks, question, cfg().paths.source_dir, max_tasks=max_tasks, include_completed=True)
        else:
            plan = await run_in_threadpool(plan_crawler_tasks_resilient, question, cfg().paths.source_dir, max_tasks=max_tasks, session_summary=session_summary)
        plan.setdefault("toolsets", toolsets_payload())
        return plan

    @app.post("/api/crawler/summary")
    async def crawler_summary_post(request: Request) -> dict[str, Any]:
        payload = await request.json()
        limit = int(payload.get("limit") or 20)
        query = str(payload.get("query") or "")
        return _recent_crawler_manifest_summary(cfg().paths.source_dir, limit=max(1, min(limit, 100)), query=query)

    @app.post("/api/collaboration/start")
    async def collaboration_start(request: Request) -> dict[str, Any]:
        payload = await request.json()
        return _chat(cfg(), payload | {"agent": "mcagent_rag"})

    @app.post("/api/llm-profiles")
    async def llm_profiles_post(request: Request) -> dict[str, Any]:
        payload = await request.json()
        return save_profiles_payload(cfg(), payload)

    @app.post("/api/llm-profiles/test")
    async def llm_profiles_test(request: Request) -> JSONResponse:
        payload = await request.json()
        raw_profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
        existing = profile_by_id(cfg(), str(raw_profile.get("id") or payload.get("id") or "")) if raw_profile else None
        try:
            result = await run_in_threadpool(test_profile_connection, raw_profile, existing=existing)
            return JSONResponse(result)
        except Exception as exc:  # noqa: BLE001 - surface connection failure to settings UI.
            return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=200)

    @app.post("/api/ingest")
    def ingest() -> dict[str, Any]:
        result = _ingest_after_crawl(cfg())
        return {"stats": result["stats"], "knowledge_map": result["knowledge_map"], "status": _status_payload(cfg())}

    @app.post("/api/jobs/start-ingest")
    def start_ingest() -> JSONResponse:
        job, created = _start_job("ingest", "Import crawler exports", lambda item: _run_ingest_job(item, cfg()))
        return JSONResponse({"job": _job_to_dict(job), "created": created}, status_code=202 if created else 409)

    @app.post("/api/jobs/stop")
    async def stop_job(request: Request) -> JSONResponse:
        payload = await request.json()
        job = _request_job_stop(str(payload.get("id") or ""))
        if job is None:
            return JSONResponse({"error": "job not found"}, status_code=404)
        return JSONResponse({"job": _job_to_dict(job)}, status_code=202)

    @app.post("/api/session/delete")
    async def session_delete(request: Request) -> dict[str, Any]:
        payload = await request.json()
        return _delete_session(str(payload.get("session_id") or "default"))

    @app.post("/api/session")
    async def session_get(request: Request) -> dict[str, Any]:
        payload = await request.json()
        session_id = normalize_session_id(payload.get("session_id"))
        history = DEFAULT_SESSION_STORE.history(session_id)
        return {"session_id": session_id, "history": history}

    @app.post("/api/session/context")
    async def session_context(request: Request) -> dict[str, Any]:
        payload = await request.json()
        session_id = normalize_session_id(payload.get("session_id"))
        agent = str(payload.get("agent") or "mcagent_rag")
        summary = _session_summary(payload | {"session_id": session_id})
        return DEFAULT_SESSION_STORE.context(session_id, agent=agent, summary=summary).to_dict()

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)

    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local MCagent FastAPI backend.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", help="Path to config JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - dependency check is user-facing.
        raise SystemExit("FastAPI backend requires uvicorn. Run: pip install -r requirements.txt") from exc
    app = create_app(load_config(args.config))
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0
