from __future__ import annotations

import argparse
import concurrent.futures
import copy
from dataclasses import asdict, dataclass, field, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import contextlib
from datetime import datetime
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from typing import Any
import sqlite3
import zipfile

from .agent_memory import append_memory_event, memory_summary
from .agent_message import AgentMessage, agent_reply_message_from_payload, make_agent_message, message_from_payload
from .agent_runtime import (
    build_handoff_contract,
    classify_crawler_tool_result,
    make_agent_loop_event,
)
from .artifact_reference_service import ArtifactReferenceService
from .artifact_save_service import ArtifactSaveError, ArtifactSaveService
from .agent_execution import build_agent_execution_context
from .agent_executor import AgentToolExecutor
from .agent_router import LlmAgentToolRouterService, json_object_from_llm_text
from .chat import SYSTEM_PROMPT, format_context, format_sources
from .cleaners import _HTMLTextExtractor, normalize_text
from .config import AppConfig, OllamaConfig, PROJECT_ROOT, load_config
from .crawler_llm_planner import plan_crawler_tasks_resilient, plan_crawler_tasks_rule_fallback, reflect_crawler_progress, review_topic_discovery_candidates
from .crawler_delegation_service import CrawlerDelegationService
from .crawler_planner import CONCEPTS, decompose_crawler_queries, plan_crawler_tasks, toolsets_payload
from .crawler_runtime_step_service import CrawlerRuntimeStepService
from .crawler_task_materialization_service import CrawlerTaskMaterializationService
from .crawler_task_preparation_service import CrawlerTaskPreparationService
from .crawler_temporary_extract_service import CrawlerTemporaryExtractService
from .crawler_result_accounting_service import CrawlerResultAccountingService
from .crawler_loop_control_service import CrawlerLoopControlService
from .crawler_topic_discovery_service import CrawlerTopicDiscoveryReviewService
from .crawler_job_finalization_service import CrawlerJobFinalizationService
from .crawler_job_progress_service import CrawlerJobProgressService
from .crawler_job_setup_service import CrawlerJobSetupService
from .crawler_planner_wait_service import CrawlerPlannerWaitService
from .evidence_service import EvidenceWorkflowService
from .graphs import dispatch_agent_message_graph
from .graphs.crawler_job import run_crawler_job_graph
from .ingest import IngestStats, ingest_exports
from .job_view_service import JobReadableViewService
from .llm import OllamaOpenAIClient, OpenAICompatibleClient
from .llm_profiles import (
    client_for_agent,
    client_from_profile,
    profile_by_id,
    profiles_payload,
    resolve_profile_from_model,
    save_profiles_payload,
    test_profile_connection,
)
from .query_intent import analyze_query
from .rag_service import RagRetrievalService
from .retriever import Retriever
from .schema import SearchResult
from .session_state import DEFAULT_SESSION_STORE, merge_limited, normalize_session_id, payload_history
from .storage import connect, count_rows


AGENT_CONSOLE_DIR = PROJECT_ROOT / "frontend"
WEB_DIR = Path(os.environ.get("AGENT_CONSOLE_DIR", AGENT_CONSOLE_DIR)).resolve()
STATIC_DIR = WEB_DIR / "static"
MAX_ROUGH_TOP_K = 200
MAX_FINAL_CONTEXT_K = 12
MIN_FINAL_CONTEXT_K = 4
MAX_MODEL_CONTEXT_CHARS = 10000
MAX_SOURCE_CONTEXT_CHARS = 1500
MAX_DEEP_EVIDENCE_CHARS = 900
RAW_HTML_SCAN_FILE_LIMIT = 300
RAW_HTML_SCAN_SECONDS = 6.0
DEFAULT_ANSWER_MAX_TOKENS = 3000
AUTO_ANSWER_MAX_TOKENS = 3000
ANSWER_MAX_TOKENS_CAP = 6000
DEFAULT_CRAWLER_PLANNER_TIMEOUT_SECONDS = 110
DEFAULT_MCAGENT_CONTEXT_TIMEOUT_SECONDS = 120
DEFAULT_CHAT_RUNTIME_TIMEOUT_SECONDS = 75
HANDOFF_BRIEF_LLM_TIMEOUT_SECONDS = 20
RUNTIME_REVIEW_LLM_TIMEOUT_SECONDS = 20
MAX_JOBS = 40
GROW_PROGRESS_PATH = PROJECT_ROOT / "runtime" / "grow_knowledge_base_progress.json"
JOBS_HISTORY_PATH = PROJECT_ROOT / "runtime" / "jobs_history.json"
GROW_LOG_CANDIDATES = (
    PROJECT_ROOT / "runtime" / "grow_knowledge_base_broad_safe_20260516.log",
    PROJECT_ROOT / "runtime" / "grow_knowledge_base_broad_20260516.log",
    PROJECT_ROOT / "runtime" / "grow_knowledge_base_focused_after_cleanup.log",
    PROJECT_ROOT / "runtime" / "grow_knowledge_base.log",
)

AGENTS = [
    {
        "id": "mcagent_rag",
        "name": "MCagent",
        "description": "LLM + \u672c\u5730 RAG\uff1b\u8d44\u6599\u4e0d\u8db3\u65f6\u5411 Crawler \u63d0\u4ea4\u8d44\u6599\u7f3a\u53e3\u3002",
        "uses_llm": True,
        "uses_retrieval": True,
    },
    {
        "id": "crawler_agent",
        "name": "Crawler",
        "description": "\u72ec\u7acb\u722c\u866b Agent\uff1b\u53ef\u63a5\u7528\u6237\u6216 MCagent \u4efb\u52a1\uff0c\u81ea\u4e3b\u89c4\u5212\u91c7\u96c6\u4e0e\u4ea4\u4ed8\u683c\u5f0f\u3002",
        "uses_llm": True,
        "uses_retrieval": False,
    },
]


SESSION_STORE = DEFAULT_SESSION_STORE
JOBS: dict[str, "Job"] = {}
JOBS_ORDER: list[str] = []
JOBS_LOCK = threading.Lock()
RAW_HTML_SCAN_LOCK = threading.Lock()
RAW_HTML_SCAN_CACHE: dict[str, tuple[float, int, str]] = {}
INGEST_LOCK = threading.Lock()
SOURCE_STATUS_LOCK = threading.Lock()
SOURCE_STATUS_CACHE: dict[str, Any] = {"time": 0.0, "source_dir": "", "payload": None}


@dataclass(slots=True)
class Job:
    id: str
    kind: str
    title: str
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    ended_at: float | None = None
    summary: str = ""
    error: str | None = None
    result: dict[str, Any] | None = None
    stop_requested: bool = False
    stop_requested_at: float | None = None
    current_pid: int | None = None


@dataclass(slots=True)
class CrawlerDelegationRun:
    plan: Any
    job: Job | None
    created: bool
    note: str
    response: dict[str, Any] | None = None


@dataclass(slots=True)
class RagEvidenceRouteResult:
    selected: list[SearchResult]
    evidence_report: Any | None
    response: dict[str, Any] | None = None


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _read_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8", errors="replace")
    return json.loads(raw) if raw.strip() else {}


def _has_likely_encoding_damage(value: Any) -> bool:
    if isinstance(value, str):
        text = value.strip()
        if "\ufffd" in text:
            return True
        compact = re.sub(r"\s+", "", text)
        if re.search(r"\?{5,}", compact):
            return True
        latin_mojibake_markers = ("\u00c3", "\u00c2", "\u00e5", "\u00e6", "\u00e7", "\u00e9", "\u00e8", "\u00e4")
        c1_controls = sum(1 for char in text if 0x80 <= ord(char) <= 0x9F)
        latin_hits = sum(text.count(marker) for marker in latin_mojibake_markers)
        if c1_controls >= 1 and latin_hits >= 2:
            return True
        mojibake_markers = tuple(
            chr(code)
            for code in (
                0x6D94,
                0x58AD,
                0x95AD,
                0x93C1,
                0x9356,
                0x934F,
                0x7D5D,
                0x7ED4,
                0x9416,
                0x8255,
                0x8785,
                0x20AC,
            )
        )
        if sum(1 for marker in mojibake_markers if marker in text) >= 3:
            return True
        return False
    if isinstance(value, dict):
        return any(_has_likely_encoding_damage(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_likely_encoding_damage(item) for item in value)
    return False


def _send_json(handler: BaseHTTPRequestHandler, data: Any, status: int = 200) -> None:
    body = json.dumps(data, ensure_ascii=False, default=_json_default).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _send_text(handler: BaseHTTPRequestHandler, text: str, content_type: str, status: int = 200, cache_control: str = "") -> None:
    body = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    if cache_control:
        handler.send_header("Cache-Control", cache_control)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _send_sse_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "close")
    handler.end_headers()


def _write_sse(handler: BaseHTTPRequestHandler, event: str, data: Any) -> None:
    payload = json.dumps(data, ensure_ascii=False, default=_json_default)
    body = f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
    handler.wfile.write(body)
    handler.wfile.flush()


def _job_to_dict(job: Job) -> dict[str, Any]:
    _refresh_job_ingest_state(job)
    data = asdict(job)
    _sanitize_job_planned_tasks_for_display(data)
    data["readable"] = _job_readable_summary(data)
    return data


def _job_to_light_dict(job: Job) -> dict[str, Any]:
    data = _job_light_snapshot(job)
    _sanitize_job_planned_tasks_for_display(data)
    data["readable"] = _job_readable_summary(data, refresh_manifest=False)
    return data


def _job_light_snapshot(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "title": job.title,
        "status": job.status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "ended_at": job.ended_at,
        "summary": _tail_text(str(job.summary or ""), 1200),
        "error": _tail_text(str(job.error or ""), 1200) if job.error else None,
        "stop_requested": job.stop_requested,
        "stop_requested_at": job.stop_requested_at,
        "current_pid": job.current_pid,
        "result": _light_job_result(job.result),
    }


def _light_job_result(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return result
    light: dict[str, Any] = {}
    for key in (
        "planned_tasks",
        "blocked_planned_tasks",
        "success_count",
        "failure_count",
        "candidate_count",
        "replan_count",
        "ingest",
        "ingest_error",
        "ingest_background",
        "loop",
    ):
        if key in result:
            light[key] = copy.deepcopy(result.get(key))
    plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
    if plan:
        light["plan"] = _light_job_plan(plan)
    tasks = result.get("tasks") if isinstance(result.get("tasks"), list) else []
    if tasks:
        light["tasks"] = [_light_job_task(item) for item in tasks if isinstance(item, dict)]
    return light


def _light_job_plan(plan: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "strategy",
        "planner_model",
        "planner_recovered_from_error",
        "topic",
        "target_hint",
        "package_type",
        "delivery_target",
        "cleaning_policy",
        "planner_error",
    )
    light = {key: copy.deepcopy(plan.get(key)) for key in keys if key in plan}
    for key, limit in (
        ("question", 700),
        ("reason", 900),
    ):
        if key in plan:
            light[key] = _tail_text(str(plan.get(key) or ""), limit)
    for key, limit in (
        ("coverage_goals", 8),
        ("known_components", 12),
        ("success_criteria", 10),
        ("subqueries", 24),
        ("sources", 16),
    ):
        value = plan.get(key)
        if isinstance(value, list):
            light[key] = copy.deepcopy(value[:limit])
    raw_plan = plan.get("raw_plan") if isinstance(plan.get("raw_plan"), dict) else {}
    if raw_plan:
        light["raw_plan_summary"] = {
            key: _tail_text(str(raw_plan.get(key) or ""), 500)
            for key in ("topic", "delivery_target", "reason")
            if raw_plan.get(key)
        }
    model_prior = plan.get("model_prior") if isinstance(plan.get("model_prior"), dict) else {}
    if model_prior:
        light["model_prior"] = {
            "target": _tail_text(str(model_prior.get("target") or ""), 160),
            "aliases": [str(item)[:100] for item in list(model_prior.get("aliases") or [])[:6] if str(item).strip()],
            "likely_source_graph": [str(item)[:140] for item in list(model_prior.get("likely_source_graph") or [])[:8] if str(item).strip()],
            "search_leads": [str(item)[:120] for item in list(model_prior.get("search_leads") or [])[:8] if str(item).strip()],
            "verification_questions": [str(item)[:160] for item in list(model_prior.get("verification_questions") or [])[:6] if str(item).strip()],
            "evidence_status": str(model_prior.get("evidence_status") or "hypothesis_only"),
            "allowed_use": str(model_prior.get("allowed_use") or "planning_only"),
            "forbidden_use": _tail_text(str(model_prior.get("forbidden_use") or ""), 240),
        }
    refs = plan.get("artifact_refs") if isinstance(plan.get("artifact_refs"), list) else []
    if refs:
        light["artifact_refs"] = [_light_artifact_ref(item) for item in refs[-20:] if isinstance(item, dict)]
    reflections = plan.get("agent_reflections") if isinstance(plan.get("agent_reflections"), list) else []
    if reflections:
        light["agent_reflections"] = [_light_agent_reflection(item) for item in reflections[-10:] if isinstance(item, dict)]
    return light


def _light_artifact_ref(item: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "source", "url", "path", "kind", "format", "bytes", "text_like")
    light = {key: copy.deepcopy(item.get(key)) for key in keys if key in item}
    if "title" in item:
        light["title"] = _tail_text(str(item.get("title") or ""), 300)
    return light


def _light_agent_reflection(item: dict[str, Any]) -> dict[str, Any]:
    light: dict[str, Any] = {}
    for key in ("at_index", "action", "selected_index", "planner"):
        if key in item:
            light[key] = copy.deepcopy(item.get(key))
    if "reason" in item:
        light["reason"] = _tail_text(str(item.get("reason") or ""), 900)
    contract = item.get("contract") if isinstance(item.get("contract"), dict) else {}
    if contract:
        light["contract"] = {
            key: copy.deepcopy(contract.get(key))
            for key in ("valid", "issues", "requires_llm_task_materialization", "pending_count")
            if key in contract
        }
    tasks = item.get("tasks") if isinstance(item.get("tasks"), list) else []
    if tasks:
        light["tasks"] = [_light_planned_task(task) for task in tasks[:4] if isinstance(task, dict)]
    return light


def _light_planned_task(item: dict[str, Any]) -> dict[str, Any]:
    light: dict[str, Any] = {}
    for key in ("source", "priority", "from_selected_action_plan"):
        if key in item:
            light[key] = copy.deepcopy(item.get(key))
    for key, limit in (("query", 500), ("reason", 700)):
        if key in item:
            light[key] = _tail_text(str(item.get(key) or ""), limit)
    return light


def _light_job_task(item: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "source",
        "query",
        "reason",
        "returncode",
        "elapsed_seconds",
        "export_dir",
        "ingest_deferred",
        "ingest_skipped",
        "empty_result",
        "off_topic_result",
        "manifest_stats",
        "transport",
        "timed_out",
        "failure_reason",
        "crawler_review_action",
        "crawler_review_next_action",
        "mcagent_source_count",
        "mcagent_source_paths",
    )
    light = {key: copy.deepcopy(item.get(key)) for key in keys if key in item}
    if isinstance(light.get("manifest_stats"), dict):
        light["manifest_stats"] = _sanitize_manifest_stats_for_display(light["manifest_stats"])
    for key in ("observation", "topic_validation", "existing_evidence_review"):
        value = item.get(key)
        if isinstance(value, dict):
            light[key] = _light_nested_text_dict(value)
    exchange = item.get("agent_message_exchange") if isinstance(item.get("agent_message_exchange"), dict) else {}
    if exchange:
        light["agent_message_exchange"] = _light_agent_message_exchange(exchange)
    if "output" in item:
        light["output"] = _tail_text(str(item.get("output") or ""), 900)
    if "mcagent_trace" in item and isinstance(item.get("mcagent_trace"), list):
        light["mcagent_trace"] = [_light_trace_step(step) for step in item["mcagent_trace"][-10:] if isinstance(step, dict)]
    return light


def _sanitize_manifest_stats_for_display(stats: dict[str, Any]) -> dict[str, Any]:
    sanitized = copy.deepcopy(stats)
    for key in ("candidate_preview", "expanded_candidate_preview"):
        items = sanitized.get(key)
        if not isinstance(items, list):
            continue
        clean_items: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            clean = {
                field: value
                for field, value in item.items()
                if not _has_likely_encoding_damage(value)
            }
            if clean:
                clean_items.append(clean)
        sanitized[key] = clean_items
    return sanitized


def _light_nested_text_dict(value: dict[str, Any]) -> dict[str, Any]:
    light: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, str):
            light[str(key)] = _tail_text(item, 700)
        elif isinstance(item, (int, float, bool)) or item is None:
            light[str(key)] = item
        elif isinstance(item, list):
            light[str(key)] = copy.deepcopy(item[:8])
        elif isinstance(item, dict):
            light[str(key)] = _light_nested_text_dict(item)
    return light


def _light_agent_message_exchange(exchange: dict[str, Any]) -> dict[str, Any]:
    light: dict[str, Any] = {}
    for key in ("request", "reply"):
        message = exchange.get(key)
        if isinstance(message, dict):
            light[key] = _light_agent_message_dict(message)
    return light


def _light_agent_message_dict(message: dict[str, Any]) -> dict[str, Any]:
    light = {
        key: copy.deepcopy(message.get(key))
        for key in ("message_id", "from_agent", "from_agent_id", "to_agent", "to_agent_id", "intent", "conversation_id", "reply_to", "requires_reply", "created_at")
        if key in message
    }
    if "content" in message:
        light["content"] = _tail_text(str(message.get("content") or ""), 1200)
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    if metadata:
        light["metadata"] = _light_nested_text_dict(metadata)
    value = message.get("tuple")
    if isinstance(value, list) and len(value) == 3:
        light["tuple"] = [str(value[0]), _tail_text(str(value[1]), 500), str(value[2])]
    return light


def _light_trace_step(step: dict[str, Any]) -> dict[str, Any]:
    light = {
        key: copy.deepcopy(step.get(key))
        for key in ("stage", "status")
        if key in step
    }
    detail = step.get("detail") if isinstance(step.get("detail"), dict) else {}
    if detail:
        compact: dict[str, Any] = {}
        for key in ("tool", "reason", "goal", "job_id", "status", "transport", "from_agent", "to_agent", "collection_target", "delivery_target", "requested_by"):
            if key in detail:
                value = detail.get(key)
                compact[key] = _tail_text(value, 700) if isinstance(value, str) else copy.deepcopy(value)
        if "tuple" in detail and isinstance(detail.get("tuple"), list):
            compact["tuple"] = copy.deepcopy(detail.get("tuple"))
        light["detail"] = compact
    return light


def _job_from_agent_response(response: dict[str, Any]) -> Job | None:
    raw = response.get("job") if isinstance(response, dict) else None
    if not isinstance(raw, dict):
        return None
    job_id = str(raw.get("id") or "")
    with JOBS_LOCK:
        _restore_jobs_locked()
        if job_id and job_id in JOBS:
            return JOBS[job_id]
    try:
        allowed = {field.name for field in fields(Job)}
        return Job(**{key: value for key, value in raw.items() if key in allowed})
    except Exception:
        return None


def _user_message_fields(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "from_agent": "User",
        "content": str(payload.get("content") or payload.get("message") or payload.get("question") or ""),
        "to_agent": str(payload.get("to_agent") or payload.get("to") or payload.get("agent") or "MCagent"),
        "intent": str(payload.get("intent") or "user_chat"),
        "conversation_id": str(payload.get("session_id") or payload.get("conversation_id") or ""),
        "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
    }


def _job_readable_summary(job: dict[str, Any], *, refresh_manifest: bool = True) -> dict[str, Any]:
    if refresh_manifest:
        _refresh_job_manifest_stats_for_display(job)
    return JobReadableViewService(source_label=_source_label).build(job)


def _refresh_job_manifest_stats_for_display(job: dict[str, Any]) -> None:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    tasks = result.get("tasks") if isinstance(result.get("tasks"), list) else []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        export_dir = str(task.get("export_dir") or "")
        if not export_dir:
            continue
        stats = task.get("manifest_stats") if isinstance(task.get("manifest_stats"), dict) else {}
        refreshed = _crawler_manifest_stats(export_dir)
        if refreshed.get("manifest_path") or refreshed.get("records") or refreshed.get("skipped") or refreshed.get("errors"):
            task["manifest_stats"] = {**stats, **refreshed}
            task["observation"] = classify_crawler_tool_result(task).to_dict()


def _sanitize_job_planned_tasks_for_display(job: dict[str, Any]) -> None:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    planned = result.get("planned_tasks") if isinstance(result.get("planned_tasks"), list) else []
    if not planned:
        return
    displayable, blocked = CrawlerTaskMaterializationService().split_displayable_planned_tasks(planned)
    if len(displayable) == len(planned) and not blocked:
        return
    result["planned_tasks"] = displayable
    existing = result.get("blocked_planned_tasks") if isinstance(result.get("blocked_planned_tasks"), list) else []
    result["blocked_planned_tasks"] = [*existing, *blocked]


def _persist_jobs_locked() -> None:
    try:
        JOBS_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "jobs_order": JOBS_ORDER[:MAX_JOBS],
            "jobs": [_job_light_snapshot(JOBS[job_id]) for job_id in JOBS_ORDER if job_id in JOBS],
        }
        tmp = JOBS_HISTORY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, default=_json_default, indent=2), encoding="utf-8")
        tmp.replace(JOBS_HISTORY_PATH)
    except Exception:
        return


def _restore_jobs_locked() -> None:
    if JOBS or not JOBS_HISTORY_PATH.exists():
        return
    try:
        payload = json.loads(JOBS_HISTORY_PATH.read_text(encoding="utf-8"))
        items = payload.get("jobs") if isinstance(payload, dict) else []
        if not isinstance(items, list):
            return
        for item in items[:MAX_JOBS]:
            if not isinstance(item, dict) or not item.get("id") or not item.get("kind"):
                continue
            allowed = {field.name for field in fields(Job)}
            job = Job(**{key: value for key, value in item.items() if key in allowed})
            if job.status in {"queued", "running"}:
                job.status = "stopped"
                job.ended_at = job.ended_at or time.time()
                job.summary = job.summary or "服务重启后恢复历史记录；原任务已不再运行。"
            JOBS[job.id] = job
            JOBS_ORDER.append(job.id)
    except Exception:
        JOBS.clear()
        JOBS_ORDER.clear()


def _refresh_job_ingest_state(job: Job) -> None:
    if not isinstance(job.result, dict):
        return
    if not job.result.get("ingest_background") or job.result.get("ingest") or job.result.get("ingest_error"):
        return
    if job.ended_at is None or time.time() - float(job.ended_at) < 20:
        return
    export_dirs = [
        str(result.get("export_dir") or "")
        for result in job.result.get("tasks") or []
        if isinstance(result, dict) and result.get("ingest_deferred") and result.get("export_dir")
    ]
    if not export_dirs:
        return
    config = load_config()
    try:
        conn = connect(config.paths.db_path)
        try:
            imported = 0
            for export_dir in export_dirs:
                like = str(Path(export_dir)) + "%"
                imported += int(conn.execute("SELECT COUNT(*) FROM documents WHERE source_path LIKE ?", (like,)).fetchone()[0])
            total_docs, total_chunks = count_rows(conn)
        finally:
            conn.close()
    except Exception:
        return
    if imported <= 0:
        return
    job.result["ingest"] = {
        "stats": {
            "documents_loaded": imported,
            "total_documents": total_docs,
            "total_chunks": total_chunks,
        },
        "recovered_from_database": True,
        "export_dirs": export_dirs,
    }
    job.result["ingest_error"] = ""
    for item in job.result.get("loop") or []:
        if isinstance(item, dict) and item.get("phase") == "ingest":
            item["status"] = "done"
            item["note"] = "Background ingest finished; status recovered from database."
    if "已启动后台入库" in job.summary:
        job.summary = job.summary.replace("已启动后台入库。", "后台入库已完成。")


def _jobs_payload() -> dict[str, Any]:
    with JOBS_LOCK:
        _restore_jobs_locked()
        jobs = [_job_light_snapshot(JOBS[job_id]) for job_id in JOBS_ORDER if job_id in JOBS]
    for job in jobs:
        _sanitize_job_planned_tasks_for_display(job)
        job["readable"] = _job_readable_summary(job, refresh_manifest=False)
    return {"jobs": jobs}


def _running_job(kind: str, reuse_predicate: Any | None = None) -> Job | None:
    for job in JOBS.values():
        if job.kind == kind and job.status in {"queued", "running"} and not job.stop_requested:
            if reuse_predicate is not None and not reuse_predicate(job):
                continue
            return job
    return None


def _append_job(job: Job) -> None:
    JOBS[job.id] = job
    JOBS_ORDER.insert(0, job.id)
    del JOBS_ORDER[MAX_JOBS:]
    for stale_id in list(JOBS):
        if stale_id not in JOBS_ORDER:
            JOBS.pop(stale_id, None)
    _persist_jobs_locked()


def _update_job(job: Job, **changes: Any) -> None:
    with JOBS_LOCK:
        for key, value in changes.items():
            setattr(job, key, value)
        _persist_jobs_locked()


def _start_job(kind: str, title: str, target: Any, *, reuse_predicate: Any | None = None, initial_result: dict[str, Any] | None = None) -> tuple[Job, bool]:
    with JOBS_LOCK:
        running = _running_job(kind, reuse_predicate=reuse_predicate)
        if running:
            return running, False
        job = Job(id=f"{int(time.time() * 1000)}-1", kind=kind, title=title)
        if initial_result is not None:
            job.result = initial_result
        _append_job(job)
    thread = threading.Thread(target=target, args=(job,), daemon=True, name=f"mcagent-{kind}")
    thread.start()
    return job, True


def _crawler_job_reuse_tokens(text: str) -> set[str]:
    noise = {
        "crawler",
        "crawleragent",
        "mcagent",
        "rag",
        "from",
        "content",
        "to",
        "user",
        "human",
        "save",
        "saved",
        "collect",
        "collection",
        "source",
        "sources",
        "资料",
        "采集",
        "保存",
        "补充",
        "公开",
        "来源",
        "内容",
        "任务",
        "可引用",
        "判断",
        "本地",
        "确认",
    }
    tokens = set()
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_+.-]{2,}|[\u4e00-\u9fff]{2,12}", text.lower()):
        if token in noise:
            continue
        if token.endswith("agent"):
            continue
        tokens.add(token)
    return tokens


def _crawler_job_reuse_signature(
    *,
    question: str,
    delivery_target: str,
    requested_by: str,
    source: str,
    session_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = session_summary if isinstance(session_summary, dict) else {}
    text_parts = [
        question,
        str(summary.get("collection_target") or ""),
        str(summary.get("task_goal") or ""),
        str(summary.get("authoritative_task_goal") or ""),
        str(summary.get("current_topic") or ""),
    ]
    tokens = sorted(_crawler_job_reuse_tokens(" ".join(text_parts)))
    return {
        "question": question,
        "tokens": tokens,
        "delivery_target": delivery_target,
        "requested_by": requested_by,
        "source": _source_alias(source or "planner"),
    }


def _crawler_job_signatures_match(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
    existing_tokens = set(str(item).lower() for item in existing.get("tokens") or [])
    incoming_tokens = set(str(item).lower() for item in incoming.get("tokens") or [])
    if not existing_tokens or not incoming_tokens:
        return False
    overlap = len(existing_tokens & incoming_tokens)
    smaller = max(1, min(len(existing_tokens), len(incoming_tokens)))
    union = max(1, len(existing_tokens | incoming_tokens))
    if str(existing.get("delivery_target") or "").lower() != str(incoming.get("delivery_target") or "").lower():
        return False
    if str(existing.get("requested_by") or "").lower() != str(incoming.get("requested_by") or "").lower():
        return False
    return overlap >= 3 and (overlap / smaller >= 0.6 or overlap / union >= 0.45)


def _crawler_job_reuse_candidate(job: Job, incoming_signature: dict[str, Any]) -> bool:
    result = job.result if isinstance(job.result, dict) else {}
    existing = result.get("reuse_signature") if isinstance(result.get("reuse_signature"), dict) else {}
    if not existing:
        plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
        existing = _crawler_job_reuse_signature(
            question=str(plan.get("question") or plan.get("topic") or job.title or ""),
            delivery_target=str(plan.get("delivery_target") or ""),
            requested_by=str(result.get("requested_by") or ""),
            source=str(result.get("source") or "planner"),
        )
    return _crawler_job_signatures_match(existing, incoming_signature)


def _request_job_stop(job_id: str) -> Job | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        job.stop_requested = True
        job.stop_requested_at = time.time()
        pid = job.current_pid
    if pid:
        _terminate_process_tree(pid)
    return job


def _terminate_process_tree(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except OSError:
            pass
        return
    try:
        os.kill(pid, 15)
    except OSError:
        pass


def _tail_text(text: str, limit: int = 4000) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[-limit:]


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}


def _latest_growth_log() -> Path | None:
    candidates = [path for path in GROW_LOG_CANDIDATES if path.exists()]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime)


def _growth_progress_from_log() -> dict[str, Any]:
    log_path = _latest_growth_log()
    if not log_path:
        return {}
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    recent = lines[-240:]
    cycle = 0
    cycle_before_mb = 0.0
    after_mb = 0.0
    added_mb = 0.0
    commands_completed = 0
    current_command = ""
    current_topic = ""
    status = "unknown"
    stopped_reason = ""
    for line in recent:
        cycle_match = re.search(r"cycle=(\d+)\s+before_mb=([0-9.]+)", line)
        if cycle_match:
            cycle = int(cycle_match.group(1))
            cycle_before_mb = float(cycle_match.group(2))
            commands_completed = 0
            status = "running"
        run_match = re.search(r"\]\s+RUN\s+(.+)$", line)
        if run_match:
            current_command = run_match.group(1)
            status = "ingesting" if current_command.endswith("ingest.py") else "running"
            query_match = re.search(r"--query\s+(.+?)(?:\s+--|$)", current_command)
            current_topic = query_match.group(1).strip().strip('"') if query_match else current_topic
        if "] DONE " in line:
            commands_completed += 1
            current_command = ""
        after_match = re.search(r"cycle=(\d+)\s+after_mb=([0-9.]+)\s+added_mb=([0-9.]+)", line)
        if after_match:
            cycle = int(after_match.group(1))
            after_mb = float(after_match.group(2))
            added_mb = float(after_match.group(3))
        if "Stopping early:" in line:
            stopped_reason = "low_yield"
            status = "stopping"
        if line.startswith("Report:") or "Final size:" in line:
            status = "finished"
    return {
        "status": status,
        "source": "log",
        "log_path": str(log_path),
        "cycle": cycle,
        "cycles_total": 16 if "broad" in log_path.name else 4,
        "target_bytes": 768 * 1024 * 1024 if "broad" in log_path.name else 512 * 1024 * 1024,
        "cycle_before_mb": cycle_before_mb,
        "current_mb": after_mb or cycle_before_mb,
        "added_mb": added_mb,
        "commands_completed": commands_completed,
        "commands_total": 5 if "broad" in log_path.name else 24,
        "current_command": current_command,
        "current_topic": current_topic,
        "stopped_reason": stopped_reason,
        "updated_at": datetime.fromtimestamp(log_path.stat().st_mtime).isoformat(timespec="seconds"),
    }


def _external_crawler_processes() -> list[dict[str, Any]]:
    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -like '*grow_knowledge_base.py*' -or $_.CommandLine -like '*grow_closing_song_knowledge.py*' -or $_.CommandLine -like '*fetch_*seed.py*' -or $_.CommandLine -like '*ingest.py*' } | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except Exception:
        return []
    text = (completed.stdout or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    items = data if isinstance(data, list) else [data]
    output: list[dict[str, Any]] = []
    for item in items:
        command_line = str(item.get("CommandLine") or "")
        if "Get-CimInstance Win32_Process" in command_line:
            continue
        output.append({"pid": item.get("ProcessId"), "name": item.get("Name"), "command": command_line})
    return output


def _crawler_progress_payload(source_dir: Path) -> dict[str, Any]:
    progress = _read_json_file(GROW_PROGRESS_PATH)
    if progress:
        progress = {"source": "progress_file", **progress}
    else:
        progress = _growth_progress_from_log()
    processes = _external_crawler_processes()
    grow_processes = [
        item
        for item in processes
        if "grow_knowledge_base.py" in str(item.get("command") or "")
        or "grow_closing_song_knowledge.py" in str(item.get("command") or "")
    ]
    progress_finished = str(progress.get("status") or "").lower() == "finished"
    if grow_processes and progress and progress.get("status") in {"finished", "unknown", ""}:
        progress["status"] = "running"
    progress["processes"] = processes
    progress["batch_processes"] = grow_processes
    progress["other_processes"] = [item for item in processes if item not in grow_processes]
    current_bytes = int(progress.get("current_bytes") or 0)
    if current_bytes <= 0:
        try:
            sources = _source_status_payload(source_dir)
            current_bytes = int(sources.get("total_bytes") or 0)
        except OSError:
            current_bytes = 0
    progress.setdefault("current_bytes", current_bytes)
    progress["current_mb"] = round(float(progress.get("current_bytes") or current_bytes) / 1024 / 1024, 2)
    target_bytes = float(progress.get("target_bytes") or 0)
    progress["target_mb"] = round(target_bytes / 1024 / 1024, 2) if target_bytes else 0
    if target_bytes:
        progress["target_percent"] = max(0, min(100, round(float(progress.get("current_bytes") or current_bytes) / target_bytes * 100, 1)))
    cycle = int(progress.get("cycle") or 0)
    cycles_total = int(progress.get("cycles_total") or 0)
    command_total = int(progress.get("commands_total") or 0)
    command_done = int(progress.get("commands_completed") or 0)
    if cycles_total and command_total:
        progress["cycle_percent"] = max(0, min(100, round(((max(0, cycle - 1) + min(1, command_done / max(1, command_total))) / cycles_total) * 100, 1)))
    elif cycles_total:
        progress["cycle_percent"] = max(0, min(100, round(cycle / cycles_total * 100, 1)))
    else:
        progress["cycle_percent"] = 0
    finished_by_progress = progress_finished or (
        int(progress.get("cycles_total") or 0) > 0
        and int(progress.get("commands_total") or 0) > 0
        and float(progress.get("cycle_percent") or 0) >= 100
    )
    progress["active"] = bool(grow_processes) or (not finished_by_progress and str(progress.get("status") or "").lower() in {"running", "queued"})
    if finished_by_progress and not grow_processes:
        progress["status"] = "finished"
    return progress


def _source_status_payload(source_dir: Path) -> dict[str, Any]:
    now = time.time()
    key = str(source_dir)
    with SOURCE_STATUS_LOCK:
        cached = SOURCE_STATUS_CACHE.get("payload")
        if cached and SOURCE_STATUS_CACHE.get("source_dir") == key and now - float(SOURCE_STATUS_CACHE.get("time") or 0) < 60:
            return dict(cached)

    file_count = 0
    manifest_count = 0
    report_count = 0
    total_bytes = 0
    latest: list[dict[str, Any]] = []
    partial = False
    if source_dir.exists():
        latest_heap: list[tuple[float, str, int]] = []
        deadline = time.time() + 2.5
        for path in source_dir.rglob("*"):
            if time.time() > deadline:
                partial = True
                break
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            file_count += 1
            total_bytes += int(stat.st_size)
            if path.name.lower() == "manifest.json":
                manifest_count += 1
            if path.suffix.lower() == ".md" and "report" in path.name.lower():
                report_count += 1
            latest_heap.append((stat.st_mtime, str(path), stat.st_size))
            latest_heap.sort(key=lambda item: item[0], reverse=True)
            del latest_heap[12:]
        latest = [{"path": path, "size": size, "mtime": mtime} for mtime, path, size in latest_heap]

    payload = {
        "source_dir": str(source_dir),
        "files": file_count,
        "manifests": manifest_count,
        "reports": report_count,
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / 1024 / 1024, 2),
        "latest_files": latest,
        "partial": partial,
    }
    with SOURCE_STATUS_LOCK:
        SOURCE_STATUS_CACHE.update({"time": now, "source_dir": key, "payload": dict(payload)})
    return payload


def _export_dir_from_output(output: str) -> str:
    for line in output.splitlines():
        if line.lower().startswith("exported to:"):
            return line.split(":", 1)[1].strip()
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return ""
    if isinstance(data, dict) and data.get("export_dir"):
        return str(data.get("export_dir") or "").strip()
    return ""


def _refresh_knowledge_map() -> dict[str, Any]:
    script = PROJECT_ROOT / "scripts" / "build_knowledge_map.py"
    if not script.exists():
        return {"updated": False, "error": "build_knowledge_map.py not found"}
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(PROJECT_ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
        check=False,
    )
    return {"updated": completed.returncode == 0, "returncode": completed.returncode, "output": _tail_text(completed.stdout or "", 1200)}


def _ingest_after_crawl(config: AppConfig, source_dirs: list[str | Path] | None = None) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(4):
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                if source_dirs is None:
                    stats = ingest_exports(config, rebuild_index=False)
                else:
                    allowed_roots = list(dict.fromkeys(Path(source_dir).resolve() for source_dir in source_dirs if Path(source_dir).exists()))
                    stats = ingest_exports(config, allowed_roots=allowed_roots) if allowed_roots else IngestStats()
            output = "\n".join(part for part in [stdout.getvalue(), stderr.getvalue()] if part)
            return {"stats": asdict(stats), "output": _tail_text(output, 1200), "knowledge_map": _refresh_knowledge_map(), "attempts": attempt + 1}
        except sqlite3.OperationalError as exc:
            last_error = exc
            if "locked" not in str(exc).lower() or attempt == 3:
                break
            time.sleep(1.5 * (attempt + 1))
    raise last_error or RuntimeError("ingest failed")


def _crawler_record_has_content(record: dict[str, Any]) -> bool:
    structured_keys = ("name", "price", "source", "fields", "rows", "value")
    if any(str(record.get(key) or "").strip() for key in structured_keys):
        return True
    try:
        byte_count = int(record.get("bytes") or 0) if record.get("bytes") is not None else None
    except (TypeError, ValueError):
        byte_count = None
    if byte_count is not None and byte_count > 0:
        return True
    try:
        if int(record.get("chars") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    path = Path(str(record.get("path") or ""))
    if path.is_file():
        try:
            return path.stat().st_size > 0
        except OSError:
            return False
    return False


def _crawler_accepted_ingest_roots(result: dict[str, Any]) -> list[str]:
    export_dir = Path(str(result.get("export_dir") or ""))
    validation = result.get("topic_validation") if isinstance(result.get("topic_validation"), dict) else {}
    matched_indexes = validation.get("matched_indexes")
    if not export_dir.exists() or not isinstance(matched_indexes, list):
        return []
    manifest = _read_json_file(export_dir / "manifest.json")
    records = manifest.get("records") if isinstance(manifest.get("records"), list) else []
    accepted_records: list[dict[str, Any]] = []
    for index in matched_indexes:
        try:
            record = records[int(index)]
        except (TypeError, ValueError, IndexError):
            continue
        if isinstance(record, dict) and _crawler_record_has_content(record):
            accepted_records.append(record)
    if not accepted_records:
        return []
    accepted_dir = export_dir / "accepted_by_crawler"
    accepted_dir.mkdir(parents=True, exist_ok=True)
    accepted_manifest = {
        "manifest_type": "crawler_accepted_records",
        "source_manifest": str(export_dir / "manifest.json"),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "records": [],
    }
    for record_index, record in enumerate(accepted_records):
        copied_record = dict(record)
        for field_name in ("path", "raw_html_path"):
            source_path = Path(str(record.get(field_name) or ""))
            if not source_path.exists() or not source_path.is_file():
                continue
            target_path = accepted_dir / source_path.name
            if source_path.resolve() != target_path.resolve():
                target_path.write_bytes(source_path.read_bytes())
            copied_record[field_name] = str(target_path)
        copied_record["accepted_index"] = record_index
        copied_record["crawler_acceptance"] = {
            "judge": "Crawler LLM",
            "reason": validation.get("reason"),
            "source_record_index": record.get("index"),
            "note": "Only records explicitly selected by CrawlerAgent are mirrored here for RAG ingest.",
        }
        accepted_manifest["records"].append(copied_record)
    (accepted_dir / "manifest.json").write_text(json.dumps(accepted_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return [str(accepted_dir)]


def _run_background_ingest(job_id: str, config: AppConfig, accepted_export_dirs: list[str] | None = None) -> None:
    with INGEST_LOCK:
        try:
            result = _ingest_after_crawl(config, source_dirs=accepted_export_dirs)
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if job and isinstance(job.result, dict):
                    job.result["ingest"] = result
                    job.result["ingest_error"] = ""
                    for item in job.result.get("loop") or []:
                        if isinstance(item, dict) and item.get("phase") == "ingest":
                            item["status"] = "done"
                            item["note"] = "Background ingest finished."
            append_memory_event("crawler_background_ingest_completed", {"job_id": job_id, "ingest": result})
        except Exception as exc:  # noqa: BLE001
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if job and isinstance(job.result, dict):
                    job.result["ingest_error"] = f"{type(exc).__name__}: {exc}"
                    for item in job.result.get("loop") or []:
                        if isinstance(item, dict) and item.get("phase") == "ingest":
                            item["status"] = "failed"
                            item["note"] = f"Background ingest failed: {type(exc).__name__}: {exc}"


def _source_alias(source: str) -> str:
    source = source.lower()
    aliases = {
        "minecraft_wiki": "mediawiki",
        "wiki": "mediawiki",
        "modrinth_api": "modrinth",
        "modrinth_agent": "modrinth",
        "autofill": "modrinth",
        "mcmod_cn": "mcmod",
        "mcmod_search": "mcmod",
        "mcwiki_cn": "mcmod",
        "ftb_wiki": "ftbwiki",
        "mod_wiki": "ftbwiki",
        "create_wiki": "createwiki",
        "docs": "followup",
        "deep_followup": "followup",
        "modrinth_followup": "followup",
        "search": "web_discovery",
        "public_search": "web_discovery",
        "reader": "fetch_url",
        "http_fetch": "fetch_url",
        "url_fetch": "fetch_url",
        "fetch_url": "fetch_url",
        "browser": "playwright",
        "browser_extract": "playwright",
        "browser_collect": "browser_collect",
        "browser_structured": "browser_collect",
        "structured_browser": "browser_collect",
        "product_collect": "browser_collect",
        "save": "save_artifact",
        "artifact": "save_artifact",
        "save_artifact": "save_artifact",
        "read": "read_local_file",
        "read_file": "read_local_file",
        "local_file_read": "read_local_file",
        "read_local_file": "read_local_file",
        "grep": "search_local_files",
        "search_files": "search_local_files",
        "local_file_search": "search_local_files",
        "search_local_files": "search_local_files",
        "mcagent_context": "mcagent_context",
        "ask_mcagent": "mcagent_context",
        "rag_context": "mcagent_context",
        "local_rag_context": "mcagent_context",
        "modpack_download": "modpack_download",
        "pack_download": "modpack_download",
        "archive_download": "modpack_download",
        "pack_internal": "modpack_internal",
        "modpack_archive": "modpack_internal",
        "modpack_internal": "modpack_internal",
        "topic_discovery": "topic_discovery",
        "discovery": "topic_discovery",
    }
    return aliases.get(source, source)


def _source_label(source: str) -> str:
    return {
        "mediawiki": "Minecraft Wiki API",
        "modrinth": "Modrinth API",
        "mcmod": "MC百科搜索",
        "ftbwiki": "FTB Wiki API",
        "createwiki": "Create Wiki API",
        "followup": "公开项目文档跟进",
        "web_discovery": "公开搜索发现",
        "fetch_url": "本地 URL 抓取/正文提取",
        "playwright": "Playwright 浏览器采集",
        "browser_collect": "浏览器结构化采集",
        "mcagent_context": "MCagent/RAG 上下文",
        "read_local_file": "Local file read",
        "search_local_files": "Local file search",
        "modpack_download": "整合包包体发现/下载",
        "modpack_internal": "整合包内部解析",
        "topic_discovery": "主题种子发现",
        "planner": "多源补库计划",
    }.get(_source_alias(source), source)


def _save_dir_hint(source: str) -> str:
    return {
        "mediawiki": r"D:\magic\MC_Agent\data\crawler_exports\mediawiki\...",
        "modrinth": r"D:\magic\MC_Agent\data\crawler_exports\modrinth_agent\...",
        "mcmod": r"D:\magic\MC_Agent\data\crawler_exports\mcmod\...",
        "ftbwiki": r"D:\magic\MC_Agent\data\crawler_exports\ftbwiki\...",
        "createwiki": r"D:\magic\MC_Agent\data\crawler_exports\createwiki\...",
        "followup": r"D:\magic\MC_Agent\data\crawler_exports\followup\...",
        "web_discovery": r"D:\magic\MC_Agent\data\crawler_exports\web_discovery\...",
        "fetch_url": r"D:\magic\MC_Agent\data\crawler_exports\fetch_url\...",
        "playwright": r"D:\magic\MC_Agent\data\crawler_exports\playwright\...",
        "browser_collect": r"用户指定目录，或 D:\magic\MC_Agent\data\crawler_exports\browser_collect\...",
        "mcagent_context": r"D:\magic\MC_Agent\data\crawler_exports\mcagent_context\...",
        "read_local_file": r"D:\magic\MC_Agent\data\crawler_exports\local_file_read\...",
        "search_local_files": r"D:\magic\MC_Agent\data\crawler_exports\local_file_search\...",
        "modpack_download": r"D:\magic\MC_Agent\data\crawler_exports\modpack_download\...",
        "modpack_internal": r"D:\magic\MC_Agent\data\crawler_exports\manual_research\...",
        "topic_discovery": r"D:\magic\MC_Agent\data\crawler_exports\topic_discovery\...",
    }.get(_source_alias(source), r"D:\magic\MC_Agent\data\crawler_exports\...")


def _looks_like_archive_url(value: str) -> bool:
    match = re.search(r"https?://[^\s<>'\"]+", str(value or ""), flags=re.I)
    if not match:
        return False
    path = urllib.parse.urlparse(match.group(0).rstrip(".,;:)")).path.lower()
    return path.endswith((".mrpack", ".zip"))


def _modpack_archive_for_query(query: str) -> str:
    archive_root = PROJECT_ROOT / "data" / "manual_research"
    archives = list(archive_root.glob("**/pack_archive/*.zip")) + list(archive_root.glob("**/pack_archive/*.mrpack"))
    archives = [archive for archive in archives if _is_readable_zip_archive(archive)]
    if not archives:
        return ""
    query_text = str(query or "")
    generic_tokens = {
        "minecraft",
        "modpack",
        "mod",
        "pack",
        "data",
        "guide",
        "整合包",
        "资料",
        "数据",
        "完整资料",
        "完整数据",
        "模组",
        "项目页",
        "下载页",
        "任务线",
        "核心玩法",
        "列表",
        "内容",
    }
    ascii_tokens = {
        token
        for token in re.sub(r"[^a-z0-9]+", " ", query_text.lower()).split()
        if len(token) >= 3 and token not in generic_tokens
    }
    cjk_tokens = {
        token
        for token in re.findall(r"[\u4e00-\u9fff]{2,}", query_text)
        if token not in generic_tokens
    }
    query_tokens = ascii_tokens | cjk_tokens
    if not query_tokens:
        return ""
    meaningful_tokens = {
        token for token in query_tokens if token.lower() not in {"archive", "pack", "modpack", "zip", "mrpack", "such", "definitely", "download"}
    }
    if not meaningful_tokens:
        return ""
    scored: list[tuple[int, Path]] = []
    for archive in archives:
        parent = archive.parent.parent
        sidecar_text = ""
        for sidecar in list(parent.glob("*.md"))[:8] + list(parent.glob("*.json"))[:8]:
            try:
                sidecar_text += "\n" + sidecar.read_text(encoding="utf-8", errors="ignore")[:4000]
            except OSError:
                continue
        haystack = (str(archive) + "\n" + sidecar_text).lower()
        normalized_haystack = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", haystack)
        score = sum(1 for token in meaningful_tokens if token.lower() in normalized_haystack)
        scored.append((score, archive))
    scored.sort(key=lambda item: item[0], reverse=True)
    if scored and scored[0][0] > 0:
        return str(scored[0][1])
    return ""


def _is_readable_zip_archive(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as zipped:
            return bool(zipped.namelist())
    except (OSError, zipfile.BadZipFile):
        return False


def _round_command(source: str, payload: dict[str, Any]) -> list[str]:
    source = _source_alias(source)
    query = str(payload.get("query") or payload.get("question") or "").strip()
    if source == "ftbwiki":
        return [sys.executable, str(PROJECT_ROOT / "scripts" / "fetch_ftbwiki_seed.py"), "--query", query, "--search-limit", str(int(payload.get("search_limit") or 12))]
    if source == "createwiki":
        return [sys.executable, str(PROJECT_ROOT / "scripts" / "fetch_createwiki_seed.py"), "--query", query, "--search-limit", str(int(payload.get("search_limit") or 12))]
    if source == "followup":
        command = [sys.executable, str(PROJECT_ROOT / "scripts" / "fetch_followup_seed.py"), "--max-urls", str(int(payload.get("max_urls") or 60))]
        if query:
            command.extend(["--query", query])
        return command
    if source == "web_discovery":
        return [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_web_discovery_seed.py"),
            "--query",
            query,
            "--max-results",
            str(min(max(1, int(payload.get("search_limit") or 4)), 4)),
            "--max-pages",
            str(min(max(1, int(payload.get("max_urls") or 3)), 3)),
            "--max-variants",
            str(min(max(1, int(payload.get("max_variants") or 3)), 3)),
            "--request-timeout",
            str(min(max(1, int(payload.get("request_timeout") or 8)), 10)),
            "--budget-seconds",
            str(min(max(10, int(payload.get("budget_seconds") or 60)), 90)),
        ]
    if source == "fetch_url":
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_url_seed.py"),
            "--query",
            query,
        ]
        timeout = str(payload.get("timeout") or payload.get("timeout_ms") or "").strip()
        user_agent = str(payload.get("user_agent") or "").strip()
        if timeout:
            try:
                seconds = max(1, int(int(timeout) / 1000)) if int(timeout) > 1000 else int(timeout)
                command.extend(["--timeout", str(seconds)])
            except ValueError:
                pass
        if user_agent:
            command.extend(["--user-agent", user_agent])
        return command
    if source == "playwright":
        return [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_playwright_seed.py"),
            "--query",
            query,
            "--max-results",
            str(min(max(1, int(payload.get("search_limit") or 3)), 3)),
            "--max-pages",
            str(min(max(1, int(payload.get("max_urls") or 2)), 2)),
        ]
    if source == "browser_collect":
        source_context = " ".join(
            str(payload.get(key) or "")
            for key in ("question", "source_question", "collection_target", "target", "topic")
        )
        collect_query = query
        taobao_pattern = r"淘宝|(?<![A-Za-z0-9_-])taobao(?![A-Za-z0-9_-])"
        if not re.search(taobao_pattern, collect_query, flags=re.I) and re.search(taobao_pattern, source_context, flags=re.I):
            collect_query = f"taobao {collect_query}".strip()
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "browser_collect_seed.py"),
            "--query",
            collect_query,
            "--max-items",
            str(int(payload.get("max_items") or 50)),
        ]
        output_dir = str(payload.get("output_dir") or "").strip()
        start_url = str(payload.get("start_url") or "").strip()
        timeout_ms = str(payload.get("timeout_ms") or "").strip()
        if output_dir:
            command.extend(["--output-dir", output_dir])
        if start_url:
            command.extend(["--start-url", start_url])
        if timeout_ms:
            command.extend(["--timeout-ms", timeout_ms])
        return command
    if source == "save_artifact":
        if payload.get("content_ref_error"):
            message = {
                "provider": "save_artifact",
                "status": "failed",
                "saved_to_local": False,
                "failure_reason": str(payload.get("content_ref_error") or ""),
            }
            return [sys.executable, "-c", "import json, sys; print(json.dumps(" + repr(message) + ", ensure_ascii=False, indent=2)); sys.exit(1)"]
        runtime_dir = PROJECT_ROOT / "runtime" / "tool_payloads"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        payload_path = runtime_dir / f"save_artifact_{uuid.uuid4().hex}.json"
        artifact_payload = {
            "content": payload.get("content", ""),
            "format": payload.get("format") or payload.get("artifact_format") or "txt",
            "path": payload.get("path") or payload.get("output_path") or payload.get("output_dir") or "",
            "filename": payload.get("filename") or "",
            "overwrite": bool(payload.get("overwrite")),
            "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        }
        payload_path.write_text(json.dumps(artifact_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "save_artifact.py"),
            "--payload",
            str(payload_path),
        ]
    if source == "read_local_file":
        path = str(payload.get("path") or payload.get("file") or payload.get("query") or "").strip()
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "read_local_file.py"),
            "--path",
            path,
        ]
        max_chars = str(payload.get("max_chars") or "").strip()
        if max_chars:
            command.extend(["--max-chars", max_chars])
        return command
    if source == "search_local_files":
        path = str(payload.get("path") or payload.get("root") or "").strip()
        search_query = str(payload.get("search_query") or payload.get("pattern") or query).strip()
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "search_local_files.py"),
            "--path",
            path,
            "--query",
            search_query,
        ]
        max_files = str(payload.get("max_files") or payload.get("search_limit") or "").strip()
        if max_files:
            command.extend(["--max-files", max_files])
        return command
    if source == "topic_discovery":
        return [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "discover_topic_seeds.py"),
            "--query",
            query,
            "--max-files",
            str(int(payload.get("max_files") or 120)),
            "--max-queries",
            str(int(payload.get("max_queries") or 40)),
        ]
    if source == "modpack_download":
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_modpack_archive_seed.py"),
            "--query",
            query,
            "--limit",
            str(int(payload.get("search_limit") or payload.get("max_urls") or 8)),
        ]
        wants_download = payload.get("download") is True or str(payload.get("download") or "").strip().lower() in {"1", "true", "yes", "on", "full"}
        if payload.get("no_download") is True or payload.get("probe_only") is True:
            wants_download = False
        if not wants_download:
            command.append("--no-download")
            command.append("--quick-probe")
        return command
    if source == "modpack_internal":
        archive = str(
            payload.get("zip")
            or payload.get("archive")
            or payload.get("archive_path")
            or payload.get("path")
            or _modpack_archive_for_query(query)
            or ""
        ).strip()
        if not archive:
            message = {
                "provider": "modpack_internal",
                "archive_found": False,
                "failure_reason": "No matching local modpack archive was found for this query, and no zip/archive_path/path was provided.",
                "next_action": "CrawlerAgent should decide whether to run modpack_download, inspect project/download pages, use a discovered direct archive URL, or report that no public archive is accessible.",
                "message": "No matching local modpack archive was found. CrawlerAgent should decide whether to search project pages, Modrinth/CurseForge, public download sources, or ask for an archive.",
            }
            return [sys.executable, "-c", "import json, sys; print(json.dumps(" + repr(message) + ", ensure_ascii=False, indent=2)); sys.exit(2)"]
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "extract_modpack_internals.py"),
            "--zip",
            archive,
        ]
        return command
    if source == "mcmod":
        return [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_mcmod_seed.py"),
            "--query",
            query,
            "--limit",
            str(min(max(1, int(payload.get("search_limit") or 4)), 6)),
        ]
    if source == "modrinth":
        include_flag = payload.get("include_modpack_contents")
        include_modpack_contents = include_flag is True or str(include_flag or "").strip().lower() in {"1", "true", "yes", "on"}
        if not include_modpack_contents:
            reason_text = " ".join(str(payload.get(key) or "") for key in ("reason", "query", "collection_target", "task_goal"))
            include_modpack_contents = bool(
                re.search(r"整合包|modpack", reason_text, flags=re.I)
                and re.search(r"\.mrpack|manifest|modlist|mod list|包体|内部|完整模组列表|included mods|pack contents", reason_text, flags=re.I)
            )
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_modrinth_seed.py"),
            "--query",
            query,
            "--mods",
            str(int(payload.get("mods") or 60)),
            "--modpacks",
            str(int(payload.get("modpacks") or 20)),
            "--resourcepacks",
            str(int(payload.get("resourcepacks") or 10)),
            "--shaders",
            str(int(payload.get("shaders") or 8)),
        ]
        if include_modpack_contents:
            command.append("--include-modpack-contents")
        return command
    return [sys.executable, str(PROJECT_ROOT / "scripts" / "fetch_mediawiki_seed.py"), "--query", query, "--search-limit", str(int(payload.get("search_limit") or 12))]


def _command_timeout(source: str) -> int:
    source = _source_alias(source)
    if source == "modpack_download":
        return 420
    if source == "mcmod":
        return 90
    if source == "web_discovery":
        return 120
    if source == "playwright":
        return 150
    if source == "browser_collect":
        return 150
    if source in {"followup", "fetch_url", "save_artifact", "read_local_file", "search_local_files"}:
        return 360
    if source == "modrinth":
        return 240
    if source == "topic_discovery":
        return 180
    if source == "modpack_internal":
        return 300
    return 240


def _run_crawler_command(command: list[str], source: str, job: Job | None = None) -> dict[str, Any]:
    timeout = _command_timeout(source)
    started_at = time.time()
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if job is not None:
            _update_job(job, current_pid=process.pid)
        while True:
            if process.poll() is not None:
                stdout, _ = process.communicate()
                output = _tail_text(stdout or "")
                returncode = process.returncode
                break
            if job is not None and job.stop_requested:
                _terminate_process_tree(process.pid)
                try:
                    stdout, _ = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, _ = process.communicate(timeout=5)
                return {
                    "source": _source_alias(source),
                    "returncode": 130,
                    "command": command,
                    "output": _tail_text(stdout or "Crawler task stopped by user."),
                    "timeout_seconds": timeout,
                    "timed_out": False,
                    "stopped": True,
                    "export_dir": _export_dir_from_output(stdout or ""),
                }
            if time.time() - started_at > timeout:
                _terminate_process_tree(process.pid)
                try:
                    stdout, _ = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, _ = process.communicate(timeout=5)
                output = _tail_text(stdout or "")
                returncode = 124
                break
            time.sleep(0.5)
    except subprocess.TimeoutExpired as exc:
        output = _tail_text(str(exc.stdout or "") + "\n" + str(exc.stderr or ""))
        returncode = 124
    finally:
        if job is not None and process is not None and job.current_pid == process.pid:
            _update_job(job, current_pid=None)
    return {
        "source": _source_alias(source),
        "returncode": returncode,
        "command": command,
        "output": output,
        "timeout_seconds": timeout,
        "timed_out": returncode == 124,
        "export_dir": _export_dir_from_output(output),
    }


def _crawler_requested_output_dir(payload: dict[str, Any], plan: dict[str, Any]) -> str:
    for value in (
        payload.get("output_dir"),
        payload.get("path"),
        plan.get("output_dir") if isinstance(plan, dict) else "",
    ):
        if str(value or "").strip():
            return str(value or "").strip()
    summary = payload.get("session_summary") if isinstance(payload.get("session_summary"), dict) else {}
    return CrawlerTaskPreparationService._extract_windows_path(
        "\n".join(
            str(item or "")
            for item in (
                payload.get("original_user_request"),
                payload.get("source_question"),
                payload.get("question"),
                payload.get("query"),
                summary.get("original_user_message"),
                summary.get("collection_target"),
                summary.get("task_goal"),
            )
        )
    )


def _read_manifest_records(export_dir: str) -> list[dict[str, Any]]:
    manifest_path = Path(str(export_dir or "")) / "manifest.json"
    data = _read_json_file(manifest_path)
    records = data.get("records") if isinstance(data.get("records"), list) else []
    output: list[dict[str, Any]] = []
    for item in records:
        if isinstance(item, dict):
            output.append(item)
    return output


def _export_crawler_user_delivery(*, payload: dict[str, Any], plan: dict[str, Any], task_results: list[dict[str, Any]], collection_summary: dict[str, Any]) -> dict[str, Any]:
    output_dir = _crawler_requested_output_dir(payload, plan)
    if not output_dir:
        return {}
    rows: list[dict[str, Any]] = []
    for result in task_results:
        export_dir = str(result.get("export_dir") or "")
        for record in _read_manifest_records(export_dir):
            rows.append(
                {
                    "source": result.get("source"),
                    "query": result.get("query"),
                    "title": record.get("title") or record.get("name") or "",
                    "url": record.get("url") or record.get("source_url") or "",
                    "path": record.get("path") or record.get("markdown_path") or "",
                    "format": record.get("format") or "",
                    "bytes": record.get("bytes") or record.get("chars") or "",
                    "export_dir": export_dir,
                }
            )
    if not rows:
        return {}
    md_lines = [
        "# Crawler Collection Delivery",
        "",
        f"- Target: {plan.get('topic') or plan.get('question') or payload.get('question') or ''}",
        f"- Records: {len(rows)}",
        f"- Success count: {collection_summary.get('success_count') if isinstance(collection_summary, dict) else ''}",
        "",
        "## Records",
    ]
    for index, row in enumerate(rows, start=1):
        md_lines.extend(
            [
                "",
                f"### {index}. {row.get('title') or row.get('url') or row.get('path') or row.get('query')}",
                f"- Source: {row.get('source')}",
                f"- Query: {row.get('query')}",
                f"- URL: {row.get('url')}",
                f"- Local path: {row.get('path')}",
            ]
        )
    content = {
        "target": plan.get("topic") or plan.get("question") or payload.get("question") or "",
        "collection_summary": collection_summary,
        "records": rows,
    }
    try:
        md = ArtifactSaveService().save(
            content="\n".join(md_lines),
            artifact_format="md",
            path=output_dir,
            filename="crawler_result.md",
            overwrite=True,
            metadata={"provider": "crawler_user_delivery", "format": "md"},
        )
        js = ArtifactSaveService().save(
            content=content,
            artifact_format="json",
            path=output_dir,
            filename="crawler_result.json",
            overwrite=True,
            metadata={"provider": "crawler_user_delivery", "format": "json"},
        )
    except (ArtifactSaveError, OSError, TypeError, ValueError) as exc:
        return {"status": "failed", "output_dir": output_dir, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "status": "ok",
        "output_dir": output_dir,
        "files": [md.to_dict(), js.to_dict()],
        "record_count": len(rows),
    }


def _crawler_manifest_stats(export_dir: str) -> dict[str, Any]:
    if not export_dir:
        return {"records": 0, "skipped": 0, "errors": 0}
    manifest_path = Path(export_dir) / "manifest.json"
    data = _read_json_file(manifest_path)
    records = data.get("records") if isinstance(data.get("records"), list) else []
    skipped = data.get("skipped") if isinstance(data.get("skipped"), list) else []
    errors = data.get("errors") if isinstance(data.get("errors"), list) else []
    downloads = data.get("downloads") if isinstance(data.get("downloads"), list) else []
    candidates = data.get("candidates") if isinstance(data.get("candidates"), list) else []
    candidate_records = data.get("candidate_records") if isinstance(data.get("candidate_records"), list) else candidates
    expanded_candidate_records = data.get("expanded_candidate_records") if isinstance(data.get("expanded_candidate_records"), list) else []
    blockers = data.get("blockers") if isinstance(data.get("blockers"), list) else []

    def preview_items(items: list[Any]) -> list[dict[str, Any]]:
        previews: list[dict[str, Any]] = []
        for item in list(items or []):
            if not isinstance(item, dict):
                continue
            preview = {
                key: item.get(key)
                for key in ("title", "url", "snippet", "search_relevance", "fetch_url", "fetch_title", "fetch_kind")
                if item.get(key) not in (None, "") and not _has_likely_encoding_damage(item.get(key))
            }
            if preview:
                previews.append(preview)
            if len(previews) >= 8:
                break
        return previews

    record_bytes = 0
    usable_records = 0
    empty_records = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        structured_text = " ".join(
            str(record.get(key) or "").strip()
            for key in ("title", "name", "price", "url", "source", "summary", "text", "content")
            if str(record.get(key) or "").strip()
        )
        byte_count: int | None = None
        try:
            if record.get("bytes") is not None:
                byte_count = int(record.get("bytes") or 0)
        except (TypeError, ValueError):
            byte_count = None
        path = Path(str(record.get("path") or ""))
        if byte_count is None and path.is_file():
            try:
                byte_count = int(path.stat().st_size)
            except OSError:
                byte_count = None
        if byte_count is not None:
            record_bytes += max(0, byte_count)
        try:
            chars = int(record.get("chars") or 0)
        except (TypeError, ValueError):
            chars = 0
        if byte_count is None and chars <= 0 and structured_text:
            byte_count = len(structured_text.encode("utf-8", errors="replace"))
            record_bytes += byte_count
            chars = len(structured_text)
        if (byte_count is not None and byte_count > 0) or chars > 0:
            usable_records += 1
        else:
            empty_records += 1
    return {
        "manifest_path": str(manifest_path) if manifest_path.exists() else "",
        "records": len(records),
        "usable_records": usable_records,
        "empty_records": empty_records,
        "record_bytes": record_bytes,
        "skipped": len(skipped),
        "errors": len(errors),
        "downloads": len(downloads),
        "candidates": len(candidate_records) if isinstance(candidate_records, list) else len(candidates),
        "candidate_preview": preview_items(candidate_records if isinstance(candidate_records, list) else []),
        "expanded_candidate_preview": preview_items(expanded_candidate_records if isinstance(expanded_candidate_records, list) else []),
        "blockers": len(blockers),
        "status": str(data.get("status") or ""),
        "note": str(data.get("note") or ""),
        "failure_reason": str(data.get("failure_reason") or ""),
        "next_action": str(data.get("next_action") or ""),
        "archive_url_detected": bool(data.get("archive_url_detected")),
    }


def _inline_failure_manifest_stats(result: dict[str, Any]) -> dict[str, Any]:
    output = str(result.get("output") or "")
    data: dict[str, Any] = {}
    match = re.search(r"\{.*\}", output, flags=re.S)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                data = parsed
        except json.JSONDecodeError:
            data = {}
    if not data:
        return {"records": 0, "skipped": 0, "errors": 0}
    return {
        "manifest_path": "",
        "records": 0,
        "skipped": 1 if data.get("archive_found") is False else 0,
        "errors": 1 if int(result.get("returncode") or 0) != 0 else 0,
        "downloads": 0,
        "candidates": 0,
        "status": str(data.get("status") or ""),
        "note": str(data.get("message") or ""),
        "failure_reason": str(data.get("failure_reason") or data.get("message") or ""),
        "next_action": str(data.get("next_action") or ""),
    }


def _run_mcagent_context_tool(config: AppConfig, payload: dict[str, Any], plan: dict[str, Any], session_summary: dict[str, Any] | None) -> dict[str, Any]:
    timeout_value = str(payload.get("timeout") or payload.get("timeout_seconds") or payload.get("timeout_ms") or "").strip()
    try:
        timeout_seconds = int(int(timeout_value) / 1000) if timeout_value and int(timeout_value) > 1000 else int(timeout_value or DEFAULT_MCAGENT_CONTEXT_TIMEOUT_SECONDS)
    except ValueError:
        timeout_seconds = DEFAULT_MCAGENT_CONTEXT_TIMEOUT_SECONDS
    timeout_seconds = min(max(0.01, timeout_seconds), 240)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_run_mcagent_context_tool_inner, config, payload, plan, session_summary, timeout_seconds)
    try:
        return future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError:
        future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        return _mcagent_context_timeout_result(payload, timeout_seconds)
    finally:
        if future.done():
            executor.shutdown(wait=False, cancel_futures=True)


def _run_mcagent_context_tool_inner(
    config: AppConfig,
    payload: dict[str, Any],
    plan: dict[str, Any],
    session_summary: dict[str, Any] | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    query = str(payload.get("query") or payload.get("question") or "").strip()
    collection_target = str(payload.get("collection_target") or payload.get("source_question") or payload.get("question") or "").strip()
    focus = _mcagent_context_focus(query or collection_target, collection_target)
    request_message = make_agent_message(
        "CrawlerAgent",
        (
            "请你使用自己的本地资料库/RAG 能力检查这个主题，告诉我本地已有证据、还缺哪些资料，"
            f"以及 CrawlerAgent 下一步应该去网上补什么。\n主题：{focus}"
        ),
        "MCagent",
        intent="mcagent_context_request",
        conversation_id=str(payload.get("session_id") or ""),
        metadata={"tool": "mcagent_context", "collection_target": collection_target},
    )
    inter_agent_request = f"{request_message.from_agent} -> {request_message.to_agent}: {request_message.content}"
    export_dir = PROJECT_ROOT / "data" / "crawler_exports" / "mcagent_context" / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:8]}"
    export_dir.mkdir(parents=True, exist_ok=True)
    report_path = export_dir / "mcagent_context.md"
    manifest_path = export_dir / "manifest.json"
    started = time.time()
    try:
        dispatch_payload = dict(payload)
        dispatch_payload["session_summary"] = dict(session_summary or {})
        dispatch_payload["model"] = str(payload.get("model") or dispatch_payload.get("model") or "default")
        dispatch_payload["mcagent_context_request"] = {
            "focus": focus,
            "collection_target": collection_target,
            "delivery_target": str(plan.get("delivery_target") or payload.get("delivery_target") or ""),
        }
        mcagent_response = _send_agent_message(
            config,
            dispatch_payload,
            from_agent=request_message.from_agent,
            content=request_message.content,
            to_agent=request_message.to_agent,
            intent=request_message.intent,
            conversation_id=request_message.conversation_id,
            metadata=request_message.metadata,
        )
        mcagent_answer = str(mcagent_response.get("answer") or "").strip()
        raw_reply = mcagent_response.get("agent_message")
        if isinstance(raw_reply, dict):
            reply_message = message_from_payload(
                {"agent_message": raw_reply, "session_id": request_message.conversation_id},
                default_to_agent="CrawlerAgent",
                default_content=mcagent_answer,
            )
        else:
            reply_message = make_agent_message(
                "MCagent",
                mcagent_answer,
                "CrawlerAgent",
                intent="mcagent_context_reply",
                conversation_id=request_message.conversation_id,
                reply_to=request_message.message_id,
                metadata={"tool": "mcagent_context"},
            )
        sources = list(mcagent_response.get("sources") or []) if isinstance(mcagent_response.get("sources"), list) else []
        mcagent_trace = list(mcagent_response.get("trace") or []) if isinstance(mcagent_response.get("trace"), list) else []
        evidence_report = mcagent_response.get("evidence") if isinstance(mcagent_response.get("evidence"), dict) else {}
        gap_summary = (
            "MCagent reply delivered through AgentMessage bus. CrawlerAgent must read the reply, sources, "
            "evidence report, and trace, then judge whether more collection is needed.\n"
            f"{mcagent_answer[:3500]}"
        ).strip()
        lines = [
            "# Inter-Agent Context: MCagent -> CrawlerAgent",
            "",
            "<!-- source: mcagent_context -->",
            "",
            "## CrawlerAgent Request",
            "",
            inter_agent_request,
            "",
            "## MCagent Reply",
            "",
            reply_message.content,
            "",
            "## MCagent Local Search Focus",
            "",
            focus,
            "",
            "## Gap Summary",
            "",
            gap_summary,
            "",
            "## MCagent Runtime Trace",
            "",
        ]
        for event in mcagent_trace[:40]:
            if not isinstance(event, dict):
                continue
            lines.append(f"- {event.get('stage')}: {event.get('status')} | {json.dumps(event.get('detail'), ensure_ascii=False, default=str)[:500]}")
        lines.extend(["", "## MCagent Sources", ""])
        if sources:
            for index, item in enumerate(sources, start=1):
                if not isinstance(item, dict):
                    continue
                source_line = item.get("url") or item.get("source_path") or item.get("path") or ""
                text = normalize_text(str(item.get("text") or item.get("snippet") or ""))[:900]
                lines.extend(
                    [
                        f"### S{index}. {item.get('title') or 'MCagent source'}",
                        "",
                        f"- score: {item.get('score')}",
                        f"- source: {source_line}",
                        "",
                        text,
                        "",
                    ]
                )
        else:
            lines.extend(["No local MCagent/RAG evidence was found.", ""])
        report_path.write_text("\n".join(lines), encoding="utf-8")
        context_records: list[dict[str, Any]] = [
            {
                "title": "MCagent Reply To CrawlerAgent",
                "url": None,
                "path": str(report_path),
                "snippet": f"local_sources={len(sources)}; delivered_via=_send_agent_message",
                "metadata": {
                    "from_agent": "MCagent",
                    "to_agent": "CrawlerAgent",
                    "local_source_count": len(sources),
                    "transport": "_send_agent_message",
                    "delivery_target": str(plan.get("delivery_target") or payload.get("delivery_target") or ""),
                },
            }
        ]
        for index, item in enumerate(sources, start=1):
            if not isinstance(item, dict):
                continue
            local_path = str(item.get("source_path") or item.get("path") or "").strip()
            if not local_path:
                continue
            title = str(item.get("title") or f"MCagent local source S{index}").strip()
            snippet = normalize_text(str(item.get("text") or item.get("snippet") or ""))[:500]
            context_records.append(
                {
                    "title": title,
                    "url": item.get("url"),
                    "path": local_path,
                    "snippet": snippet or f"MCagent local source S{index}",
                    "metadata": {
                        "from_agent": "MCagent",
                        "to_agent": "CrawlerAgent",
                        "rank": item.get("rank") or index,
                        "score": item.get("score"),
                        "transport": "_send_agent_message",
                        "record_role": "mcagent_local_source",
                    },
                }
            )
        manifest = {
            "source": "mcagent_context",
            "query": focus,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "inter_agent": {
                "from_agent": request_message.from_agent,
                "to_agent": request_message.to_agent,
                "reply_from": reply_message.from_agent,
                "reply_to": reply_message.to_agent,
                "request": request_message.content,
                "reply": reply_message.content,
                "messages": [request_message.to_dict(), reply_message.to_dict()],
                "transport": "_send_agent_message",
            },
            "agent_message_exchange": _agent_message_response_trace(request_message, reply_message),
            "mcagent_trace": mcagent_trace,
            "evidence_report": evidence_report,
            "records": context_records,
            "sources": sources,
            "errors": [],
            "skipped": [],
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "source": "mcagent_context",
            "returncode": 0,
            "command": ["internal", "mcagent_context"],
            "output": f"MCagent/RAG local context collected through AgentMessage bus. local_sources={len(sources)} export_dir={export_dir}",
            "timeout_seconds": timeout_seconds,
            "elapsed_seconds": round(time.time() - started, 3),
            "timed_out": False,
            "export_dir": str(export_dir),
            "mcagent_gap_summary": gap_summary,
            "mcagent_answer": reply_message.content,
            "mcagent_source_count": len(sources),
            "mcagent_source_paths": [str(item.get("source_path") or item.get("path") or "") for item in sources if isinstance(item, dict) and str(item.get("source_path") or item.get("path") or "").strip()][:12],
            "agent_message_exchange": _agent_message_response_trace(request_message, reply_message),
            "mcagent_trace": mcagent_trace,
            "transport": "_send_agent_message",
        }
    except Exception as exc:  # noqa: BLE001
        manifest = {
            "source": "mcagent_context",
            "query": focus,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "records": [],
            "errors": [{"error": f"{type(exc).__name__}: {exc}"}],
            "skipped": [],
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "source": "mcagent_context",
            "returncode": 1,
            "command": ["internal", "mcagent_context"],
            "output": f"{type(exc).__name__}: {exc}",
            "timeout_seconds": timeout_seconds,
            "elapsed_seconds": round(time.time() - started, 3),
            "timed_out": False,
            "export_dir": str(export_dir),
        }


def _mcagent_context_timeout_result(payload: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    query = str(payload.get("query") or payload.get("question") or "").strip()
    return {
        "source": "mcagent_context",
        "returncode": 124,
        "command": ["internal", "mcagent_context"],
        "output": (
            f"mcagent_context timed out after {timeout_seconds}s. "
            "This objective blocker is returned to CrawlerAgent; CrawlerAgent should continue with public archive/download discovery instead of waiting on local context."
        ),
        "timeout_seconds": timeout_seconds,
        "elapsed_seconds": timeout_seconds,
        "timed_out": True,
        "export_dir": "",
        "query": query,
    }


def _crawler_reusable_duplicate_evidence(export_dir: str, question: str, task_query: str, plan: dict[str, Any]) -> dict[str, Any]:
    """Ask CrawlerAgent whether duplicate-skipped pages are useful existing evidence.

    A duplicate skip means the page was already collected earlier. It should not
    count as fresh data, but if the page is relevant it should count as evidence
    Crawler can hand back to MCagent/RAG instead of reporting an empty result.
    """
    if not export_dir:
        return {"matched": False, "records": []}
    manifest_path = Path(export_dir) / "manifest.json"
    data = _read_json_file(manifest_path)
    skipped = data.get("skipped") if isinstance(data.get("skipped"), list) else []
    records: list[dict[str, Any]] = []
    for item in skipped:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "").lower()
        previous_path = str(item.get("previous_path") or "").strip()
        if not any(marker in reason for marker in ("duplicate", "known_project", "known")) or not previous_path or not Path(previous_path).exists():
            continue
        records.append(
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "path": previous_path,
                "duplicate_reason": item.get("reason"),
            }
        )
        if len(records) >= 8:
            break
    if not records:
        return {"matched": False, "records": []}
    terms = _topic_terms_for_validation(question, task_query, plan)
    try:
        judgement = _crawler_llm_record_relevance(question, task_query, plan, records, terms)
    except Exception as exc:  # noqa: BLE001
        judgement = {"matched": False, "reason": "llm_duplicate_reuse_error", "notes": f"{type(exc).__name__}: {exc}"}
    matched_indexes = judgement.get("matched_indexes") if isinstance(judgement, dict) else []
    matched_records: list[dict[str, Any]] = []
    if isinstance(matched_indexes, list):
        for index in matched_indexes:
            try:
                matched_records.append(records[int(index)])
            except (TypeError, ValueError, IndexError):
                continue
    if not matched_records and judgement.get("matched"):
        matched_records = records[:3]
    return {
        "matched": bool(judgement.get("matched")) if isinstance(judgement, dict) else False,
        "reason": str(judgement.get("reason") or "") if isinstance(judgement, dict) else "",
        "notes": str(judgement.get("notes") or "") if isinstance(judgement, dict) else "",
        "cleanup_action": str(judgement.get("cleanup_action") or "") if isinstance(judgement, dict) else "",
        "next_action": str(judgement.get("next_action") or "") if isinstance(judgement, dict) else "",
        "rejected_indexes": list(judgement.get("rejected_indexes") or [])[:8] if isinstance(judgement, dict) else [],
        "judge": str(judgement.get("judge") or "Crawler LLM") if isinstance(judgement, dict) else "Crawler LLM",
        "records": matched_records,
    }


def _manifest_source_from_path(path: Path) -> str:
    try:
        rel = path.resolve().relative_to((PROJECT_ROOT / "data" / "crawler_exports").resolve())
        return rel.parts[0] if rel.parts else ""
    except ValueError:
        return path.parent.parent.name if path.parent.parent else ""


def _crawler_manifest_brief(manifest_path: Path) -> dict[str, Any]:
    data = _read_json_file(manifest_path)
    records = data.get("records") if isinstance(data.get("records"), list) else []
    skipped = data.get("skipped") if isinstance(data.get("skipped"), list) else []
    errors = data.get("errors") if isinstance(data.get("errors"), list) else []
    record_samples: list[dict[str, Any]] = []
    for record_index, record in enumerate(records[:5]):
        if isinstance(record, dict):
            path = Path(str(record.get("path") or ""))
            path_bytes: int | None = None
            if path.is_file():
                try:
                    path_bytes = path.stat().st_size
                except OSError:
                    path_bytes = None
            record_samples.append(
                {
                    "index": record_index,
                    "title": record.get("title"),
                    "url": record.get("url"),
                    "path": record.get("path"),
                    "chars": record.get("chars"),
                    "bytes": record.get("bytes") if record.get("bytes") is not None else path_bytes,
                    "raw_html_path": record.get("raw_html_path"),
                }
            )
    skipped_reasons: dict[str, int] = {}
    for item in skipped:
        if isinstance(item, dict):
            reason = str(item.get("reason") or "unknown")
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
    error_samples: list[dict[str, Any]] = []
    for item in errors[:5]:
        if isinstance(item, dict):
            error_samples.append({"stage": item.get("stage"), "query": item.get("query"), "error": item.get("error")})
        else:
            error_samples.append({"error": str(item)})
    return {
        "manifest_path": str(manifest_path),
        "export_dir": str(manifest_path.parent),
        "source": _source_alias(_manifest_source_from_path(manifest_path)),
        "query": data.get("query"),
        "created_at": data.get("created_at"),
        "records": len(records),
        "skipped": len(skipped),
        "errors": len(errors),
        "record_samples": record_samples,
        "skipped_reasons": skipped_reasons,
        "error_samples": error_samples,
    }


def _crawler_result_summary(task_results: list[dict[str, Any]], plan: dict[str, Any] | None = None) -> dict[str, Any]:
    by_source: dict[str, dict[str, Any]] = {}
    useful_records: list[dict[str, Any]] = []
    empty_tasks: list[dict[str, Any]] = []
    off_topic_tasks: list[dict[str, Any]] = []
    uncertain_tasks: list[dict[str, Any]] = []
    failed_tasks: list[dict[str, Any]] = []
    duplicate_count = 0
    low_relevance_count = 0
    raw_html_count = 0
    total_records = 0
    total_skipped = 0
    total_errors = 0
    observation_counts: dict[str, int] = {}
    for result in task_results:
        source = _source_alias(str(result.get("source") or ""))
        observation = classify_crawler_tool_result(result)
        result.setdefault("observation", observation.to_dict())
        observation_counts[observation.status] = observation_counts.get(observation.status, 0) + 1
        entry = by_source.setdefault(
            source,
            {
                "source": source,
                "tasks": 0,
                "records": 0,
                "skipped": 0,
                "errors": 0,
                "empty": 0,
                "off_topic": 0,
                "uncertain": 0,
                "failed": 0,
                "status_counts": {},
            },
        )
        entry["tasks"] += 1
        entry["status_counts"][observation.status] = entry["status_counts"].get(observation.status, 0) + 1
        stats = result.get("manifest_stats") if isinstance(result.get("manifest_stats"), dict) else {}
        records = int(stats.get("records") or 0)
        skipped = int(stats.get("skipped") or 0)
        errors = int(stats.get("errors") or 0)
        entry["records"] += records
        entry["skipped"] += skipped
        entry["errors"] += errors
        total_records += records
        total_skipped += skipped
        total_errors += errors
        task_brief = {
            "source": source,
            "query": result.get("query"),
            "returncode": result.get("returncode"),
            "records": records,
            "skipped": skipped,
            "errors": errors,
            "export_dir": result.get("export_dir"),
            "status": stats.get("status"),
            "failure_reason": stats.get("failure_reason") or stats.get("note"),
            "observation_status": observation.status,
            "observation_summary": observation.summary,
            "retryable": observation.retryable,
            "suggested_next": observation.suggested_next,
        }
        if result.get("empty_result"):
            entry["empty"] += 1
            empty_tasks.append(task_brief)
        if result.get("off_topic_result"):
            entry["off_topic"] += 1
            off_topic_tasks.append(task_brief | {"topic_validation": result.get("topic_validation")})
        if result.get("uncertain_result"):
            entry["uncertain"] += 1
            uncertain_tasks.append(task_brief | {"topic_validation": result.get("topic_validation")})
        if result.get("records_pending_review"):
            entry["uncertain"] += 1
            uncertain_tasks.append(task_brief | {"pending_review": True})
        duplicate_review = result.get("existing_evidence_review")
        if isinstance(duplicate_review, dict) and duplicate_review:
            entry["off_topic"] += 1
            off_topic_tasks.append(task_brief | {"duplicate_review": duplicate_review})
        if int(result.get("returncode") or 0) != 0:
            entry["failed"] += 1
            failed_tasks.append(task_brief | {"output_tail": _tail_text(str(result.get("output") or ""), 500)})
        manifest_path = stats.get("manifest_path")
        if manifest_path:
            brief = _crawler_manifest_brief(Path(str(manifest_path)))
            for reason, count in brief.get("skipped_reasons", {}).items():
                lowered = str(reason).lower()
                if "duplicate" in lowered:
                    duplicate_count += int(count)
                if "relevance" in lowered or "low" in lowered:
                    low_relevance_count += int(count)
            if bool(result.get("topic_validation", {}).get("matched")):
                matched_indexes = result.get("topic_validation", {}).get("matched_indexes")
                matched_set = {int(item) for item in matched_indexes or [] if str(item).isdigit()}
                for sample in brief.get("record_samples", []):
                    sample_index = sample.get("index")
                    if matched_set and (not str(sample_index).isdigit() or int(sample_index) not in matched_set):
                        continue
                    sample_path = Path(str(sample.get("path") or ""))
                    try:
                        sample_chars = int(sample.get("chars") or 0)
                    except (TypeError, ValueError):
                        sample_chars = 0
                    try:
                        sample_bytes = int(sample.get("bytes") or 0) if sample.get("bytes") is not None else (sample_path.stat().st_size if sample_path.is_file() else 0)
                    except (OSError, TypeError, ValueError):
                        sample_bytes = 0
                    if sample_chars <= 0 and sample_bytes <= 0:
                        continue
                    if sample.get("raw_html_path"):
                        raw_html_count += 1
                    if len(useful_records) < 12:
                        useful_records.append({"source": source, "query": result.get("query"), **sample})
        reused = result.get("existing_evidence_reused")
        if isinstance(reused, dict) and reused.get("matched"):
            for sample in reused.get("records") or []:
                if isinstance(sample, dict) and len(useful_records) < 12:
                    useful_records.append(
                        {
                            "source": source,
                            "query": result.get("query"),
                            "title": sample.get("title"),
                            "url": sample.get("url"),
                            "path": sample.get("path"),
                            "status": "existing_duplicate_reused",
                        }
                    )
    next_actions: list[str] = []
    failure_reasons = [
        str(item.get("failure_reason") or "")
        for item in empty_tasks + failed_tasks
        if str(item.get("failure_reason") or "").strip()
    ]
    if failure_reasons:
        next_actions.append("Crawler observed a provider/tool failure reason: " + failure_reasons[0])
    if total_records == 0:
        next_actions.append("No ingestible records were produced; CrawlerAgent should choose a different source, direct URL, browser path, or narrower entity query.")
    if empty_tasks:
        next_actions.append("Some tools returned empty results; CrawlerAgent should reflect on aliases, source choice, and whether browser/manual discovery is needed.")
    if off_topic_tasks:
        next_actions.append("Some results were off-topic; CrawlerAgent should inspect examples and adjust source/query/URL strategy.")
        review_actions = [
            str(item.get("duplicate_review", {}).get("next_action") or item.get("topic_validation", {}).get("next_action") or "").strip()
            for item in off_topic_tasks
            if isinstance(item, dict)
        ]
        review_actions = [item for item in review_actions if item]
        if review_actions:
            next_actions.append("CrawlerAgent review requested: " + review_actions[0])
    if uncertain_tasks:
        next_actions.append("Some relevance judgments are uncertain; CrawlerAgent should re-read samples before keeping, expanding, or discarding them.")
    if duplicate_count:
        next_actions.append("Many duplicate records were skipped; prefer new tutorials, tables, recipe pages, internal files, or unexplored URLs.")
    if low_relevance_count:
        next_actions.append("Public search produced low-relevance results; consider source-specific discovery, direct browser search, or project archive analysis.")
    if total_errors:
        next_actions.append("Some tools returned errors; check observation status for quota, auth, captcha, network, timeout, or parse blockers.")
    if not next_actions:
        next_actions.append("New useful data exists; MCagent can re-query the local index to verify answer quality.")
    return {
        "topic": (plan or {}).get("topic") or (plan or {}).get("target_hint"),
        "delivery_target": (plan or {}).get("delivery_target"),
        "totals": {
            "tasks": len(task_results),
            "records": total_records,
            "skipped": total_skipped,
            "errors": total_errors,
            "empty_tasks": len(empty_tasks),
            "off_topic_tasks": len(off_topic_tasks),
            "uncertain_tasks": len(uncertain_tasks),
            "failed_tasks": len(failed_tasks),
            "duplicate_skipped": duplicate_count,
            "low_relevance_skipped": low_relevance_count,
            "raw_html_records": raw_html_count,
            "observation_statuses": observation_counts,
        },
        "by_source": list(by_source.values()),
        "useful_records": useful_records,
        "empty_tasks": empty_tasks[:12],
        "off_topic_tasks": off_topic_tasks[:12],
        "uncertain_tasks": uncertain_tasks[:12],
        "failed_tasks": failed_tasks[:12],
        "next_actions": next_actions,
    }


def _recent_crawler_manifest_summary(source_dir: Path, *, limit: int = 20, query: str = "") -> dict[str, Any]:
    root = source_dir.resolve()
    manifests = sorted(root.rglob("manifest.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    query_lower = query.strip().lower()
    briefs: list[dict[str, Any]] = []
    for manifest_path in manifests:
        brief = _crawler_manifest_brief(manifest_path)
        haystack = " ".join(str(brief.get(key) or "") for key in ("query", "source", "manifest_path")).lower()
        if query_lower and query_lower not in haystack:
            continue
        briefs.append(brief)
        if len(briefs) >= limit:
            break
    pseudo_results: list[dict[str, Any]] = []
    for brief in briefs:
        pseudo_results.append(
            {
                "source": brief.get("source"),
                "query": brief.get("query"),
                "returncode": 0,
                "export_dir": brief.get("export_dir"),
                "manifest_stats": {
                    "manifest_path": brief.get("manifest_path"),
                    "records": brief.get("records"),
                    "skipped": brief.get("skipped"),
                    "errors": brief.get("errors"),
                },
                "empty_result": int(brief.get("records") or 0) == 0,
            }
        )
    return {
        "limit": limit,
        "query": query,
        "manifest_count": len(briefs),
        "manifests": briefs,
        "collection_summary": _crawler_result_summary(pseudo_results, {}),
    }


AGENT_DELIVERY_TERMS = {
    "mcagent",
    "crawler",
    "rag",
    "ingest",
    "index",
    "chunk",
    "chunks",
    "markdown",
    "manifest",
    "html",
    "raw",
    "data",
    "complete",
    "full",
    "入库",
    "索引",
    "切分",
    "资料库",
    "知识库",
}

MINECRAFT_CONTEXT_TERMS = {
    "minecraft",
    "mc百科",
    "mcmod",
    "mod",
    "modpack",
    "整合包",
    "模组",
    "forge",
    "fabric",
    "neoforge",
    "curseforge",
    "modrinth",
    "ftb",
    "tacz",
    "slashblade",
}


def _topic_terms_for_validation(question: str, task_query: str, plan: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for value in (plan.get("target_hint"), plan.get("topic"), task_query, question):
        if value:
            values.append(str(value))
    for key in ("known_components", "coverage_goals", "subqueries"):
        value = plan.get(key)
        if isinstance(value, list):
            values.extend(str(item) for item in value if str(item).strip())
    joined = " ".join(values)
    terms: list[str] = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}|[\u4e00-\u9fff]{2,12}", joined):
        lowered = token.lower()
        if lowered in AGENT_DELIVERY_TERMS:
            continue
        if set(token) <= {"?"}:
            continue
        terms.append(token)
    # Add useful aliases from mixed CN/EN targets.
    lowered_joined = joined.lower()
    if "closing song" in lowered_joined:
        terms.extend(["Closing", "Song", "落幕曲"])
    if "落幕曲" in joined:
        terms.extend(["落幕曲", "Closing", "Song"])
    return list(dict.fromkeys(term for term in terms if len(term) >= 2))[:12]


def _task_query_terms_for_validation(task_query: str) -> list[str]:
    noise = AGENT_DELIVERY_TERMS | {"攻略", "教程", "玩法", "获取", "步骤", "合成", "配方", "资料", "数据", "完整", "相关"}
    terms: list[str] = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}|[\u4e00-\u9fff]{2,12}", task_query):
        lowered = token.lower()
        if lowered in noise or token in noise:
            continue
        terms.append(token)
    return list(dict.fromkeys(terms))[:8]


def _has_minecraft_context(text: str) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in MINECRAFT_CONTEXT_TERMS)


def _record_text_for_validation(record: dict[str, Any]) -> str:
    parts = [str(record.get(key) or "") for key in ("title", "name", "price", "url", "source", "path", "snippet", "description")]
    extra_structured = {
        key: record.get(key)
        for key in ("name", "price", "url", "source", "category", "rating", "fields", "rows")
        if record.get(key) not in (None, "")
    }
    if extra_structured:
        parts.append(json.dumps(extra_structured, ensure_ascii=False, default=str))
    path = record.get("path")
    if path:
        path_obj = Path(str(path))
        if path_obj.suffix.lower() not in {".md", ".txt", ".json", ".html", ".htm", ".csv", ".xml", ".snbt", ".toml", ".ini", ".cfg", ".properties", ".js"}:
            return "\n".join(parts)
        try:
            if path_obj.stat().st_size > 5_000_000:
                return "\n".join(parts)
            parts.append(path_obj.read_text(encoding="utf-8", errors="replace")[:12000])
        except (OSError, MemoryError):
            pass
    return "\n".join(parts)


def _crawler_llm_record_relevance(
    question: str,
    task_query: str,
    plan: dict[str, Any],
    records: list[dict[str, Any]],
    terms: list[str],
) -> dict[str, Any]:
    samples: list[dict[str, str]] = []
    for record in records[:8]:
        text = _record_text_for_validation(record)
        path = Path(str(record.get("path") or ""))
        path_bytes: int | None = None
        if path.is_file():
            try:
                path_bytes = path.stat().st_size
            except OSError:
                path_bytes = None
        samples.append(
            {
                "title": str(record.get("title") or "")[:160],
                "url": str(record.get("url") or "")[:240],
                "bytes": str(record.get("bytes") if record.get("bytes") is not None else (path_bytes if path_bytes is not None else "")),
                "chars": str(record.get("chars") if record.get("chars") is not None else ""),
                "excerpt": normalize_text(text)[:900],
            }
        )
    config = load_config()
    client, _label = client_for_agent(config, "crawler_agent", temperature=0.0, timeout_seconds=90)
    prompt = (
        "You are CrawlerAgent auditing crawler evidence for a RAG knowledge base.\n"
        "Tools only fetched objective page text/metadata. You, CrawlerAgent, must decide whether each record is useful, junk, blocked, or needs retry.\n"
        "Reject not-found pages, login/captcha/access-denied pages, generic navigation shells, unrelated noise, and pages that only prove a wrong URL exists.\n"
        "If a record is rejected, state whether Crawler should retry with another URL/source/query, reuse no evidence, or ignore/delete the artifact from this job's accepted outputs.\n"
        "Important: a modpack can include component mods/items/systems. A useful page does NOT need to mention the modpack name if the task query or context indicates it is a component to collect.\n"
        "If the task query is a known or plausible component name such as TACZ, FTB Quests, SlashBlade, a boss name, an item name, or a system name, judge the page by whether it explains that component. Do not require the page to also contain the modpack name.\n"
        "Classify records as useful if they are direct project pages OR plausible component/system/tutorial pages for the target. Reject broad unrelated noise.\n"
        "Output only compact JSON: {\"matched\": true/false, \"reason\": \"direct|component|not_found|login_required|blocked|shell|noise|uncertain\", \"matched_indexes\": [0], \"rejected_indexes\": [1], \"cleanup_action\": \"keep|ignore_for_job|delete_artifact|retry_other_source\", \"next_action\": \"...\", \"notes\": \"...\"}\n"
        f"Target/question: {question}\n"
        f"Task query: {task_query}\n"
        f"Plan topic: {plan.get('topic') or plan.get('target_hint') or ''}\n"
        f"Known components: {json.dumps(plan.get('known_components') or [], ensure_ascii=False)}\n"
        f"Coverage goals: {json.dumps(plan.get('coverage_goals') or [], ensure_ascii=False)}\n"
        f"Candidate terms: {json.dumps(terms, ensure_ascii=False)}\n"
        f"Records: {json.dumps(samples, ensure_ascii=False)}"
    )
    text = client.chat(
        [
            {"role": "system", "content": "You judge crawler record relevance. Output only JSON."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_tokens=2500,
    )
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    match = re.search(r"\{.*\}", stripped, flags=re.S)
    if match:
        stripped = match.group(0)
    value = json.loads(stripped)
    if not isinstance(value, dict):
        raise ValueError("crawler relevance judge did not return object")
    matched_indexes = value.get("matched_indexes")
    if not isinstance(matched_indexes, list):
        matched_indexes = []
    rejected_indexes = value.get("rejected_indexes")
    if not isinstance(rejected_indexes, list):
        rejected_indexes = []
    return {
        "matched": bool(value.get("matched")),
        "reason": str(value.get("reason") or "llm_judged"),
        "matched_indexes": [int(item) for item in matched_indexes if str(item).isdigit()][:8],
        "rejected_indexes": [int(item) for item in rejected_indexes if str(item).isdigit()][:8],
        "cleanup_action": str(value.get("cleanup_action") or "").strip()[:80],
        "next_action": str(value.get("next_action") or "").strip()[:300],
        "notes": str(value.get("notes") or "")[:500],
        "judge": "Crawler LLM",
    }


def _crawler_topic_match(export_dir: str, question: str, task_query: str, plan: dict[str, Any]) -> dict[str, Any]:
    manifest_path = Path(export_dir) / "manifest.json" if export_dir else Path("")
    data = _read_json_file(manifest_path)
    records = data.get("records") if isinstance(data.get("records"), list) else []
    if not records:
        return {"matched": False, "reason": "no_records", "matched_records": 0, "records": 0}
    terms = _topic_terms_for_validation(question, task_query, plan)
    task_terms = _task_query_terms_for_validation(task_query)
    llm_judgement: dict[str, Any] | None = None
    try:
        llm_judgement = _crawler_llm_record_relevance(question, task_query, plan, records, terms)
    except Exception as exc:  # noqa: BLE001
        llm_judgement = {"matched": False, "reason": "llm_judge_error_uncertain", "notes": f"{type(exc).__name__}: {exc}", "judge": "Crawler LLM"}
    matched_indexes = llm_judgement.get("matched_indexes") if isinstance(llm_judgement, dict) else []
    llm_examples: list[dict[str, str]] = []
    if isinstance(matched_indexes, list):
        for index in matched_indexes[:3]:
            if isinstance(index, int) and 0 <= index < len(records):
                record = records[index]
                if not _crawler_record_has_content(record):
                    continue
                llm_examples.append({"title": str(record.get("title") or ""), "url": str(record.get("url") or ""), "hits": "Crawler LLM component/direct judgement"})
    rejected_indexes = llm_judgement.get("rejected_indexes") if isinstance(llm_judgement, dict) else []
    rejected_examples: list[dict[str, str]] = []
    if isinstance(rejected_indexes, list):
        for index in rejected_indexes[:3]:
            if isinstance(index, int) and 0 <= index < len(records):
                record = records[index]
                rejected_examples.append({"title": str(record.get("title") or ""), "url": str(record.get("url") or ""), "reason": str(llm_judgement.get("reason") or "")})
    component_candidates: list[dict[str, str]] = []
    if task_terms:
        for record in records:
            raw_text = _record_text_for_validation(record)
            text = raw_text.lower()
            query_hits = [term for term in task_terms if term.lower() in text]
            if not query_hits or not _has_minecraft_context(raw_text):
                continue
            component_candidates.append(
                {
                    "title": str(record.get("title") or ""),
                    "url": str(record.get("url") or ""),
                    "hits": ", ".join(query_hits[:6]),
                }
            )
            if len(component_candidates) >= 3:
                break
    if not llm_judgement.get("matched") and str(llm_judgement.get("reason") or "") == "llm_judge_error_uncertain" and component_candidates:
        llm_examples = component_candidates
    accepted_indexes = [int(index) for index in matched_indexes if isinstance(index, int) and 0 <= index < len(records) and _crawler_record_has_content(records[index])][:8] if isinstance(matched_indexes, list) else []
    empty_matched_indexes = [int(index) for index in matched_indexes if isinstance(index, int) and 0 <= index < len(records) and not _crawler_record_has_content(records[index])][:8] if isinstance(matched_indexes, list) else []
    effective_matched = bool(llm_judgement.get("matched")) and bool(accepted_indexes)
    reason = str(llm_judgement.get("reason") or "uncertain") if isinstance(llm_judgement, dict) else "uncertain"
    if bool(llm_judgement.get("matched")) and empty_matched_indexes and not accepted_indexes:
        reason = "empty_artifact"
    return {
        "matched": effective_matched if isinstance(llm_judgement, dict) else False,
        "reason": reason,
        "matched_records": len(llm_examples),
        "matched_indexes": accepted_indexes,
        "empty_matched_indexes": empty_matched_indexes,
        "rejected_indexes": [int(index) for index in rejected_indexes if isinstance(index, int) and 0 <= index < len(records)][:8] if isinstance(rejected_indexes, list) else [],
        "records": len(records),
        "terms": terms,
        "task_terms": task_terms,
        "examples": llm_examples,
        "component_candidates": component_candidates,
        "rejected_examples": rejected_examples,
        "cleanup_action": str(llm_judgement.get("cleanup_action") or "") if isinstance(llm_judgement, dict) else "",
        "next_action": str(llm_judgement.get("next_action") or "") if isinstance(llm_judgement, dict) else "",
        "note": "Tool output is objective evidence only. Crawler LLM judgement decides whether records are accepted, rejected, retried, or ignored for this job.",
        "llm_judgement": llm_judgement,
    }


def _run_ingest_job(job: Job, config: AppConfig) -> None:
    _update_job(job, status="running", started_at=time.time(), summary="Import started.")
    try:
        result = _ingest_after_crawl(config)
        stats = result["stats"]
        _update_job(
            job,
            status="succeeded",
            ended_at=time.time(),
            summary=f"Import finished: documents_loaded={stats['documents_loaded']}, chunks_written={stats['chunks_written']}, errors={stats['errors']}",
            result=result,
        )
    except Exception as exc:  # noqa: BLE001
        _update_job(job, status="failed", ended_at=time.time(), summary=_tail_text(traceback.format_exc()), error=f"{type(exc).__name__}: {exc}")


def _all_source_tasks(
    question: str,
    config: AppConfig,
    include_completed: bool = False,
    session_summary: dict[str, Any] | None = None,
    max_tasks: int = 16,
) -> list[dict[str, Any]]:
    if not include_completed:
        llm_plan = plan_crawler_tasks_resilient(question, config.paths.source_dir, max_tasks=max_tasks, session_summary=session_summary)
        tasks = list(llm_plan.get("tasks") or [])
        if tasks:
            return tasks[:max(1, max_tasks)]
    plan = plan_crawler_tasks(question, config.paths.source_dir, max_tasks=max_tasks, include_completed=include_completed)
    tasks = list(plan.get("tasks") or [])
    intent = analyze_query(question, CONCEPTS)
    decomposed = decompose_crawler_queries(question, intent)
    query = str(decomposed.get("project_query") or "").strip() or (intent.search_queries[0] if intent.search_queries else intent.entity or question)
    short_queries = [str(item) for item in decomposed.get("queries") or [] if str(item).strip()]
    focused_query = short_queries[0] if short_queries else query
    wanted = [
        ("mcmod", query, "中文 MC 资料、整合包和教程页"),
        ("modrinth", query, "项目元数据、整合包 .mrpack 清单"),
        ("followup", query, "项目 Source/Wiki/README/公开文档"),
        ("fetch_url", focused_query, "本地 HTTP 抓取指定 URL 并提取正文/raw HTML"),
        ("playwright", focused_query, "Playwright 浏览器搜索/渲染，保存正文与 raw HTML"),
        ("web_discovery", focused_query, "公开搜索兜底发现资料源"),
    ]
    context_text = "\n".join([question, json.dumps(session_summary or {}, ensure_ascii=False)])
    if re.search(r"MCagent|MCAgent|RAG|本地资料|本地上下文|缺口|缺失|还缺|找补", context_text, flags=re.I):
        wanted.insert(0, ("mcagent_context", query, "ask MCagent/RAG for local evidence and missing-data gaps before external collection"))
    if intent.domain == "vanilla":
        wanted.insert(0, ("mediawiki", question, "原版 Minecraft Wiki"))
    if any(token in question.lower() for token in ("create", "机械动力")):
        wanted.insert(0, ("createwiki", "Create mod " + query, "Create 专门 Wiki"))
    if any(token in question.lower() for token in ("twilight", "暮色", "ae2", "mekanism", "botania")):
        wanted.insert(0, ("ftbwiki", query, "大型模组 Wiki"))
    existing = {(_source_alias(str(item.get("source") or "")), str(item.get("query") or "")) for item in tasks}
    for source, item_query, reason in wanted:
        key = (_source_alias(source), item_query)
        if key not in existing:
            extra: dict[str, Any] = {}
            if source == "mcmod":
                extra["search_limit"] = 4
            elif source == "modrinth":
                extra.update({"mods": 16, "modpacks": 5, "resourcepacks": 3, "shaders": 1})
            elif source == "followup":
                extra["max_urls"] = 12
            elif source == "web_discovery":
                extra.update({"search_limit": 6, "max_urls": 4})
            elif source == "fetch_url":
                extra.update({"timeout": 35})
            elif source == "mcagent_context":
                extra["timeout_seconds"] = DEFAULT_MCAGENT_CONTEXT_TIMEOUT_SECONDS
            elif source == "playwright":
                extra.update({"search_limit": 4, "max_urls": 2})
            elif source in {"mediawiki", "ftbwiki", "createwiki"}:
                extra["search_limit"] = 8
            tasks.append({"source": source, "query": item_query, "reason": reason, "priority": 50, **extra})
    expanded: list[dict[str, Any]] = []
    for task in tasks:
        source = _source_alias(str(task.get("source") or ""))
        if source in {"mcmod", "fetch_url", "web_discovery", "playwright"} and short_queries:
            limit = 2 if source == "mcmod" else 2
            for index, short_query in enumerate(short_queries[:limit]):
                cloned = dict(task)
                cloned["query"] = short_query
                cloned["priority"] = int(cloned.get("priority") or 50) - index
                cloned["reason"] = f"{cloned.get('reason') or ''}；短查询拆分"
                expanded.append(cloned)
        else:
            expanded.append(task)
    tasks = expanded
    priority = {"mcagent_context": 110, "mcmod": 100, "modrinth": 90, "ftbwiki": 85, "createwiki": 85, "fetch_url": 88, "playwright": 82, "followup": 74, "web_discovery": 70, "mediawiki": 50}
    for task in tasks:
        source = _source_alias(str(task.get("source") or ""))
        if source == "mcmod":
            task["search_limit"] = min(max(1, int(task.get("search_limit") or 4)), 4)
        elif source == "modrinth":
            task.setdefault("mods", 16)
            task.setdefault("modpacks", 5)
            task.setdefault("resourcepacks", 3)
            task.setdefault("shaders", 1)
        elif source == "followup":
            task["max_urls"] = min(int(task.get("max_urls") or 12), 12)
        elif source == "web_discovery":
            task["search_limit"] = min(max(1, int(task.get("search_limit") or 6)), 6)
            task["max_urls"] = min(max(1, int(task.get("max_urls") or 4)), 4)
        elif source == "fetch_url":
            task.setdefault("timeout", 35)
        elif source == "mcagent_context":
            task.setdefault("timeout_seconds", DEFAULT_MCAGENT_CONTEXT_TIMEOUT_SECONDS)
        elif source == "playwright":
            task["search_limit"] = min(max(1, int(task.get("search_limit") or 4)), 4)
            task["max_urls"] = min(max(1, int(task.get("max_urls") or 2)), 2)
        elif source in {"mediawiki", "ftbwiki", "createwiki"}:
            task.setdefault("search_limit", 8)
    tasks.sort(key=lambda item: priority.get(_source_alias(str(item.get("source") or "")), 0), reverse=True)
    return tasks[:max(1, max_tasks)]


def _crawler_task_identity(task: dict[str, Any]) -> tuple[str, str]:
    source = _source_alias(str(task.get("source") or ""))
    query = re.sub(r"\s+", " ", str(task.get("query") or "").strip()).lower()
    return source, query


def _crawler_bad_result(result: dict[str, Any]) -> bool:
    observation = classify_crawler_tool_result(result)
    result.setdefault("observation", observation.to_dict())
    return observation.bad


def _crawler_failure_summary(task_results: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for result in task_results[-limit:]:
        manifest_stats = result.get("manifest_stats") if isinstance(result.get("manifest_stats"), dict) else {}
        topic_validation = result.get("topic_validation") if isinstance(result.get("topic_validation"), dict) else {}
        observation = classify_crawler_tool_result(result)
        result.setdefault("observation", observation.to_dict())
        summary.append(
            {
                "source": result.get("source"),
                "query": result.get("query"),
                "reason": observation.status,
                "observation_summary": observation.summary,
                "retryable": observation.retryable,
                "suggested_next": observation.suggested_next,
                "returncode": result.get("returncode"),
                "records": manifest_stats.get("records", 0),
                "skipped": manifest_stats.get("skipped", 0),
                "errors": manifest_stats.get("errors", 0),
                "topic_reason": topic_validation.get("reason"),
                "output_tail": _tail_text(str(result.get("output") or ""), limit=500),
            }
        )
    return summary


def _replan_crawler_tasks(
    question: str,
    config: AppConfig,
    plan: dict[str, Any],
    task_results: list[dict[str, Any]],
    existing_tasks: list[dict[str, Any]],
    *,
    max_new_tasks: int = 6,
) -> list[dict[str, Any]]:
    materializer = CrawlerTaskMaterializationService()
    failure_summary = _crawler_failure_summary(task_results)
    session_summary = materializer.replan_session_summary(
        question=question,
        plan=plan,
        failure_summary=failure_summary,
        existing_tasks=existing_tasks,
        identity_fn=_crawler_task_identity,
    )
    replan_question = materializer.replan_question(question=question)
    new_plan = plan_crawler_tasks_resilient(
        replan_question,
        config.paths.source_dir,
        max_tasks=max(1, max_new_tasks),
        session_summary=session_summary,
    )
    new_tasks = materializer.materialize_replan_tasks(
        new_plan=new_plan,
        existing_tasks=existing_tasks,
        identity_fn=_crawler_task_identity,
        source_alias_fn=_source_alias,
        max_new_tasks=max_new_tasks,
    )
    if new_tasks:
        materializer.record_replan(
            plan=plan,
            task_results_count=len(task_results),
            failure_summary=failure_summary,
            new_tasks=new_tasks,
            new_plan=new_plan,
        )
    return new_tasks


def _fallback_tasks_from_topic_discovery(result: dict[str, Any], existing_tasks: list[dict[str, Any]], *, max_new_tasks: int = 16) -> list[dict[str, Any]]:
    manifest_path = (result.get("manifest_stats") or {}).get("manifest_path") if isinstance(result.get("manifest_stats"), dict) else ""
    if not manifest_path:
        return []
    data = _read_json_file(Path(str(manifest_path)))
    seed_queries = data.get("seed_queries") if isinstance(data.get("seed_queries"), list) else []
    return CrawlerTaskMaterializationService().fallback_topic_tasks(
        seed_queries=seed_queries,
        existing_tasks=existing_tasks,
        identity_fn=_crawler_task_identity,
        max_new_tasks=max_new_tasks,
        context_text=str(result.get("query") or result.get("question") or ""),
    )


def _llm_tasks_from_topic_discovery(
    question: str,
    config: AppConfig,
    result: dict[str, Any],
    existing_tasks: list[dict[str, Any]],
    *,
    max_new_tasks: int = 16,
) -> list[dict[str, Any]]:
    manifest_path = (result.get("manifest_stats") or {}).get("manifest_path") if isinstance(result.get("manifest_stats"), dict) else ""
    if not manifest_path:
        return []
    data = _read_json_file(Path(str(manifest_path)))
    seed_queries = data.get("seed_queries") if isinstance(data.get("seed_queries"), list) else []
    phrases = data.get("discovered_phrases") if isinstance(data.get("discovered_phrases"), list) else []
    source_files = data.get("source_files") if isinstance(data.get("source_files"), list) else []
    if not seed_queries and not phrases:
        return []
    materializer = CrawlerTaskMaterializationService()
    existing_brief = materializer.existing_brief(existing_tasks, identity_fn=_crawler_task_identity)
    try:
        plan = review_topic_discovery_candidates(
            question,
            [str(item) for item in seed_queries],
            [str(item) for item in phrases],
            existing_brief,
            max_tasks=max_new_tasks,
        )
    except Exception as exc:  # noqa: BLE001
        result["topic_discovery_review_error"] = f"{type(exc).__name__}: {exc}"
        return []
    new_tasks = materializer.materialize_topic_review_tasks(
        review_plan=plan,
        existing_tasks=existing_tasks,
        identity_fn=_crawler_task_identity,
        source_alias_fn=_source_alias,
        max_new_tasks=max_new_tasks,
    )
    if new_tasks:
        return new_tasks
    return _fallback_tasks_from_topic_discovery(result, existing_tasks, max_new_tasks=max_new_tasks)


def _reflect_crawler_progress_with_timeout(
    question: str,
    plan: dict[str, Any],
    task_results: list[dict[str, Any]],
    pending_tasks: list[dict[str, Any]],
    *,
    session_summary: dict[str, Any] | None,
    max_new_tasks: int,
    timeout_seconds: int = 90,
) -> dict[str, Any]:
    timeout_limit = max(1, int(timeout_seconds))
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        reflect_crawler_progress,
        question,
        plan,
        task_results,
        pending_tasks,
        session_summary=session_summary,
        max_new_tasks=max_new_tasks,
    )
    try:
        return future.result(timeout=timeout_limit)
    except concurrent.futures.TimeoutError:
        future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        bad_recent = sum(1 for item in task_results[-3:] if classify_crawler_tool_result(item).bad)
        if pending_tasks:
            issues = ["reflection_timeout_continued_with_pending_task"]
            if bad_recent >= 2:
                issues.append("reflection_timeout_low_yield_but_pending_task_available")
            return {
                "action": "execute_pending",
                "selected_index": 0,
                "reason": f"CrawlerAgent reflection timed out after {timeout_limit}s; continue with the next already planned objective task instead of letting reflection latency end the job.",
                "tasks": [],
                "done_summary": "",
                "planner": "runtime_reflection_timeout",
                "contract": {
                    "valid": True,
                    "issues": issues,
                    "requires_llm_task_materialization": False,
                    "pending_count": len(pending_tasks),
                },
            }
        if bad_recent >= 2:
            return {
                "action": "finish",
                "selected_index": 0,
                "reason": (
                    f"CrawlerAgent reflection timed out after {timeout_limit}s after repeated empty/off-topic observations; "
                    "finish with the objective evidence already collected instead of executing more slow pending tasks blindly."
                ),
                "tasks": [],
                "done_summary": "CrawlerAgent stopped after repeated low-yield observations and a reflection timeout.",
                "planner": "runtime_reflection_timeout",
                "contract": {
                    "valid": True,
                    "issues": ["reflection_timeout_finished_after_low_yield"],
                    "requires_llm_task_materialization": False,
                    "pending_count": len(pending_tasks),
                },
            }
        return {
            "action": "finish",
            "selected_index": 0,
            "reason": f"CrawlerAgent reflection timed out after {timeout_limit}s; finish with the objective observations collected so far instead of hanging the job.",
            "tasks": [],
            "done_summary": "CrawlerAgent reflection timed out while reviewing recent tool observations.",
            "planner": "runtime_reflection_timeout",
        }
    finally:
        if future.done():
            executor.shutdown(wait=False, cancel_futures=True)


def _run_crawler_job(job: Job, payload: dict[str, Any], config: AppConfig) -> None:
    run_crawler_job_graph(config, job, payload, agent_loop=_run_crawler_job_agent_loop)


def _prepare_crawler_job_plan(
    *,
    job: Job,
    payload: dict[str, Any],
    config: AppConfig,
    source: str,
    question: str,
    job_setup: CrawlerJobSetupService,
    job_progress: CrawlerJobProgressService,
) -> dict[str, Any]:
    plan: dict[str, Any] = {}
    if job_setup.is_planner_source(source):
        session_summary = payload.get("session_summary") if isinstance(payload.get("session_summary"), dict) else None
        max_tasks = int(payload.get("max_tasks") or 16)
        if job.stop_requested:
            _update_job(job, ended_at=time.time(), **job_setup.stopped_update(stage="before_plan"))
            return {"stopped": True, "plan": plan, "tasks": [], "session_summary": session_summary}
        if not bool(payload.get("include_completed")):
            plan = _plan_crawler_with_job_timeout(job, question, config, max_tasks, session_summary)
            if plan.get("stopped"):
                _update_job(job, ended_at=time.time(), **job_setup.stopped_update(stage="planning", plan=plan))
                return {"stopped": True, "plan": plan, "tasks": [], "session_summary": session_summary}
            tasks = list(plan.get("tasks") or [])
        else:
            plan = plan_crawler_tasks(question, config.paths.source_dir, max_tasks=max_tasks, include_completed=True)
            tasks = list(plan.get("tasks") or [])
        if job.stop_requested:
            _update_job(job, ended_at=time.time(), **job_setup.stopped_update(stage="after_plan", plan=plan, tasks=tasks))
            return {"stopped": True, "plan": plan, "tasks": tasks, "session_summary": session_summary}
        if not tasks:
            tasks = _all_source_tasks(question, config, include_completed=True, session_summary=session_summary, max_tasks=max_tasks)
            plan = job_setup.fallback_plan(tasks=tasks)
        planned_update = job_progress.planned(topic=str(plan.get("topic") or question), task_count=len(tasks), plan=plan, tasks=tasks)
        if isinstance(job.result, dict) and isinstance(planned_update.get("result"), dict):
            for key in ("reuse_signature", "requested_by", "delivery_target", "agent_message"):
                if key in job.result and key not in planned_update["result"]:
                    planned_update["result"][key] = job.result[key]
        _update_job(job, **planned_update)
        return {"stopped": False, "plan": plan, "tasks": tasks, "session_summary": session_summary}
    tasks = job_setup.single_source_tasks(source=source, payload=payload, question=question)
    return {"stopped": False, "plan": plan, "tasks": tasks, "session_summary": None}


def _prepare_crawler_task_execution(
    *,
    payload: dict[str, Any],
    task: dict[str, Any],
    question: str,
    plan: dict[str, Any],
    current_index: int,
    artifact_refs: ArtifactReferenceService,
    task_preparation: CrawlerTaskPreparationService,
) -> dict[str, Any]:
    task_source = _source_alias(str(task.get("source") or "mediawiki"))
    task_payload = task_preparation.build_payload(base_payload=payload, task=task, question=question, task_source=task_source)
    task_payload = artifact_refs.resolve_payload_refs(task_payload, list(plan.get("artifact_refs") or []))
    if task_source == "fetch_url" and _looks_like_archive_url(str(task_payload.get("query") or "")):
        task_source = "modpack_download"
        task_payload["source"] = "modpack_download"
        task_payload["reason"] = (
            str(task_payload.get("reason") or "")
            + " Executor routed binary .mrpack/.zip URL to modpack_download because fetch_url only extracts readable text."
        ).strip()
        plan.setdefault("agent_reflections", []).append(
            {
                "at_index": current_index,
                "action": "route_archive_url_to_modpack_download",
                "reason": "Objective tool boundary: fetch_url is for readable text; binary .mrpack/.zip URLs must be probed/downloaded by modpack_download.",
                "planner": "executor objective routing",
                "task": {"source": task_source, "query": str(task_payload.get("query") or "")},
            }
        )
    preflight_task = dict(task_payload)
    preflight_task["reason"] = str(task.get("reason") or task_payload.get("reason") or "")
    preflight_context = "\n".join(
        str(item or "")
        for item in (
            question,
            plan.get("topic"),
            plan.get("target_hint"),
            plan.get("delivery_target"),
            plan.get("package_type"),
            plan.get("coverage_goals"),
        )
    )
    return {
        "task_source": task_source,
        "task_payload": task_payload,
        "preflight_task": preflight_task,
        "preflight_context": preflight_context,
    }


def _record_crawler_task_result_metadata(
    *,
    result: dict[str, Any],
    task: dict[str, Any],
    task_source: str,
    task_payload: dict[str, Any],
    question: str,
    plan: dict[str, Any],
    result_index: int,
    artifact_refs: ArtifactReferenceService,
) -> int:
    result["query"] = str(task_payload.get("query") or "")
    result["reason"] = str(task.get("reason") or "")
    result["manifest_stats"] = _crawler_manifest_stats(str(result.get("export_dir") or ""))
    if not result.get("export_dir") and int(result.get("returncode") or 0) != 0:
        result["manifest_stats"] = _inline_failure_manifest_stats(result)
    new_artifact_refs = artifact_refs.collect_from_result(result=result, result_index=result_index)
    if new_artifact_refs:
        compact_refs = artifact_refs.compact_refs(new_artifact_refs, limit=12)
        result["artifact_refs"] = compact_refs
        plan.setdefault("artifact_refs", [])
        plan["artifact_refs"].extend(compact_refs)
        plan["artifact_refs"] = plan["artifact_refs"][-60:]
    records_loaded = int(result["manifest_stats"].get("records") or 0)
    existing_evidence = (
        _crawler_reusable_duplicate_evidence(
            str(result.get("export_dir") or ""),
            question,
            str(task_payload.get("query") or ""),
            plan,
        )
        if result["returncode"] == 0 and records_loaded == 0 and int(result["manifest_stats"].get("skipped") or 0) > 0
        else {"matched": False, "records": []}
    )
    if existing_evidence.get("matched"):
        result["existing_evidence_reused"] = existing_evidence
    elif existing_evidence.get("reason") or existing_evidence.get("notes") or existing_evidence.get("cleanup_action"):
        result["existing_evidence_review"] = existing_evidence
    if result["returncode"] == 0 and records_loaded > 0 and task_source not in {"modpack_download"}:
        result["topic_validation"] = _crawler_topic_match(
            str(result.get("export_dir") or ""),
            question,
            str(task_payload.get("query") or ""),
            plan,
        )
    return records_loaded


def _apply_crawler_task_accounting(
    *,
    result: dict[str, Any],
    task_source: str,
    task_payload: dict[str, Any],
    question: str,
    payload: dict[str, Any],
    plan: dict[str, Any],
    tasks: list[dict[str, Any]],
    index: int,
    max_total_tasks: int,
    result_accounting: CrawlerResultAccountingService,
) -> dict[str, Any]:
    accounting = result_accounting.apply(
        result=result,
        task_source=task_source,
        delivery_target=str(plan.get("delivery_target") or payload.get("delivery_target") or ""),
        followup_query=str(task_payload.get("query") or question),
    )
    accepted_export_dirs: list[str] = []
    accepted_ingest_roots: list[str] = []
    if accounting.get("needs_ingest") and result.get("export_dir"):
        accepted_export_dirs.append(str(result.get("export_dir") or ""))
        accepted_ingest_roots.extend(_crawler_accepted_ingest_roots(result))
    followup_task = accounting.get("followup_task") if isinstance(accounting.get("followup_task"), dict) else None
    inserted_followup = False
    followup_is_required_external_probe = task_source == "mcagent_context" and str(followup_task.get("source") or "") != "mcagent_context" if followup_task else False
    has_capacity = len(tasks) < max_total_tasks or followup_is_required_external_probe
    if followup_task and _crawler_task_identity(followup_task) not in {_crawler_task_identity(existing) for existing in tasks} and has_capacity:
        tasks.insert(index, followup_task)
        inserted_followup = True
        if followup_is_required_external_probe and len(tasks) > max_total_tasks:
            plan["max_total_tasks_extended_for_context_followup"] = len(tasks)
        reason = str(followup_task.get("reason") or "Objective tool result produced a required executable follow-up.")
        plan.setdefault("agent_reflections", []).append(
            {
                "at_index": index,
                "action": "add_tasks",
                "reason": reason,
                "planner": "executor objective result",
                "tasks": [followup_task],
            }
        )
    return {
        "success_delta": int(accounting.get("success_delta") or 0),
        "candidate_delta": int(accounting.get("candidate_delta") or 0),
        "failure_delta": int(accounting.get("failure_delta") or 0),
        "needs_ingest": bool(accounting.get("needs_ingest")),
        "accepted_export_dirs": accepted_export_dirs,
        "accepted_ingest_roots": accepted_ingest_roots,
        "inserted_followup": inserted_followup,
        "followup_task": followup_task if inserted_followup else None,
    }


def _execute_crawler_task_step(
    *,
    job: Job,
    config: AppConfig,
    payload: dict[str, Any],
    task: dict[str, Any],
    question: str,
    plan: dict[str, Any],
    tasks: list[dict[str, Any]],
    index: int,
    task_results: list[dict[str, Any]],
    session_summary: dict[str, Any] | None,
    artifact_refs: ArtifactReferenceService,
    task_preparation: CrawlerTaskPreparationService,
    result_accounting: CrawlerResultAccountingService,
    job_progress: CrawlerJobProgressService,
    max_total_tasks: int,
) -> dict[str, Any]:
    prepared_task = _prepare_crawler_task_execution(
        payload=payload,
        task=task,
        question=question,
        plan=plan,
        current_index=index,
        artifact_refs=artifact_refs,
        task_preparation=task_preparation,
    )
    task_source = prepared_task["task_source"]
    task_payload = prepared_task["task_payload"]
    preflight_task = prepared_task["preflight_task"]
    preflight_context = prepared_task["preflight_context"]
    preflight_result = task_preparation.blocked_preflight_result(
        task_source=task_source,
        task=preflight_task,
        context_text=preflight_context,
    )
    if preflight_result is not None:
        task_results.append(preflight_result)
        plan.setdefault("agent_reflections", []).append(
            {
                "at_index": index,
                "action": "blocked_by_capability_preflight",
                "reason": "Executor objective preflight found the selected crawler tool call is missing required inputs or domain contract.",
                "planner": "crawler capability registry",
                "task": {"source": task_source, "query": str(task_payload.get("query") or "")},
                "contract": preflight_result.get("capability_preflight") or {},
            }
        )
        _update_job(job, **job_progress.empty_query_blocked(source_label=_source_label(task_source), task_results=task_results, tasks=tasks, plan=plan))
        return {
            "task_source": task_source,
            "task_payload": task_payload,
            "result": preflight_result,
            "records_loaded": 0,
            "failure_delta": 1,
            "bad_streak_delta": 1,
            "continue_loop": True,
            "blocked": True,
        }
    if not str(task_payload.get("query") or "").strip():
        result = task_preparation.empty_query_result(task_source=task_source, task=task)
        task_results.append(result)
        _update_job(job, **job_progress.empty_query_blocked(source_label=_source_label(task_source), task_results=task_results, tasks=tasks, plan=plan))
        return {
            "task_source": task_source,
            "task_payload": task_payload,
            "result": result,
            "records_loaded": 0,
            "failure_delta": 1,
            "bad_streak_delta": 1,
            "continue_loop": True,
            "blocked": True,
        }
    _update_job(
        job,
        **job_progress.executing(
            index=index,
            task_count=len(tasks),
            source_label=_source_label(task_source),
            query=str(task_payload["query"]),
            reason=str(task.get("reason") or ""),
            task_results=task_results,
            tasks=tasks,
            plan=plan,
        ),
    )
    if task_source == "mcagent_context":
        result = _run_mcagent_context_tool(config, task_payload, plan, session_summary)
    else:
        result = _run_crawler_command(_round_command(task_source, task_payload), task_source, job=job)
    records_loaded = _record_crawler_task_result_metadata(
        result=result,
        task=task,
        task_source=task_source,
        task_payload=task_payload,
        question=question,
        plan=plan,
        result_index=len(task_results) + 1,
        artifact_refs=artifact_refs,
    )
    accounting_update = _apply_crawler_task_accounting(
        result=result,
        task_source=task_source,
        task_payload=task_payload,
        question=question,
        payload=payload,
        plan=plan,
        tasks=tasks,
        index=index,
        max_total_tasks=max_total_tasks,
        result_accounting=result_accounting,
    )
    result["observation"] = classify_crawler_tool_result(result).to_dict()
    task_results.append(result)
    return {
        "task_source": task_source,
        "task_payload": task_payload,
        "result": result,
        "records_loaded": records_loaded,
        "success_delta": int(accounting_update.get("success_delta") or 0),
        "candidate_delta": int(accounting_update.get("candidate_delta") or 0),
        "failure_delta": int(accounting_update.get("failure_delta") or 0),
        "needs_ingest": bool(accounting_update.get("needs_ingest")),
        "accepted_export_dirs": list(accounting_update.get("accepted_export_dirs") or []),
        "accepted_ingest_roots": list(accounting_update.get("accepted_ingest_roots") or []),
        "continue_loop": False,
        "blocked": False,
    }


def _finish_crawler_loop(plan: dict[str, Any], *, index: int, reason: str, planner: str) -> dict[str, Any]:
    plan["agent_finish_reason"] = reason
    plan.setdefault("agent_reflections", []).append(
        {
            "at_index": index,
            "action": "finish",
            "reason": reason,
            "planner": planner,
        }
    )
    return {"action": "finish", "bad_streak": None, "replan_count": None}


def _coverage_goals_need_guide_or_mechanics(plan: dict[str, Any]) -> bool:
    text = json.dumps(
        {
            "coverage_goals": plan.get("coverage_goals"),
            "topic": plan.get("topic"),
            "target_hint": plan.get("target_hint"),
            "question": plan.get("question"),
            "reason": plan.get("reason"),
            "model_prior": plan.get("model_prior"),
        },
        ensure_ascii=False,
        default=str,
    ).lower()
    return bool(re.search(r"玩法|入门|新手|教程|攻略|机制|配方|烹饪|食物|guide|wiki|tutorial|beginner|getting started|mechanic|recipe|cooking|food", text, flags=re.I))


def _accepted_results_cover_guide_or_mechanics(task_results: list[dict[str, Any]]) -> bool:
    for result in task_results:
        if not isinstance(result, dict):
            continue
        observation = result.get("observation") if isinstance(result.get("observation"), dict) else {}
        validation = result.get("topic_validation") if isinstance(result.get("topic_validation"), dict) else {}
        reused = result.get("existing_evidence_reused") if isinstance(result.get("existing_evidence_reused"), dict) else {}
        accepted = observation.get("status") == "ok" or bool(validation.get("matched")) or bool(reused.get("matched"))
        if not accepted:
            continue
        haystack = "\n".join(
            [
                str(result.get("query") or ""),
                str(result.get("output") or ""),
                json.dumps(result.get("artifact_refs") or [], ensure_ascii=False, default=str),
                json.dumps(validation, ensure_ascii=False, default=str),
                json.dumps(reused, ensure_ascii=False, default=str),
            ]
        ).lower()
        if re.search(r"玩法|入门|新手|教程|攻略|机制|配方|烹饪|食物|guide|wiki|tutorial|beginner|getting started|mechanic|recipe|cooking|food", haystack, flags=re.I):
            return True
    return False


def _should_continue_for_unmet_guide_coverage(plan: dict[str, Any], task_results: list[dict[str, Any]]) -> bool:
    return _coverage_goals_need_guide_or_mechanics(plan) and not _accepted_results_cover_guide_or_mechanics(task_results)


def _agent_selected_delegate(action_plan: list[dict[str, Any]] | list[Any], route_intent: str, tool_decision: dict[str, Any]) -> bool:
    selected_tool = str(tool_decision.get("tool") or route_intent or "").strip()
    return selected_tool == "delegate_crawler" or _action_plan_has_tool(action_plan, "delegate_crawler")


def _apply_crawler_loop_control_after_task(
    *,
    job: Job,
    config: AppConfig,
    payload: dict[str, Any],
    source: str,
    question: str,
    plan: dict[str, Any],
    tasks: list[dict[str, Any]],
    task_results: list[dict[str, Any]],
    index: int,
    success_count: int,
    candidate_count: int,
    bad_streak: int,
    replan_count: int,
    max_replans: int,
    max_total_tasks: int,
    loop_control: CrawlerLoopControlService,
    job_progress: CrawlerJobProgressService,
) -> dict[str, Any]:
    loop_signal = loop_control.update_bad_streak(result=task_results[-1], current_bad_streak=bad_streak) if task_results else {"bad_streak": bad_streak}
    bad_streak = int(loop_signal.get("bad_streak") or 0)
    if loop_control.should_replan(
        source=source,
        success_count=success_count,
        bad_streak=bad_streak,
        replan_count=replan_count,
        max_replans=max_replans,
        task_count=len(tasks),
        max_total_tasks=max_total_tasks,
    ):
        replan_count += 1
        _update_job(job, **job_progress.replanning(bad_streak=bad_streak, replan_count=replan_count, max_replans=max_replans, task_results=task_results, tasks=tasks, plan=plan))
        remaining_slots = max(0, max_total_tasks - len(tasks))
        new_tasks = _replan_crawler_tasks(
            question,
            config,
            plan,
            task_results,
            tasks,
            max_new_tasks=min(6, remaining_slots),
        )
        new_tasks = _drop_duplicate_mcagent_context_tasks(new_tasks, task_results)
        if new_tasks:
            tasks.extend(new_tasks)
        return {"action": "continue", "bad_streak": 0, "replan_count": replan_count, "new_tasks": new_tasks}
    if loop_control.should_replan_after_plan_exhausted(
        source=source,
        success_count=success_count,
        bad_streak=bad_streak,
        replan_count=replan_count,
        max_replans=max_replans,
        current_index=index,
        task_count=len(tasks),
        max_total_tasks=max_total_tasks,
    ):
        replan_count += 1
        _update_job(job, **job_progress.replanning(bad_streak=bad_streak, replan_count=replan_count, max_replans=max_replans, task_results=task_results, tasks=tasks, plan=plan))
        remaining_slots = max(0, max_total_tasks - len(tasks))
        new_tasks = _replan_crawler_tasks(
            question,
            config,
            plan,
            task_results,
            tasks,
            max_new_tasks=min(6, remaining_slots),
        )
        new_tasks = _drop_duplicate_mcagent_context_tasks(new_tasks, task_results)
        if new_tasks:
            tasks.extend(new_tasks)
            plan.setdefault("agent_reflections", []).append(
                {
                    "at_index": index,
                    "action": "replan_after_plan_exhausted",
                    "reason": "The initial executable plan was exhausted with no usable evidence; CrawlerAgent replanned from the objective failed observation instead of finalizing immediately.",
                    "planner": "Crawler replan LLM",
                    "tasks": new_tasks,
                }
            )
            return {"action": "continue", "bad_streak": 0, "replan_count": replan_count, "new_tasks": new_tasks}
        plan.setdefault("agent_reflections", []).append(
            {
                "at_index": index,
                "action": "replan_after_plan_exhausted_no_tasks",
                "reason": "The initial executable plan failed, but CrawlerAgent did not produce additional executable tasks.",
                "planner": "Crawler replan LLM",
            }
        )
        return {"action": "continue", "bad_streak": bad_streak, "replan_count": replan_count, "new_tasks": []}
    if loop_control.should_finish_after_gap_probe_satisfied(
        source=source,
        task_results=task_results,
        candidate_count=candidate_count,
        success_count=success_count,
    ):
        return _finish_crawler_loop(
            plan,
            index=index,
            reason="MCagent/RAG returned usable local context with no explicit gap list, and Crawler collected at least one external probe/candidate; finish this inter-agent gap check instead of expanding into a full crawl.",
            planner="executor inter-agent gap guard",
        )
    guide_coverage_unmet = _should_continue_for_unmet_guide_coverage(plan, task_results)
    if loop_control.should_finish_after_gap_summary_handoff_success(
        source=source,
        plan=plan,
        task_results=task_results,
        success_count=success_count,
        executed_count=len(task_results),
    ) and not guide_coverage_unmet:
        return _finish_crawler_loop(
            plan,
            index=index,
            reason="Crawler received a concrete MCagent/RAG gap handoff and has collected at least one usable external evidence batch; finish now so the accepted material can be ingested and MCagent can answer from it, instead of exhausting every slow source.",
            planner="executor gap-summary handoff success guard",
        )
    if loop_control.should_finish_after_rag_success_checkpoint(
        source=source,
        plan=plan,
        success_count=success_count,
        executed_count=len(task_results),
    ) and not guide_coverage_unmet:
        return _finish_crawler_loop(
            plan,
            index=index,
            reason="Crawler has already collected usable evidence for MCagent/RAG; finish now so accepted material can be ingested instead of spending the job budget on slower follow-up discovery.",
            planner="executor RAG success checkpoint",
        )
    if _crawler_requested_output_dir(payload, plan) and success_count > 0:
        return _finish_crawler_loop(
            plan,
            index=index,
            reason="Crawler collected usable evidence for a user-specified local delivery path; finish now so the result can be exported instead of continuing slower broad searches.",
            planner="executor user-output delivery checkpoint",
        )
    if loop_control.should_finish_after_context_plus_external_checkpoint(
        source=source,
        task_results=task_results,
        candidate_count=candidate_count,
        success_count=success_count,
        bad_streak=bad_streak,
        executed_count=len(task_results),
    ) and not guide_coverage_unmet:
        return _finish_crawler_loop(
            plan,
            index=index,
            reason="Crawler has received MCagent/RAG context and found at least one external candidate or accepted source; recent follow-up tools are low-yield, so finish with the usable material and remaining gaps instead of exhausting slow browser tasks.",
            planner="executor context-plus-external checkpoint",
        )
    if loop_control.should_finish_after_enough_success(
        source=source,
        success_count=success_count,
        executed_count=len(task_results),
        task_count=len(tasks),
        max_total_tasks=max_total_tasks,
    ):
        return _finish_crawler_loop(
            plan,
            index=index,
            reason="Crawler collected enough useful evidence and reached the job task budget; finish now with the accepted evidence and remaining gaps instead of extending the task list again.",
            planner="executor task-budget guard",
        )
    if loop_control.should_finish_after_useful_low_yield(
        source=source,
        success_count=success_count,
        bad_streak=bad_streak,
        executed_count=len(task_results),
    ):
        return _finish_crawler_loop(
            plan,
            index=index,
            reason="Useful crawler evidence was collected, and recent follow-up tasks became low-yield; finish now and report remaining gaps instead of exhausting similar searches.",
            planner="executor low-yield guard",
        )
    if loop_control.should_finish_after_no_success_low_yield(
        source=source,
        success_count=success_count,
        bad_streak=bad_streak,
        executed_count=len(task_results),
    ):
        return _finish_crawler_loop(
            plan,
            index=index,
            reason="Several crawler actions produced no usable external evidence; finish now with a clear blocked/low-yield report instead of continuing similar searches.",
            planner="executor no-success low-yield guard",
        )
    return {"action": "continue", "bad_streak": bad_streak, "replan_count": replan_count, "new_tasks": []}


def _apply_crawler_reflection_before_task(
    *,
    job: Job,
    config: AppConfig,
    question: str,
    plan: dict[str, Any],
    tasks: list[dict[str, Any]],
    task_results: list[dict[str, Any]],
    index: int,
    session_summary: dict[str, Any] | None,
    max_total_tasks: int,
    runtime_step: CrawlerRuntimeStepService,
    task_materializer: CrawlerTaskMaterializationService,
    job_progress: CrawlerJobProgressService,
) -> dict[str, Any]:
    pending_tasks = list(tasks[index:])
    reflection = _reflect_crawler_progress_with_timeout(
        question,
        plan,
        task_results,
        pending_tasks,
        session_summary=session_summary,
        max_new_tasks=max(1, min(4, max_total_tasks - len(tasks))),
    )
    plan.setdefault("agent_reflections", []).append(runtime_step.reflection_entry(index=index, reflection=reflection))
    _update_job(job, **job_progress.reflecting(reflection=reflection, task_results=task_results, tasks=tasks, plan=plan))
    action = str(reflection.get("action") or "execute_pending")
    new_tasks = [task for task in list(reflection.get("tasks") or []) if isinstance(task, dict)]
    new_tasks = _drop_duplicate_mcagent_context_tasks(new_tasks, task_results)
    new_tasks, blocked_tasks = task_materializer.filter_executable_reflection_tasks(new_tasks)
    if blocked_tasks:
        contract = reflection.get("contract") if isinstance(reflection.get("contract"), dict) else {}
        contract.setdefault("issues", [])
        contract["valid"] = False
        contract["requires_llm_task_materialization"] = True
        contract["blocked_unexecutable_tasks"] = blocked_tasks
        for blocked_task in blocked_tasks:
            issue = str(blocked_task.get("blocked_reason") or "crawler_capability_preflight_failed")
            if issue not in contract["issues"]:
                contract["issues"].append(issue)
        reflection["contract"] = contract
        plan.setdefault("agent_reflections", []).append(
            {
                "at_index": index,
                "action": "blocked_unexecutable_tasks",
                "reason": "CrawlerAgent proposed tool tasks missing required local inputs; executor returned the objective contract issue for CrawlerAgent reflection.",
                "planner": "executor objective contract",
                "tasks": blocked_tasks,
                "contract": {
                    "valid": False,
                    "issues": ["modpack_internal_requires_archive_path"],
                    "requires_llm_task_materialization": True,
                },
            }
        )
        task_results.append(
            {
                "source": "crawler_reflection_contract",
                "returncode": 2,
                "command": [],
                "output": "CrawlerAgent proposed tool tasks that failed objective capability preflight; no tool judged content relevance.",
                "timeout_seconds": 0,
                "timed_out": False,
                "export_dir": "",
                "query": "; ".join(str(task.get("query") or "") for task in blocked_tasks[:3]),
                "reason": "Objective capability contract feedback for CrawlerAgent reflection.",
                "manifest_stats": {"records": 0, "skipped": 0, "errors": len(blocked_tasks)},
                "capability_preflight": {"valid": False, "blocked_tasks": blocked_tasks},
                "empty_result": True,
                "observation": {
                    "tool_name": "crawler_reflection_contract",
                    "status": "empty",
                    "summary": "CrawlerAgent proposed non-executable tool calls; it must choose tools whose required inputs are present.",
                    "detail": {"blocked_tasks": blocked_tasks[:3]},
                    "retryable": True,
                    "suggested_next": "Choose web_discovery/playwright/modrinth/mcmod/modpack_download first, or provide a real archive_path before modpack_internal.",
                },
            }
        )
    contract = reflection.get("contract") if isinstance(reflection.get("contract"), dict) else {}
    needs_materialization = bool(contract.get("requires_llm_task_materialization"))
    if (
        action in {"add_tasks", "replan"}
        and needs_materialization
        and _has_successful_mcagent_context(task_results)
        and pending_tasks
        and not _reflection_requests_local_source_materialization(reflection, task_results)
    ):
        action = "execute_pending"
        reflection["action"] = "execute_pending"
        reflection["selected_index"] = 0
        reflection["reason"] = (
            "MCagent context is already available in the previous tool result; skip another LLM materialization pass "
            "and continue with the pending objective collection tools."
        )
        new_tasks = []
        needs_materialization = False
        plan.setdefault("agent_reflections", []).append(
            {
                "at_index": index,
                "action": "skip_unmaterialized_replan_after_mcagent_context",
                "reason": reflection["reason"],
                "planner": "executor objective result",
            }
        )
    if action in {"add_tasks", "replan"} and needs_materialization:
        remaining_slots = max(0, max_total_tasks - len(tasks))
        local_source_requested = _reflection_requests_local_source_materialization(reflection, task_results)
        materialization_budget = min(4, remaining_slots)
        if local_source_requested and materialization_budget <= 0:
            materialization_budget = min(4, max(1, len(tasks) - index))
        local_materialized_tasks = (
            _materialize_local_source_path_tasks_from_mcagent_context(
                reflection,
                task_results,
                existing_tasks=tasks,
                max_new_tasks=materialization_budget,
            )
            if local_source_requested
            else []
        )
        if local_materialized_tasks:
            new_tasks = local_materialized_tasks
            if remaining_slots <= 0 and action == "add_tasks":
                action = "replan"
                reflection["action"] = "replan"
            plan.setdefault("agent_reflections", []).append(
                {
                    "at_index": index,
                    "action": "local_source_path_tasks_materialized",
                    "reason": (
                        "CrawlerAgent requested inspection of local source paths returned by mcagent_context; "
                        "executor materialized objective read/search tasks for those exact paths without judging their relevance."
                    ),
                    "planner": "executor objective local path materializer",
                    "tasks": new_tasks,
                }
            )
        else:
            new_tasks = _replan_crawler_tasks(
                question,
                config,
                plan,
                task_results,
                tasks,
                max_new_tasks=min(6, remaining_slots),
            )
        new_tasks = _drop_duplicate_mcagent_context_tasks(new_tasks, task_results)
        if new_tasks:
            plan.setdefault("agent_reflections", []).append(
                {
                    "at_index": index,
                    "action": "replan_tasks_generated",
                    "reason": "CrawlerAgent requested replan/add_tasks without executable tasks; the executor returned the contract issue to the Crawler planning LLM to materialize executable tool actions.",
                    "planner": "Crawler replan LLM",
                    "tasks": new_tasks,
                    "contract_issue": contract.get("issues") or [],
                }
            )
    step_result = runtime_step.apply_action(
        tasks=tasks,
        index=index,
        reflection=reflection,
        max_total_tasks=max_total_tasks,
        materialized_tasks=new_tasks,
    )
    return {
        "continue_loop": bool(step_result.get("continue_loop")),
        "finished": bool(step_result.get("finished")),
        "finish_reason": str(step_result.get("finish_reason") or ""),
        "reflection": reflection,
        "new_tasks": new_tasks,
        "inserted_tasks": list(step_result.get("inserted_tasks") or []),
    }


def _apply_crawler_after_task_review(
    *,
    job: Job,
    config: AppConfig,
    question: str,
    plan: dict[str, Any],
    tasks: list[dict[str, Any]],
    task_results: list[dict[str, Any]],
    index: int,
    task_source: str,
    result: dict[str, Any],
    records_loaded: int,
    max_total_tasks: int,
    topic_discovery_review: CrawlerTopicDiscoveryReviewService,
    job_progress: CrawlerJobProgressService,
) -> dict[str, Any]:
    removed_context_tasks: list[dict[str, Any]] = []
    discovered_tasks: list[dict[str, Any]] = []
    if task_source == "mcagent_context" and result.get("returncode") == 0 and records_loaded > 0:
        removed_context_tasks = _prune_pending_mcagent_context_tasks_after_success(tasks, index)
        if removed_context_tasks:
            plan.setdefault("agent_reflections", []).append(
                {
                    "at_index": index,
                    "action": "prune_pending_mcagent_context",
                    "reason": "MCagent has already returned usable local context for this Crawler job; skip duplicate pending mcagent_context tasks and continue with web collection.",
                    "planner": "executor objective result",
                    "tasks": removed_context_tasks,
                }
            )
    if topic_discovery_review.should_review(task_source=task_source, result=result):
        remaining_slots = topic_discovery_review.remaining_slots(max_total_tasks=max_total_tasks, current_task_count=len(tasks))
        _update_job(job, **job_progress.reviewing_candidates(task_results=task_results, tasks=tasks, plan=plan))
        discovered_tasks = _llm_tasks_from_topic_discovery(question, config, result, tasks, max_new_tasks=min(16, remaining_slots))
        if discovered_tasks:
            tasks.extend(discovered_tasks)
            topic_discovery_review.record_review(plan=plan, result=result, task_results_count=len(task_results), discovered_tasks=discovered_tasks)
        elif result.get("topic_discovery_review_error"):
            topic_discovery_review.record_review(plan=plan, result=result, task_results_count=len(task_results), discovered_tasks=[])
    return {
        "removed_context_tasks": removed_context_tasks,
        "discovered_tasks": discovered_tasks,
    }


def _run_crawler_job_agent_loop(job: Job, payload: dict[str, Any], config: AppConfig) -> None:
    source = _source_alias(str(payload.get("source") or "planner"))
    question = str(payload.get("source_question") or payload.get("question") or payload.get("query") or "").strip()
    job_setup = CrawlerJobSetupService()
    runtime_step = CrawlerRuntimeStepService()
    artifact_refs = ArtifactReferenceService()
    task_preparation = CrawlerTaskPreparationService()
    result_accounting = CrawlerResultAccountingService()
    task_materializer = CrawlerTaskMaterializationService()
    loop_control = CrawlerLoopControlService()
    topic_discovery_review = CrawlerTopicDiscoveryReviewService()
    job_finalization = CrawlerJobFinalizationService()
    job_progress = CrawlerJobProgressService()
    _update_job(job, status="running", started_at=time.time(), summary="Crawler job started.")
    try:
        prepared = _prepare_crawler_job_plan(
            job=job,
            payload=payload,
            config=config,
            source=source,
            question=question,
            job_setup=job_setup,
            job_progress=job_progress,
        )
        if prepared.get("stopped"):
            return
        plan = prepared["plan"]
        tasks = prepared["tasks"]
        session_summary = prepared["session_summary"]
        task_results: list[dict[str, Any]] = []
        success_count = 0
        candidate_count = 0
        failure_count = 0
        index = 0
        bad_streak = 0
        replan_count = 0
        needs_ingest = False
        accepted_export_dirs: list[str] = []
        accepted_ingest_roots: list[str] = []
        limits = job_setup.limits(payload=payload, tasks=tasks)
        max_replans = limits["max_replans"]
        max_total_tasks = limits["max_total_tasks"]
        skip_reflection_once_at_index: int | None = None
        while index < len(tasks):
            if job.stop_requested:
                break
            skip_reflection_now = skip_reflection_once_at_index == index
            if skip_reflection_now:
                skip_reflection_once_at_index = None
                plan.setdefault("agent_reflections", []).append(
                    {
                        "at_index": index,
                        "action": "execute_materialized_task",
                        "selected_index": 0,
                        "reason": "CrawlerAgent reflection just materialized an executable task at this index; execute it before asking for another reflection.",
                        "planner": "executor loop control",
                        "tasks": [tasks[index]] if index < len(tasks) else [],
                    }
                )
            if (
                not skip_reflection_now
                and job_setup.is_planner_source(source)
                and runtime_step.should_reflect_before_task(plan=plan, task_results=task_results, index=index)
            ):
                reflection_update = _apply_crawler_reflection_before_task(
                    job=job,
                    config=config,
                    question=question,
                    plan=plan,
                    tasks=tasks,
                    task_results=task_results,
                    index=index,
                    session_summary=session_summary,
                    max_total_tasks=max_total_tasks,
                    runtime_step=runtime_step,
                    task_materializer=task_materializer,
                    job_progress=job_progress,
                )
                if reflection_update.get("continue_loop"):
                    if reflection_update.get("inserted_tasks"):
                        skip_reflection_once_at_index = index
                    continue
                if reflection_update.get("finished"):
                    plan["agent_finish_reason"] = str(reflection_update.get("finish_reason") or "")
                    break
            elif job_setup.is_planner_source(source) and index == 0 and tasks:
                plan.setdefault("agent_reflections", []).append(runtime_step.initial_llm_plan_entry(task=tasks[index]))
            task = tasks[index]
            index += 1
            step = _execute_crawler_task_step(
                job=job,
                config=config,
                payload=payload,
                task=task,
                question=question,
                plan=plan,
                tasks=tasks,
                index=index,
                task_results=task_results,
                session_summary=session_summary,
                artifact_refs=artifact_refs,
                task_preparation=task_preparation,
                result_accounting=result_accounting,
                job_progress=job_progress,
                max_total_tasks=max_total_tasks,
            )
            task_source = str(step.get("task_source") or "")
            result = step.get("result") if isinstance(step.get("result"), dict) else {}
            records_loaded = int(step.get("records_loaded") or 0)
            success_count += int(step.get("success_delta") or 0)
            candidate_count += int(step.get("candidate_delta") or 0)
            failure_count += int(step.get("failure_delta") or 0)
            bad_streak += int(step.get("bad_streak_delta") or 0)
            needs_ingest = needs_ingest or bool(step.get("needs_ingest"))
            accepted_export_dirs.extend(list(step.get("accepted_export_dirs") or []))
            accepted_ingest_roots.extend(list(step.get("accepted_ingest_roots") or []))
            if step.get("continue_loop"):
                continue
            _apply_crawler_after_task_review(
                job=job,
                config=config,
                question=question,
                plan=plan,
                tasks=tasks,
                task_results=task_results,
                index=index,
                task_source=task_source,
                result=result,
                records_loaded=records_loaded,
                max_total_tasks=max_total_tasks,
                topic_discovery_review=topic_discovery_review,
                job_progress=job_progress,
            )
            loop_update = _apply_crawler_loop_control_after_task(
                job=job,
                config=config,
                payload=payload,
                source=source,
                question=question,
                plan=plan,
                tasks=tasks,
                task_results=task_results,
                index=index,
                success_count=success_count,
                candidate_count=candidate_count,
                bad_streak=bad_streak,
                replan_count=replan_count,
                max_replans=max_replans,
                max_total_tasks=max_total_tasks,
                loop_control=loop_control,
                job_progress=job_progress,
            )
            bad_streak = int(loop_update.get("bad_streak") if loop_update.get("bad_streak") is not None else bad_streak)
            replan_count = int(loop_update.get("replan_count") if loop_update.get("replan_count") is not None else replan_count)
            if loop_update.get("action") == "finish":
                break
        collection_summary = _crawler_result_summary(task_results, plan)
        user_delivery = _export_crawler_user_delivery(payload=payload, plan=plan, task_results=task_results, collection_summary=collection_summary)
        if user_delivery:
            plan["user_delivery"] = user_delivery
        final_update = job_finalization.build(
            stop_requested=job.stop_requested,
            success_count=success_count,
            candidate_count=candidate_count,
            failure_count=failure_count,
            replan_count=replan_count,
            needs_ingest=needs_ingest,
            task_results=task_results,
            planned_tasks=tasks,
            plan=plan,
            collection_summary=collection_summary,
        )
        if user_delivery and isinstance(final_update.get("result"), dict):
            final_update["result"]["user_delivery"] = user_delivery
            if user_delivery.get("status") == "ok":
                final_update["summary"] = str(final_update["summary"]) + f" 已导出到用户目录：{user_delivery.get('output_dir')}"
        _update_job(
            job,
            status=final_update["status"],
            ended_at=time.time(),
            summary=str(final_update["summary"]),
            error=final_update["error"],
            result=final_update["result"],
        )
        if needs_ingest and not job.stop_requested:
            ingest_roots = accepted_ingest_roots or accepted_export_dirs
            threading.Thread(target=_run_background_ingest, args=(job.id, config, ingest_roots), daemon=True).start()
        append_memory_event("crawler_plan_completed", {"job_id": job.id, "question": question, "success_count": success_count, "candidate_count": candidate_count, "failure_count": failure_count, "summary": collection_summary, "tasks": task_results})
    except Exception as exc:  # noqa: BLE001
        _update_job(job, status="failed", ended_at=time.time(), summary=_tail_text(traceback.format_exc()), error=f"{type(exc).__name__}: {exc}")


def _plan_crawler_with_job_timeout(job: Job, question: str, config: AppConfig, max_tasks: int, session_summary: dict[str, Any] | None, timeout_seconds: int | None = None) -> dict[str, Any]:
    wait_status = CrawlerPlannerWaitService()
    context = wait_status.context(question=question, session_summary=session_summary)
    planner_topic = context["planner_topic"]
    handoff_brief = context["handoff_brief"]
    delivery_target = context["delivery_target"]
    timeout_limit = max(1, int(timeout_seconds or DEFAULT_CRAWLER_PLANNER_TIMEOUT_SECONDS))
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(plan_crawler_tasks_resilient, question, config.paths.source_dir, max_tasks=max_tasks, session_summary=session_summary)
    started = time.time()
    last_notice = 0.0
    try:
        while True:
            if job.stop_requested:
                future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                return wait_status.stopped_plan(planner_topic=planner_topic, handoff_brief=handoff_brief)
            try:
                return future.result(timeout=0.5)
            except concurrent.futures.TimeoutError:
                elapsed = time.time() - started
                if elapsed >= timeout_limit:
                    future.cancel()
                    executor.shutdown(wait=False, cancel_futures=True)
                    fallback = plan_crawler_tasks_rule_fallback(
                        question,
                        config.paths.source_dir,
                        max_tasks=max_tasks,
                        planner_error=f"planner exceeded {timeout_limit}s startup timeout",
                        session_summary=session_summary,
                    )
                    fallback["planner_timeout_seconds"] = timeout_limit
                    return fallback
                if elapsed - last_notice >= 5:
                    last_notice = elapsed
                    _update_job(job, **wait_status.waiting_update(elapsed_seconds=int(elapsed), planner_topic=planner_topic, handoff_brief=handoff_brief, delivery_target=delivery_target))
    finally:
        if future.done():
            executor.shutdown(wait=False, cancel_futures=True)


def _job_result_with_handoff(job: Job, extra: dict[str, Any]) -> dict[str, Any]:
    result = dict(extra)
    if isinstance(job.result, dict):
        for key in ("collaboration", "mcagent_recheck"):
            if key in job.result and key not in result:
                result[key] = job.result[key]
    return result


def _result_to_dict(result: SearchResult) -> dict[str, Any]:
    raw_html_path = None
    try:
        if result.source_path:
            raw_path = _raw_html_path_for_markdown(Path(result.source_path))
            raw_html_path = str(raw_path) if raw_path and raw_path.exists() else None
    except OSError:
        raw_html_path = None
    return {
        "rank": result.rank,
        "score": result.score,
        "chunk_id": result.chunk_id,
        "document_id": result.document_id,
        "chunk_index": result.chunk_index,
        "title": result.title,
        "source_path": result.source_path,
        "url": result.url,
        "text": result.text,
        "metadata": result.metadata | ({"raw_html_path": raw_html_path} if raw_html_path else {}),
    }


def _search(config: AppConfig, query: str, top_k: int | None = None) -> list[dict[str, Any]]:
    safe_top_k = max(1, min(int(top_k or _adaptive_preview_k(query)), MAX_ROUGH_TOP_K))
    return [_result_to_dict(result) for result in Retriever(config).search(query, top_k=safe_top_k)]


def _corpus_source_bucket(source_path: str, title: str = "") -> str:
    path = str(source_path or "").lower().replace("\\", "/")
    title_lower = str(title or "").lower()
    if "manual_research" in path or "pack_internal" in path or "pack_internals" in path:
        return "整合包内部资料"
    if "resourcepack" in path or "resourcepacks" in path:
        return "资源包"
    if "shader" in path or "shaderpacks" in path:
        return "光影"
    if "/modpack_" in path or "modpack" in path or "modpack_manifests" in path or "modpack_archive" in path:
        return "整合包"
    if any(token in title_lower for token in ("modpack", "整合包")):
        return "整合包"
    if "/mod_" in path or "/mcmod/" in path or "modrinth_agent" in path or "/createwiki/" in path or "/ftbwiki/" in path:
        return "模组"
    if any(token in title_lower for token in ("minecraft mod", " - minecraft mod", "mc百科", "modrinth")):
        return "模组"
    if "mediawiki" in path:
        return "原版/通用资料"
    return "其他资料"


def _corpus_topic_key(title: str, source_path: str = "") -> str:
    path = str(source_path or "").replace("\\", "/")
    pack_match = re.search(r"/manual_research/[^/]*?([^/]+?_pack_internals)(?:/|$)", path, flags=re.I)
    if pack_match:
        value = re.sub(r"_pack_internals$", " pack internals", pack_match.group(1), flags=re.I)
        return value.replace("_", " ")[:120]
    value = re.sub(r"\s*-\s*MC百科.*$", "", str(title or ""), flags=re.I)
    value = re.sub(r"\s*\|\s*最大的Minecraft中文MOD百科.*$", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:120] or "未命名资料"


def _corpus_inventory_item_score(title: str, source_path: str) -> int:
    path = str(source_path or "").lower().replace("\\", "/")
    score = 0
    if any(token in path for token in ("accepted_by_crawler", "modrinth_agent", "/mcmod/", "web_discovery", "fetch_url")):
        score += 50
    if any(token in path for token in ("manifest", "report", "summary", "modpack_manifests", "downloaded_archive_evidence")):
        score += 35
    if any(token in path for token in ("raw_text", "kubejs", "overrides", ".js.txt", ".json.txt")):
        score -= 45
    if re.fullmatch(r"[});\]}.\s]*", str(title or "").strip()):
        score -= 60
    if len(str(title or "").strip()) < 4:
        score -= 20
    return score


def _corpus_intro_from_text(text: str) -> str:
    cleaned_lines: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip(" #-\t")
        if not line:
            continue
        if len(line) < 8 and not re.search(r"[A-Za-z]{3,}|[\u4e00-\u9fff]{4,}", line):
            continue
        if any(token in line for token in ("登录", "注册", "编辑", "历史", "广告", "导航", "版本检索")):
            continue
        cleaned_lines.append(line)
        if len(cleaned_lines) >= 2:
            break
    intro = "；".join(cleaned_lines)
    return intro[:180] if intro else "本地库已有相关页面或采集记录，但简介文本较少。"


def _local_corpus_inventory_answer(config: AppConfig, question: str) -> dict[str, Any]:
    if not config.paths.db_path.exists():
        return {
            "answer": "本地资料库还没有初始化或没有找到数据库文件，所以现在无法盘点已入库资料。",
            "sources": [],
            "context": "",
            "agent": "mcagent_rag",
        }
    conn = connect(config.paths.db_path)
    try:
        rows = conn.execute(
            """
            SELECT
                id AS document_id,
                title,
                source_path,
                url,
                metadata_json,
                imported_at
            FROM documents
            WHERE source_path LIKE '%crawler_exports%'
            ORDER BY imported_at DESC, id DESC
            LIMIT 3000
            """
        ).fetchall()
        total_docs = int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
    finally:
        conn.close()

    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        title = str(row["title"] or "")
        source_path = str(row["source_path"] or "")
        bucket = _corpus_source_bucket(source_path, title)
        topic = _corpus_topic_key(title, source_path)
        bucket_state = buckets.setdefault(bucket, {"count": 0, "items": {}, "sources": []})
        bucket_state["count"] += 1
        items: dict[str, dict[str, Any]] = bucket_state["items"]
        score = _corpus_inventory_item_score(title, source_path)
        if topic not in items or score > int(items[topic].get("score") or 0):
            items[topic] = {
                "title": topic,
                "intro": "",
                "score": score,
                "document_id": int(row["document_id"] or 0),
                "chunk_id": 0,
                "chunk_index": 0,
                "source_path": source_path,
                "url": str(row["url"] or "") or None,
                "text": "",
            }

    preferred_order = ["整合包", "整合包内部资料", "模组", "资源包", "光影", "原版/通用资料", "其他资料"]
    display_items: list[dict[str, Any]] = []
    for bucket in preferred_order:
        state = buckets.get(bucket)
        if not state:
            continue
        items = sorted(state["items"].values(), key=lambda item: int(item.get("score") or 0), reverse=True)
        display_items.extend(items[:6])
    doc_ids = [int(item["document_id"]) for item in display_items if int(item.get("document_id") or 0) > 0]
    if doc_ids:
        placeholders = ",".join("?" for _ in doc_ids)
        conn = connect(config.paths.db_path)
        try:
            chunk_rows = conn.execute(
                f"""
                SELECT c.document_id, c.id AS chunk_id, c.chunk_index, SUBSTR(c.text, 1, 900) AS text
                FROM chunks c
                JOIN (
                    SELECT document_id, MIN(chunk_index) AS chunk_index
                    FROM chunks
                    WHERE document_id IN ({placeholders})
                    GROUP BY document_id
                ) first_chunk
                  ON first_chunk.document_id = c.document_id
                 AND first_chunk.chunk_index = c.chunk_index
                """,
                doc_ids,
            ).fetchall()
        finally:
            conn.close()
        chunk_text_by_doc = {
            int(chunk_row["document_id"]): {
                "chunk_id": int(chunk_row["chunk_id"] or 0),
                "chunk_index": int(chunk_row["chunk_index"] or 0),
                "text": str(chunk_row["text"] or ""),
            }
            for chunk_row in chunk_rows
        }
        for item in display_items:
            chunk = chunk_text_by_doc.get(int(item["document_id"]))
            if chunk:
                item.update(chunk)
            item["intro"] = _corpus_intro_from_text(str(item.get("text") or ""))

    lines = [
        f"本地资料库目前有 {total_docs} 篇已入库文档。按资料类型粗略盘点如下：",
        "",
    ]
    source_results: list[SearchResult] = []
    rank = 1
    for bucket in preferred_order:
        state = buckets.get(bucket)
        if not state:
            continue
        items = sorted(state["items"].values(), key=lambda item: int(item.get("score") or 0), reverse=True)
        lines.append(f"{bucket}：约 {state['count']} 篇文档，能看到 {len(items)} 个不同标题。")
        for item in items[:6]:
            lines.append(f"- {item['title']}：{item['intro']}")
            if len(source_results) < MAX_FINAL_CONTEXT_K and int(item["document_id"]) > 0:
                source_results.append(
                    SearchResult(
                        rank=rank,
                        score=1.0,
                        chunk_id=int(item["chunk_id"] or 0),
                        document_id=int(item["document_id"]),
                        chunk_index=int(item["chunk_index"] or 0),
                        title=str(item["title"]),
                        source_path=str(item["source_path"]),
                        url=item["url"],
                        text=str(item["text"] or ""),
                        metadata={"source": "local_corpus_inventory", "bucket": bucket},
                    )
                )
                rank += 1
        if len(items) > 6:
            lines.append(f"- 还有 {len(items) - 6} 个标题未在这轮展开。")
        lines.append("")
    if len(rows) >= 3000:
        lines.append("说明：本轮为了保持响应稳定，只读取最近 3000 篇 crawler_exports 文档做盘点。")
    lines.append("这是一轮本地资料覆盖范围盘点，不代表每个条目都有足够资料回答完整玩法攻略；具体问题仍需要 MCagent 再按证据筛选回答。")
    sources = [_result_to_dict(item) for item in source_results]
    return {
        "answer": "\n".join(lines).strip(),
        "sources": sources,
        "context": format_context(source_results),
        "agent": "mcagent_rag",
    }


def _final_context_k(config: AppConfig) -> int:
    return max(4, min(max(config.retrieval.top_k, 8), MAX_FINAL_CONTEXT_K))


def _adaptive_preview_k(query: str) -> int:
    try:
        intent = analyze_query(query, CONCEPTS)
    except Exception:
        intent = None
    if intent and intent.question_type in {"list", "recipe", "boss"}:
        return 32
    if intent and intent.domain in {"project", "known_mod"}:
        return 28
    return 18


def _adaptive_rough_k(query: str, agent: str) -> int:
    if agent == "mcagent_rag":
        return _adaptive_preview_k(query)
    return _adaptive_preview_k(query)


def _adaptive_final_context_k(query: str, config: AppConfig, agent: str) -> int:
    if agent == "retriever_only":
        return min(MAX_FINAL_CONTEXT_K, max(config.retrieval.top_k, 8))
    try:
        intent = analyze_query(query, CONCEPTS)
    except Exception:
        intent = None
    if not intent:
        return min(MAX_FINAL_CONTEXT_K, max(MIN_FINAL_CONTEXT_K, config.retrieval.top_k))
    if intent.question_type in {"list", "recipe", "boss"}:
        return MAX_FINAL_CONTEXT_K
    if intent.question_type == "guide":
        return 10
    if intent.domain in {"project", "known_mod"}:
        return 10
    if intent.domain == "vanilla":
        return 6
    return 8


def _answer_max_tokens(payload: dict[str, Any], question: str) -> int | None:
    raw = payload.get("max_tokens")
    if isinstance(raw, str):
        raw_value = raw.strip().lower()
        if raw_value == "auto":
            raw = None
        elif raw_value in {"none", "null", "unlimited", "不限制", "无限制"}:
            return None
    if raw is not None:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = DEFAULT_ANSWER_MAX_TOKENS
        if value <= 0:
            return None
        return max(1200, min(value, ANSWER_MAX_TOKENS_CAP))
    try:
        intent = analyze_query(question, CONCEPTS)
    except Exception:
        intent = None
    if intent and intent.question_type in {"list", "recipe", "boss", "guide"}:
        return 4200
    if intent and intent.domain in {"project", "known_mod"}:
        return 3600
    return AUTO_ANSWER_MAX_TOKENS


def _should_use_llm_retrieval_planner(original_question: str, contextual_question: str, session_summary: dict[str, Any]) -> bool:
    if session_summary.get("force_llm_planner"):
        return True
    if any(token in original_question for token in ("规划检索", "深入分析", "复杂", "完整资料", "全网", "多源")):
        return True
    if original_question != contextual_question and any(token in original_question for token in ("这些", "它们", "上述", "刚才")):
        return False
    try:
        intent = analyze_query(contextual_question, CONCEPTS)
    except Exception:
        return False
    return bool(intent and intent.question_type in {"recipe"} and len(intent.keywords) >= 5)


def _selected_llm_client(
    config: AppConfig,
    model: str,
    temperature: float,
    agent: str = "mcagent_rag",
    timeout_seconds: int | None = None,
) -> tuple[OpenAICompatibleClient, str]:
    profile = resolve_profile_from_model(config, model, agent=agent)
    if profile:
        return client_from_profile(
            profile,
            temperature=temperature,
            timeout_seconds=int(timeout_seconds or profile.get("timeout_seconds") or config.ollama.timeout_seconds or 180),
        )
    if model.startswith("cloud:deepseek:"):
        template = profile_by_id(config, "deepseek-template") or {
            "id": "deepseek-template",
            "name": "DeepSeek deepseek-v4-pro",
            "provider": "openai-compatible",
            "base_url": "https://api.deepseek.com",
            "model": model.split(":", 2)[2] or "deepseek-v4-pro",
            "api_key": "",
            "timeout_seconds": 180,
        }
        return client_from_profile(
            template,
            temperature=temperature,
            timeout_seconds=int(timeout_seconds or template.get("timeout_seconds") or config.ollama.timeout_seconds or 180),
        )
    endpoint_config = OllamaConfig(
        base_url=config.ollama.base_url,
        model=model or config.ollama.model,
        temperature=temperature,
        timeout_seconds=int(timeout_seconds or config.ollama.timeout_seconds),
    )
    return OllamaOpenAIClient(endpoint_config), f"Ollama {endpoint_config.model}"


def _build_answer_prompt(question: str, context: str, retrieval_note: str = "") -> str:
    note = f"\n{retrieval_note}\n" if retrieval_note else ""
    return f"""问题：{question}
{note}
MCagent 可用工具与能力：
- local_rag_search：检索本地资料库，适合回答 Minecraft、模组、整合包、教程、物品、Boss、配方等问题。
- crawler_status：查看 Crawler 采集/入库/任务进度。用户问“状态、进度、监控、入库怎么样”等，应使用这个能力。
- delegate_crawler：把资料缺口交给 CrawlerAgent。只有本轮工具选择或 planned workflow 已明确委托时才会启动；最终回答阶段不能因为资料不足自行启动 Crawler。
- answer_from_evidence：根据检索证据组织最终回答，并标注 [S1]、[S2] 来源。

工具使用原则：
- 先理解用户原始话，再结合会话上下文；不要让改写后的检索词覆盖用户第一手意图。
- 工具函数只负责检索、状态、派单和客观抽取；是否足够回答、如何组织答案，由 MCagent 基于证据判断。
- 如果用户是在下达 Crawler 任务，不要把这句话当普通 RAG 关键词检索。

本地检索资料：
{context}

请只根据以上资料回答，并使用 [S1]、[S2] 等标记引用来源。若资料只能给出部分答案，要明确说明缺口；如果用户问“有哪些/列出/包含什么/前15个”，优先逐行提取资料中的名称列表，不要编造资料外名称。列表类问题要特别注意教程段落里“合成/获得/需要/要求”后面出现的物品名；同一来源里出现多个名称时要尽量全部列出，而不是只摘前几个。整合包版本/加载器类问题必须优先使用 manifest.json 结构化事实中的 minecraft.version 与 minecraft.modLoaders 字段，不要把压缩包文件名、Release 编号或 FIX 编号当作 Minecraft 版本。"""


def _answer_question_for_user(original_question: str, contextual_question: str, retrieval_note: str) -> str:
    if original_question == contextual_question:
        return contextual_question
    if retrieval_note:
        return f"{original_question}\n\n会话上下文补充：{retrieval_note}"
    return original_question


def _list_extraction_note(question: str, results: list[SearchResult]) -> str:
    names, snippets = _extract_list_candidates(question, results)
    if not names and not snippets:
        return ""
    snippet_text = "\n".join(f"- {line}" for line in snippets[:12])
    name_text = "、".join(names) if names else "未稳定抽取到名称，以下证据行仍需模型核对"
    return (
        "检索器从同一来源全文中补充抽取到的列表类证据候选："
        + name_text
        + "。\n相关证据行：\n"
        + snippet_text
        + "\n回答时必须结合来源核对；若这些证据行能支持答案，优先完整列出，不要只看原始 chunk 前半段。"
    )


def _extract_list_candidates(question: str, results: list[SearchResult]) -> tuple[list[str], list[str]]:
    if not _looks_like_list_or_guide_question(question):
        return [], []
    focus_terms = _focus_terms_for_question(question)
    if not focus_terms:
        return [], []
    is_boss_question = _is_boss_question(question)
    names: list[str] = []
    snippets: list[str] = []
    for item in results:
        text = _read_result_full_text(item)
        if not _source_matches_focus(item, text, focus_terms):
            continue
        active_window = 0
        for line in text.splitlines():
            clean = _clean_evidence_line(line)
            if not clean:
                continue
            if _line_anchors_subject(clean, focus_terms, is_boss_question=is_boss_question):
                active_window = max(active_window, 80)
            line_names = _candidate_names_from_line(clean, focus_terms, active_window > 0, is_boss_question=is_boss_question)
            relevant = _line_anchors_subject(clean, focus_terms, is_boss_question=is_boss_question) or (active_window > 0 and bool(line_names))
            if relevant and (line_names or len(clean) <= 140) and clean not in snippets:
                snippets.append(clean[:220])
            for name in line_names:
                if active_window > 0 and name not in names:
                    names.append(name)
            if active_window > 0:
                active_window -= 1
    return names, snippets


def _line_relevant_to_focus(line: str, focus_terms: list[str]) -> bool:
    lowered = line.lower()
    for term in focus_terms:
        term_lower = term.lower()
        if term_lower not in lowered:
            continue
        if term == "拔刀剑" and ("方面" in line or "和枪械" in line):
            return False
        return True
    return False


def _repair_list_answer(question: str, answer: str, results: list[SearchResult]) -> str:
    return answer


def _answer_is_candidate_dump(answer: str) -> bool:
    body = _strip_answer_metadata(answer)
    if "候选内容" in body or "候选名称" in body:
        return True
    bullet_count = len(re.findall(r"(?m)^\s*-\s+", body))
    sentence_count = len(re.findall(r"[。！？!?]", body))
    return bullet_count >= 12 and sentence_count <= 4


def _candidate_name_set_is_reliable(question: str, names: list[str]) -> bool:
    if not names:
        return False
    if _is_boss_question(question):
        bad_markers = ("直接", "前面", "移动", "凑近", "不要", "建议", "避免", "然后", "之前", "一样", "普通", "下面", "获取", "设置", "修改")
        return all(not any(name.startswith(marker) for marker in bad_markers) for name in names[:12])
    return True


def _recipe_extraction_note(question: str, results: list[SearchResult]) -> str:
    if not any(token in question for token in ("合成", "配方", "制作", "怎么做", "如何做")):
        return ""
    focus_terms = _focus_terms_for_question(question)
    wanted_names = _extract_context_names(question, limit=16)
    recipe_lines: list[str] = []
    for item in results:
        text = _read_result_full_text(item)
        if not _source_matches_focus(item, text, focus_terms):
            continue
        for line in text.splitlines():
            clean = _clean_evidence_line(line)
            if not clean:
                continue
            has_recipe_word = any(token in clean for token in ("合成", "配方", "制作", "需要", "要求"))
            has_name = any(name in clean for name in wanted_names if len(name) >= 2)
            if has_recipe_word and (has_name or any(term in clean for term in focus_terms if len(term) >= 3)):
                if clean not in recipe_lines:
                    recipe_lines.append(clean[:240])
            if len(recipe_lines) >= 18:
                break
    if not recipe_lines:
        return ""
    return "同一来源全文中抽取到的合成/制作相关证据行：\n" + "\n".join(f"- {line}" for line in recipe_lines)


def _looks_like_list_or_guide_question(question: str) -> bool:
    intent = analyze_query(question, CONCEPTS)
    if intent.question_type in {"list", "boss", "recipe"}:
        return True
    list_tokens = ("有哪些", "有什么", "列表", "清单", "多少", "几种", "几类")
    return any(token in question for token in list_tokens)


def _is_plain_list_question(question: str) -> bool:
    if _is_procedure_question(question):
        return False
    intent = analyze_query(question, CONCEPTS)
    if intent.question_type == "list":
        return True
    return any(token in question for token in ("有哪些", "有什么", "列表", "清单", "多少", "几种", "几类", "包含哪些", "包括什么"))


def _is_procedure_question(question: str) -> bool:
    procedure_tokens = (
        "怎么打", "如何打", "怎样打", "打法", "怎么打败", "如何击败", "怎么击败",
        "怎么做", "如何做", "怎么制作", "如何制作", "怎么合成", "如何合成", "配方",
        "怎么获得", "如何获得", "哪里打", "在哪打", "在哪里打", "位置", "地点",
        "掉落什么", "掉什么", "掉落物", "奖励",
    )
    return any(token in question for token in procedure_tokens)


def _is_explanation_question(question: str) -> bool:
    return any(token in question for token in ("是什么", "介绍", "讲讲", "解释", "有什么用", "作用", "机制"))


def _is_boss_question(question: str) -> bool:
    return bool(re.search(r"boss|BOSS|Boss|首领|头目|魔王", question))


def _should_repair_list_answer(question: str) -> bool:
    if _is_procedure_question(question):
        return False
    intent = analyze_query(question, CONCEPTS)
    if intent.question_type == "recipe":
        return False
    if intent.question_type == "boss":
        return _is_plain_list_question(question)
    if intent.question_type != "list":
        return False
    if any(token in question for token in ("是什么", "怎么用", "如何用", "有什么用", "用法", "作用", "玩法", "机制")):
        return False
    return True


def _focus_terms_for_question(question: str) -> list[str]:
    intent = analyze_query(question, CONCEPTS)
    relation_terms = _relation_focus_terms(question)
    raw_terms = [*relation_terms, intent.entity, *intent.keywords, *intent.search_queries, question]
    stop = {
        "minecraft",
        "mc",
        "有哪些",
        "有什么",
        "用法",
        "作用",
        "是什么",
        "包含哪些",
        "包括什么",
        "列出",
        "列表",
        "玩法",
        "攻略",
        "介绍",
        "详细介绍",
        "一个",
        "呢",
        "boss",
    }
    terms: list[str] = []
    for raw in raw_terms:
        for term in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}|[\u4e00-\u9fff]{2,}", str(raw)):
            if any(term.endswith(suffix) and term != suffix for suffix in ("是什么", "有哪些用法", "有什么用", "玩法", "有哪些", "有什么", "用法", "作用")):
                for suffix in ("是什么", "有哪些用法", "有什么用", "玩法", "有哪些", "有什么", "用法", "作用"):
                    if term.endswith(suffix) and term != suffix:
                        term = term[: -len(suffix)]
                        break
            if term.lower() in stop:
                continue
            if term and term.lower() not in {item.lower() for item in terms}:
                terms.append(term)
            for suffix in ("拔刀剑", "整合包", "模组", "资源包", "材质包", "维度", "世界"):
                if term.endswith(suffix) and term != suffix:
                    prefix = term[: -len(suffix)].strip()
                    for part in (prefix, suffix):
                        if part and part.lower() not in {item.lower() for item in terms}:
                            terms.append(part)
    if intent.question_type == "boss" or re.search(r"boss|BOSS|Boss|首领|头目", question):
        for boss_term in ("BOSS", "Boss", "boss", "首领", "头目", "击败", "打败", "挑战"):
            if boss_term.lower() not in {item.lower() for item in terms}:
                terms.append(boss_term)
    return (terms or _fallback_focus_terms(question))[:10]


def _relation_focus_terms(question: str) -> list[str]:
    terms: list[str] = []
    patterns = [
        r"(?P<parent>[\u4e00-\u9fffA-Za-z0-9_+-]{2,40})(?:里面的|里面|里的|里|中的|中)(?P<child>[\u4e00-\u9fffA-Za-z0-9_+-]{2,40})",
        r"(?P<parent>[\u4e00-\u9fffA-Za-z0-9_+-]{2,40})(?:的|之)(?P<child>[\u4e00-\u9fffA-Za-z0-9_+-]{2,40})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, question):
            parent = _clean_relation_focus_term(match.group("parent"))
            child = _clean_relation_focus_term(match.group("child"))
            if parent:
                terms.append(parent)
            if child:
                terms.append(child)
            if parent and child:
                terms.append(f"{parent} {child}")
    return _dedupe_strings(terms)


def _clean_relation_focus_term(value: str) -> str:
    value = re.sub(r"^(?:的|之|里面的|里面|里的|里|中的|中)+", "", str(value))
    value = re.sub(r"(?:里面|里|中)$", "", value)
    value = re.sub(r"(是什么|有哪些用法|有什么用|有哪些|有什么|怎么|如何|玩法|攻略|教程|用法|作用)$", "", value)
    value = value.strip(" \t\r\n，,。；;：:？?！!")
    parts = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}|[\u4e00-\u9fff]{2,}", value)
    stop = {"什么", "有哪些", "有什么", "用法", "作用", "玩法", "攻略", "教程"}
    parts = [part for part in parts if part not in stop]
    return parts[-1] if parts else ""


def _fallback_focus_terms(question: str) -> list[str]:
    stop = {
        "minecraft",
        "mc",
        "mod",
        "mods",
        "有哪些",
        "有什么",
        "包含哪些",
        "包括什么",
        "列出",
        "列表",
        "玩法",
        "攻略",
        "介绍",
        "详细介绍",
        "如何",
        "怎么",
        "合成",
        "制作",
        "配方",
        "这些",
        "它们",
        "上面",
        "前面",
        "boss",
        "Boss",
        "BOSS",
        "首领",
        "头目",
        "可以打",
        "怎么打",
        "如何打",
        "哪里打",
        "掉落什么",
        "掉落",
    }
    suffixes = ("拔刀剑", "整合包", "资源包", "材质包", "模组", "维度", "世界")
    terms: list[str] = []
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}|[\u4e00-\u9fff]{2,}", question):
        value = raw.strip()
        lowered = value.lower()
        if lowered in stop:
            continue
        for suffix in ("有哪些", "有什么", "玩法", "攻略", "介绍"):
            if value.endswith(suffix) and value != suffix:
                value = value[: -len(suffix)].strip()
                lowered = value.lower()
                break
        if value and lowered not in stop and lowered not in {item.lower() for item in terms}:
            terms.append(value)
        for suffix in suffixes:
            if value.endswith(suffix) and value != suffix:
                prefix = value[: -len(suffix)].strip()
                for part in (prefix, suffix):
                    if part and part.lower() not in stop and part.lower() not in {item.lower() for item in terms}:
                        terms.append(part)
    return terms[:12]


def _read_result_full_text(item: SearchResult) -> str:
    try:
        source_path = Path(item.source_path)
        if source_path.exists() and source_path.suffix.lower() in {".md", ".txt"}:
            text = source_path.read_text(encoding="utf-8", errors="replace")
            raw_note = _raw_html_evidence_note(source_path)
            return text + ("\n\n" + raw_note if raw_note else "")
    except OSError:
        pass
    return item.text


def _raw_html_evidence_note(markdown_path: Path) -> str:
    raw_path = _raw_html_path_for_markdown(markdown_path)
    if raw_path is None or not raw_path.exists():
        return ""
    try:
        html = raw_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    parser = _HTMLTextExtractor()
    parser.feed(html)
    parser.close()
    table_lines: list[str] = []
    for index, table in enumerate(parser.tables[:8], start=1):
        rows = [[normalize_text(cell) for cell in row] for row in table if any(cell.strip() for cell in row)]
        if not rows:
            continue
        flat = " | ".join(" / ".join(row) for row in rows[:12])
        table_lines.append(f"- Raw HTML Table {index}: {flat[:1200]}")
    image_lines: list[str] = []
    for image in parser.images[:30]:
        src = image.get("src", "")
        alt = image.get("alt", "")
        if src:
            image_lines.append(f"- Raw HTML Image: {alt or 'image'} -> {src}")
    if not table_lines and not image_lines:
        return ""
    return "\n".join(["## Raw HTML Evidence", f"- raw_html_path: {raw_path}", *table_lines, *image_lines])


def _raw_html_text_evidence_lines(markdown_path: Path, focus_terms: list[str], limit: int = 10) -> list[str]:
    raw_path = _raw_html_path_for_markdown(markdown_path)
    if raw_path is None or not raw_path.exists():
        return []
    try:
        html = raw_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    parser = _HTMLTextExtractor()
    parser.feed(html)
    parser.close()
    lines = _evidence_lines_from_text(parser.text, focus_terms, limit=limit)
    output = [f"Raw HTML Text: {line}" for line in lines]
    for index, table in enumerate(parser.tables[:6], start=1):
        rows = [[normalize_text(cell) for cell in row] for row in table if any(cell.strip() for cell in row)]
        if not rows:
            continue
        flat = " | ".join(" / ".join(row) for row in rows[:10])
        if any(term.lower() in flat.lower() for term in focus_terms):
            output.append(f"Raw HTML Table {index}: {flat[:900]}")
    for image in parser.images[:20]:
        src = image.get("src", "")
        alt = image.get("alt", "")
        haystack = f"{alt} {src}".lower()
        if src and (not focus_terms or any(term.lower() in haystack for term in focus_terms)):
            output.append(f"Raw HTML Image: {alt or 'image'} -> {src}")
    return output[:limit]


def _raw_html_path_for_markdown(markdown_path: Path) -> Path | None:
    if markdown_path.suffix.lower() not in {".md", ".markdown"}:
        return None
    raw_dir = markdown_path.parent / "raw_html"
    direct = raw_dir / (markdown_path.stem + ".html")
    if direct.exists():
        return direct
    candidates = sorted(raw_dir.glob(markdown_path.stem.rsplit("_", 1)[0] + "*.html")) if raw_dir.exists() else []
    return candidates[0] if candidates else None


def _source_matches_focus(item: SearchResult, text: str, focus_terms: list[str]) -> bool:
    haystack = f"{item.title}\n{item.source_path}\n{text[:5000]}".lower()
    hits = [term for term in focus_terms if term.lower() in haystack]
    if not hits:
        return False
    path = item.source_path.lower().replace("\\", "/")
    if any(marker in path for marker in ("crawler_exports/mcmod/", "crawler_exports/web_discovery/", "crawler_exports/modrinth_agent/", "crawler_exports/followup/")):
        return True
    return len(hits) >= 2


def _clean_evidence_line(line: str) -> str:
    clean = re.sub(r"\s+", " ", line).strip(" \t-#>*")
    if not clean or len(clean) < 2:
        return ""
    if clean.startswith(("<!--", "-->")):
        return ""
    if clean.startswith(("source:", "score:", "url:", "Fetched", "Created", "Updated", "Search query:", "Search snippet:")):
        return ""
    if re.match(r"^(Web source|Query|Snippet|Search query|Search snippet)\s*[:：*]", clean, flags=re.I):
        return ""
    return clean


def _candidate_names_from_line(line: str, focus_terms: list[str], in_focus_window: bool = False, *, is_boss_question: bool = False) -> list[str]:
    names: list[str] = []
    if is_boss_question:
        return _boss_candidate_names_from_line(line, focus_terms, in_focus_window)
    if "拔刀剑" in focus_terms and not in_focus_window and not any(term in line for term in ("梦想一心", "幻魔", "雪鸦", "冻樱", "明兽", "天元刀", "天星刀")):
        return names
    for match in re.finditer(r"(?:先|再|然后|后|并|，|。|^)?(?:合成|制作|做出|解锁|拿到|获得|合)(?!可)([^。；;，,！!\n]{2,32})", line):
        _extend_candidate_names(names, match.group(1), focus_terms)
    for match in re.finditer(r"([^。；;，,\n]{2,24}?)(?:也)?(?:需要|要求)(?:\d|[一二三四五六七八九十百千]|火焰保护|杀敌数|附魔|前置)", line):
        _extend_candidate_names(names, match.group(1), focus_terms)
    return names


def _boss_candidate_names_from_line(line: str, focus_terms: list[str], in_focus_window: bool = False) -> list[str]:
    if not _boss_line_has_context(line, focus_terms, in_focus_window):
        return []
    names: list[str] = []
    normalized = re.sub(r"\[[Ss]\d+\]", " ", line)
    normalized = re.sub(r"(?:Boss|BOSS|boss|首领|头目|魔王|最终|可打|打法|位置|地点|掉落|刷新|生成|击败|打败|挑战|阶段)", " ", normalized)
    parts = re.split(r"[、/和与及,，;；|：:()\[\]【】《》<>“”\"'\s]+", normalized)
    for part in parts:
        name = part.strip(" -*#>\t\r\n的了也可在于中里")
        if _valid_candidate_name(name, focus_terms, is_boss_question=True) and name not in names:
            names.append(name)
    return names[:12]


def _boss_line_has_context(line: str, focus_terms: list[str], in_focus_window: bool) -> bool:
    lowered = line.lower()
    has_boss_marker = any(token in lowered for token in ("boss", "首领", "头目", "魔王"))
    has_combat_marker = any(token in line for token in ("打法", "击败", "打败", "挑战", "掉落", "刷新", "生成", "位置", "地点", "阶段", "血量"))
    has_focus = any(term.lower() in lowered for term in focus_terms if len(term) >= 2)
    if has_boss_marker and (has_focus or has_combat_marker or in_focus_window):
        return True
    if in_focus_window and has_combat_marker:
        return True
    return False


def _extend_candidate_names(names: list[str], clause: str, focus_terms: list[str]) -> None:
    clause = re.split(r"(?:后|前|时|的时候|需要|要求|可以|用于|来|，|。)", clause, maxsplit=1)[0]
    clause = re.sub(r"[0-9]+|[A-Za-z][A-Za-z0-9_+-]*|[IVXLCDM]+", " ", clause)
    for part in re.split(r"[、/和与及或者或\s]+", clause):
        name = part.strip(" ：:（）()[]【】“”\"'，,。.;；！!?？的了也后前")
        if _valid_candidate_name(name, focus_terms) and name not in names:
            names.append(name)


def _valid_candidate_name(name: str, focus_terms: list[str], *, is_boss_question: bool = False) -> bool:
    if not re.fullmatch(r"[\u4e00-\u9fff]{2,10}", name):
        return False
    if "拔刀剑" in focus_terms and not name.endswith(("刀", "剑", "刃", "樱", "鸦", "兽", "心", "魔")):
        return False
    stop = {
        "拔刀剑",
        "根据本地资料",
        "本地资料",
        "名称",
        "候选名称",
        "原回答",
        "完整正向列出",
        "同一来源全文中还明确出现了以下候选名称",
        "整合包",
        "教程",
        "攻略",
        "资料",
        "来源",
        "任务",
        "杀敌数",
        "火焰保护",
        "附魔",
        "魔术师",
        "不死图腾",
        "金苹果",
        "主世界",
        "地下矿道",
        "模组",
        "难度",
        "优化",
        "饰品",
        "大部分饰品",
        "强力",
        "奖励",
        "操作",
        "通过击杀",
        "完成任务",
        "合成可",
        "开始快速",
        "方法",
        "百科",
        "下载链接",
        "配置",
        "瓶火龙血",
        "瓶冰龙血",
        "自己的拔刀剑",
        "个不死图腾",
        "附魔金苹果",
        "表顺序",
        "个雷电瓶",
        "掉落物",
        "可能",
        "最方便",
        "推荐开启",
        "许多",
        "词条",
        "伤害",
        "左右",
        "一遍",
        "能力",
        "高达",
        "防御饰品",
        "不过",
        "材料",
        "一个",
        "包作",
        "可打",
        "打法",
        "地点",
        "位置",
        "阶段",
        "血量",
    }
    if name in stop or name in focus_terms:
        return False
    if any(token in name for token in ("可获得", "通过", "完成", "大量", "丰富", "详细", "最优", "原理", "方面")):
        return False
    if any(token in name for token in ("资料", "明确", "提到", "这些", "名称", "合成", "获得", "顺序", "掉落物", "不死图腾", "附魔金苹果", "雷电瓶")):
        return False
    if is_boss_question and name in {"最终", "主线", "可打", "挑战", "位置", "打法"}:
        return False
    if any(name.endswith(suffix) for suffix in ("要求", "需要", "可以", "应该", "方面", "数量")):
        return False
    if is_boss_question and re.search(r"(材料|词条|伤害|能力|防御|武器|饰品|金块|砖块|推荐|开启|左右|可能|许多|一个)", name):
        return False
    return True


def _line_anchors_subject(line: str, focus_terms: list[str], *, is_boss_question: bool = False) -> bool:
    lowered = line.lower()
    if is_boss_question:
        return _boss_line_has_context(line, focus_terms, False)
    if "拔刀剑" in focus_terms:
        if "拔刀剑" in line:
            if "方面" in line:
                return False
            return any(token in line for token in ("合成", "制作", "获得", "路线", "建议先", "先合成"))
        if any(term in line for term in ("梦想一心", "幻魔", "雪鸦", "冻樱", "明兽", "天元刀", "天星刀")):
            return True
        return False
    specific_terms = [term for term in focus_terms if len(term) >= 3 and term.lower() in lowered]
    if not specific_terms:
        return False
    return any(token in line for token in ("合成", "制作", "获得", "需要", "要求", "包含", "包括", "掉落", "解锁", "攻略", "路线"))


def _answer_contradicts_extracted_names(answer: str, names: list[str]) -> bool:
    negative = r"未|没|没有|并未|无法|不能|不足|缺少"
    for name in names:
        if re.search(rf"({negative})[^。；;\n]{{0,30}}{re.escape(name)}", answer):
            return True
        if re.search(rf"{re.escape(name)}[^。；;\n]{{0,30}}({negative})", answer):
            return True
    return False


def _generate_grounded_answer(
    config: AppConfig,
    question: str,
    results: list[SearchResult],
    model: str,
    temperature: float,
    max_tokens: int | None,
    retrieval_note: str = "",
    context_override: str | None = None,
    evidence_question: str | None = None,
) -> tuple[str, str]:
    evidence_question = evidence_question or question
    messages, context = _build_grounded_messages(
        question,
        results,
        retrieval_note=retrieval_note,
        context_override=context_override,
        evidence_question=evidence_question,
    )
    try:
        client, model_label = _selected_llm_client(config, model, temperature)
        answer = client.chat(messages, temperature=temperature, max_tokens=max_tokens)
    except Exception as exc:  # noqa: BLE001 - expose profile/API errors instead of hiding them behind a local fallback.
        answer = f"模型调用失败：{exc}\n\n未自动降级到本地 Ollama；请检查当前 Agent 在设置页分配的模型配置、API Key、限流或网络状态。"
        return answer, context
    else:
        answer = f"{answer}\n\n\u6a21\u578b\uff1a{model_label}" if answer.strip() else "\u672c\u5730\u8d44\u6599\u5e93\u672a\u627e\u5230\u53ef\u9760\u7b54\u6848\u3002"
    if _answer_is_garbled(answer):
        answer = "模型输出疑似乱码，本次不使用工具兜底替代最终回答。请检查模型服务或重新生成。"
    return answer, context


def _build_grounded_messages(
    question: str,
    results: list[SearchResult],
    retrieval_note: str = "",
    context_override: str | None = None,
    evidence_question: str | None = None,
) -> tuple[list[dict[str, str]], str]:
    evidence_question = evidence_question or question
    context = context_override if context_override is not None else _format_context_with_deep_evidence(evidence_question, results)
    relation_note = _relation_answer_note(evidence_question, results)
    version_note = _version_install_extraction_note(evidence_question, results)
    extraction_note = _list_extraction_note(evidence_question, results)
    recipe_note = _recipe_extraction_note(evidence_question, results)
    merged_note = "\n".join(part for part in (retrieval_note, relation_note, version_note, extraction_note, recipe_note) if part)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_answer_prompt(question, context, merged_note)},
    ]
    return messages, context


def _build_direct_answer_messages(
    original_question: str,
    contextual_question: str,
    session_summary: dict[str, Any] | None = None,
    agent: str = "mcagent_rag",
) -> list[dict[str, str]]:
    summary_text = json.dumps(session_summary or {}, ensure_ascii=False)
    role_name = "CrawlerAgent" if agent == "crawler_agent" else "MCagent"
    role_desc = (
        "你是 CrawlerAgent，一个专注网页读取、资料采集、保存/不保存交付约束和失败原因说明的爬虫 Agent。"
        if agent == "crawler_agent"
        else "你是 MCagent，一个可以自然对话、也可以在需要时使用工具的资料助手。"
    )
    user_text = (
        f"用户原话：{original_question}\n"
        f"当前会话理解：{contextual_question}\n"
        f"会话摘要：{summary_text}\n\n"
        f"请以 {role_name} 的身份直接自然回复用户。不要声称执行过未执行的工具，不要编造来源。"
    )
    return [
        {
            "role": "system",
            "content": role_desc + "本轮已经由 Agent 判断为不需要工具；请简洁、友好、按上下文直接回答。",
        },
        {"role": "user", "content": user_text},
    ]


def _generate_direct_answer(
    config: AppConfig,
    original_question: str,
    contextual_question: str,
    session_summary: dict[str, Any],
    model: str,
    temperature: float,
    max_tokens: int | None,
    agent: str = "mcagent_rag",
) -> str:
    messages = _build_direct_answer_messages(original_question, contextual_question, session_summary, agent=agent)
    client, model_label = _selected_llm_client(config, model, temperature, agent=agent)
    answer = client.chat(messages, temperature=temperature, max_tokens=max_tokens)
    answer = answer.strip()
    return f"{answer}\n\n模型：{model_label}" if answer else "我在。"


def _generate_direct_answer_stream(
    config: AppConfig,
    original_question: str,
    contextual_question: str,
    session_summary: dict[str, Any],
    model: str,
    temperature: float,
    max_tokens: int | None,
    emit_delta: Any,
    emit_thinking: Any | None = None,
    agent: str = "mcagent_rag",
) -> str:
    messages = _build_direct_answer_messages(original_question, contextual_question, session_summary, agent=agent)
    client, model_label = _selected_llm_client(config, model, temperature, agent=agent)
    chunks = _collect_streaming_answer(client, messages, temperature, max_tokens, emit_delta, emit_thinking)
    answer = "".join(chunks).strip()
    if not answer:
        retry_tokens = min(max((max_tokens or 0) * 4, 1000), 4000)
        chunks = _collect_streaming_answer(client, messages, temperature, retry_tokens, emit_delta, emit_thinking)
        answer = "".join(chunks).strip()
    return f"{answer}\n\n模型：{model_label}" if answer else "我在。"


def _generate_temporary_extract_summary(
    config: AppConfig,
    original_question: str,
    url: str,
    page_text: str,
    model: str,
    temperature: float,
    max_tokens: int | None,
) -> str:
    client, model_label = _selected_llm_client(config, model, temperature, agent="crawler_agent")
    prompt = (
        "你是 CrawlerAgent。用户要求临时读取一个公开网页并总结，不保存到本地、不入库。\n"
        "请只基于下面抓取到的网页正文回答用户，不要声称已经保存文件。\n"
        "如果正文不足以回答，就说明缺口或访问限制。\n\n"
        f"用户问题：{original_question}\n"
        f"URL：{url}\n"
        f"网页正文：\n{page_text}"
    )
    answer = client.chat(
        [
            {"role": "system", "content": "你是负责临时网页抽取与摘要的 CrawlerAgent。"},
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens or DEFAULT_ANSWER_MAX_TOKENS,
    ).strip()
    return f"{answer}\n\n模型：{model_label}" if answer else "页面内容已读取，但模型没有生成摘要。"


def _review_temporary_extract_summary(
    config: AppConfig,
    original_question: str,
    url: str,
    page_text: str,
    first_answer: str,
    missing_terms: list[str],
    excerpt: str,
    model: str,
    temperature: float,
    max_tokens: int | None,
) -> str:
    client, model_label = _selected_llm_client(config, model, temperature, agent="crawler_agent")
    prompt = (
        "你是 CrawlerAgent。工具已经客观检查到第一次临时网页摘要没有完整覆盖用户点名内容，"
        "或答案疑似截断。请重新基于网页正文回答用户，不保存到本地、不入库。\n"
        "要求：结构化回答；覆盖用户点名术语；如果网页正文确实没有某个术语，就明确说缺失，不要编造。\n\n"
        f"用户问题：{original_question}\n"
        f"URL：{url}\n"
        f"第一次答案：\n{first_answer[:2500]}\n\n"
        f"工具客观发现第一次答案缺少的用户点名术语：{', '.join(missing_terms) if missing_terms else '无，但答案疑似截断或过短'}\n"
        f"相关网页片段：\n{excerpt or page_text[:5000]}"
    )
    answer = client.chat(
        [
            {"role": "system", "content": "你是负责临时网页抽取与摘要的 CrawlerAgent。"},
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
        max_tokens=min(max(max_tokens or DEFAULT_ANSWER_MAX_TOKENS, 1600), 5000),
    ).strip()
    return f"{answer}\n\n模型：{model_label}" if answer else first_answer


def _choose_temporary_extract_url(
    config: AppConfig,
    *,
    question: str,
    collection_target: str,
    candidates: list[dict[str, Any]],
    model: str,
    temperature: float,
) -> str:
    client, _model_label = _selected_llm_client(config, model, temperature, agent="crawler_agent")
    compact_candidates = [
        {
            "rank": item.get("rank"),
            "title": str(item.get("title") or "")[:180],
            "url": str(item.get("url") or ""),
            "snippet": str(item.get("snippet") or "")[:300],
        }
        for item in candidates[:10]
    ]
    prompt = (
        "You are CrawlerAgent. Objective web discovery returned candidate URLs for a temporary no-save extraction. "
        "Choose exactly one candidate URL that best matches the user's requested page/topic. "
        "Do not invent URLs. Return JSON only: {\"url\":\"one candidate url\",\"reason\":\"brief reason\"}.\n"
        f"User request: {question}\n"
        f"Collection target: {collection_target}\n"
        f"Candidates: {json.dumps(compact_candidates, ensure_ascii=False)}"
    )
    raw = client.chat(
        [
            {"role": "system", "content": "Return exactly one valid JSON object."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_tokens=500,
    )
    data = json_object_from_llm_text(raw)
    return str(data.get("url") or "").strip()


def _generate_grounded_answer_stream(
    config: AppConfig,
    question: str,
    results: list[SearchResult],
    model: str,
    temperature: float,
    max_tokens: int | None,
    emit_delta: Any,
    emit_thinking: Any | None = None,
    retrieval_note: str = "",
    context_override: str | None = None,
    evidence_question: str | None = None,
) -> tuple[str, str]:
    messages, context = _build_grounded_messages(
        question,
        results,
        retrieval_note=retrieval_note,
        context_override=context_override,
        evidence_question=evidence_question,
    )
    try:
        client, model_label = _selected_llm_client(config, model, temperature)
        chunks = _collect_streaming_answer(client, messages, temperature, max_tokens, emit_delta, emit_thinking)
        answer = "".join(chunks).strip()
        if not answer:
            retry_tokens = min(max((max_tokens or 0) * 4, 4000), 8000)
            chunks = _collect_streaming_answer(client, messages, temperature, retry_tokens, emit_delta, emit_thinking)
            answer = "".join(chunks).strip()
        if not answer:
            raise RuntimeError("model streaming completed without visible answer content")
        answer = f"{answer}\n\n模型：{model_label}"
    except Exception as exc:  # noqa: BLE001
        answer = f"模型调用失败：{exc}\n\n未自动降级到本地 Ollama；请检查当前 Agent 在设置页分配的模型配置、API Key、限流或网络状态。"
        return answer, context
    if _answer_is_garbled(answer):
        answer = "模型输出疑似乱码，本次不使用工具兜底替代最终回答。请检查模型服务或重新生成。"
    return answer, context


def _collect_streaming_answer(
    client: OpenAICompatibleClient,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int | None,
    emit_delta: Any,
    emit_thinking: Any | None = None,
) -> list[str]:
    chunks: list[str] = []
    reasoning_events = 0
    last_thinking_emit = 0.0
    for event in client.stream_events(messages, temperature=temperature, max_tokens=max_tokens):
        event_type = event.get("type")
        if event_type == "content":
            chunk = event.get("text") or ""
            if not chunk:
                continue
            chunks.append(chunk)
            emit_delta(chunk)
            continue
        if event_type == "reasoning":
            reasoning_events += 1
            now = time.time()
            if emit_thinking is not None and now - last_thinking_emit >= 3.0:
                last_thinking_emit = now
                emit_thinking({"reasoning_events": reasoning_events})
    return chunks

def _format_context_fast(question: str, results: list[SearchResult]) -> str:
    focus_terms = _focus_terms_for_question(question)
    parts: list[str] = []
    for result in results:
        source = result.url or result.source_path
        compact = _compact_source_text(result.text, focus_terms, min(MAX_SOURCE_CONTEXT_CHARS, 900))
        parts.append("\n".join([
            f"[S{result.rank}] {result.title}",
            f"source: {source}",
            f"score: {result.score:.4f}",
            compact,
        ]))
    return "\n\n".join(parts)


def _relation_answer_note(question: str, results: list[SearchResult]) -> str:
    terms = _relation_focus_terms(question)
    if len(terms) < 2:
        return ""
    parent = terms[0]
    child = terms[1]
    child_hits = [
        item.title
        for item in results[:8]
        if child.lower() in f"{item.title}\n{item.text[:1200]}".lower()
    ]
    parent_hits = [
        item.title
        for item in results[:12]
        if parent.lower() in f"{item.title}\n{item.text[:1600]}".lower()
    ]
    if not child_hits:
        return ""
    lines = [
        f"检索规划识别到当前问题是“{parent}”中的组件/系统“{child}”。",
        f"回答时先说明：本轮证据可以支持解释“{child}”本身；若只有间接证据支持它属于“{parent}”，要把关联程度说清楚，但不要因为组件资料标题不含“{parent}”就直接否定。",
        f"组件证据标题示例：{'; '.join(child_hits[:4])}",
    ]
    if parent_hits:
        lines.append(f"父主题关联证据标题示例：{'; '.join(parent_hits[:3])}")
    return "\n".join(lines)


def _version_install_extraction_note(question: str, results: list[SearchResult]) -> str:
    if not _should_surface_version_install_evidence(question):
        return ""
    fact_lines: list[str] = []
    for item in results[:8]:
        text = _read_result_full_text(item)
        if not text:
            continue
        extracted = _extract_version_install_lines(text)
        for line in extracted:
            entry = f"[S{item.rank}] {line}"
            if entry not in fact_lines:
                fact_lines.append(entry)
            if len(fact_lines) >= 18:
                break
        if len(fact_lines) >= 18:
            break
    if not fact_lines:
        return ""
    return (
        "检索器从同一来源全文里抽取到的版本/安装/配置事实行如下；"
        "回答整合包概览、版本、加载器、Java、启动器、内存时必须优先核对这些行。"
        "如果这些行来自项目页/下载页而不是 manifest，请按来源说明置信边界，但不要说资料未提及：\n"
        + "\n".join(f"- {line}" for line in fact_lines)
    )


def _u(codepoints: str) -> str:
    return "".join(chr(int(part, 16)) for part in codepoints.split())


_VERSION_INSTALL_LABELS = (
    _u("6574 5408 5305 7248 672C"),
    "Minecraft " + _u("7248 672C"),
    _u("52A0 8F7D 5668 002F 5E73 53F0"),
    "Java " + _u("8981 6C42"),
    _u("5B89 88C5 65B9 5F0F"),
    _u("5185 5B58 5EFA 8BAE"),
    _u("4E0B 8F7D 5730 5740"),
    _u("8FD0 884C 73AF 5883"),
)


_VERSION_INSTALL_TEXT = {
    "heading": _u("6839 636E 672C 5730 8D44 6599 FF0C 5F53 524D 80FD 786E 8BA4 7684 7248 672C 4E0E 5B89 88C5 8981 6C42 5982 4E0B FF1A"),
    "missing_prefix": _u("4ECD 7F3A 53E3 FF1A"),
    "full_install_steps": _u("5B8C 6574 5B89 88C5 6B65 9AA4"),
    "full_compat_table": _u("5B8C 6574 4F9D 8D56 002F 6A21 7EC4 517C 5BB9 8868"),
    "colon": _u("FF1A"),
    "semicolon": _u("FF1B"),
    "comma": _u("3001"),
    "period": _u("3002"),
}


_VI = {
    "latest_version": _u("6700 65B0 7248 672C"),
    "history_version": _u("5386 53F2 7248 672C"),
    "mc_java_version": _u("6211 7684 4E16 754C 004A 0061 0076 0061 7248 672C"),
    "resource_version": _u("8D44 6E90 7248 672C"),
    "java_requirement": _u("004A 0061 0076 0061 7248 672C 9700 6C42"),
    "platform": _u("5E73 53F0"),
    "core": _u("6838 5FC3"),
    "loader": _u("52A0 8F7D 5668"),
    "install": _u("5B89 88C5"),
    "memory": _u("5185 5B58"),
    "download_url": _u("4E0B 8F7D 5730 5740"),
    "runtime": _u("8FD0 884C 73AF 5883"),
    "client": _u("5BA2 6237 7AEF"),
    "server": _u("670D 52A1 7AEF"),
    "api_requirement": "API" + _u("9700 6C42"),
    "related_links": _u("76F8 5173 94FE 63A5"),
}


def _local_version_install_answer(question: str, results: list[SearchResult]) -> str:
    if not _is_version_install_question(question):
        return ""
    if re.search(r"\b(?:archive|source|quest|quests|kubejs|internal)\b|包体|来源|任务|内部", str(question or ""), flags=re.I):
        return ""
    labels = _version_install_fact_labels()
    facts: dict[str, list[str]] = {label: [] for label in labels}
    filtered_results = _filter_version_install_fact_results(question, results)
    if not filtered_results:
        return ""
    for item in filtered_results[:8]:
        extracted = _extract_version_install_fact_map(_read_result_full_text(item))
        for label, values in extracted.items():
            _append_fact_values(facts[label], values, item.rank)

    facts = {key: _dedupe_strings(values)[:3] for key, values in facts.items() if values}
    if not facts:
        return ""
    lines = [_VERSION_INSTALL_TEXT["heading"]]
    for label in labels:
        values = facts.get(label) or []
        if values:
            lines.append(f"- {label}{_VERSION_INSTALL_TEXT['colon']}" + _VERSION_INSTALL_TEXT["semicolon"].join(values))
    missing = [
        label
        for label in (_VERSION_INSTALL_TEXT["full_install_steps"], _VERSION_INSTALL_TEXT["full_compat_table"])
        if label not in facts
    ]
    if missing:
        lines.append(_VERSION_INSTALL_TEXT["missing_prefix"] + _VERSION_INSTALL_TEXT["comma"].join(missing) + _VERSION_INSTALL_TEXT["period"])
    return "\n".join(lines)


def _filter_version_install_fact_results(question: str, results: list[SearchResult]) -> list[SearchResult]:
    if _is_modpack_fact_query_text(question):
        return _filter_answer_evidence_by_required_terms(question, results)
    subject_terms = _primary_fact_subject_terms(question)
    if not subject_terms:
        return _filter_answer_evidence_by_required_terms(question, results)[:3]
    output: list[SearchResult] = []
    for item in results:
        title_source = f"{item.title}\n{item.source_path}\n{item.url or ''}".lower().replace("\\", "/")
        if any(term in title_source for term in subject_terms):
            output.append(item)
    for index, item in enumerate(output, start=1):
        item.rank = index
    return output


def _filter_fact_answer_sources(question: str, results: list[SearchResult], answer: str) -> list[SearchResult]:
    if _is_modpack_archive_fact_question(question):
        filtered = _filter_answer_evidence_by_required_terms(question, results)
    elif _is_version_install_question(question):
        filtered = _filter_version_install_fact_results(question, results)
    else:
        filtered = results
    cited_ranks = {int(match.group(1)) for match in re.finditer(r"\[S(\d+)\]", str(answer or ""))}
    if cited_ranks:
        filtered = [item for item in filtered if int(item.rank) in cited_ranks]
    for index, item in enumerate(filtered, start=1):
        item.rank = index
    return filtered


def _primary_fact_subject_terms(question: str) -> list[str]:
    text = str(question or "")
    lowered = text.lower()
    terms: list[str] = []
    known_aliases: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
        (("农夫乐事", "farmer's delight", "farmers delight", "farmers-delight"), ("农夫乐事", "farmer's delight", "farmers delight", "farmer-s-delight", "farmers-delight", "class/2820", "mod/farmers-delight")),
        (("机械动力", "create mod", "create /", " create ", "createmod", "createmod.net"), ("机械动力", "create", "create mod", "createmod", "wiki.createmod.net", "modrinth.com/mod/create", "class/2021")),
        (("乌托邦探险之旅", "utopian journey", "utopia-journey"), ("乌托邦探险之旅", "utopian journey", "utopia-journey", "modpack/1337")),
        (("香草纪元", "vanillaera", "fareschron"), ("香草纪元", "vanillaera", "fareschron", "fares chron")),
        (("craftoria",), ("craftoria", "craftoria-1.31.0", "Craftoria-1.31.0.zip")),
        (("落幕曲", "closing song"), ("落幕曲", "closing song")),
    )
    for needles, aliases in known_aliases:
        if any((needle in text) or (needle in lowered) for needle in needles):
            terms.extend(alias.lower() for alias in aliases)
    if "\u4e4c\u6258\u90a6\u63a2\u9669\u4e4b\u65c5" in text or "utopia journey" in lowered or "utopian journey" in lowered or "utopia-journey" in lowered:
        terms.extend(["\u4e4c\u6258\u90a6\u63a2\u9669\u4e4b\u65c5", "\u4e4c\u6258\u90a6", "utopia journey", "utopian journey", "utopia-journey", "modpack/1337"])
    for match in re.finditer(r"([A-Za-z][A-Za-z0-9_ '\-]{2,}|[\u4e00-\u9fff]{2,})(?:是什么|支持|版本|加载器|安装|下载|项目页|配置|运行环境)", text):
        value = match.group(1).strip(" ，,。？?：:")
        if value and value not in {"请说明它", "它", "这个"}:
            terms.append(value.lower())
    return _dedupe_strings(terms)[:8]


def _is_modpack_fact_query_text(question: str) -> bool:
    text = str(question or "")
    lowered = text.lower()
    return any(marker in lowered or marker in text for marker in ("modpack", "整合包", "包体", ".mrpack", ".zip", "manifest", "香草纪元", "乌托邦探险之旅", "utopian journey", "vanillaera", "fareschron", "craftoria", "落幕曲", "closing song"))


def _local_modpack_archive_fact_answer(question: str, results: list[SearchResult]) -> str:
    if not _is_modpack_archive_fact_question(question):
        return ""
    filtered_results = _filter_answer_evidence_by_required_terms(question, results)
    if not filtered_results:
        return ""
    facts: dict[str, list[str]] = {
        "filename": [],
        "source_page_or_metadata_endpoint": [],
        "direct_archive_url": [],
        "final_probe_url": [],
        "probe_status": [],
        "probe_content_type": [],
        "probe_content_range": [],
        "probe_magic_hex": [],
        "archive_magic": [],
        "bytes": [],
        "sha256": [],
        "local_archive_path": [],
        "zip_entries": [],
        "mods_count": [],
        "ftbquests_count": [],
    }
    evidence_ranks: list[int] = []
    for item in filtered_results[:8]:
        text = _read_result_full_text(item)
        if "source: modpack_download_evidence" not in text.lower() and "downloaded_archive_evidence" not in str(item.source_path).lower():
            continue
        extracted = _extract_bullet_fact_map(text)
        if not extracted:
            continue
        evidence_ranks.append(item.rank)
        for key in facts:
            value = extracted.get(key)
            if value:
                facts[key].append(f"{value} [S{item.rank}]")
    facts = {key: _dedupe_strings(values)[:2] for key, values in facts.items() if values}
    if not facts:
        return ""
    lines = ["根据本地下载证据，当前能确认的包体事实如下："]
    if facts.get("filename"):
        lines.append("- 文件名：" + "；".join(facts["filename"]))
    if facts.get("source_page_or_metadata_endpoint"):
        lines.append("- 元数据/来源入口：" + "；".join(facts["source_page_or_metadata_endpoint"]))
    if facts.get("direct_archive_url"):
        lines.append("- 直连包体 URL：" + "；".join(facts["direct_archive_url"]))
    if facts.get("bytes"):
        lines.append("- 大小：" + "；".join(facts["bytes"]))
    if facts.get("sha256"):
        lines.append("- SHA256：" + "；".join(facts["sha256"]))
    probe_parts = []
    for key, label in (
        ("probe_status", "HTTP"),
        ("probe_content_type", "Content-Type"),
        ("probe_content_range", "Content-Range"),
        ("probe_magic_hex", "magic"),
        ("archive_magic", "archive"),
    ):
        if facts.get(key):
            probe_parts.append(f"{label}=" + "；".join(facts[key]))
    if probe_parts:
        lines.append("- 下载校验证据：" + "，".join(probe_parts))
    extra_parts = []
    for key, label in (("zip_entries", "zip_entries"), ("mods_count", "mods_count"), ("ftbquests_count", "ftbquests_count")):
        if facts.get(key):
            extra_parts.append(f"{label}=" + "；".join(facts[key]))
    if extra_parts:
        lines.append("- 包内摘要：" + "，".join(extra_parts))
    if facts.get("local_archive_path"):
        lines.append("- 本地归档路径：" + "；".join(facts["local_archive_path"]))
    if evidence_ranks:
        lines.append("这些是 Crawler 下载证据文件里记录的客观事实；候选是否采信由 CrawlerAgent 基于这些事实判断。")
    return "\n".join(lines)


def _extract_bullet_fact_map(text: str) -> dict[str, str]:
    facts: dict[str, str] = {}
    for line in str(text or "").splitlines():
        match = re.match(r"\s*-\s*([A-Za-z0-9_/-]+)\s*:\s*(.+?)\s*$", line)
        if not match:
            continue
        key = match.group(1).strip().lower().replace("-", "_")
        value = match.group(2).strip()
        if value and key not in facts:
            facts[key] = value
    return facts


def _is_modpack_archive_fact_question(question: str) -> bool:
    text = str(question or "")
    lowered = text.lower()
    if not re.search(r"整合包|modpack|包体|archive|\.mrpack|\.zip", text, flags=re.I):
        return False
    return any(
        token in lowered
        for token in (
            "sha256",
            "hash",
            "checksum",
            "bytes",
            "content-range",
            "direct_archive_url",
            "downloaded_archive_evidence",
        )
    ) or any(token in text for token in ("包体", "来源", "大小", "校验", "哈希", "直链", "下载地址"))


def _version_install_fact_labels() -> tuple[str, ...]:
    return _VERSION_INSTALL_LABELS


def _append_fact_values(target: list[str], values: list[str], rank: int) -> None:
    for value in values:
        clean = _normalize_version_install_fact_line(value)
        if clean:
            target.append(f"{clean} [S{rank}]")


def _extract_version_install_fact_map(text: str) -> dict[str, list[str]]:
    labels = _version_install_fact_labels()
    facts: dict[str, list[str]] = {label: [] for label in labels}
    body = str(text or "")
    labels_to_patterns: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            labels[0],
            (
                rf"{re.escape(_VI['latest_version'])}[：:]\s*([0-9][0-9A-Za-z_.-]*)",
                rf"{re.escape(_VI['history_version'])}[：:]\s*([0-9][0-9A-Za-z_.-]*)",
                r"(?i)latest version[：:]\s*([0-9][0-9A-Za-z_.-]*)",
            ),
        ),
        (
            labels[1],
            (
                rf"{re.escape(_VI['mc_java_version'])}\s*[\r\n/ ]+\s*(1\.\d+(?:\.\d+)?)",
                rf"{re.escape(_VI['resource_version'])}\s*[\r\n/ ]+\s*(1\.\d+(?:\.\d+)?)",
                r"(?i)minecraft(?: java)? version\s*[\r\n: /]+\s*(1\.\d+(?:\.\d+)?)",
            ),
        ),
        (
            labels[3],
            (
                rf"{re.escape(_VI['java_requirement'])}[：:]\s*([0-9][0-9A-Za-z_. -]*)",
                r"(?i)java requirement[：:]\s*([0-9][0-9A-Za-z_. -]*)",
                rf"{re.escape(_u('63A8 8350'))}Java{re.escape(_u('7248 672C'))}[：:]\s*(Java\s*[0-9A-Za-z_. -]+)",
            ),
        ),
        (
            labels[6],
            (rf"{re.escape(_VI['download_url'])}[：:]\s*(https?://\S+)",),
        ),
    )
    for label, patterns in labels_to_patterns:
        for pattern in patterns:
            for match in re.finditer(pattern, body):
                facts[label].append(match.group(1).strip(" ,;，；。"))

    launcher_hits = [name.upper() for name in re.findall(r"(?i)(?:pcl2?|hmcl)", body)]
    if launcher_hits:
        facts[labels[4]].append("/".join(_dedupe_strings(launcher_hits)))
    for pattern in (
        rf"{re.escape(_VI['memory'])}(?:{re.escape(_u('9700 6C42'))}|{re.escape(_u('5EFA 8BAE'))}|{re.escape(_u('8BBE 7F6E'))})?(?:{re.escape(_u('FF1A'))}|:)\s*([^\r\n]+)",
        rf"(?i)memory (?:requirement|recommendation|setting)?(?:{re.escape(_u('FF1A'))}|:)\s*([^\r\n]+)",
    ):
        for match in re.finditer(pattern, body):
            memory_hits = re.findall(r"(?i)(?:8|10|12|16|32)\s*g(?:b)?", match.group(1))
            if memory_hits:
                facts[labels[5]].append(", ".join(_dedupe_strings(memory_hits)))
    for line in _extract_version_install_lines(body):
        if _version_install_line_is_navigation_noise(line):
            continue
        for loader in re.findall(r"(?i)\b(?:fabric|forge|quilt|neoforge)\b", line):
            facts[labels[2]].append(loader)
        if _VI["install"] in line and not facts[labels[4]]:
            facts[labels[4]].append(line)
        if _VI["memory"] in line:
            memory_hits = re.findall(r"(?i)(?:8|10|12|16|32)\s*g(?:b)?", line)
            if memory_hits:
                facts[labels[5]].append(", ".join(_dedupe_strings(memory_hits)))
        if _VI["runtime"] in line or _VI["client"] in line or _VI["server"] in line:
            normalized_runtime = _normalize_version_install_fact_line(line)
            if normalized_runtime and len(normalized_runtime) <= 100:
                facts[labels[7]].append(normalized_runtime)
    return {label: _clean_version_install_values(label, values) for label, values in facts.items() if values}


def _clean_version_install_values(label: str, values: list[str]) -> list[str]:
    cleaned = _dedupe_strings(values)
    labels = _version_install_fact_labels()
    if label == labels[0]:
        dotted = [value for value in cleaned if "." in value]
        if dotted:
            cleaned = dotted
    if label == labels[4]:
        launcher = [value for value in cleaned if re.search(r"(?i)pcl|hmcl", value)]
        if launcher:
            cleaned = launcher
    return cleaned


def _normalize_version_install_fact_line(line: str) -> str:
    value = re.sub(r"\s+", " ", str(line or "")).strip(" /")
    if not value:
        return ""
    parts = [part.strip(" /") for part in value.split(" / ") if part.strip(" /")]
    useful: list[str] = []
    for part in parts or [value]:
        lowered = part.lower()
        if _version_install_neighbor_is_noise(part):
            continue
        if lowered.startswith(_VI["api_requirement"].lower()) or lowered.startswith(_VI["related_links"].lower()):
            continue
        if "[image:" in lowered or "static/image/" in lowered:
            continue
        if len(part) > 180:
            part = part[:180].rstrip() + "..."
        useful.append(part)
    return " / ".join(_dedupe_strings(useful))[:260]

def _is_version_install_question(question: str) -> bool:
    lowered = question.lower()
    return any(
        token in question
        for token in ("版本", "安装", "配置", "加载器", "内存", "运行环境", "要求", "兼容")
    ) or any(token in lowered for token in ("version", "install", "fabric", "forge", "java", "memory", "requirement"))


def _should_surface_version_install_evidence(question: str) -> bool:
    if _is_version_install_question(question):
        return True
    return _is_modpack_overview_question(question)


def _is_modpack_overview_question(question: str) -> bool:
    lowered = question.lower()
    is_modpack_topic = any(token in question for token in ("整合包", "模组包")) or "modpack" in lowered
    is_overview = any(token in question for token in ("是什么", "介绍", "简介", "概览", "详情", "资料", "信息")) or any(
        token in lowered for token in ("what is", "overview", "intro", "introduction", "about")
    )
    return is_modpack_topic and is_overview


def _extract_version_install_lines(text: str) -> list[str]:
    wanted = (
        "minecraft",
        "java",
        "fabric",
        "forge",
        "quilt",
        "pcl",
        "hmcl",
        "版本",
        "资源版本",
        "最新版本",
        "历史版本",
        "我的世界java版本",
        "平台",
        "核心",
        "加载器",
        "安装",
        "内存",
        "运行环境",
        "客户端",
        "服务端",
        "下载地址",
    )
    lines = [line.strip() for line in text.splitlines()]
    output: list[str] = []
    for index, line in enumerate(lines):
        clean = _clean_evidence_line(line)
        if not clean:
            continue
        if _version_install_line_is_navigation_noise(clean):
            continue
        lowered = clean.lower()
        if not any(token in lowered for token in wanted):
            continue
        compact_clean = re.sub(r"\s+", "", clean.lower())
        window: list[str] = []
        if index > 0:
            previous = _clean_evidence_line(lines[index - 1])
            if previous and len(previous) <= 120 and not _version_install_neighbor_is_noise(previous):
                window.append(previous)
        window.append(clean)
        for offset in (1, 2):
            if index + offset >= len(lines):
                continue
            following = _clean_evidence_line(lines[index + offset])
            if following and len(following) <= 120 and not _version_install_neighbor_is_noise(following):
                window.append(following)
            if (
                "我的世界java版本" in compact_clean
                or clean.lower() in {"minecraft java version", "minecraft version", "platform", "平台"}
            ) and following:
                break
        entry = " / ".join(_dedupe_strings(window))
        if _version_install_line_is_navigation_noise(entry):
            continue
        if entry and entry not in output:
            output.append(entry[:260])
        if len(output) >= 18:
            break
    return output


def _version_install_line_is_navigation_noise(line: str) -> bool:
    value = re.sub(r"\s+", " ", str(line or "")).strip()
    if not value:
        return True
    parts = [part.strip() for part in re.split(r"\s*/\s*|\s+\|\s+", value) if part.strip()]
    nav_like = 0
    for part in parts:
        lowered = part.lower()
        if re.fullmatch(r"(?:forge|fabric|quilt|neoforge)\s*(?:模组|整合包|mods?|modpacks?)", lowered, flags=re.I):
            nav_like += 1
        elif re.fullmatch(r"1\.\d+(?:\.\d+)?\s*(?:模组|整合包|mods?|modpacks?)", lowered, flags=re.I):
            nav_like += 1
        elif part in {"版本检索", "元素检索", "常用地址", "最新收录", "有新动态"}:
            nav_like += 1
    return bool(parts) and nav_like >= max(2, len(parts) // 2)


def _version_install_neighbor_is_noise(line: str) -> bool:
    lowered = line.lower()
    if "<!--" in lowered or "-->" in lowered:
        return True
    if "｜" in line or "|" in line:
        return True
    if lowered.startswith(("web source", "query", "snippet", "search ")):
        return True
    return False


def _answer_is_garbled(answer: str) -> bool:
    body = _strip_answer_metadata(answer)
    compact = re.sub(r"\s+", "", body)
    if not compact:
        return True
    question_marks = compact.count("?") + compact.count("？") + compact.count("\ufffd")
    cjk = len(re.findall(r"[\u4e00-\u9fff]", compact))
    ascii_letters = len(re.findall(r"[A-Za-z]", compact))
    if question_marks >= 6 and question_marks > cjk + ascii_letters:
        return True
    if len(compact) <= 24 and question_marks >= max(4, len(compact) // 2):
        return True
    return False


def _local_extractive_answer(question: str, results: list[SearchResult], *, fast: bool = False) -> str:
    if _is_boss_question(question):
        return _local_boss_extractive_answer(question, results, fast=fast)
    if _is_modpack_mod_list_question(question):
        modpack_answer = _local_modpack_mod_list_answer(question, results)
        if modpack_answer:
            return modpack_answer
    is_guide_question = any(token in question for token in ("怎么玩", "怎么入门", "新手", "开局", "路线", "攻略", "流程"))
    is_procedure_question = _is_procedure_question(question)
    is_explanation_question = _is_explanation_question(question)
    if is_guide_question or is_procedure_question or is_explanation_question:
        names = []
        snippets = _generic_extractive_snippets(question, results, limit=10, fast=fast)
    elif _is_plain_list_question(question):
        names, snippets = _extract_list_candidates(question, results) if not fast else ([], _generic_extractive_snippets(question, results, limit=10, fast=True))
    else:
        names = []
        snippets = _generic_extractive_snippets(question, results, fast=fast)
    recipe_note = "" if fast else _recipe_extraction_note(question, results)
    lines: list[str] = []
    if names:
        source_rank = results[0].rank if results else 1
        lines.append("\u6839\u636e\u672c\u5730\u8bc1\u636e\u62bd\u53d6\u5230\u4ee5\u4e0b\u5019\u9009\u5185\u5bb9\uff1a")
        lines.extend(f"- {name}" for name in names[:24])
        lines.append(f"\u4ee5\u4e0a\u5185\u5bb9\u6765\u81ea\u672c\u5730\u8d44\u6599 [S{source_rank}]\u3002")
    elif recipe_note:
        lines.append("\u6839\u636e\u672c\u5730\u8d44\u6599\uff0c\u76f8\u5173\u5236\u4f5c/\u914d\u65b9\u8bc1\u636e\u5982\u4e0b\uff1a")
        for line in recipe_note.splitlines()[1:12]:
            lines.append(line)
    elif snippets:
        if is_guide_question:
            return _compose_guide_fallback_answer(question, snippets, results)
        if is_explanation_question:
            lines.append("本地资料能解释到以下相关内容，但关联强弱需要看来源标题和正文：")
        else:
            lines.append("\u672c\u5730\u8d44\u6599\u4e2d\u627e\u5230\u4ee5\u4e0b\u76f8\u5173\u8bc1\u636e\uff1a")
        lines.extend(f"- {line}" for line in snippets[:10])
    else:
        lines.append("\u6a21\u578b\u8c03\u7528\u8d85\u65f6\uff1b\u672c\u5730\u68c0\u7d22\u547d\u4e2d\u4e86\u8d44\u6599\uff0c\u4f46\u6ca1\u6709\u62bd\u53d6\u5230\u8db3\u591f\u7a33\u5b9a\u7684\u7ed3\u6784\u5316\u7b54\u6848\u3002")
    return "\n".join(lines)


def _is_modpack_mod_list_question(question: str) -> bool:
    return bool(
        re.search(r"有哪些.*(?:模组|mods?|MOD)|(?:模组|mods?|MOD).*有哪些", question, flags=re.I)
        or re.search(r"(?:包含|内置|自带|整合包|modpack).{0,18}(?:模组|mods?|MOD).{0,18}(?:列表|清单|数量|总览|明细)", question, flags=re.I)
        or re.search(r"(?:模组|mods?|MOD).{0,18}(?:列表|清单|数量|总览|明细|included)", question, flags=re.I)
        or re.search(r"included\s+mods?|mod\s+list", question, flags=re.I)
    )


def _compose_guide_fallback_answer(question: str, snippets: list[str], results: list[SearchResult]) -> str:
    source_rank = results[0].rank if results else 1
    steps: list[str] = []

    def add(text: str) -> None:
        if text not in steps:
            steps.append(text)

    for line in _select_diverse_guide_snippets(snippets):
        if _looks_like_page_title_or_external_download(line):
            continue
        if _guide_snippet_is_low_value(line):
            continue
        add(line)

    topic = _guide_answer_topic_label(question, results)
    prefix = f"基于当前本地资料，{topic}新手可以这样起步：" if topic else "基于当前本地资料，新手可以这样起步："
    lines = [prefix, ""]
    if steps:
        lines.extend(f"{index}. {step}" for index, step in enumerate(steps[:6], start=1))
    else:
        lines.append("当前命中的本地资料没有抽取到足够清晰的玩法步骤，需要先让 Crawler 补充更直接的教程或机制资料。")
    lines.append("")
    lines.append(f"说明：以上依据当前命中的本地资料整理 [S{source_rank}]；更完整的阶段路线、物品细节和版本差异仍需要继续补库。")
    return "\n".join(lines)


def _select_diverse_guide_snippets(snippets: list[str], limit: int = 6) -> list[str]:
    buckets: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("progression", ("进度", "指引", "指导", "入门", "新手", "开局", "默认按 L")),
        ("farming", ("野生作物", "开花的植物", "种子", "样本", "农场", "探索")),
        ("cutting", ("砧板", "刀", "燧石刀", "切割", "切成", "食材")),
        ("cooking", ("厨锅", "煎锅", "热源", "炉灶", "烹饪", "合成书")),
        ("recipe", ("配方", "合成", "材料", "制作", "获取")),
    )
    selected: list[str] = []
    used: set[str] = set()
    seen_bucket: set[str] = set()
    for _, markers in buckets:
        for line in snippets:
            if line in used:
                continue
            if any(marker in line for marker in markers):
                selected.append(line)
                used.add(line)
                seen_bucket.add(_guide_snippet_bucket(line))
                break
    for line in snippets:
        if len(selected) >= limit:
            break
        bucket = _guide_snippet_bucket(line)
        if bucket in seen_bucket and bucket != "general":
            continue
        if line not in used:
            selected.append(line)
            used.add(line)
            seen_bucket.add(bucket)
    return selected[:limit]


def _guide_snippet_bucket(line: str) -> str:
    if any(marker in line for marker in ("砧板", "刀", "燧石刀", "切割", "切成", "食材")):
        return "cutting"
    if any(marker in line for marker in ("厨锅", "煎锅", "热源", "炉灶", "烹饪", "合成书")):
        return "cooking"
    if any(marker in line for marker in ("野生作物", "开花的植物", "种子", "样本", "农场")):
        return "farming"
    if any(marker in line for marker in ("进度", "指引", "指导", "入门", "新手", "开局", "默认按 L")):
        return "progression"
    if any(marker in line for marker in ("配方", "合成", "材料", "制作", "获取")):
        return "recipe"
    return "general"


def _guide_answer_topic_label(question: str, results: list[SearchResult]) -> str:
    direct = re.match(r"\s*(.+?)(?:\s+)?(?:新手|萌新|入门|应该|怎么|怎样|如何|起步|开始)", str(question or ""))
    if direct:
        label = re.sub(r"(?:基于|请|介绍|讲讲|说说|详细).*?$", "", direct.group(1)).strip(" ，,。？?：:")
        if label and len(label) <= 80:
            return label
    intent = analyze_query(question, CONCEPTS)
    label = str(intent.entity or "").strip()
    if not label and results:
        label = re.sub(r"^\[[^\]]+\]", "", str(results[0].title or "")).strip()
        label = re.split(r"[\(\|｜]", label, maxsplit=1)[0].strip()
    label = re.sub(r"(?:新手|萌新|入门|怎么玩|玩法|攻略|教程|流程|路线).*$", "", label).strip()
    if label.lower() in {"minecraft", "mc", "mod", "mods", "modpack"}:
        return ""
    return label


def _guide_snippet_is_low_value(line: str) -> bool:
    clean = str(line or "").strip()
    if not clean:
        return True
    if len(clean) <= 8:
        return True
    low_value = ("剪刀：分解皮革物品", "Image:", "图片]", "添加新资料", "编辑资料", "关系类型", "Delight)", "'s Delight", "Compats")
    return any(token in clean for token in low_value)


def _looks_like_page_title_or_external_download(line: str) -> bool:
    return any(token in line for token in ("下载", "最新版", "手机版", "安装包", "游戏狗", "软件库", "MC百科|最大的Minecraft中文MOD百科"))


def _local_modpack_mod_list_answer(question: str, results: list[SearchResult]) -> str:
    best_names: list[str] = []
    best_result: SearchResult | None = None
    for item in results:
        text = _read_result_full_text(item)
        names = _extract_included_mod_names(text)
        if len(names) > len(best_names):
            best_names = names
            best_result = item
    if not best_names:
        return ""
    source_rank = best_result.rank if best_result else (results[0].rank if results else 1)
    total = len(best_names)
    shown = best_names[:180]
    lines = [f"本地整合包清单里解析到 {total} 个模组/文件，先列前 {len(shown)} 个："]
    lines.extend(f"- {name}" for name in shown)
    if total > len(shown):
        lines.append(f"- ……还有 {total - len(shown)} 个未列出")
    lines.append(f"以上来自整合包内容清单 [S{source_rank}]。")
    return "\n".join(lines)


def _extract_included_mod_names(text: str) -> list[str]:
    names: list[str] = []
    in_section = False
    lines = [raw_line.strip() for raw_line in text.splitlines()]
    for line in lines:
        if re.match(r"#{2,4}\s+Included Mods / Files", line, flags=re.I):
            in_section = True
            continue
        if in_section and line.startswith("#"):
            break
        if not in_section:
            continue
        match = re.match(r"-\s+(.+?)\s+—\s+`(?:mods|resourcepacks|shaderpacks)/", line)
        if not match:
            continue
        name = re.sub(r"\s+\([^)]*\)\s*$", "", match.group(1)).strip()
        if name and name not in names:
            names.append(name)
    contains_index = next((index for index, line in enumerate(lines) if re.match(r"包含模组\s*[（(]\d+[）)]", line)), -1)
    if contains_index >= 0:
        stop_markers = ("整合包介绍", "更新日志", "最近参与编辑", "相关链接", "编辑整合包")
        ignored_names = {
            "前言",
            "主站",
            "整合包",
            "常用地址",
            "版本检索",
            "元素检索",
            "社群",
            "实用工具",
            "特性",
            "词典",
        }
        section_lines = lines[contains_index + 1 :]
        for index, line in enumerate(section_lines):
            if index > 20 and any(marker == line for marker in stop_markers):
                break
            if not re.match(r"v?\d[\w.+-]{0,40}$", line):
                continue
            if index == 0:
                continue
            name = ""
            for previous in range(index - 1, max(-1, index - 6), -1):
                candidate = section_lines[previous].strip()
                if candidate:
                    name = candidate
                    break
            if not name or name in ignored_names:
                continue
            if re.match(r"v?\d[\w.+-]{0,40}$", name):
                continue
            if len(name) > 120 or len(name) < 2:
                continue
            if any(token in name for token in ("今日", "登录", "浏览", "编辑", "红票", "黑票", "Image:")):
                continue
            name = re.sub(r"\s+", " ", name).strip()
            if name and name not in names:
                names.append(name)
    return names


def _local_boss_extractive_answer(question: str, results: list[SearchResult], *, fast: bool = False) -> str:
    focus_terms = _focus_terms_for_question(question)
    names: list[str] = []
    named_lines: list[str] = []
    detail_lines: list[str] = []
    wants_list = _is_plain_list_question(question)
    wants_detail = _is_procedure_question(question)
    detail_markers = _boss_detail_markers_for_question(question)
    for item in results:
        text = item.text if fast else _read_result_full_text(item)
        if not _source_matches_focus(item, text, focus_terms):
            continue
        for raw_line in text.splitlines():
            clean = _clean_evidence_line(raw_line)
            if not clean or _noisy_evidence_line(clean):
                continue
            lowered = clean.lower()
            if not any(token in lowered for token in ("boss", "首领", "头目", "魔王")):
                continue
            if not any(token in clean for token in ("最终", "打法", "击败", "打败", "挑战", "掉落", "刷新", "生成", "位置", "地点", "血量")):
                continue
            line_names = _boss_names_from_context_line(clean)
            for name in line_names:
                if name not in names:
                    names.append(name)
            if line_names and clean not in named_lines:
                named_lines.append(_normalize_boss_evidence_line(clean)[:260])
            if wants_detail and _boss_detail_line_matches_question(clean, detail_markers) and clean not in detail_lines and not _boss_detail_line_is_noise(clean):
                detail_lines.append(clean[:260])
            if len(named_lines) + len(detail_lines) >= 8:
                break
        if len(named_lines) + len(detail_lines) >= 8:
            break
    if names:
        if wants_detail:
            heading = "本地资料里找到这些 Boss 相关打法/位置/掉落证据："
            output = [heading]
            if detail_lines:
                output.extend(f"- {line}" for line in detail_lines[:8])
            else:
                output.extend(f"- {line}" for line in named_lines[:6])
            output.append("")
            output.append("已能稳定点名的 Boss/类 Boss 目标：" + "、".join(names[:12]))
            output.append("说明：以上是当前本地资料能抽到的证据句，不等于完整攻略；缺少的 Boss 位置、掉落或阶段机制仍应让 Crawler 继续补齐。")
            return "\n".join(output)
        output = ["本地资料里能明确点名的 Boss/类 Boss 目标有："]
        output.extend(f"- {name}" for name in names[:12])
        if named_lines:
            output.append("")
            output.append("对应证据：")
            output.extend(f"- {line}" for line in named_lines[:6])
        if wants_list:
            output.append("")
            output.append("说明：这些不是完整 Boss 清单，只是当前本地证据能稳定点名的内容；完整清单仍建议让 Crawler 继续补齐。")
        return "\n".join(output)
    lines = detail_lines[:8]
    if not lines:
        return "本地资料里暂时没有稳定的 Boss 清单证据；需要 Crawler 继续补齐 Boss 名称、位置、打法和掉落。"
    output = ["本地资料里找到这些 Boss 相关证据，但还不足以保证是完整清单："]
    output.extend(f"- {line}" for line in lines)
    return "\n".join(output)


def _boss_detail_markers_for_question(question: str) -> tuple[str, ...]:
    if any(token in question for token in ("掉落", "掉什么", "掉落物", "奖励")):
        return ("掉落", "掉落物", "奖励", "获得", "获取")
    if any(token in question for token in ("哪里", "在哪", "位置", "地点", "生成", "刷新")):
        return ("位置", "地点", "生成", "刷新", "维度", "主世界", "末地", "地下", "海上")
    if any(token in question for token in ("怎么打", "如何打", "怎样打", "打法", "击败", "打败", "挑战")):
        return ("打法", "击败", "打败", "挑战", "准备", "输出", "控制", "躲", "站撸", "扫射", "药水", "护甲", "武器", "法术")
    return ("打法", "击败", "掉落", "位置", "生成", "刷新", "挑战")


def _boss_detail_line_matches_question(line: str, markers: tuple[str, ...]) -> bool:
    return any(marker in line for marker in markers)


def _normalize_boss_evidence_line(line: str) -> str:
    line = re.sub(r"将(?P<name>[\u4e00-\u9fff]{2,8})魔改成为", r"\g<name>被魔改成为", line)
    return line


def _boss_names_from_context_line(line: str) -> list[str]:
    names: list[str] = []
    patterns = [
        r"把(?P<name>[\u4e00-\u9fff]{2,8})排在[^。；;，,]{0,18}(?:最终)?boss",
        r"(?P<name>[\u4e00-\u9fff]{2,8})(?:被)?魔改[^。；;，,]{0,18}(?:最终)?Boss",
        r"(?P<name>[\u4e00-\u9fff]{2,8})[^。；;，,]{0,12}成为[^。；;，,]{0,18}(?:最终)?Boss",
        r"(?:最终|主线|可打|挑战)[^。；;，,]{0,20}(?P<name>[\u4e00-\u9fff]{2,8})(?:boss|Boss|BOSS|首领)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, line, flags=re.I):
            candidate = match.group("name")
            candidate = re.sub(r"^(?:将|把|被)", "", candidate)
            candidate = re.sub(r"(?:魔改|作为|成为|排在).*$", "", candidate)
            candidate = candidate.strip(" 的了也和与及")
            if _valid_candidate_name(candidate, [], is_boss_question=True) and candidate not in names:
                names.append(candidate)
    explicit_names = ("下亚", "亚波伦", "末影龙", "炎魔", "火龙", "冰龙", "电龙", "凋零")
    if any(token in line.lower() for token in ("boss", "首领", "头目", "魔王")):
        for name in explicit_names:
            if name in line and name not in names:
                names.append(name)
    return names


def _boss_detail_line_is_noise(line: str) -> bool:
    if len(line) > 180 and not any(name in line for name in ("亚波伦", "下亚", "末影龙", "火龙", "冰龙", "电龙")):
        return True
    if len(re.findall(r"[（(]\d+[）)]", line)) >= 2:
        return True
    return any(token in line for token in ("词条", "护甲值", "盔甲韧性", "服务器", "联机", "下载", "安装", "悬赏令", "词缀", "存储"))


def _generic_extractive_snippets(question: str, results: list[SearchResult], limit: int = 8, *, fast: bool = False) -> list[str]:
    focus_terms = _focus_terms_for_question(question)
    guide_or_mechanics = _needs_general_grounded_answer(question)
    snippets: list[str] = []
    for item in results[:8]:
        text = item.text if fast else _read_result_full_text(item)
        per_item_limit = 10 if guide_or_mechanics else 4
        lines = _evidence_lines_from_text(text, focus_terms, limit=per_item_limit, allow_marker_without_focus=guide_or_mechanics)
        if focus_terms and not guide_or_mechanics:
            lines = [
                line for line in lines
                if any(term.lower() in line.lower() for term in focus_terms if len(term) >= 2)
            ]
        if not lines:
            lines = []
        for line in lines:
            clean = _clean_evidence_line(line)
            if clean and not _noisy_evidence_line(clean) and clean not in snippets:
                snippets.append(clean[:260])
                if len(snippets) >= limit and not guide_or_mechanics:
                    return snippets
    if guide_or_mechanics:
        return snippets[: max(limit, 24)]
    return snippets[:limit]


def _noisy_evidence_line(line: str) -> bool:
    clean = line.strip()
    if not clean:
        return True
    lowered = clean.lower()
    if lowered.startswith(("[image:", "image:", "![", "http://", "https://", "//www.mcmod.cn/static")):
        return True
    if any(token in lowered for token in ("logo.png", "identicons", "/static/public/images", "@480x300", "favicon", "loading-colourful.gif", "loading.gif")):
        return True
    if re.search(r"^\[?image[:\]]", clean, flags=re.I):
        return True
    if len(clean) < 8 and not re.search(r"\d|[\u4e00-\u9fff]{4,}", clean):
        return True
    title_noise = ("MC百科|最大的Minecraft中文MOD百科", "Minecraft中文MOD百科")
    if any(token in clean for token in title_noise) and not any(marker in clean for marker in ("玩法", "攻略", "路线", "合成", "获取", "Boss", "任务")):
        return True
    return False


def _format_context_with_deep_evidence(question: str, results: list[SearchResult]) -> str:
    parts: list[str] = []
    focus_terms = _focus_terms_for_question(question)
    used = 0
    for result in results:
        source = result.url or result.source_path
        deep = _deep_evidence_for_result(question, result, focus_terms)
        base_text = _compact_source_text(result.text, focus_terms, MAX_SOURCE_CONTEXT_CHARS)
        lines = [
            f"[S{result.rank}] {result.title}",
            f"source: {source}",
            f"score: {result.score:.4f}",
            base_text,
        ]
        if deep:
            lines.extend(["", "## Same-source Deep Evidence", deep])
        block = "\n".join(lines)
        if used + len(block) > MAX_MODEL_CONTEXT_CHARS and parts:
            break
        parts.append(block)
        used += len(block)
    return "\n\n---\n\n".join(parts)


def _compact_source_text(text: str, focus_terms: list[str], budget: int) -> str:
    text = text.strip()
    if "从本地整合包页面解析到" in text and "包含模组/文件" in text:
        lines: list[str] = []
        used = 0
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            prefix = "" if line.startswith(("#", "- ")) else "- "
            entry = f"{prefix}{line}"
            if used + len(entry) + 1 > budget:
                break
            lines.append(entry)
            used += len(entry) + 1
        if lines:
            return "\n".join(lines)
    lines = _evidence_lines_from_text(text, focus_terms, limit=10)
    compact = "\n".join(f"- {line[:260]}" for line in lines)
    if compact:
        return compact[:budget]
    if len(text) <= budget:
        return text
    return text[:budget].rstrip() + "\n...[truncated]"


def _deep_evidence_for_result(question: str, item: SearchResult, focus_terms: list[str]) -> str:
    try:
        source_path = Path(item.source_path)
    except TypeError:
        return ""
    full_text = _read_result_full_text(item)
    deep_limit = 30 if _is_boss_focus(focus_terms) else 14
    lines = _evidence_lines_from_text(full_text, focus_terms, limit=deep_limit)
    raw_lines = _raw_html_text_evidence_lines(source_path, focus_terms, limit=6)
    if raw_lines:
        lines.extend(line for line in raw_lines if line not in lines)
    if not lines:
        return ""
    output: list[str] = []
    used = 0
    for line in lines:
        clean = line[:320]
        if used + len(clean) > MAX_DEEP_EVIDENCE_CHARS:
            break
        output.append(f"- {clean}")
        used += len(clean)
    return "\n".join(output)


def _evidence_lines_from_text(text: str, focus_terms: list[str], limit: int = 18, *, allow_marker_without_focus: bool = False) -> list[str]:
    if not text:
        return []
    strong_terms = [term for term in focus_terms if len(term) >= 2]
    evidence_markers = (
        "合成", "配方", "材料", "获取", "获得", "掉落", "生成", "刷新", "位置",
        "步骤", "路线", "前置", "要求", "打法", "击败", "打败", "挑战", "击杀", "斩杀", "胜利", "机制", "奖励", "用途",
        "玩法", "攻略", "教程", "流程", "进度", "任务", "新手", "入门", "开局", "前期", "中期", "后期",
        "烹饪", "食物", "食材", "厨锅", "煎锅", "砧板", "刀", "种子", "作物", "农场", "热源",
        "guide", "tutorial", "progression", "beginner", "recipe", "cooking", "knife", "cutting board", "crop", "farm",
        "Boss", "BOSS", "boss", "表", "图片", "Table", "Image",
    )
    output: list[str] = []
    active_window = 0
    for raw_line in text.splitlines():
        is_heading = raw_line.lstrip().startswith("#")
        for line in _split_evidence_line(raw_line):
            line = _clean_evidence_line(line)
            if not line or _noisy_evidence_line(line):
                continue
            has_focus = any(term.lower() in line.lower() for term in strong_terms)
            if has_focus:
                active_window = 8
            has_marker = any(marker in line for marker in evidence_markers)
            if has_focus or (active_window > 0 and (has_marker or is_heading)) or (allow_marker_without_focus and has_marker):
                if line not in output:
                    output.append(line)
                    if len(output) >= limit:
                        break
            if active_window > 0:
                active_window -= 1
        if len(output) >= limit:
            break
    return output


def _split_evidence_line(raw_line: str) -> list[str]:
    clean = str(raw_line or "").strip()
    if len(clean) <= 320:
        return [clean]
    parts = re.split(r"(?<=[。！？!?；;])\s+|\s{2,}", clean)
    merged: list[str] = []
    for part in parts:
        value = part.strip()
        if not value:
            continue
        if len(value) <= 360:
            merged.append(value)
        else:
            merged.extend(value[index : index + 360] for index in range(0, len(value), 360))
    return merged or [clean[:360]]


def _is_boss_focus(focus_terms: list[str]) -> bool:
    return any(term.lower() in {"boss", "首领", "头目"} or term in {"BOSS", "Boss"} for term in focus_terms)


def _dedupe_results(results: list[SearchResult], limit: int = 8) -> list[SearchResult]:
    seen_docs: set[int] = set()
    seen_titles: set[str] = set()
    output: list[SearchResult] = []
    for item in results:
        title_key = _canonical_title_key(item.title)
        if int(item.document_id) in seen_docs or title_key in seen_titles:
            continue
        seen_docs.add(int(item.document_id))
        seen_titles.add(title_key)
        output.append(item)
        if len(output) >= limit:
            break
    for index, item in enumerate(output, start=1):
        item.rank = index
    return output


def _dedupe_results_by_chunk_quality(results: list[SearchResult], limit: int = 8) -> list[SearchResult]:
    ranked = sorted(
        results,
        key=lambda item: (_guide_mechanics_dimension_priority(item), _guide_mechanics_evidence_score(item), float(item.score or 0.0)),
        reverse=True,
    )
    output: list[SearchResult] = []
    seen_chunks: set[int] = set()
    seen_title_dimensions: set[tuple[str, str]] = set()
    per_title: dict[str, int] = {}
    for item in ranked:
        if int(item.chunk_id) in seen_chunks:
            continue
        title_key = _canonical_title_key(item.title) or str(item.document_id)
        dimension = _guide_mechanics_dimension(item)
        if (title_key, dimension) in seen_title_dimensions:
            continue
        if per_title.get(title_key, 0) >= 5:
            continue
        seen_chunks.add(int(item.chunk_id))
        seen_title_dimensions.add((title_key, dimension))
        per_title[title_key] = per_title.get(title_key, 0) + 1
        output.append(item)
        if len(output) >= limit:
            break
    for index, item in enumerate(output, start=1):
        item.rank = index
    return output


def _guide_mechanics_dimension(item: SearchResult) -> str:
    text = f"{item.title}\n{item.text[:2600]}".lower()
    progression_hits = sum(1 for marker in ("进度", "指导手册", "新手", "入门", "开局", "progression", "beginner", "guide") if marker in text)
    farming_hits = sum(1 for marker in ("野生作物", "种子", "农场", "开花的植物", "作物", "crop", "seed", "farm") if marker in text)
    cutting_hits = sum(1 for marker in ("砧板", "燧石刀", "切割", "切成", "knife", "cutting board") if marker in text)
    cooking_hits = sum(1 for marker in ("厨锅", "煎锅", "炉灶", "热源", "烹饪", "cooking", "pot", "pan", "stove") if marker in text)
    if progression_hits >= 2 and farming_hits >= 1:
        return "progression"
    if farming_hits >= 2 and cutting_hits == 0 and cooking_hits == 0:
        return "farming"
    if cutting_hits >= 1 and cutting_hits >= cooking_hits:
        return "cutting"
    if cooking_hits >= 1:
        return "cooking"
    checks = (
        ("farming", ("野生作物", "种子", "农场", "开花的植物", "作物", "crop", "seed", "farm")),
        ("cutting", ("砧板", "燧石刀", "切割", "切成", "食材", "knife", "cutting board")),
        ("cooking", ("厨锅", "煎锅", "炉灶", "热源", "烹饪", "cooking", "pot", "pan", "stove")),
        ("progression", ("进度", "指导手册", "新手", "入门", "开局", "progression", "beginner", "guide")),
        ("recipe", ("配方", "合成", "制作", "材料", "recipe", "craft")),
    )
    for name, markers in checks:
        if any(marker in text for marker in markers):
            return name
    return "general"


def _guide_mechanics_dimension_priority(item: SearchResult) -> int:
    order = {
        "progression": 6,
        "farming": 5,
        "cutting": 4,
        "cooking": 3,
        "recipe": 2,
        "general": 1,
    }
    return order.get(_guide_mechanics_dimension(item), 0)


def _supplement_raw_html_results(config: AppConfig, question: str, results: list[SearchResult], limit: int = 8) -> list[SearchResult]:
    focus_terms = _focus_terms_for_question(question)
    useful_terms = [term for term in focus_terms if len(term) >= 2 and term.lower() not in {"minecraft", "mc", "mod"}]
    if not useful_terms:
        return results
    existing_docs = {int(item.document_id) for item in results}
    existing_paths = {str(item.source_path).lower() for item in results}
    candidates: list[tuple[float, Path]] = []
    raw_root = config.paths.source_dir
    raw_files = sorted(raw_root.rglob("raw_html/*.html"), key=lambda path: path.stat().st_mtime, reverse=True) if raw_root.exists() else []
    started = time.monotonic()
    for raw_path in raw_files[:RAW_HTML_SCAN_FILE_LIMIT]:
        if time.monotonic() - started > RAW_HTML_SCAN_SECONDS:
            break
        markdown_path = _markdown_path_for_raw_html(raw_path)
        if markdown_path is None or str(markdown_path).lower() in existing_paths:
            continue
        try:
            stat = raw_path.stat()
        except OSError:
            continue
        if stat.st_size > 12_000_000:
            continue
        haystack = _cached_raw_html_scan_text(raw_path, stat.st_mtime, stat.st_size)
        if not haystack:
            continue
        hits = [term for term in useful_terms if term.lower() in haystack]
        if not hits:
            continue
        score = len(hits) / max(len(useful_terms), 1)
        if any(term.lower() in str(markdown_path).lower() for term in useful_terms):
            score += 0.35
        if "raw_html" in str(raw_path).lower():
            score += 0.1
        candidates.append((score, markdown_path))
    if not candidates:
        return results
    candidates.sort(key=lambda item: item[0], reverse=True)
    additions: list[SearchResult] = []
    for score, markdown_path in candidates[:limit]:
        result = _search_result_for_markdown_path(config, markdown_path, question, score)
        if result is None or int(result.document_id) in existing_docs:
            continue
        existing_docs.add(int(result.document_id))
        additions.append(result)
        if len(additions) >= limit:
            break
    merged = [*results, *additions][:limit]
    for index, item in enumerate(merged, start=1):
        item.rank = index
    return merged


def _cached_raw_html_scan_text(raw_path: Path, mtime: float, size: int) -> str:
    key = str(raw_path)
    with RAW_HTML_SCAN_LOCK:
        cached = RAW_HTML_SCAN_CACHE.get(key)
        if cached and cached[0] == mtime and cached[1] == size:
            return cached[2]
    try:
        html = raw_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    parser = _HTMLTextExtractor()
    parser.feed(html)
    parser.close()
    text = f"{raw_path}\n{parser.title}\n{parser.text}\n" + "\n".join(
        f"{image.get('alt','')} {image.get('src','')}" for image in parser.images[:80]
    )
    text = text.lower()
    with RAW_HTML_SCAN_LOCK:
        if len(RAW_HTML_SCAN_CACHE) > 1500:
            RAW_HTML_SCAN_CACHE.clear()
        RAW_HTML_SCAN_CACHE[key] = (mtime, size, text)
    return text


def _markdown_path_for_raw_html(raw_path: Path) -> Path | None:
    if raw_path.parent.name.lower() != "raw_html":
        return None
    run_dir = raw_path.parent.parent
    direct = run_dir / f"{raw_path.stem}.md"
    if direct.exists():
        return direct
    candidates = sorted(run_dir.glob(raw_path.stem.rsplit("_", 1)[0] + "*.md"))
    return candidates[0] if candidates else None


def _search_result_for_markdown_path(config: AppConfig, markdown_path: Path, question: str, score: float) -> SearchResult | None:
    conn = connect(config.paths.db_path)
    try:
        doc = conn.execute(
            """
            SELECT id, title, source_path, url, metadata_json
            FROM documents
            WHERE source_path = ?
            """,
            (str(markdown_path),),
        ).fetchone()
        if doc is None:
            return None
        rows = conn.execute(
            """
            SELECT id AS chunk_id, chunk_index, text, metadata_json
            FROM chunks
            WHERE document_id = ?
            ORDER BY chunk_index
            """,
            (int(doc["id"]),),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    terms = _focus_terms_for_question(question)
    best = max(
        rows,
        key=lambda row: sum(1 for term in terms if term.lower() in str(row["text"]).lower()),
    )
    evidence_text = str(best["text"])
    if _is_modpack_mod_list_question(question):
        try:
            full_text = markdown_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            full_text = ""
        names = _extract_included_mod_names(full_text)
        if names:
            shown = names[:180]
            evidence_text = (
                f"# {doc['title']}\n\n"
                f"从本地整合包页面解析到 {len(names)} 个包含模组/文件。以下是给模型上下文节选的前 {len(shown)} 个，完整清单仍保留在来源页面：\n"
                + "\n".join(f"- {name}" for name in shown)
            )
    metadata: dict[str, Any] = {}
    for raw in (doc["metadata_json"], best["metadata_json"]):
        if raw:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = {}
            if isinstance(value, dict):
                metadata.update(value)
    raw_path = _raw_html_path_for_markdown(markdown_path)
    if raw_path:
        metadata["raw_html_path"] = str(raw_path)
    return SearchResult(
        rank=0,
        score=score,
        chunk_id=int(best["chunk_id"]),
        document_id=int(doc["id"]),
        chunk_index=int(best["chunk_index"]),
        title=str(doc["title"]),
        source_path=str(doc["source_path"]),
        url=str(doc["url"]) if doc["url"] else None,
        text=evidence_text,
        metadata=metadata,
    )


def _canonical_title_key(title: str) -> str:
    title = re.sub(r"\s*-\s*MC百科.*$", "", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip().lower()
    return title


def _same_theme_tutorial_query(question: str) -> str:
    intent = analyze_query(question, CONCEPTS)
    entity = intent.entity or question
    terms = [entity]
    for keyword in intent.keywords:
        if keyword and keyword not in terms:
            terms.append(keyword)
    for marker in ("合成", "配方", "制作", "recipe", "crafting"):
        if marker.lower() in question.lower() and marker not in terms:
            terms.append(marker)
    if "落幕曲" in question and "梦想一心" not in terms:
        terms.append("梦想一心")
    return " ".join(terms[:5])


def _fallback_theme_results(question: str, rough_results: list[SearchResult], limit: int) -> list[SearchResult]:
    if not ("落幕曲" in question and "拔刀剑" in question):
        return []
    preferred: list[SearchResult] = []
    for item in rough_results:
        title = item.title
        path = item.source_path.lower().replace("\\", "/")
        if "落幕曲" not in title and "closing song" not in title.lower():
            continue
        if not any(marker in path for marker in ("crawler_exports/mcmod/", "crawler_exports/web_discovery/")):
            continue
        preferred.append(item)
    preferred.sort(
        key=lambda item: (
            1 if any(token in item.title for token in ("攻略", "教程", "制作", "配置")) else 0,
            1 if "mcmod" in item.source_path.lower() else 0,
            item.score,
        ),
        reverse=True,
    )
    return _dedupe_results(preferred, limit=limit)


def _modpack_manifest_results(question: str, rough_results: list[SearchResult], limit: int) -> list[SearchResult]:
    if not _is_modpack_mod_list_question(question):
        return []
    scored: list[tuple[int, SearchResult]] = []
    for item in rough_results:
        text = _read_result_full_text(item)
        names = _extract_included_mod_names(text)
        if not names:
            continue
        title = item.title.lower()
        score = len(names)
        if "utopia" in title or "乌托邦" in item.title:
            score += 500
        if "modpack_" in item.source_path.lower().replace("\\", "/"):
            score += 100
        shown = names[:180]
        evidence_text = (
            f"# {item.title}\n\n"
            f"从本地整合包页面解析到 {len(names)} 个包含模组/文件。以下是给模型上下文节选的前 {len(shown)} 个，完整清单仍保留在来源页面：\n"
            + "\n".join(f"- {name}" for name in shown)
        )
        scored.append((
            score,
            SearchResult(
                rank=item.rank,
                score=item.score,
                chunk_id=item.chunk_id,
                document_id=item.document_id,
                chunk_index=item.chunk_index,
                title=item.title,
                source_path=item.source_path,
                url=item.url,
                text=evidence_text,
                metadata=item.metadata,
            ),
        ))
    scored.sort(key=lambda pair: (pair[0], pair[1].score), reverse=True)
    return _dedupe_results([item for _score, item in scored], limit=limit)


def _supplement_local_modpack_manifest_results(config: AppConfig, question: str, limit: int) -> list[SearchResult]:
    if not _is_modpack_mod_list_question(question):
        return []
    root = config.paths.source_dir
    if not root.exists():
        return []
    question_terms = _focus_terms_for_question(question)
    candidates: list[tuple[int, Path]] = []
    parent_terms = _parent_topic_terms(question)
    for markdown_path in root.rglob("*.md"):
        lowered_path = markdown_path.as_posix().lower()
        if not any(marker in lowered_path for marker in ("modpack", "mcmod", "modrinth")):
            continue
        try:
            text = markdown_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names = _extract_included_mod_names(text)
        if not names:
            continue
        haystack = f"{markdown_path.name}\n{text[:1200]}".lower()
        score = len(names)
        for term in parent_terms:
            if term.lower() in haystack:
                score += 800
        for term in question_terms:
            term_lower = term.lower()
            if term_lower in {"模组", "mods", "mod"}:
                continue
            if term_lower in haystack:
                score += 500
        if "utopia" in haystack and any(term in question for term in ("乌托邦", "utopia", "Utopia")):
            score += 500
        candidates.append((score, markdown_path))
    candidates.sort(key=lambda item: item[0], reverse=True)
    results: list[SearchResult] = []
    for score, markdown_path in candidates[: max(limit, 4)]:
        result = _search_result_for_markdown_path(config, markdown_path, question, min(1.0, score / 600))
        if result is not None:
            results.append(result)
        if len(results) >= limit:
            break
    return _dedupe_results(results, limit=limit)


def _ensure_modpack_mod_list_context(
    config: AppConfig,
    question: str,
    selected: list[SearchResult],
    rough_results: list[SearchResult],
    limit: int,
) -> list[SearchResult]:
    if not _is_modpack_mod_list_question(question):
        return selected
    parsed = _modpack_manifest_results(question, [*selected, *rough_results], limit)
    if not parsed:
        parsed = _supplement_local_modpack_manifest_results(config, question, limit)
    if not parsed:
        return selected
    return _dedupe_results([*parsed, *selected], limit=limit)


def _supplement_project_keyword_results(config: AppConfig, question: str, results: list[SearchResult], limit: int) -> list[SearchResult]:
    intent = analyze_query(question, CONCEPTS)
    if intent.domain not in {"project", "known_mod"}:
        return results
    if _needs_general_grounded_answer(question):
        local_results = _supplement_local_guide_mechanics_results(config, question, results, limit)
        if len(local_results) >= min(3, limit):
            return local_results
    retriever = Retriever(config)
    existing_docs = {int(item.document_id) for item in results}
    additions: list[SearchResult] = []
    queries = [
        _same_theme_tutorial_query(question),
        *_guide_mechanics_supplement_queries(question, intent),
        intent.entity,
        *intent.keywords[1:8],
        *intent.search_queries[:4],
    ]
    queries = _dedupe_strings([str(query) for query in queries if query])
    for keyword in queries:
        try:
            candidates = retriever.search(str(keyword), top_k=24)
        except Exception:
            continue
        for item in candidates:
            path = item.source_path.lower().replace("\\", "/")
            if "crawler_exports/mediawiki/" in path or int(item.document_id) in existing_docs:
                continue
            if not _result_contains_project_entity(item, question, intent):
                continue
            if _needs_general_grounded_answer(question) and _guide_mechanics_evidence_score(item) <= 0:
                continue
            if len(additions) >= max(2, limit):
                continue
            existing_docs.add(int(item.document_id))
            additions.append(item)
            if _guide_mechanics_evidence_score(item) >= 8:
                break
            break
    merged = _rank_answer_evidence_for_focus(question, [*results, *additions])
    if _needs_general_grounded_answer(question):
        return _dedupe_results_by_chunk_quality(merged, limit=limit)
    return _dedupe_results(merged, limit=limit)


def _supplement_local_guide_mechanics_results(config: AppConfig, question: str, results: list[SearchResult], limit: int) -> list[SearchResult]:
    aliases = _guide_mechanics_entity_aliases(question)
    markers = _guide_mechanics_literal_markers(question)
    if not aliases or not markers or not config.paths.db_path.exists():
        return results
    existing_chunks = {int(item.chunk_id) for item in results}
    alias_clauses: list[str] = []
    marker_clauses: list[str] = []
    params: list[str] = []
    for alias in aliases[:8]:
        alias_like = f"%{alias}%"
        alias_clauses.append("(documents.title LIKE ? OR documents.source_path LIKE ? OR documents.url LIKE ?)")
        params.extend([alias_like, alias_like, alias_like])
    for marker in markers[:18]:
        marker_clauses.append("chunks.text LIKE ?")
        params.append(f"%{marker}%")
    rows: list[Any] = []
    conn = connect(config.paths.db_path)
    try:
        rows = conn.execute(
            f"""
            SELECT
                chunks.id AS chunk_id,
                chunks.document_id AS document_id,
                chunks.chunk_index AS chunk_index,
                chunks.text AS text,
                chunks.metadata_json AS chunk_metadata,
                documents.title AS title,
                documents.source_path AS source_path,
                documents.url AS url,
                documents.metadata_json AS document_metadata
            FROM chunks
            JOIN documents ON documents.id = chunks.document_id
            WHERE ({" OR ".join(alias_clauses)})
              AND ({" OR ".join(marker_clauses)})
            ORDER BY chunks.id DESC
            LIMIT 160
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    candidates: list[SearchResult] = []
    seen_chunks = set(existing_chunks)
    for row in rows:
        chunk_id = int(row["chunk_id"])
        if chunk_id in seen_chunks:
            continue
        metadata: dict[str, Any] = {}
        for raw in (row["document_metadata"], row["chunk_metadata"]):
            if raw:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = {}
                if isinstance(parsed, dict):
                    metadata.update(parsed)
        item = SearchResult(
            rank=0,
            score=1.0 + _guide_mechanics_literal_row_score(str(row["text"])),
            chunk_id=chunk_id,
            document_id=int(row["document_id"]),
            chunk_index=int(row["chunk_index"]),
            title=str(row["title"]),
            source_path=str(row["source_path"]),
            url=str(row["url"]) if row["url"] else None,
            text=str(row["text"]),
            metadata=metadata,
        )
        if not _result_contains_project_entity(item, question, analyze_query(question, CONCEPTS)):
            continue
        if _guide_mechanics_evidence_score(item) <= 0:
            continue
        seen_chunks.add(chunk_id)
        candidates.append(item)
    candidates.extend(_neighboring_guide_mechanics_chunks(config, candidates, seen_chunks, limit=limit))
    merged = _rank_answer_evidence_for_focus(question, [*results, *candidates])
    return _dedupe_results_by_chunk_quality(merged, limit=limit)


def _neighboring_guide_mechanics_chunks(
    config: AppConfig,
    seeds: list[SearchResult],
    seen_chunks: set[int],
    *,
    limit: int,
) -> list[SearchResult]:
    """Add nearby chunks from the same source page so tools expose coherent local evidence."""
    if not seeds or not config.paths.db_path.exists():
        return []
    doc_windows: dict[int, tuple[int, int]] = {}
    for item in sorted(seeds, key=lambda candidate: _guide_mechanics_evidence_score(candidate), reverse=True):
        doc_id = int(item.document_id)
        chunk_index = int(item.chunk_index)
        low, high = doc_windows.get(doc_id, (chunk_index, chunk_index))
        doc_windows[doc_id] = (min(low, chunk_index - 1), max(high, chunk_index + 2))
    rows: list[Any] = []
    conn = connect(config.paths.db_path)
    try:
        for doc_id, (low, high) in list(doc_windows.items())[:8]:
            rows.extend(
                conn.execute(
                    """
                    SELECT
                        chunks.id AS chunk_id,
                        chunks.document_id AS document_id,
                        chunks.chunk_index AS chunk_index,
                        chunks.text AS text,
                        chunks.metadata_json AS chunk_metadata,
                        documents.title AS title,
                        documents.source_path AS source_path,
                        documents.url AS url,
                        documents.metadata_json AS document_metadata
                    FROM chunks
                    JOIN documents ON documents.id = chunks.document_id
                    WHERE chunks.document_id = ?
                      AND chunks.chunk_index BETWEEN ? AND ?
                    ORDER BY chunks.chunk_index ASC
                    """,
                    (doc_id, low, high),
                ).fetchall()
            )
    finally:
        conn.close()
    additions: list[SearchResult] = []
    for row in rows:
        chunk_id = int(row["chunk_id"])
        if chunk_id in seen_chunks:
            continue
        text = str(row["text"])
        if _looks_like_site_tail_chunk(text):
            continue
        score = _guide_mechanics_literal_row_score(text)
        if score <= 0:
            continue
        metadata: dict[str, Any] = {}
        for raw in (row["document_metadata"], row["chunk_metadata"]):
            if raw:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = {}
                if isinstance(parsed, dict):
                    metadata.update(parsed)
        item = SearchResult(
            rank=0,
            score=0.95 + score,
            chunk_id=chunk_id,
            document_id=int(row["document_id"]),
            chunk_index=int(row["chunk_index"]),
            title=str(row["title"]),
            source_path=str(row["source_path"]),
            url=str(row["url"]) if row["url"] else None,
            text=text,
            metadata=metadata,
        )
        if _guide_mechanics_evidence_score(item) <= 0:
            continue
        additions.append(item)
        seen_chunks.add(chunk_id)
        if len(additions) >= max(2, limit):
            break
    return additions


def _looks_like_site_tail_chunk(text: str) -> bool:
    clean = str(text or "")
    if not clean:
        return True
    tail_markers = (
        "Copyright MC百科",
        "关注百科",
        "意见反馈",
        "鄂ICP备",
        "公网安备",
        "短评加载中",
        "浏览器: 计算中",
    )
    useful_markers = (
        "进度",
        "野生作物",
        "砧板",
        "刀",
        "燧石刀",
        "厨房套件",
        "厨锅",
        "煎锅",
        "热源",
        "配方",
        "合成",
        "玩法",
        "教程",
        "步骤",
        "获取",
    )
    tail_hits = sum(1 for marker in tail_markers if marker in clean)
    useful_hits = sum(1 for marker in useful_markers if marker in clean)
    return tail_hits >= 2 and useful_hits <= 2


def _guide_mechanics_entity_aliases(question: str) -> list[str]:
    intent = analyze_query(question, CONCEPTS)
    aliases = [*(_primary_fact_subject_terms(question) or []), str(intent.entity or "")]
    return [
        alias
        for alias in _dedupe_strings([str(item).strip() for item in aliases if str(item).strip()])
        if len(alias) >= 3 and alias.lower() not in {"minecraft", "modpack", "mods", "mod"}
    ]


def _guide_mechanics_literal_markers(question: str) -> list[str]:
    markers = [
        "进度", "进度界面", "新手", "入门", "开局", "前期", "玩法", "教程", "攻略", "流程", "路线",
        "机制", "配方", "合成", "制作", "获取", "获得", "材料",
        "beginner", "getting started", "guide", "tutorial", "progression", "mechanic", "recipe",
    ]
    if any(token in question for token in ("食物", "烹饪", "厨锅", "砧板", "刀", "农夫乐事")) or "delight" in question.lower():
        markers.extend(["烹饪", "食物", "食材", "厨锅", "煎锅", "砧板", "燧石刀", "刀", "野生作物", "作物", "种子", "农场", "热源"])
    return _dedupe_strings(markers)


def _guide_mechanics_literal_row_score(text: str) -> float:
    return min(_guide_mechanics_evidence_score(SearchResult(0, 0.0, 0, 0, 0, "", "", None, text)), 20.0) / 20.0


def _guide_mechanics_supplement_queries(question: str, intent: Any) -> list[str]:
    if not _needs_general_grounded_answer(question):
        return []
    entity = str(getattr(intent, "entity", "") or "").strip()
    seeds = [entity, *[str(item) for item in getattr(intent, "search_queries", [])[:3] if item]]
    if not entity:
        terms = _primary_fact_subject_terms(question) or _focus_terms_for_question(question)
        entity = terms[0] if terms else ""
    if not entity:
        return []
    dimensions = [
        "新手 入门 开局 进度 玩法",
        "攻略 教程 流程 路线 机制",
        "配方 合成 制作 获取",
        "beginner guide tutorial progression mechanics",
    ]
    if any(token in question for token in ("食物", "烹饪", "厨锅", "砧板", "刀")) or "delight" in question.lower():
        dimensions.extend(["烹饪 食材 厨锅 煎锅 砧板 刀 作物", "cooking recipe cutting board knife crop"])
    return _dedupe_strings([f"{seed} {dimension}".strip() for seed in [entity, *seeds[:1]] for dimension in dimensions])[:4]


def _result_contains_project_entity(item: SearchResult, question: str, intent: Any) -> bool:
    entity_terms = _primary_fact_subject_terms(question)
    entity = str(getattr(intent, "entity", "") or "").strip()
    if entity:
        entity_terms.append(entity)
    if not entity_terms:
        entity_terms = _focus_terms_for_question(question)[:4]
    haystack = f"{item.title}\n{item.source_path}\n{item.url or ''}\n{item.text[:1800]}".lower()
    useful_terms = [
        term.lower()
        for term in entity_terms
        if len(str(term).strip()) >= 2 and str(term).lower() not in {"minecraft", "mc", "mod", "mods", "modpack", "模组", "整合包"}
    ]
    if not useful_terms:
        return True
    return any(term in haystack for term in useful_terms)


def _prefer_parent_topic_results(question: str, selected: list[SearchResult], rough_results: list[SearchResult], limit: int) -> list[SearchResult]:
    parent_terms = _parent_topic_terms(question)
    if not parent_terms:
        return selected
    matched = [item for item in selected if _result_contains_any_term(item, parent_terms)]
    rough_matched = [item for item in rough_results if _result_contains_any_term(item, parent_terms)]
    if matched and (len(matched) >= min(2, len(selected)) or len(rough_matched) >= 2):
        return _dedupe_results([*matched, *rough_matched], limit=limit)
    if not rough_matched:
        return selected
    merged = _dedupe_results([*rough_matched, *selected], limit=limit)
    return merged


def _parent_topic_terms(question: str) -> list[str]:
    terms: list[str] = []
    known_aliases = {
        "落幕曲": ["落幕曲", "Closing Song"],
        "乌托邦": ["乌托邦", "Utopia", "乌托邦探险之旅", "Utopian Journey"],
    }
    for key, aliases in known_aliases.items():
        if key in question or any(alias.lower() in question.lower() for alias in aliases if re.search(r"[A-Za-z]", alias)):
            terms.extend(aliases)
    for match in re.finditer(r"([\u4e00-\u9fffA-Za-z0-9_+-]{2,40})(?:的|里|里的|里面的|中|中的).{0,16}(?:Boss|BOSS|boss|首领|头目|模组|玩法|攻略|配方|掉落)", question):
        term = match.group(1).strip()
        if term and term not in terms:
            terms.append(term)
    return _dedupe_strings(terms)[:6]


def _result_contains_any_term(item: SearchResult, terms: list[str]) -> bool:
    haystack = f"{item.title}\n{item.source_path}\n{item.text[:2400]}".lower()
    return any(term.lower() in haystack for term in terms if term)


def _insufficient_evidence_answer(question: str) -> str:
    return "本地检索命中了候选资料，但 MCagent 判断这些证据还不足以稳定回答本轮问题。本轮没有自动启动 Crawler；只有在工具选择或计划阶段明确选择委托时，才会通过 From-Content-To 消息交给 CrawlerAgent。"
    if any(token in question for token in ("掉落", "掉什么", "掉落物", "奖励")):
        return "本地资料里还没有足够稳定的掉落/奖励证据。MCagent 不会把少量碎片当成完整掉落表，所以已把这个缺口交给 Crawler 继续补齐。"
    if any(token in question for token in ("怎么打", "如何打", "打法", "哪里打", "在哪打", "位置", "地点")):
        return "本地资料里还没有足够稳定的打法或位置证据。MCagent 先不硬编攻略，已把需要补齐的部分交给 Crawler。"
    return "本地检索命中了资料，但证据筛选器判断这些资料还不够稳定，所以已触发多源补库。"


def _crawler_delegation_note(job: Job, question: str, created: bool, plan: dict[str, Any] | None = None) -> str:
    return _crawler_delegation_note_for(job, question, created, requested_by="mcagent", delivery_target="MCagent/RAG")


def _crawler_delegation_note_for(job: Job, question: str, created: bool, *, requested_by: str, delivery_target: str = "") -> str:
    target_text = delivery_target or "\u7531\u4efb\u52a1\u76ee\u6807\u5224\u65ad"
    if created:
        if requested_by == "user":
            return (
                "\n\n\u91c7\u96c6\u4efb\u52a1\uff1a\u4f60\u5df2\u76f4\u63a5\u59d4\u6258 CrawlerAgent \u91c7\u96c6\u8d44\u6599\u3002\n"
                f"- \u4efb\u52a1ID\uff1a{job.id}\n"
                f"- \u91c7\u96c6\u76ee\u6807\uff1a{question}\n"
                f"- \u4ea4\u4ed8\u5bf9\u8c61\uff1a{target_text}\n"
                "- Crawler \u4f1a\u81ea\u884c\u7406\u89e3\u76ee\u6807\u3001\u89c4\u5212\u641c\u7d22\u8bcd\u3001\u9009\u62e9\u6570\u636e\u6e90\u3001\u4fdd\u5b58 Markdown/manifest/raw HTML\uff0c\u5e76\u6309\u4ea4\u4ed8\u5bf9\u8c61\u51b3\u5b9a\u6e05\u6d17\u65b9\u5f0f\u3002\n"
                "- \u8fdb\u5ea6\u3001\u5f53\u524d\u52a8\u4f5c\u3001\u6210\u679c\u3001\u81ea\u5ba1\u548c\u53d7\u9650\u6765\u6e90\u4f1a\u76f4\u63a5\u663e\u793a\u5728\u8fd9\u6761\u6d88\u606f\u7684\u4efb\u52a1\u5361\u7247\u91cc\u3002"
            )
        if requested_by == "user_via_mcagent":
            return (
                "\n\n\u91c7\u96c6\u4efb\u52a1\uff1a\u6211\u5df2\u628a\u4f60\u7684\u8bf7\u6c42\u8f6c\u8fbe\u7ed9 CrawlerAgent\u3002\n"
                f"- \u4efb\u52a1ID\uff1a{job.id}\n"
                f"- \u8f6c\u8fbe\u76ee\u6807\uff1a{question}\n"
                f"- \u4ea4\u4ed8\u5bf9\u8c61\uff1a{target_text}\n"
                "- Crawler \u4f1a\u81ea\u5df1\u89c4\u5212\u5173\u952e\u8bcd\u548c\u6765\u6e90\uff0c\u91c7\u96c6\u540e\u6309 MCagent/RAG \u53ef\u8bfb\u683c\u5f0f\u6e05\u6d17\u5165\u5e93\u3002\n"
                "- \u8fdb\u5ea6\u3001\u5f53\u524d\u52a8\u4f5c\u3001\u6210\u679c\u3001\u81ea\u5ba1\u548c\u53d7\u9650\u6765\u6e90\u4f1a\u76f4\u63a5\u663e\u793a\u5728\u8fd9\u6761\u6d88\u606f\u7684\u4efb\u52a1\u5361\u7247\u91cc\u3002"
            )
        return (
            "\n\n\u8865\u5e93\u52a8\u4f5c\uff1aMCagent \u5224\u65ad\u5f53\u524d\u8d44\u6599\u4e0d\u8db3\uff0c\u5df2\u628a\u8d44\u6599\u7f3a\u53e3\u4ea4\u7ed9 CrawlerAgent\u3002\n"
            f"- \u4efb\u52a1ID\uff1a{job.id}\n"
            f"- \u7f3a\u53e3\u4e3b\u9898\uff1a{question}\n"
            "- Crawler \u4f1a\u81ea\u884c\u89c4\u5212\u641c\u7d22\u8bcd\u3001\u9009\u62e9\u6570\u636e\u6e90\u3001\u6293\u53d6 Markdown/manifest/raw HTML\uff0c\u5e76\u5728\u5b8c\u6210\u540e\u81ea\u52a8\u5165\u5e93\u3002\n"
            "- \u8fdb\u5ea6\u3001\u5f53\u524d\u52a8\u4f5c\u3001\u6210\u679c\u3001\u81ea\u5ba1\u548c\u53d7\u9650\u6765\u6e90\u4f1a\u76f4\u63a5\u663e\u793a\u5728\u8fd9\u6761\u6d88\u606f\u7684\u4efb\u52a1\u5361\u7247\u91cc\u3002"
        )
    prefix = "\u5df2\u6709 Crawler \u4efb\u52a1\u5728\u8fd0\u884c\uff0c\u672c\u6b21\u4e0d\u91cd\u590d\u521b\u5efa\u3002" if requested_by == "user" else "\u5df2\u6709 Crawler \u4efb\u52a1\u5728\u8fd0\u884c\uff0c\u672c\u6b21\u4e0d\u91cd\u590d\u6d3e\u5355\u3002"
    return f"\n\n\u91c7\u96c6\u4efb\u52a1\uff1a{prefix}\n- \u5f53\u524d\u4efb\u52a1ID\uff1a{job.id}\n- \u72b6\u6001\uff1a{job.status}"


def _crawler_agent_context_delegation_answer(local_summary: str, delegation_note: str) -> str:
    summary = str(local_summary or "").strip()
    summary = re.split(r"\n\s*---\s*\n\s*(?:根据你的指示|正在转达|采集任务)", summary, maxsplit=1)[0].strip()
    summary = re.split(r"(?:正在转达给\s*CrawlerAgent|我正将采集任务转交给\s*CrawlerAgent|请稍候。?补库完成后)", summary, maxsplit=1)[0].strip()
    summary = summary.replace("我正将采集任务转交给 **CrawlerAgent**", "我会继续执行采集")
    summary = summary.replace("我正将采集任务转交给 CrawlerAgent", "我会继续执行采集")
    summary = summary.replace("转交给 **CrawlerAgent**", "进入 Crawler 采集流程")
    summary = summary.replace("转交给 CrawlerAgent", "进入 Crawler 采集流程")
    lines = ["我是 CrawlerAgent。已按你的要求先读取 MCagent/RAG 本地上下文。"]
    if summary:
        lines.extend(["", "本地上下文初步结论：", summary])
    lines.extend(["", "接下来我会基于这些缺口继续执行网上采集，并把结果交付给 MCagent/RAG。"])
    if delegation_note.strip():
        lines.append(delegation_note.strip())
    return "\n".join(lines)


def _collaboration_dialog(question: str, job: Job, created: bool, plan: dict[str, Any] | None = None, reason: str = "") -> list[dict[str, Any]]:
    return _collaboration_dialog_for(question, job, created, requested_by="mcagent", delivery_target="MCagent/RAG", reason=reason)


def _collaboration_dialog_for(question: str, job: Job, created: bool, *, requested_by: str, delivery_target: str = "", reason: str = "") -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    crawler_plan: dict[str, Any] = {}
    if isinstance(job.result, dict):
        maybe_plan = job.result.get("plan")
        crawler_plan = maybe_plan if isinstance(maybe_plan, dict) else {}
        tasks = list(job.result.get("planned_tasks") or [])
    target_label = delivery_target or "\u7531\u4efb\u52a1\u76ee\u6807\u5224\u65ad"
    if requested_by == "user":
        dialog = [
            {"speaker": "\u7528\u6237", "state": "\u59d4\u6258", "text": f"\u76f4\u63a5\u8981\u6c42 Crawler \u91c7\u96c6\uff1a{question}"},
            {"speaker": "Crawler", "state": "\u7406\u89e3", "text": f"\u8c03\u7528\u8005\u662f\u7528\u6237\uff1b\u4ea4\u4ed8\u5bf9\u8c61\u662f {target_label}\u3002\u6211\u4f1a\u81ea\u884c\u89c4\u5212\u91c7\u96c6\u3001\u4fdd\u5b58\u548c\u6e05\u6d17\u65b9\u5f0f\u3002"},
        ]
    elif requested_by == "user_via_mcagent":
        dialog = [
            {"speaker": "\u7528\u6237", "state": "\u8bf7\u6c42\u8f6c\u8fbe", "text": f"\u8ba9 MCagent \u901a\u77e5 Crawler \u83b7\u53d6\u8d44\u6599\uff1a{question}"},
            {"speaker": "MCagent", "state": "\u8f6c\u8fbe", "text": f"\u7528\u6237\u8ba9\u6211\u8f6c\u8fbe\uff1a\u8bf7\u83b7\u53d6\u300c{question}\u300d\u3002\u8fd9\u6279\u6570\u636e\u4ea4\u4ed8\u7ed9 {delivery_target or 'MCagent/RAG'}\uff0c\u6e05\u6d17\u6210 MCagent \u80fd\u68c0\u7d22\u3001\u5f15\u7528\u7684\u683c\u5f0f\u3002"},
            {"speaker": "Crawler", "state": "\u63a5\u6536", "text": "\u6536\u5230\u3002\u6211\u4f1a\u628a\u8fd9\u5f53\u4f5c\u7528\u6237\u7ecf MCagent \u8f6c\u8fbe\u7684\u91c7\u96c6\u4efb\u52a1\uff0c\u81ea\u5df1\u5206\u6790\u76ee\u6807\u3001\u89c4\u5212\u5173\u952e\u8bcd\u548c\u6765\u6e90\uff0c\u5e76\u4fdd\u5b58 Markdown\u3001manifest \u4e0e raw HTML\u3002"},
        ]
    else:
        dialog = [
            {"speaker": "MCagent", "state": "\u5224\u65ad", "text": reason or "\u672c\u5730\u8d44\u6599\u4e0d\u8db3\uff0c\u9700\u8981 CrawlerAgent \u8865\u9f50\u8bc1\u636e\u3002"},
            {"speaker": "MCagent", "state": "\u6d3e\u5355", "text": f"\u53d1\u9001\u7ed9 Crawler \u7684\u8d44\u6599\u7f3a\u53e3\uff1a{question}"},
        ]
    if tasks:
        topic = crawler_plan.get("topic") or question
        goals = crawler_plan.get("coverage_goals") or []
        goal_text = "\uff1b\u8986\u76d6\u76ee\u6807\uff1a" + "\u3001".join(str(item) for item in goals[:6]) if goals else ""
        task_text = "\uff1b".join(f"{_source_label(str(item.get('source')))}={item.get('query')}" for item in tasks[:10])
        dialog.append({"speaker": "Crawler", "state": "\u89c4\u5212", "text": f"Crawler LLM \u5df2\u89c4\u5212\u4e3b\u9898\uff1a{topic}{goal_text}\u3002\u4efb\u52a1\uff1a{task_text}"})
    else:
        dialog.append({"speaker": "Crawler", "state": "\u63a5\u6536" if created else "\u6392\u961f\u4e2d", "text": f"\u4efb\u52a1 {job.id}\uff0c\u72b6\u6001 {job.status}\u3002Crawler \u5c06\u81ea\u884c\u89c4\u5212\u641c\u7d22\u8bcd\u548c\u6570\u636e\u6e90\u3002"})
    return dialog


def _start_crawler_job_from_crawler_tool(config: AppConfig, payload: dict[str, Any], question: str, plan: dict[str, Any] | None = None) -> tuple[Job, bool]:
    if str(payload.get("agent") or "") != "crawler_agent":
        raise RuntimeError("Crawler jobs can only be started by CrawlerAgent after it receives an AgentMessage and selects its own collection tool.")
    received_message = _received_agent_message_for_tool(payload, expected_agent="crawler_agent", expected_tool="delegate_crawler")
    crawler_payload = dict(payload)
    collection_question = str(question).strip() if payload.get("preserve_crawler_request") else _clean_crawler_task_question(question)
    session_summary = _session_summary(payload)
    requested_source = _source_alias(str(payload.get("source") or "planner"))
    handoff = _delegation_handoff(payload, question, collection_question)
    if requested_source in {"planner", "auto", "smart", "orchestrator"}:
        crawler_payload.update({"source": "planner", "question": collection_question, "source_question": collection_question, "query": collection_question})
    else:
        crawler_payload.update(
            {
                "source": requested_source,
                "question": collection_question,
                "source_question": collection_question,
                "query": str(payload.get("query") or collection_question),
            }
        )
    crawler_payload["requested_by"] = handoff["requested_by"]
    crawler_payload["handoff_from"] = handoff["handoff_from"]
    crawler_payload["original_user_request"] = handoff["original_user_request"]
    crawler_payload["delivery_target"] = str(payload.get("delivery_target") or _infer_delivery_target(collection_question, session_summary))
    crawler_payload["max_tasks"] = int(payload.get("max_tasks") or crawler_payload.get("max_tasks") or 6)
    requested_output_dir = CrawlerTaskPreparationService._extract_windows_path(
        "\n".join(
            str(item or "")
            for item in (
                collection_question,
                question,
                payload.get("original_user_request"),
                handoff["original_user_request"],
                session_summary.get("collection_target") if isinstance(session_summary, dict) else "",
                session_summary.get("task_goal") if isinstance(session_summary, dict) else "",
            )
        )
    )
    if requested_output_dir:
        crawler_payload["output_dir"] = requested_output_dir
    agent_message = received_message
    crawler_payload["agent_message"] = agent_message.to_dict()
    explicit_collection_target = str((session_summary or {}).get("collection_target") or "").strip()
    planner_collection_target = explicit_collection_target or collection_question
    planner_summary = dict(session_summary or {})
    planner_summary.update(
        {
            "agent_message": agent_message.to_dict(),
            "message_transport": "From-Content-To",
            "requested_by": crawler_payload["requested_by"],
            "handoff_from": crawler_payload["handoff_from"],
            "original_user_request": crawler_payload["original_user_request"],
            "delivery_target": crawler_payload["delivery_target"],
            "collection_target": planner_collection_target,
            "task_goal": planner_collection_target,
            "authoritative_task_goal": planner_collection_target,
            "current_topic": planner_collection_target,
        }
    )
    crawler_payload["session_summary"] = planner_summary
    reuse_signature = _crawler_job_reuse_signature(
        question=planner_collection_target,
        delivery_target=str(crawler_payload["delivery_target"] or ""),
        requested_by=str(crawler_payload["requested_by"] or ""),
        source=str(crawler_payload.get("source") or "planner"),
        session_summary=planner_summary,
    )
    initial_result = {
        "source": str(crawler_payload.get("source") or "planner"),
        "requested_by": crawler_payload["requested_by"],
        "delivery_target": crawler_payload["delivery_target"],
        "reuse_signature": reuse_signature,
        "agent_message": agent_message.to_dict(),
        "plan": {
            "question": planner_collection_target,
            "topic": planner_collection_target,
            "delivery_target": crawler_payload["delivery_target"],
        },
        "loop": [
            {"phase": "understand", "status": "queued"},
            {"phase": "plan", "status": "pending"},
            {"phase": "act", "status": "pending"},
            {"phase": "verify", "status": "pending"},
        ],
    }
    job, created = _start_job(
        "crawler",
        "Crawler 采集任务" if crawler_payload["delivery_target"].lower() == "human" else "Crawler 多源补库 -> RAG",
        lambda item: _run_crawler_job(item, crawler_payload, config),
        reuse_predicate=lambda item: _crawler_job_reuse_candidate(item, reuse_signature),
        initial_result=initial_result,
    )
    if created:
        append_memory_event(
            "crawler_gap_delegated",
            {
                "job_id": job.id,
                "question": collection_question,
                "missing_evidence": str(payload.get("missing_evidence") or payload.get("reason") or ""),
                "session_summary": session_summary,
                "requested_by": crawler_payload["requested_by"],
                "handoff_from": crawler_payload["handoff_from"],
                "original_user_request": crawler_payload["original_user_request"],
                "delivery_target": crawler_payload["delivery_target"],
                "agent_message": agent_message.to_dict(),
            },
        )
    return job, created


def _fallback_delegate_handoff_brief(
    *,
    original_question: str,
    collection_target: str,
    session_summary: dict[str, Any],
    requested_by: str,
    delivery_target: str,
) -> str:
    topics = [str(item) for item in (session_summary.get("topics") or [])[:6] if str(item).strip()]
    names = [str(item) for item in (session_summary.get("names") or [])[:8] if str(item).strip()]
    gaps = [str(item) for item in (session_summary.get("gaps") or [])[:8] if str(item).strip()]
    acceptance = [
        "CrawlerAgent 自行理解目标、规划采集来源和搜索策略。",
        "如果交付给 MCagent/RAG，资料应清洗成可检索、可引用、带来源的 Markdown/manifest/raw HTML 或 raw text。",
        "如果遇到登录、验证码、配额、空结果、跑偏或重复，应说明客观失败原因和下一条可行路径。",
    ]
    if gaps:
        acceptance.append(f"优先补齐 MCagent 近期暴露的资料缺口：{'；'.join(gaps[:8])}。")
    known_context = []
    if topics:
        known_context.append(f"当前会话主题：{'、'.join(topics[:6])}")
    if names:
        known_context.append(f"近期出现的实体/名称：{'、'.join(names[:8])}")
    contract = build_handoff_contract(
        requested_by=requested_by or "unknown",
        from_agent="MCagent" if requested_by in {"mcagent", "user_via_mcagent"} else "user",
        to_agent="CrawlerAgent",
        user_request=original_question,
        task_goal=collection_target or original_question,
        delivery_target=delivery_target or "由 CrawlerAgent 根据任务目标判断",
        known_context="；".join(known_context),
        acceptance_criteria=acceptance,
    )
    return contract.to_prompt_text()


def _build_delegate_handoff_brief(
    config: AppConfig,
    *,
    model: str,
    original_question: str,
    collection_target: str,
    session_summary: dict[str, Any],
    requested_by: str,
    delivery_target: str,
    mcagent_gap_summary: str = "",
) -> tuple[str, str]:
    fallback = _fallback_delegate_handoff_brief(
        original_question=original_question,
        collection_target=collection_target,
        session_summary=session_summary,
        requested_by=requested_by,
        delivery_target=delivery_target,
    )
    try:
        client, label = _selected_llm_client(config, model, 0.0, timeout_seconds=HANDOFF_BRIEF_LLM_TIMEOUT_SECONDS)
        caller_text = "用户直接委托 CrawlerAgent" if requested_by == "user" else "用户经 MCagent 转达给 CrawlerAgent" if requested_by == "user_via_mcagent" else "MCagent 委托 CrawlerAgent"
        prompt = (
            "你是 Agent Runtime 的 CrawlerAgent 任务说明整理器。\n"
            "你的任务不是搜索、不是回答用户、不是拆关键词，而是把这次委托完整说明给 CrawlerAgent。\n"
            "交接摘要必须包含：调用关系、用户原话、任务目标、相关会话背景、已知资料缺口、交付对象、交付要求。\n"
            "调用关系必须严格依据 requested_by；requested_by=user 时，不要写成来自 MCagent 或由 MCagent 转发。\n"
            "如果用户原话依赖上下文，就用 session_summary 和 mcagent_gap_summary 补充背景；如果不依赖上下文，也要保留原始目标。\n"
            "输出 JSON：{\"handoff_brief\":\"给 CrawlerAgent 的完整交接摘要\", \"reason\":\"一句简短理由\"}\n"
            f"caller_relationship: {caller_text}\n"
            f"original_user_message: {original_question}\n"
            f"router_collection_target: {collection_target}\n"
            f"requested_by: {requested_by}\n"
            f"delivery_target: {delivery_target}\n"
            f"session_summary: {json.dumps(session_summary or {}, ensure_ascii=False)}\n"
            f"mcagent_gap_summary: {mcagent_gap_summary[:4000]}"
        )
        raw_text = client.chat(
            [
                {"role": "system", "content": "只输出合法 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=700,
        )
        value = json_object_from_llm_text(raw_text)
        resolved = str(value.get("handoff_brief") or "").strip()
        reason = str(value.get("reason") or label).strip()
        if resolved:
            if requested_by == "user" and re.search(r"MCagent|MCAgent|MC Agent|转交|转达|经 MCagent|来自 MCagent", resolved, flags=re.I):
                return fallback[:900], "LLM handoff brief conflicted with requested_by=user; used identity-safe fallback."
            return resolved[:900], reason[:300]
    except Exception:
        pass
    return fallback[:900], "使用会话摘要生成通用委托交接说明。"


def _prepare_and_start_crawler_delegation(
    config: AppConfig,
    payload: dict[str, Any],
    *,
    active_agent: str,
    model: str,
    original_question: str,
    current_question: str,
    collection_question: str,
    session_summary: dict[str, Any],
    add_trace: Any,
    gap_summary: str = "",
    planning_instruction: str = "",
    delivery_target: str = "",
    action_plan: list[dict[str, Any]] | None = None,
) -> CrawlerDelegationRun:
    augmented_summary = dict(session_summary or {})
    augmented_summary.setdefault("original_user_message", original_question)
    augmented_summary.setdefault("original_question", original_question)
    augmented_summary.setdefault("source_question", current_question or original_question)
    if action_plan:
        augmented_summary["selected_action_plan"] = [dict(step) for step in action_plan if isinstance(step, dict)]
    prepare_started = time.time()
    delegation_plan = CrawlerDelegationService(
        delegation_handoff=_delegation_handoff,
        infer_delivery_target=_infer_delivery_target,
        build_handoff_brief=_build_delegate_handoff_brief,
    ).prepare(
        config,
        payload,
        model=model,
        original_question=original_question,
        current_question=current_question,
        collection_target=collection_question,
        session_summary=augmented_summary,
        gap_summary=gap_summary,
        planning_instruction=planning_instruction,
        delivery_target=delivery_target,
    )
    add_trace(
        "delegate",
        "handoff_brief",
        {
            "brief": delegation_plan.handoff_brief,
            "reason": delegation_plan.brief_reason,
            "elapsed_ms": round((time.time() - prepare_started) * 1000),
        },
    )
    if active_agent == "crawler_agent":
        start_started = time.time()
        delegate_payload = _payload_with_agent_message_tool(delegation_plan.delegate_payload, tool="delegate_crawler", intent="collection_request")
        job, created = _start_crawler_job_from_crawler_tool(config, delegate_payload, delegation_plan.collection_question)
        add_trace(
            "delegate",
            "crawler_job_start_returned",
            {"job_id": job.id, "status": job.status, "created": created, "elapsed_ms": round((time.time() - start_started) * 1000)},
        )
        response = None
    else:
        from_agent = "MCagent" if delegation_plan.requested_by in {"mcagent", "user_via_mcagent"} else "User"
        message = make_agent_message(
            from_agent,
            delegation_plan.collection_question,
            "CrawlerAgent",
            intent="collection_request",
            conversation_id=str(payload.get("session_id") or ""),
            metadata={
                "tool": "collection_request",
                "requested_by": delegation_plan.requested_by,
                "delivery_target": delegation_plan.delivery_target,
                "handoff_brief": delegation_plan.handoff_brief,
                "selected_action_plan": delegation_plan.planner_summary.get("selected_action_plan") or [],
            },
        )
        add_trace("message", "sending", message.to_dict())
        message_started = time.time()
        response = _send_agent_message(
            config,
            delegation_plan.delegate_payload,
            from_agent=message.from_agent,
            content=message.content,
            to_agent=message.to_agent,
            intent=message.intent,
            conversation_id=message.conversation_id,
            metadata=message.metadata,
        )
        job = _job_from_agent_response(response)
        if job is None:
            add_trace(
                "message",
                "reply_without_crawler_job",
                {
                    "request": message.to_dict(),
                    "reply": response.get("agent_message"),
                    "selected_tool": _selected_tool_from_response(response),
                    "reason": "CrawlerAgent received the From-Content-To collection request but did not return a crawler job. Runtime reports this objective result instead of fabricating a job or crashing the stream.",
                    "elapsed_ms": round((time.time() - message_started) * 1000),
                },
            )
            note = _crawler_delegation_no_job_note(delegation_plan.collection_question, response)
            return CrawlerDelegationRun(plan=delegation_plan, job=None, created=False, note=note, response=response)
        created = "已有" not in str(response.get("answer") or "") and str(job.status or "") in {"queued", "running", "succeeded"}
        add_trace(
            "message",
            "reply_received",
            {
                "request": message.to_dict(),
                "reply": response.get("agent_message"),
                "job_id": job.id,
                "job_status": job.status,
                "elapsed_ms": round((time.time() - message_started) * 1000),
            },
        )
    note = _crawler_delegation_note_for(
        job,
        delegation_plan.collection_question,
        created,
        requested_by=delegation_plan.requested_by,
        delivery_target=delegation_plan.delivery_target,
    )
    add_trace("delegate", "planned_workflow", {"job_id": job.id, "status": job.status, "task": delegation_plan.collection_question})
    return CrawlerDelegationRun(plan=delegation_plan, job=job, created=created, note=note, response=response)


def _selected_tool_from_response(response: dict[str, Any] | None) -> str:
    if not isinstance(response, dict):
        return ""
    for step in response.get("trace") or []:
        if not isinstance(step, dict):
            continue
        if step.get("stage") != "decide" or step.get("status") != "tool_selected":
            continue
        detail = step.get("detail") if isinstance(step.get("detail"), dict) else {}
        if detail.get("tool"):
            return str(detail.get("tool") or "")
        decision = detail.get("decision") if isinstance(detail.get("decision"), dict) else {}
        if decision.get("tool"):
            return str(decision.get("tool") or "")
    return ""


def _crawler_delegation_no_job_note(question: str, response: dict[str, Any] | None) -> str:
    selected_tool = _selected_tool_from_response(response)
    answer = str((response or {}).get("answer") or "").strip()
    parts = [
        "\n\n采集任务：MCagent 已通过 From-Content-To 消息把请求交给 CrawlerAgent，但 CrawlerAgent 本轮没有返回后台采集任务。",
        f"- 转达目标：{question}",
    ]
    if selected_tool:
        parts.append(f"- CrawlerAgent 本轮选择的工具：{selected_tool}")
    if answer:
        parts.append("- CrawlerAgent 回复：" + answer[:700])
    parts.append("- 运行时没有伪造任务；请查看本轮 Agent trace 判断为什么未进入 delegate_crawler。")
    return "\n".join(parts)


def _optional_delegation_payload(delegation: CrawlerDelegationRun, *, task: str = "", selected_action_plan: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "delegation": {
            "requested_by": delegation.plan.requested_by,
            "delivery_target": delegation.plan.delivery_target,
            "task": task or delegation.plan.collection_question,
            "handoff_brief": delegation.plan.handoff_brief,
        },
    }
    if selected_action_plan is not None:
        payload["delegation"]["selected_action_plan"] = selected_action_plan
    if delegation.job is not None:
        payload["job"] = _job_to_dict(delegation.job)
        payload["collaboration"] = _collaboration_dialog_for(
            task or delegation.plan.collection_question,
            delegation.job,
            delegation.created,
            requested_by=delegation.plan.requested_by,
            delivery_target=delegation.plan.delivery_target,
        )
    return payload


def _delegation_handoff(payload: dict[str, Any], original_question: str, cleaned_question: str) -> dict[str, str]:
    agent = str(payload.get("agent") or "mcagent_rag")
    explicit = str(payload.get("requested_by") or "").strip()
    if explicit:
        requested_by = explicit
    elif agent == "crawler_agent":
        requested_by = "user"
    elif _user_requested_mcagent_crawler_handoff(original_question):
        requested_by = "user_via_mcagent"
    else:
        requested_by = "mcagent"
    handoff_from = "MCagent" if requested_by in {"mcagent", "user_via_mcagent"} else "user"
    return {
        "requested_by": requested_by,
        "handoff_from": handoff_from,
        "original_user_request": str(payload.get("original_user_request") or original_question or cleaned_question),
    }


def _user_explicitly_asked_mcagent_to_tell_crawler(question: str) -> bool:
    if not _user_requested_mcagent_crawler_handoff(question):
        return False
    text = str(question or "")
    if re.search(r"(刚才|之前|上次|最近|历史|进度|状态|结果|为什么|是否已经|有没有)(?:.*)(?:Crawler|爬虫|采集|来源|入库|任务)", text, flags=re.I):
        return False
    if re.search(r"(?:自审|审计|接受|拒绝)(?:.*)(?:结果|报告|记录|详情|状态|历史)", text, flags=re.I):
        return False
    if re.search(r"(先|首先|先让|先请|先帮我)?\s*(?:盘点|检查|检索|查询|看看|列出|介绍).{0,24}(?:本地|库存|资料库|知识库|已有资料|有哪些资料)", text):
        return False
    if re.search(r"(?:本地还缺|还缺哪些资料|缺哪些资料|缺口|列出来|列出).{0,40}(?:Crawler|爬虫).{0,20}(?:补充|补齐|采集|获取)", text, flags=re.I):
        return False
    return True


def _user_requested_mcagent_crawler_handoff(question: str) -> bool:
    text = str(question or "")
    if not re.search(r"Crawler|爬虫", text, flags=re.I):
        return False
    relay_verbs = ("告诉", "叫", "让", "派", "通知", "转达", "交给", "请")
    collect_verbs = ("收集", "采集", "获取", "抓取", "爬取", "补充", "补库", "更新资料")
    has_relay = any(item in text for item in relay_verbs)
    has_collect = any(item in text for item in collect_verbs)
    # This is a general handoff detector: the user is asking MCagent to relay a
    # collection job to Crawler. The actual task target is still cleaned and
    # planned by Crawler, not decided here.
    return has_relay or has_collect


def _infer_delivery_target(question: str, session_summary: dict[str, Any] | None = None) -> str:
    text = f"{question}\n{json.dumps(session_summary or {}, ensure_ascii=False)}"
    lowered = text.lower()
    if "mcagent" in lowered or "rag" in lowered or "入库" in text or "知识库" in text or "资料库" in text:
        return "MCagent/RAG"
    return "human"


def _request_forbids_persistence(text: str) -> bool:
    value = str(text or "").lower()
    patterns = (
        r"不(?:用|要|必|需)?保存",
        r"别保存",
        r"无需保存",
        r"不要入库",
        r"不用入库",
        r"不入库",
        r"只在聊天",
        r"直接(?:返回|回答|给我)",
        r"do not save",
        r"don't save",
        r"without saving",
        r"no save",
        r"no local",
        r"no persistence",
    )
    return any(re.search(pattern, value, flags=re.I) for pattern in patterns)


def _request_wants_persistence(text: str) -> bool:
    value = str(text or "")
    lowered = value.lower()
    if re.search(r"[A-Za-z]:\\|\\\\[^\\/\s]+\\|/[^ \t\r\n]+/[^ \t\r\n]+", value):
        return True
    if re.search(r"\b(?:xlsx|xls|csv|json|jsonl|md|markdown|html|txt)\b.{0,80}\b(?:to|at|under|in)\s+[A-Za-z]:\\", value, flags=re.I):
        return True
    if re.search(r"\b(?:save|write|export|output|generate|create)\b.{0,80}\b(?:xlsx|xls|csv|json|jsonl|md|markdown|html|txt)\b", lowered, flags=re.I):
        return True
    if re.search(r"\b(?:xlsx|xls|csv|json|jsonl|md|markdown|html|txt)\b.{0,80}\b(?:save|write|export|output|generate|create|outputs?)\b", lowered, flags=re.I):
        return True
    patterns = (
        r"保存(?:到|为|成|在)?",
        r"写(?:入|到)",
        r"导出",
        r"下载",
        r"入库",
        r"补库",
        r"知识库",
        r"资料库",
        r"给\s*MCagent\s*用",
        r"给\s*RAG\s*用",
        r"交给\s*MCagent",
        r"交付(?:给|到)\s*MCagent",
        r"save",
        r"write",
        r"export",
        r"download",
        r"persist",
        r"ingest",
        r"rag",
    )
    return any(re.search(pattern, lowered, flags=re.I) for pattern in patterns)


def _should_use_temporary_extract_without_persistence(agent: str, original_question: str, collection_target: str, delivery_target: str) -> bool:
    if agent != "crawler_agent":
        return False
    combined = f"{original_question}\n{collection_target}\n{delivery_target}"
    lowered = combined.lower()
    if "mcagent/rag" in lowered:
        return False
    if _request_wants_persistence(combined) and not _request_forbids_persistence(combined):
        return False
    return _request_forbids_persistence(combined) or bool(CrawlerTemporaryExtractService().extract_url(combined))


def _mentions_mcagent_context(text: str) -> bool:
    value = str(text or "")
    lowered = value.lower()
    patterns = (
        r"mcagent",
        r"mc agent",
        r"rag",
        r"知识库",
        r"资料库",
        r"本地库",
        r"本地资料",
    )
    return any(re.search(pattern, lowered, flags=re.I) for pattern in patterns)


def _asks_for_context_or_gaps(text: str) -> bool:
    value = str(text or "")
    lowered = value.lower()
    patterns = (
        r"缺",
        r"缺口",
        r"缺失",
        r"还差",
        r"已有",
        r"有什么",
        r"有哪些",
        r"问下",
        r"查询",
        r"检查",
        r"inspect",
        r"context",
        r"gap",
        r"missing",
        r"what.*know",
    )
    return any(re.search(pattern, lowered, flags=re.I) for pattern in patterns)


def _asks_for_collection_or_handoff(text: str) -> bool:
    value = str(text or "")
    lowered = value.lower()
    patterns = (
        r"补",
        r"补全",
        r"补充",
        r"补给",
        r"补库",
        r"找",
        r"网上",
        r"联网",
        r"采集",
        r"爬取",
        r"抓取",
        r"获取",
        r"保存",
        r"入库",
        r"给他",
        r"给\s*MCagent",
        r"collect",
        r"crawl",
        r"fetch",
        r"find",
        r"fill",
        r"ingest",
    )
    return any(re.search(pattern, lowered, flags=re.I) for pattern in patterns)


def _mentions_crawler_agent(text: str) -> bool:
    value = str(text or "")
    lowered = value.lower()
    return bool(re.search(r"crawler|crawleragent|爬虫|采集器", lowered, flags=re.I))


def _forbids_crawler_handoff(text: str) -> bool:
    value = str(text or "").lower()
    value = re.sub(r"(不要|不用|别|不必|无需)强制.{0,12}(包体|下载|\.mrpack|\.zip|整合包)", " ", value, flags=re.I)
    value = re.sub(r"不强制.{0,12}(包体|下载|\.mrpack|\.zip|整合包)", " ", value, flags=re.I)
    return bool(re.search(r"(不要|不用|别|禁止|不要自动|不用自动).{0,16}(crawler|crawleragent|爬虫|采集|委托)", value, flags=re.I))


def _needs_general_grounded_answer(question: str) -> bool:
    text = str(question or "")
    lowered = text.lower()
    return any(
        token in text
        for token in (
            "怎么玩",
            "怎么开始",
            "怎样开始",
            "如何开始",
            "应该怎样开始",
            "应该怎么开始",
            "起步",
            "开始玩",
            "玩法",
            "路线",
            "流程",
            "入门",
            "新手",
            "前期",
            "中期",
            "后期",
            "机制",
            "系统",
            "教程",
            "攻略",
            "够不够回答",
        )
    ) or any(
        token in lowered
        for token in ("how to play", "guide", "route", "progression", "beginner", "mid game", "mechanic", "tutorial")
    )


def _mcagent_context_focus(question: str, collection_target: str = "") -> str:
    raw = " ".join(part for part in (str(collection_target or "").strip(), str(question or "").strip()) if part)
    value = str(collection_target or question or "").strip()
    value = re.sub(r"用户原始目标\s*[:：]", " ", value, flags=re.I)
    value = re.sub(r"(?:先|先去|先帮我|先请)?\s*(?:问下|问问|询问|咨询|问)\s*(?:MC\s*Agent|MCagent|MCAgent|RAG)?\s*(?:本地|本地关于|本地已有|本地资料|本地上下文|知识库|资料库)?", " ", value, flags=re.I)
    value = re.sub(r"(?:然后|再|之后)\s*(?:你)?\s*(?:去)?\s*(?:网上|联网|互联网上)?\s*(?:找|搜索|补充|补齐|采集|爬取|抓取|获取).*$", " ", value, flags=re.I)
    value = re.sub(r"(?:根据|基于)\s*(?:MC\s*Agent|MCagent|MCAgent|RAG)\s*(?:指出|返回|提供|发现|报告|回答)?(?:的)?", " ", value, flags=re.I)
    value = re.sub(r"(?i)MC\s*Agent|MCagent|\bRAG\b", " ", value)
    value = re.sub(r"(还缺哪些东西|还缺什么|缺哪些东西|缺什么|有哪些缺口|缺口有哪些|缺失哪些内容|缺失什么)", " ", value)
    value = re.sub(r"(本地关于|本地已有|本地上下文|问下|询问|问问|查询|检查|本地资料库|本地资料|知识库|资料库|你去|网上|联网|找|补给他|补给|补库|补充|采集|爬取|抓取|获取)", " ", value)
    value = re.sub(r"(?:交付|提供|入库|保存|转达|转交|给)\s*(?:MC\s*Agent|MCagent|MCAgent|RAG|他|它)?", " ", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" ，。；;:：")
    value = _compact_mcagent_context_focus(value, raw)
    value = _expand_mcagent_context_aliases(value)
    return value or str(question or "").strip()


def _compact_mcagent_context_focus(value: str, raw_context: str = "") -> str:
    raw = f"{raw_context} {value}".strip()
    cleaned = _strip_mcagent_inventory_focus_noise(value)
    entity = _mcagent_context_entity_from_text(cleaned) or _mcagent_context_entity_from_text(raw)
    if not entity:
        entity = cleaned
    entity = _strip_mcagent_inventory_focus_noise(entity)
    entity = re.sub(r"\s+", " ", entity).strip(" ，。；;:：")
    dimensions = _mcagent_context_dimension_terms(raw)
    parts = [entity, *[term for term in dimensions if term not in entity]]
    compact = " ".join(part for part in parts if part).strip()
    if len(compact) > 220:
        compact = compact[:220].rsplit(" ", 1)[0].strip() or compact[:220].strip()
    return compact


def _strip_mcagent_inventory_focus_noise(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    text = re.sub(r"(?i)\b(?:mcagent|mca?gent|rag|crawleragent|crawler)\b", " ", text)
    text = re.sub(r"(?:本地|库存|资料库|知识库|已入库|已有|现有|盘点|检查|审计|报告|回复|回答|发现|缺失列表|缺口列表|缺失项|缺口项|缺失|缺少|还缺|不足|待补|需要补)(?:的)?", " ", text)
    text = re.sub(r"(?:整合包|模组|资料|数据|内容|来源|文档|chunk|chunks?)\s*\d+\s*(?:篇|条|个|份|项)?", " ", text, flags=re.I)
    text = re.sub(r"\d+\s*(?:篇|条|个|份|项)\s*(?:整合包|模组|资料|数据|内容|来源|文档)?", " ", text)
    text = re.sub(r"(?:请|后|根据|基于|简单|介绍|一下|哪些|什么|可以回答|能回答|覆盖|包括|相关|Minecraft资料)", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" ，。；;:：")
    return _strip_context_focus_leftover_prefix(text)


def _mcagent_context_entity_from_text(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    if "乌托邦" in text or re.search(r"utopian\s+journey|utopia-journey", text, flags=re.I):
        return "乌托邦探险之旅 Utopian Journey MC 1.20.1 Fabric 整合包"
    if "香草纪元" in text or re.search(r"vanilla\s*era|vanillaera|fares\s*chron", text, flags=re.I):
        return "香草纪元 VanillaEra 食旅纪行 整合包"
    if "农夫乐事" in text or re.search(r"farmer'?s\s+delight|farmers[- ]delight", text, flags=re.I):
        return "农夫乐事 Farmer's Delight"
    patterns = [
        r"([A-Za-z][A-Za-z0-9_ .+'’:-]{2,70})\s*(?:Minecraft\s+)?(?:modpack|mod)\b",
        r"([\u4e00-\u9fffA-Za-z0-9_ （）()+.·' -]{2,70}?)(?:整合包|modpack)",
        r"([A-Za-z][A-Za-z0-9_ .+'’:-]{2,70}|[\u4e00-\u9fff]{2,30})\s*(?:模组|mod)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        entity = _strip_mcagent_inventory_focus_noise(match.group(1))
        if entity and not re.fullmatch(r"(?:资料|数据|内容|信息|公开|完整|详细|本地|网络|网上|相关|对应)", entity, flags=re.I):
            suffix = "整合包" if re.search(r"整合包|modpack", match.group(0), flags=re.I) and "整合包" not in entity and "modpack" not in entity.lower() else ""
            return f"{entity}{suffix}".strip()
    tokens = [
        item
        for item in re.findall(r"[A-Za-z][A-Za-z0-9_+'’:-]{2,}|[\u4e00-\u9fff]{2,}", text)
        if item not in {"资料", "数据", "内容", "信息", "公开", "完整", "详细", "本地", "网络", "网上", "相关", "对应", "玩法", "路线", "教程", "攻略", "列表", "清单"}
        and item.lower() not in {"minecraft", "modpack", "mod", "guide", "wiki", "docs", "documentation"}
    ]
    return " ".join(tokens[:4]).strip()


def _mcagent_context_dimension_terms(value: str) -> list[str]:
    text = str(value or "")
    lowered = text.lower()
    dimensions: list[str] = []
    checks = [
        ("模组列表", ("模组列表", "mod list", "mods list", "modlist")),
        ("任务线", ("任务线", "任务系统", "ftb任务", "ftb quests", "questline", "quests")),
        ("Boss", ("Boss", "boss", "首领")),
        ("玩法路线", ("玩法路线", "游玩路线", "进度指南", "进度路线", "毕业路线", "毕业攻略", "progression", "walkthrough", "route")),
        ("新手入门", ("新手", "入门", "开局", "beginner", "getting started")),
        ("玩法指南", ("玩法指南", "攻略", "教程", "guide", "tutorial", "how to play")),
        ("版本与安装", ("版本", "安装", "兼容", "version", "install", "loader")),
        ("下载/包体", ("下载", "包体", ".mrpack", ".zip", "archive", "download")),
        ("配置文件", ("配置文件", "manifest", "overrides", "config")),
        ("更新日志", ("更新日志", "changelog", "release")),
    ]
    for label, needles in checks:
        if any((needle.lower() in lowered) if re.search(r"[A-Za-z]", needle) else (needle in text) for needle in needles):
            dimensions.append(label)
    if re.search(r"缺口|缺失|缺少|还缺|不足|待补|需要补", text):
        dimensions.insert(0, "资料缺口")
    return _dedupe_strings(dimensions)[:8]


def _filter_mcagent_context_evidence(
    focus: str,
    selected: list[SearchResult],
    evidence_report: dict[str, Any],
) -> list[SearchResult]:
    """Keep only evidence that is safe to describe as local context for Crawler."""
    if not selected:
        return []
    if str(evidence_report.get("verdict") or "").lower() not in {"ok", ""}:
        return []
    terms = _mcagent_context_required_terms(focus)
    if not terms:
        return selected
    filtered: list[SearchResult] = []
    for item in selected:
        haystack = f"{item.title}\n{item.source_path}\n{item.url or ''}\n{item.text[:2400]}".lower()
        if any(term.lower() in haystack for term in terms):
            filtered.append(item)
    return filtered


def _filter_answer_evidence_by_required_terms(focus: str, selected: list[SearchResult]) -> list[SearchResult]:
    if not selected:
        return []
    strict_terms = _strict_entity_terms_for_focus(focus)
    if not strict_terms:
        subject_terms = _primary_fact_subject_terms(focus)
        if subject_terms:
            strict_terms = subject_terms
    if strict_terms:
        filtered = [
            item
            for item in selected
            if _search_result_matches_strict_entity(item, strict_terms)
        ]
        filtered = _rank_answer_evidence_for_focus(focus, filtered)
        for index, item in enumerate(filtered, start=1):
            item.rank = index
        return filtered
    terms = _mcagent_context_required_terms(focus)
    if not terms:
        return selected
    filtered: list[SearchResult] = []
    for item in selected:
        haystack = f"{item.title}\n{item.source_path}\n{item.url or ''}\n{item.text[:3000]}".lower()
        if any(term.lower() in haystack for term in terms):
            filtered.append(item)
    filtered = _rank_answer_evidence_for_focus(focus, filtered)
    for index, item in enumerate(filtered, start=1):
        item.rank = index
    return filtered


def _rank_answer_evidence_for_focus(focus: str, selected: list[SearchResult]) -> list[SearchResult]:
    if not selected or not _needs_general_grounded_answer(focus):
        return selected
    return sorted(
        selected,
        key=lambda item: (
            _guide_mechanics_evidence_score(item),
            float(item.score or 0.0),
            -int(item.rank or 0),
        ),
        reverse=True,
    )


def _guide_mechanics_evidence_score(item: SearchResult) -> float:
    haystack = f"{item.title}\n{item.source_path}\n{item.url or ''}\n{item.text[:3600]}".lower()
    guide_terms = (
        "新手", "萌新", "入门", "开局", "前期", "中期", "后期", "教程", "攻略", "流程", "路线", "进度", "任务",
        "beginner", "getting started", "guide", "tutorial", "progression", "quest",
    )
    mechanics_terms = (
        "机制", "配方", "合成", "制作", "烹饪", "食物", "食材", "厨锅", "煎锅", "砧板", "刀", "种子", "作物", "农场", "热源",
        "recipe", "cooking", "craft", "knife", "cutting board", "crop", "farm", "stove", "pan", "pot",
    )
    procedure_terms = (
        "首先", "先", "然后", "接着", "需要", "可以", "打开", "按住", "获得", "收集", "建造", "探索",
        "start", "use", "craft", "collect", "open",
    )
    low_value_terms = (
        "依赖", "附属", "关系类型", "运行环境", "编辑资料", "浏览次数", "下载次数", "评分", "投票",
        "乐事 (", "delight)", "delight +", "compats", "compat",
        "dependency", "dependent", "relation", "rating", "downloads",
    )
    title_list_terms = ("依赖", "附属", "关系类型", "delight)", "compats", "compat")
    guide_hits = sum(1 for term in guide_terms if term in haystack)
    mechanics_hits = sum(1 for term in mechanics_terms if term in haystack)
    procedure_hits = sum(1 for term in procedure_terms if term in haystack)
    low_hits = sum(1 for term in low_value_terms if term in haystack)
    if low_hits and guide_hits + mechanics_hits + procedure_hits <= low_hits + 2:
        return -float(low_hits)
    score = guide_hits * 3.0 + mechanics_hits * 2.4 + procedure_hits * 1.2
    if guide_hits and mechanics_hits:
        score += 5.0
    if score <= 0:
        score -= 4.0
    score -= min(low_hits * 2.5, 10.0)
    if any(term in haystack for term in title_list_terms) and not any(term in haystack for term in ("进度", "开局", "砧板", "厨锅", "烹饪", "配方", "progression", "tutorial")):
        score -= 8.0
    return score


def _filter_answer_evidence_with_recovery(
    focus: str,
    selected: list[SearchResult],
    rough_results: list[SearchResult],
    limit: int,
) -> list[SearchResult]:
    """Filter answer evidence, then recover entity-matching rough candidates if selected was polluted."""
    filtered = _filter_answer_evidence_by_required_terms(focus, selected)
    if filtered or not rough_results:
        return filtered
    recovered = _filter_answer_evidence_by_required_terms(focus, rough_results)
    return _dedupe_results(recovered, limit=limit)


def _strict_entity_terms_for_focus(focus: str) -> list[str]:
    text = str(focus or "")
    lowered = text.lower()
    if "\u4e4c\u6258\u90a6\u63a2\u9669\u4e4b\u65c5" in text or "utopia journey" in lowered:
        return ["\u4e4c\u6258\u90a6\u63a2\u9669\u4e4b\u65c5", "\u4e4c\u6258\u90a6", "utopia journey", "utopian journey", "utopia-journey", "modpack/1337"]
    if "乌托邦探险之旅" in text or "utopian journey" in lowered or "utopia-journey" in lowered:
        return ["乌托邦探险之旅", "utopian journey", "utopia-journey", "modpack/1337"]
    return []


def _search_result_matches_strict_entity(item: SearchResult, terms: list[str]) -> bool:
    title_source = f"{item.title}\n{item.source_path}\n{item.url or ''}".lower()
    if "\u4e4c\u6258\u90a6" in terms and "\u4e4c\u6258\u90a6\u63a2\u9669\u4e4b\u65c5" not in title_source:
        utopia_pack_markers = ("utopia journey", "utopian journey", "utopia-journey", "modpack/1337", "1337")
        body_head = str(item.text or "").lower()[:2000]
        if not any(marker in title_source or marker in body_head for marker in utopia_pack_markers):
            return False
    if any(term.lower() in title_source for term in terms):
        return True
    if any(term.lower() in { "农夫乐事", "farmer's delight", "farmers delight", "farmer-s-delight", "farmers-delight" } for term in terms):
        return False
    body = str(item.text or "").lower()
    body_hits = [term for term in terms[:3] if term.lower() in body[:1600]]
    return len(body_hits) >= 2


def _mcagent_context_required_terms(focus: str) -> list[str]:
    text = str(focus or "")
    lowered = text.lower()
    terms: list[str] = []
    if "\u4e4c\u6258\u90a6\u63a2\u9669\u4e4b\u65c5" in text or "utopian journey" in lowered or "utopia-journey" in lowered:
        terms.extend(["\u4e4c\u6258\u90a6\u63a2\u9669\u4e4b\u65c5", "utopian journey", "utopia-journey"])
    elif "\u4e4c\u6258\u90a6" in text:
        terms.append("\u4e4c\u6258\u90a6")
    if "\u9999\u8349\u7eaa\u5143" in text or "vanillaera" in lowered or "fareschron" in lowered:
        terms.extend(["\u9999\u8349\u7eaa\u5143", "vanillaera", "fareschron", "fares chron"])
    if "craftoria" in lowered:
        terms.extend(["craftoria", "craftoria-1.31.0"])
    if "\u843d\u5e55\u66f2" in text or "closing song" in lowered:
        terms.extend(["\u843d\u5e55\u66f2", "closing song"])
    extracted = [
        item
        for item in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{3,}|[\u4e00-\u9fff]{3,}", text)
        if item.lower() not in {"minecraft", "modpack", "crawleragent"}
        and item not in {"\u6574\u5408\u5305", "\u5b8c\u6574\u8d44\u6599", "\u914d\u7f6e\u6587\u4ef6", "\u7248\u672c\u517c\u5bb9\u6027\u4fe1\u606f", "\u793e\u533a\u66f4\u65b0\u65e5\u5fd7"}
    ]
    terms.extend(extracted[:6])
    return _dedupe_strings(terms)


def _clean_inter_agent_collection_target(original_question: str, proposed_collection: str = "") -> str:
    proposed = str(proposed_collection or "").strip()
    original = str(original_question or "").strip()
    workflow_markers = (
        "CrawlerAgent 应",
        "CrawlerAgent should",
        "MCagent 使用",
        "用户原始目标",
        "mcagent_context",
        "planning_instruction",
    )
    source = original if any(marker.lower() in proposed.lower() for marker in workflow_markers) else (proposed or original)
    focus = _mcagent_context_focus(source, source)
    return focus or original or proposed


def _prune_pending_mcagent_context_tasks_after_success(tasks: list[dict[str, Any]], index: int) -> list[dict[str, Any]]:
    removed: list[dict[str, Any]] = []
    kept_tail: list[dict[str, Any]] = []
    for task in tasks[index:]:
        if _source_alias(str(task.get("source") or "")) == "mcagent_context":
            removed.append(task)
        else:
            kept_tail.append(task)
    if removed:
        tasks[index:] = kept_tail
    return removed


def _has_successful_mcagent_context(task_results: list[dict[str, Any]]) -> bool:
    return any(
        _source_alias(str(result.get("source") or "")) == "mcagent_context"
        and int(result.get("returncode") or 0) == 0
        and int(((result.get("manifest_stats") or {}).get("records") if isinstance(result.get("manifest_stats"), dict) else 0) or 0) > 0
        for result in task_results
    )


def _drop_duplicate_mcagent_context_tasks(new_tasks: list[dict[str, Any]], task_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not _has_successful_mcagent_context(task_results):
        return new_tasks
    return [task for task in new_tasks if _source_alias(str(task.get("source") or "")) != "mcagent_context"]


def _reflection_requests_local_source_materialization(reflection: dict[str, Any], task_results: list[dict[str, Any]]) -> bool:
    if not _has_successful_mcagent_context(task_results):
        return False
    if not any(result.get("mcagent_source_paths") for result in task_results if isinstance(result, dict)):
        return False
    text = " ".join(
        str(reflection.get(key) or "")
        for key in ("reason", "done_summary")
    ).lower()
    return any(
        marker in text
        for marker in (
            "local source",
            "local file",
            "source path",
            "source_paths",
            "artifact",
            "local evidence",
            "local files",
            "mcagent context",
            "read ",
            "inspect ",
            "search_local",
            "read_local",
            "本地文件",
            "本地路径",
            "本地资料",
            "读取",
            "检查本地",
        )
    )


def _materialize_local_source_path_tasks_from_mcagent_context(
    reflection: dict[str, Any],
    task_results: list[dict[str, Any]],
    *,
    existing_tasks: list[dict[str, Any]],
    max_new_tasks: int,
) -> list[dict[str, Any]]:
    if max_new_tasks <= 0 or not _reflection_requests_local_source_materialization(reflection, task_results):
        return []
    paths: list[str] = []
    for result in task_results:
        if not isinstance(result, dict) or _source_alias(str(result.get("source") or "")) != "mcagent_context":
            continue
        for value in result.get("mcagent_source_paths") or []:
            text = str(value or "").strip()
            if text and text not in paths:
                paths.append(text)
    if not paths:
        return []
    existing = {
        (_source_alias(str(task.get("source") or "")), str(task.get("path") or "").strip().lower(), str(task.get("query") or "").strip().lower())
        for task in existing_tasks
        if isinstance(task, dict)
    }
    reason_text = " ".join(str(reflection.get(key) or "") for key in ("reason", "done_summary"))
    query_terms = _local_source_materialization_query(reason_text)
    tasks: list[dict[str, Any]] = []
    for index, path in enumerate(paths):
        source = "read_local_file" if re.search(r"\.[A-Za-z0-9]{1,8}$", path) else "search_local_files"
        query = query_terms or "inspect local source returned by MCagent context"
        task = {
            "source": source,
            "query": query,
            "path": path,
            "reason": "Inspect exact local source path returned by MCagent over the AgentMessage bus; CrawlerAgent will judge relevance after reading.",
            "priority": 142 - index,
        }
        key = (_source_alias(source), path.lower(), query.lower())
        if key in existing:
            continue
        tasks.append(task)
        existing.add(key)
        if len(tasks) >= max_new_tasks:
            break
    return tasks


def _local_source_materialization_query(text: str) -> str:
    lowered = str(text or "").lower()
    terms: list[str] = []
    checks = [
        ("version loader install", ("version", "loader", "install", "安装", "版本")),
        ("download archive mrpack zip", ("download", "archive", ".mrpack", ".zip", "下载", "包体")),
        ("mod list dependencies", ("mod list", "modlist", "dependencies", "模组列表")),
        ("gameplay progression quest route", ("progression", "gameplay", "quest", "route", "玩法", "路线", "任务")),
        ("boss summon drops", ("boss", "summon", "drop", "掉落", "召唤")),
    ]
    for label, needles in checks:
        if any(needle.lower() in lowered for needle in needles):
            terms.append(label)
    return " ".join(dict.fromkeys(terms))[:180]


def _expand_mcagent_context_aliases(value: str) -> str:
    text = _strip_context_focus_leftover_prefix(str(value or "").strip())
    if not text:
        return text
    lowered = text.lower()
    minecraft_hint = any(term in lowered for term in ("minecraft", "modpack", "mc", "fabric")) or any(term in text for term in ("整合包", "模组", "玩法", "新手", "版本", "任务", "Boss", "boss"))
    if "乌托邦" in text and minecraft_hint:
        aliases = ["乌托邦探险之旅", "Utopian Journey", "MC 1.20.1 Fabric 整合包"]
        for alias in aliases:
            if alias.lower() not in lowered:
                text = f"{text} {alias}"
                lowered = text.lower()
    return _strip_context_focus_leftover_prefix(text)


def _strip_context_focus_leftover_prefix(value: str) -> str:
    return re.sub(r"^(?:下|一下)\s*(?=[\u4e00-\u9fffA-Za-z0-9])", "", str(value or "").strip())


def _clean_crawler_task_question(question: str) -> str:
    value = str(question).strip()
    value = re.sub(r"^\s*(?:\u8bf7|\u9ebb\u70e6|\u5e2e\u6211|\u5e2e\u5fd9)?\s*(?:\u544a\u8bc9|\u53eb|\u8ba9|\u6d3e|\u901a\u77e5|\u8f6c\u8fbe|\u8f6c\u4ea4|\u4ea4\u7ed9)?\s*(?:MCagent|MCAgent|MC Agent)?\s*(?:\u53bb)?\s*(?:\u544a\u8bc9|\u53eb|\u8ba9|\u6d3e|\u901a\u77e5|\u8f6c\u8fbe|\u8f6c\u4ea4|\u4ea4\u7ed9)?\s*(?:CrawlerAgent|Crawler|\u722c\u866bAgent|\u722c\u866bagent|\u722c\u866b)\s*(?:\u4f60|\u4ed6)?\s*(?:\u8ba9\u4ed6)?\s*(?:\u53bb|\u6765|\u5e2e\u6211|\u5e2e\u5fd9|\u7ee7\u7eed)?\s*(?:\u6536\u96c6|\u91c7\u96c6|\u83b7\u53d6|\u6293\u53d6|\u722c\u53d6|\u8865\u5145|\u8865\u5e93|\u66f4\u65b0\u8d44\u6599)?\s*", "", value, flags=re.I)
    value = re.sub(
        r"^\s*(?:\u8bf7|\u9ebb\u70e6|\u5e2e\u6211|\u5e2e\u5fd9|\u5e2e)?\s*(?:\u7ed9|\u4e3a)?\s*(?:MCagent|MCAgent|MC Agent|RAG|\u672c\u5730\u8d44\u6599\u5e93|\u77e5\u8bc6\u5e93)\s*(?:\u53bb|\u6765)?\s*(?:\u6536\u96c6|\u91c7\u96c6|\u83b7\u53d6|\u6293\u53d6|\u722c\u53d6|\u8865\u5145|\u8865\u5e93|\u66f4\u65b0\u8d44\u6599)\s*",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(r"^\s*(?:\u6536\u96c6|\u91c7\u96c6|\u83b7\u53d6|\u6293\u53d6|\u722c\u53d6|\u8865\u5145|\u8865\u5e93|\u66f4\u65b0\u8d44\u6599)\s*", "", value)
    value = re.sub(r"\s*(?:\u7ed9\s*MCagent\s*\u7528|\u7ed9\s*RAG\s*\u7528|\u7528\u4e8e\s*RAG|\u5165\u5e93|\u6e05\u6d17\u5165\u5e93|\u4fdd\u5b58\u5230\u672c\u5730\u8d44\u6599\u5e93)\s*$", "", value, flags=re.I)
    value = value.strip(" \t\r\n\uff0c\u3002\uff1b;\uff1a:")
    return value or question


def _status_payload(config: AppConfig) -> dict[str, Any]:
    source_dir = config.paths.source_dir
    sources = _source_status_payload(source_dir)
    if config.paths.db_path.exists():
        with connect(config.paths.db_path) as conn:
            doc_count, chunk_count = count_rows(conn)
        db_counts = {"documents": doc_count, "chunks": chunk_count}
    else:
        db_counts = {"documents": 0, "chunks": 0}
    return {
        "project_root": str(PROJECT_ROOT),
        "config": asdict(config),
        "database": {
            "db_path": str(config.paths.db_path),
            "db_exists": config.paths.db_path.exists(),
            "db_size": config.paths.db_path.stat().st_size if config.paths.db_path.exists() else 0,
            "documents": db_counts.get("documents", 0),
            "chunks": db_counts.get("chunks", 0),
            "index_path": str(config.paths.index_path),
            "index_exists": config.paths.index_path.exists(),
            "index_size": config.paths.index_path.stat().st_size if config.paths.index_path.exists() else 0,
        },
        "sources": sources,
        "toolsets": toolsets_payload(),
        "memory": memory_summary(),
        "jobs": _jobs_payload()["jobs"],
        "crawler_progress": _crawler_progress_payload(source_dir),
    }


def _available_models(config: AppConfig) -> list[dict[str, Any]]:
    profile_models = [
        {
            "id": f"profile:{profile['id']}",
            "value": f"profile:{profile['id']}",
            "profile_id": profile["id"],
            "label": str(profile.get("name") or profile.get("model") or profile.get("id")),
            "provider": str(profile.get("provider") or "openai-compatible"),
            "key_configured": bool(profile.get("key_configured")),
        }
        for profile in profiles_payload(config).get("profiles", [])
    ]
    legacy_models = [
        {"id": config.ollama.model, "label": f"Ollama {config.ollama.model}", "provider": "ollama"},
        {"id": "cloud:deepseek:deepseek-v4-pro", "label": "DeepSeek deepseek-v4-pro", "provider": "deepseek"},
    ]
    return [*profile_models, *legacy_models]


def _append_session(
    payload: dict[str, Any],
    question: str,
    answer: str,
    sources: list[dict[str, Any]],
    *,
    extra: dict[str, Any] | None = None,
) -> None:
    session_id = normalize_session_id(payload.get("session_id"))
    turn = {"time": time.time(), "question": question, "answer": answer, "sources": sources}
    if extra:
        turn.update({key: value for key, value in extra.items() if value not in (None, "", [])})
    SESSION_STORE.append_turn(session_id, turn, max_turns=80)
    _update_session_summary(session_id, turn)


def _session_history(payload: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
    session_id = normalize_session_id(payload.get("session_id"))
    server_history = SESSION_STORE.history(session_id, limit=limit)
    provided_history = payload_history(payload, limit=limit)
    if not provided_history:
        return server_history
    if not server_history:
        return provided_history
    seen = {(str(item.get("question") or ""), str(item.get("answer") or "")[:120]) for item in server_history}
    merged = list(server_history)
    for item in provided_history:
        key = (str(item.get("question") or ""), str(item.get("answer") or "")[:120])
        if key not in seen:
            merged.append(item)
            seen.add(key)
    return merged[-limit:]


def _session_summary(payload: dict[str, Any]) -> dict[str, Any]:
    explicit = payload.get("session_summary") if isinstance(payload.get("session_summary"), dict) else {}
    session_id = normalize_session_id(payload.get("session_id"))
    summary = SESSION_STORE.summary(session_id)
    if not summary:
        summary = _summary_from_history(payload_history(payload, limit=20))
    summary = _session_summary_with_events(session_id, summary)
    if explicit:
        merged = dict(summary)
        for key, value in explicit.items():
            if isinstance(value, list) and isinstance(merged.get(key), list):
                merged[key] = merge_limited(list(merged.get(key) or []), [str(item) for item in value], limit=80)
            elif value not in (None, "", []):
                merged[key] = value
        return merged
    return summary


def _session_summary_with_events(session_id: str, summary: dict[str, Any]) -> dict[str, Any]:
    value = dict(summary or {})
    events = SESSION_STORE.events(session_id, limit=30)
    if not events:
        return value
    agent_events: list[dict[str, Any]] = []
    for event in events[-30:]:
        if not isinstance(event, dict):
            continue
        kind = str(event.get("kind") or event.get("event") or "").strip()
        if kind not in {"agent_message", "agent_reply", "crawler_delegation", "crawler_job"}:
            continue
        light = {
            key: copy.deepcopy(event.get(key))
            for key in (
                "kind",
                "from_agent",
                "to_agent",
                "content",
                "intent",
                "task",
                "requested_by",
                "delivery_target",
                "job_id",
                "job_status",
                "answer",
                "time",
            )
            if event.get(key) not in (None, "", [])
        }
        if "content" in light:
            light["content"] = _tail_text(str(light["content"]), 900)
        if "answer" in light:
            light["answer"] = _tail_text(str(light["answer"]), 900)
        agent_events.append(light)
    if not agent_events:
        return value
    value["recent_agent_events"] = agent_events[-12:]
    value["last_agent_event"] = agent_events[-1]
    value["agent_event_count"] = len(agent_events)
    message_topics = []
    for event in agent_events[-12:]:
        message_topics.extend(_fallback_focus_terms(str(event.get("content") or event.get("task") or "")))
    if message_topics:
        value["topics"] = merge_limited(value.get("topics") or [], message_topics, limit=24)
        value["entities"] = merge_limited(value.get("entities") or [], message_topics, limit=48)
    return value


def _session_summary_with_received_message(summary: dict[str, Any], message: AgentMessage) -> dict[str, Any]:
    """Expose received message facts to the Agent without choosing its tool for it."""

    value = dict(summary or {})
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    value["received_agent_message"] = {
        "from_agent": message.from_agent,
        "to_agent": message.to_agent,
        "content": message.content,
        "intent": message.intent,
        "metadata": metadata,
    }
    if message.intent == "collection_request":
        value.setdefault("handoff_from", message.from_agent)
        value.setdefault("collection_target", str(metadata.get("collection_target") or message.content or "").strip())
        value.setdefault("delivery_target", str(metadata.get("delivery_target") or "").strip())
        value.setdefault("requested_by", str(metadata.get("requested_by") or "").strip())
        if metadata.get("handoff_brief"):
            value.setdefault("handoff_brief", str(metadata.get("handoff_brief") or "").strip())
        if isinstance(metadata.get("selected_action_plan"), list):
            value.setdefault("selected_action_plan", metadata.get("selected_action_plan"))
    return value


def _summary_from_history(history: list[dict[str, Any]]) -> dict[str, Any]:
    if not history:
        return {}
    summary: dict[str, Any] = {"topics": [], "entities": [], "names": [], "gaps": [], "turn_count": 0}
    for turn in history[-20:]:
        summary["turn_count"] = int(summary.get("turn_count") or 0) + 1
        question = str(turn.get("question") or "")
        answer = _strip_answer_metadata(str(turn.get("answer") or ""))
        topics = _fallback_focus_terms(question)
        names = _extract_context_names(answer, limit=24)
        gaps = _extract_context_gaps(answer, limit=12)
        for source in turn.get("sources") or []:
            if isinstance(source, dict):
                topics.extend(_fallback_focus_terms(str(source.get("title") or "")))
        summary["topics"] = merge_limited(summary.get("topics") or [], topics, limit=24)
        summary["names"] = merge_limited(summary.get("names") or [], names, limit=40)
        summary["gaps"] = merge_limited(summary.get("gaps") or [], gaps, limit=24)
        summary["entities"] = merge_limited(summary.get("entities") or [], [*topics[:8], *names[:16]], limit=48)
    return summary


def _delete_session(session_id: str) -> dict[str, Any]:
    return SESSION_STORE.delete(session_id)


def _update_session_summary(session_id: str, turn: dict[str, Any]) -> None:
    def updater(summary: dict[str, Any]) -> dict[str, Any]:
        if not summary:
            summary = {"topics": [], "entities": [], "names": [], "gaps": [], "turn_count": 0}
        summary["turn_count"] = int(summary.get("turn_count") or 0) + 1
        question = str(turn.get("question") or "")
        answer = _strip_answer_metadata(str(turn.get("answer") or ""))
        topics = _fallback_focus_terms(question)
        names = _extract_context_names(answer, limit=24)
        gaps = _extract_context_gaps(answer, limit=12)
        for source in turn.get("sources") or []:
            if isinstance(source, dict):
                topics.extend(_fallback_focus_terms(str(source.get("title") or "")))
        delegation = turn.get("delegation") if isinstance(turn.get("delegation"), dict) else {}
        if delegation:
            topics.extend(_fallback_focus_terms(str(delegation.get("task") or "")))
            event = {
                "kind": "crawler_delegation",
                "from_agent": "MCagent",
                "to_agent": "CrawlerAgent",
                "task": str(delegation.get("task") or ""),
                "requested_by": str(delegation.get("requested_by") or ""),
                "delivery_target": str(delegation.get("delivery_target") or ""),
                "time": turn.get("time") or time.time(),
            }
            recent = list(summary.get("recent_agent_events") or [])
            recent.append({key: value for key, value in event.items() if value not in (None, "", [])})
            summary["recent_agent_events"] = recent[-12:]
            summary["last_agent_event"] = summary["recent_agent_events"][-1]
        job = turn.get("job") if isinstance(turn.get("job"), dict) else {}
        if job:
            event = {
                "kind": "crawler_job",
                "job_id": str(job.get("id") or ""),
                "job_status": str(job.get("status") or ""),
                "task": str(job.get("title") or ""),
                "time": turn.get("time") or time.time(),
            }
            recent = list(summary.get("recent_agent_events") or [])
            recent.append({key: value for key, value in event.items() if value not in (None, "", [])})
            summary["recent_agent_events"] = recent[-12:]
            summary["last_agent_event"] = summary["recent_agent_events"][-1]
        summary["topics"] = merge_limited(summary.get("topics") or [], topics, limit=24)
        summary["names"] = merge_limited(summary.get("names") or [], names, limit=40)
        summary["gaps"] = merge_limited(summary.get("gaps") or [], gaps, limit=24)
        summary["entities"] = merge_limited(summary.get("entities") or [], [*topics[:8], *names[:16]], limit=48)
        return summary

    SESSION_STORE.update_summary(session_id, updater)


def _append_agent_session_event(session_id: str, event: dict[str, Any]) -> None:
    clean = {key: value for key, value in dict(event or {}).items() if value not in (None, "", [])}
    if not clean:
        return
    if "content" in clean:
        clean["content"] = _tail_text(str(clean["content"]), 1500)
    if "answer" in clean:
        clean["answer"] = _tail_text(str(clean["answer"]), 1500)
    SESSION_STORE.append_event(session_id, clean, max_events=120)


def _record_agent_message_event(message: AgentMessage, *, kind: str = "agent_message") -> None:
    session_id = normalize_session_id(message.conversation_id)
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    _append_agent_session_event(
        session_id,
        {
            "kind": kind,
            "message_id": message.message_id,
            "from_agent": message.from_agent,
            "to_agent": message.to_agent,
            "content": message.content,
            "intent": message.intent,
            "task": str(metadata.get("collection_target") or metadata.get("task_goal") or ""),
            "requested_by": str(metadata.get("requested_by") or ""),
            "delivery_target": str(metadata.get("delivery_target") or ""),
            "time": time.time(),
        },
    )


def _record_agent_response_event(session_id: str, response: dict[str, Any]) -> None:
    raw_message = response.get("agent_message") if isinstance(response, dict) else None
    if isinstance(raw_message, dict):
        try:
            message = message_from_payload({"agent_message": raw_message, "session_id": session_id}, default_to_agent="User", default_content=str(response.get("answer") or ""))
            _record_agent_message_event(message, kind="agent_reply")
        except Exception:
            pass
    delegation = response.get("delegation") if isinstance(response.get("delegation"), dict) else {}
    if delegation:
        _append_agent_session_event(
            session_id,
            {
                "kind": "crawler_delegation",
                "from_agent": "MCagent" if str(delegation.get("requested_by") or "") in {"mcagent", "user_via_mcagent"} else str(response.get("agent") or ""),
                "to_agent": "CrawlerAgent",
                "task": str(delegation.get("task") or ""),
                "requested_by": str(delegation.get("requested_by") or ""),
                "delivery_target": str(delegation.get("delivery_target") or ""),
                "answer": str(response.get("answer") or ""),
                "time": time.time(),
            },
        )
    job = response.get("job") if isinstance(response.get("job"), dict) else {}
    if job:
        _append_agent_session_event(
            session_id,
            {
                "kind": "crawler_job",
                "job_id": str(job.get("id") or ""),
                "job_status": str(job.get("status") or ""),
                "task": str((delegation or {}).get("task") or job.get("title") or ""),
                "requested_by": str((delegation or {}).get("requested_by") or ""),
                "delivery_target": str((delegation or {}).get("delivery_target") or ""),
                "time": time.time(),
            },
        )


def _strip_answer_metadata(answer: str) -> str:
    markers = (
        "\n模型：",
        "\n来源：",
        "\n补库动作：",
        "\nMCagent ↔ Crawler",
        "\nobserve ·",
        "\nretrieve ·",
        "\ndecide ·",
        "\nanswer ·",
        "\ndone ·",
    )
    cut = len(answer)
    for marker in markers:
        index = answer.find(marker)
        if index >= 0:
            cut = min(cut, index)
    return answer[:cut].strip()


def _question_terms_for_coreference(question: str) -> list[str]:
    stop = {
        "minecraft", "mc", "mod", "mods",
        "\u8fd9\u4e9b", "\u5b83\u4eec", "\u4ed6\u4eec", "\u4e0a\u8ff0", "\u4e0a\u9762", "\u524d\u9762", "\u521a\u624d", "\u8fd9\u4e2a", "\u90a3\u4e2a", "\u8be5", "\u5176",
        "\u54ea\u4e9b", "\u6709\u4ec0\u4e48", "\u6709\u54ea\u4e9b", "\u5982\u4f55", "\u600e\u4e48", "\u600e\u6837", "\u662f\u5426", "\u80fd\u5426", "\u53ef\u4ee5", "\u4e00\u4e0b", "\u8be6\u7ec6",
        "\u4ecb\u7ecd", "\u73a9\u6cd5", "\u653b\u7565", "\u5408\u6210", "\u914d\u65b9", "\u5236\u4f5c", "\u6750\u6599", "\u83b7\u53d6", "\u83b7\u5f97", "\u5217\u8868", "\u5217\u51fa",
    }
    terms: list[str] = []
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}|[\u4e00-\u9fff]{2,}", question):
        term = raw.strip()
        lowered = term.lower()
        for suffix in ("\u6709\u54ea\u4e9b", "\u6709\u4ec0\u4e48", "\u5982\u4f55", "\u600e\u4e48", "\u600e\u6837", "\u73a9\u6cd5", "\u653b\u7565", "\u4ecb\u7ecd", "\u5408\u6210", "\u914d\u65b9", "\u5236\u4f5c", "\u6750\u6599"):
            if term.endswith(suffix) and term != suffix:
                term = term[: -len(suffix)].strip()
                lowered = term.lower()
                break
        if term and lowered not in stop and lowered not in {item.lower() for item in terms}:
            terms.append(term)
    return terms[:10]


def _is_followup_question(question: str, inherited_terms: list[str] | None = None) -> bool:
    inherited_terms = inherited_terms or []
    if not inherited_terms:
        return False
    pronouns = ("\u8fd9\u4e9b", "\u5b83\u4eec", "\u4ed6\u4eec", "\u4e0a\u8ff0", "\u4e0a\u9762", "\u524d\u9762", "\u521a\u624d", "\u8fd9\u4e2a", "\u90a3\u4e2a", "\u8be5", "\u5176")
    if any(token in question for token in pronouns):
        return True
    list_or_detail_words = ("有哪些", "有什么", "哪里", "掉落", "打法", "Boss", "BOSS", "boss", "首领")
    if any(token in question for token in list_or_detail_words):
        explicit_terms = _question_terms_for_coreference(question)
        meaningful_terms = [
            term for term in explicit_terms
            if term.lower() not in {"boss"} and term not in {"有哪些", "有什么", "首领", "哪里", "掉落", "打法"}
        ]
        if not meaningful_terms:
            return True
    action_words = ("\u5982\u4f55", "\u600e\u4e48", "\u600e\u6837", "\u73a9\u6cd5", "\u653b\u7565", "\u5408\u6210", "\u914d\u65b9", "\u5236\u4f5c", "\u6750\u6599", "\u83b7\u53d6", "\u83b7\u5f97", "\u7528\u9014", "\u673a\u5236")
    if not any(token in question for token in action_words):
        return False
    explicit_terms = _question_terms_for_coreference(question)
    # If the new question has its own concrete subject, keep it independent.
    if len(explicit_terms) >= 2:
        return False
    if len(explicit_terms) == 1 and explicit_terms[0].lower() not in {term.lower() for term in inherited_terms}:
        return False
    return True


def _extract_context_names(text: str, limit: int = 12) -> list[str]:
    names: list[str] = []
    stop = {
        "用户", "模型", "DeepSeek", "Ollama", "MC百科", "Minecraft", "本地资料", "可靠答案", "补库动作",
        "来源", "证据", "资料库", "整合包", "模组", "玩法", "攻略", "MCagent",
    }
    for line in text.splitlines():
        clean = re.sub(r"\[[Ss]\d+\]", "", line).strip(" -*•\t\r\n>?:：")
        if not clean or clean.startswith(("模型：", "来源：", "补库动作：")):
            continue
        if any(marker in clean for marker in ("本地资料库未找到", "请求失败", "模型调用失败", "Crawler 多源补库")) and not line.lstrip().startswith(("-", "•", "*")):
            continue
        candidates = re.findall(r"[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9:：·'_-]{1,24}", clean)
        for candidate in candidates:
            value = candidate.strip(" ?:,，。.;；!！?？、()（）[]【】")
            if value in stop or value.lower() in {item.lower() for item in names}:
                continue
            if len(value) > 18 and not re.search(r"[A-Za-z]", value):
                continue
            if any(bad in value for bad in ("本地资料", "可靠答案", "模型调用", "补库动作", "来源", "证据")):
                continue
            names.append(value)
            if len(names) >= limit:
                return names
    return names


def _extract_context_gaps(text: str, limit: int = 12) -> list[str]:
    gaps: list[str] = []
    in_gap_section = False
    gap_heading = re.compile(r"(缺口|缺漏|不足|仍缺|还缺|未找到|没有找到|需要进一步|需要补充)")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if in_gap_section and gaps:
                break
            continue
        clean = re.sub(r"\[[Ss]\d+\]", "", line)
        clean = re.sub(r"^\s*(?:#{1,6}\s*)?(?:[-*•]\s*)?(?:\d+[.)、]\s*)?", "", clean).strip()
        clean = clean.strip(" -*•\t\r\n>?:：")
        clean = clean.replace("**", "").replace("__", "")
        if not clean:
            continue
        is_heading = raw_line.lstrip().startswith("#")
        if is_heading and gap_heading.search(clean):
            in_gap_section = True
            continue
        if in_gap_section and raw_line.lstrip().startswith("#") and not gap_heading.search(clean):
            break
        is_candidate = in_gap_section or bool(gap_heading.search(clean))
        if not is_candidate:
            continue
        if clean.startswith(("模型：", "来源：", "补库动作：")):
            continue
        if len(clean) > 220:
            clean = clean[:220].rstrip() + "..."
        if clean and clean not in gaps:
            gaps.append(clean)
            if len(gaps) >= limit:
                break
    return gaps


def _conversation_note(payload: dict[str, Any], history: list[dict[str, Any]]) -> str:
    summary = _session_summary(payload)
    lines = ["以下是当前会话摘要，供 MCagent 理解追问、省略指代和交付目标；不要覆盖用户原始问题。"]
    if summary:
        topics = ", ".join((summary.get("topics") or [])[:12])
        names = ", ".join((summary.get("names") or [])[:20])
        gaps = "；".join((summary.get("gaps") or [])[:8])
        if topics:
            lines.append(f"- 已讨论主题：{topics}")
        if names:
            lines.append(f"- 已出现实体/名称：{names}")
        if gaps:
            lines.append(f"- 上轮/近期回答暴露的资料缺口：{gaps}")
    for item in history[-5:]:
        question = str(item.get("question") or "").strip()
        answer = _strip_answer_metadata(str(item.get("answer") or "").strip())
        if len(answer) > 360:
            answer = answer[:360].rstrip() + "..."
        lines.append(f"- 用户问：{question}")
        if answer:
            lines.append(f"  MCagent答：{answer}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _contextualize_question(payload: dict[str, Any], question: str) -> tuple[str, str, bool]:
    history = _session_history(payload, limit=10)
    note = _conversation_note(payload, history)
    if not history:
        return question, note, False
    summary = _session_summary(payload)
    anchors: list[str] = []
    for item in history:
        previous_question = str(item.get("question") or "")
        try:
            intent = analyze_query(previous_question, CONCEPTS)
            anchors.extend([intent.entity, *intent.keywords])
        except Exception:
            anchors.extend(_fallback_focus_terms(previous_question))
    anchors.extend(summary.get("topics") or [])
    if _question_has_explicit_coreference(question):
        recent_names = _recent_answer_names_for_coreference(history)
        if recent_names:
            anchors = [*recent_names, *anchors]
        anchors.extend((summary.get("names") or [])[:12])
        anchors.extend((summary.get("entities") or [])[:16])
    compact_terms = _dedupe_strings([term for term in anchors[:24] if _useful_context_term(term)])
    should_rewrite = _is_followup_question(question, compact_terms)
    if not should_rewrite:
        return question, note, False
    compact = " ".join(compact_terms[:14])
    if not compact:
        return question, note, False
    rewritten = f"{compact} {question}"
    review = note + f"\n本轮检索补充问题：{rewritten}"
    return rewritten, review, True


def _question_has_explicit_coreference(question: str) -> bool:
    return any(token in question for token in ("这些", "它们", "他们", "上述", "上面", "前面", "刚才", "这个", "那个", "该", "其"))


def _recent_answer_names_for_coreference(history: list[dict[str, Any]], limit: int = 12) -> list[str]:
    if not history:
        return []
    answer = _strip_answer_metadata(str(history[-1].get("answer") or ""))
    names: list[str] = []
    for line in answer.splitlines():
        clean = line.strip()
        if not clean.startswith(("-", "•", "*")):
            continue
        value = clean.strip("-•* \t")
        if _valid_candidate_name(value, [], is_boss_question=True) or _valid_candidate_name(value, []):
            if value not in names:
                names.append(value)
        if len(names) >= limit:
            break
    return names


def _useful_context_term(term: str) -> bool:
    value = str(term).strip()
    if len(value) < 2:
        return False
    stop = {
        "来源", "攻略", "教程", "列表", "大全", "玩法", "合成", "配方", "制作", "获取", "获得",
        "如何从", "开始", "快速", "根据", "资料", "本地", "模型", "回答", "证据", "有哪些",
        "Closing", "Song", "Minecraft", "MCagent", "DeepSeek", "Ollama",
    }
    if value in stop:
        return False
    if value.startswith(("来源", "模型", "根据本地", "以上内容")):
        return False
    if value.endswith(("有哪些", "有什么", "如何", "怎么")):
        return False
    if value.startswith(("如何从", "开始快速")):
        return False
    if len(value) > 18 and not re.search(r"[A-Za-z]", value):
        return False
    return True


def _dedupe_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        value = str(item).strip()
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output


def _crawler_monitor_answer(config: AppConfig) -> dict[str, Any]:
    status = _status_payload(config)
    db = status["database"]
    sources = status["sources"]
    jobs = status["jobs"]
    progress = status.get("crawler_progress") or {}
    active_jobs = [job for job in jobs if job.get("status") in {"queued", "running"}]
    lines = [
        "采集监控摘要",
        "",
        f"- 本地库：{db['documents']} documents / {db['chunks']} chunks",
        f"- 导出目录：{sources['files']} 个文件，{sources['manifests']} 个 manifest，约 {sources.get('total_mb', 0)} MB",
    ]
    if active_jobs:
        lines.append("")
        lines.append("当前后台任务：")
        for job in active_jobs[:3]:
            lines.append(f"- {job.get('id')}：{job.get('status')}，{job.get('summary') or '正在运行，尚未产生总结'}")
            result = job.get("result") if isinstance(job.get("result"), dict) else {}
            plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
            if plan:
                topic = plan.get("topic") or plan.get("target_hint")
                delivery = plan.get("delivery_target")
                if topic or delivery:
                    lines.append(f"  主题/交付：{topic or '未写明'} / {delivery or '未写明'}")
            tasks = result.get("planned_tasks") if isinstance(result.get("planned_tasks"), list) else []
            if tasks:
                first = tasks[0]
                lines.append(f"  当前计划示例：{first.get('source')} · {first.get('query')}")
    else:
        lines.append("")
        lines.append("当前后台任务：没有正在运行的 Crawler job。")
        latest_job = next((job for job in jobs if job.get("kind") == "crawler"), None)
        if latest_job:
            readable = latest_job.get("readable") if isinstance(latest_job.get("readable"), dict) else {}
            lines.append("")
            lines.append("最近 Crawler 任务：")
            lines.append(f"- 主体：CrawlerAgent")
            lines.append(f"- 任务ID：{latest_job.get('id')}")
            lines.append(f"- 状态：{latest_job.get('status')}，{latest_job.get('summary') or '没有总结'}")
            if readable.get("headline") or readable.get("target"):
                lines.append(f"- 目标：{readable.get('headline') or readable.get('target')}")
            if readable.get("progress_text"):
                lines.append(f"- 进度：{readable.get('progress_text')}")
            useful_outputs = readable.get("useful_outputs") if isinstance(readable.get("useful_outputs"), list) else []
            blocked_outputs = readable.get("blocked_outputs") if isinstance(readable.get("blocked_outputs"), list) else []
            if useful_outputs:
                useful_sources = "、".join(str(item.get("source") or "资料") for item in useful_outputs[:5] if isinstance(item, dict))
                lines.append(f"- 补到/复用：{len(useful_outputs)} 类{('，' + useful_sources) if useful_sources else ''}")
            if blocked_outputs:
                lines.append(f"- 受限/低价值：{len(blocked_outputs)} 条，已记录在采集详情中。")
        elif sources.get("latest_files"):
            lines.append("")
            lines.append("最近采集文件：")
            for item in sources["latest_files"][:3]:
                lines.append(f"- 主体：CrawlerAgent")
                lines.append(f"  动作：已写入采集导出文件")
                lines.append(f"  路径：{item.get('path')}")
    if progress:
        lines.append("")
        lines.append("批量采集进度：")
        active_text = "运行中" if progress.get("active") else "未运行"
        cycle = f"{progress.get('cycle') or 0}/{progress.get('cycles_total') or 0}"
        commands = f"{progress.get('commands_completed') or 0}/{progress.get('commands_total') or 0}"
        lines.append(f"- 状态：{active_text}（{progress.get('status') or 'unknown'}）")
        if progress.get("topic_profile"):
            lines.append(f"- 侧重主题：{progress.get('topic_profile')}")
        if progress.get("cycle_percent") is not None:
            lines.append(f"- 轮次/命令：{cycle} 轮，{commands} 个命令，进度 {progress.get('cycle_percent')}%")
        if progress.get("target_percent") is not None:
            lines.append(f"- 容量目标：{progress.get('current_mb')} MB / {progress.get('target_mb')} MB（{progress.get('target_percent')}%）")
        if progress.get("current_topic"):
            lines.append(f"- 当前主题：{progress.get('current_topic')}")
        if progress.get("current_command"):
            lines.append(f"- 当前命令：{progress.get('current_command')}")
        if progress.get("message"):
            lines.append(f"- 说明：{progress.get('message')}")
    if sources.get("latest_files"):
        latest = sources["latest_files"][0]
        lines.append("")
        lines.append(f"最近文件：{latest.get('path')}")
    lines.append("")
    lines.append("下一步：如果刚完成采集但 documents/chunks 没增长，先运行后台导入；如果有跑偏网页，先清理再继续补库。")
    return {"answer": "\n".join(lines), "sources": [], "status": status}


def _retrieval_planning_summary(summary: dict[str, Any], original_question: str, contextual_question: str) -> dict[str, Any]:
    merged = dict(summary or {})
    if contextual_question != original_question:
        merged["original_question"] = original_question
        merged["contextual_question"] = contextual_question
        merged["planning_instruction"] = "先理解用户原始问题，再把会话上下文作为补充实体；不要让改写文本覆盖用户第一手意图。"
    return merged


def _combined_retrieval_question(original_question: str, contextual_question: str, plan: Any | None) -> str:
    parts = [original_question]
    if contextual_question != original_question:
        parts.append(contextual_question)
    if plan is not None:
        parts.extend(getattr(plan, "subqueries", [])[:6])
        parts.extend(getattr(plan, "required_terms", [])[:6])
    return " ".join(_dedupe_strings([str(item) for item in parts if str(item).strip()]))


def _with_trace(payload: dict[str, Any], trace: list[dict[str, Any]]) -> dict[str, Any]:
    if "agent_message" not in payload and str(payload.get("answer") or "").strip():
        payload["agent_message"] = agent_reply_message_from_payload(
            {"agent_message": _incoming_message_detail_from_trace(trace), "session_id": payload.get("session_id") or ""},
            from_agent_id=str(payload.get("agent") or "mcagent_rag"),
            content=str(payload.get("answer") or ""),
        ).to_dict()
    payload["trace"] = trace
    return payload


def _incoming_message_detail_from_trace(trace: list[dict[str, Any]]) -> dict[str, Any]:
    for step in reversed(trace):
        if step.get("stage") == "message" and step.get("status") == "received" and isinstance(step.get("detail"), dict):
            return dict(step.get("detail") or {})
    return {"from_agent": "User", "to_agent": "MCagent", "content": ""}


def _trace_step(stage: str, status: str, detail: Any = None) -> dict[str, Any]:
    return make_agent_loop_event(stage, status, detail).to_trace_dict()


def _agent_display_name(agent_id: str) -> str:
    if agent_id == "crawler_agent":
        return "CrawlerAgent"
    if agent_id == "mcagent_rag":
        return "MCagent"
    if agent_id == "retriever_only":
        return "MCagent"
    return str(agent_id or "User")


def _incoming_agent_message(payload: dict[str, Any], *, agent: str, question: str) -> AgentMessage:
    return message_from_payload(payload, default_to_agent=_agent_display_name(agent), default_content=question)


def _payload_with_agent_message_tool(payload: dict[str, Any], *, tool: str, intent: str = "") -> dict[str, Any]:
    next_payload = dict(payload)
    message = message_from_payload(
        next_payload,
        default_to_agent=_agent_display_name(str(next_payload.get("agent") or "")),
        default_content=str(next_payload.get("question") or next_payload.get("query") or ""),
    )
    metadata = dict(message.metadata or {})
    metadata["tool"] = tool
    if not intent:
        intent = message.intent or ("collection_request" if tool == "delegate_crawler" else "")
    updated = make_agent_message(
        message.from_agent,
        message.content,
        message.to_agent,
        intent=intent,
        conversation_id=message.conversation_id or str(next_payload.get("session_id") or ""),
        reply_to=message.reply_to,
        requires_reply=message.requires_reply,
        metadata=metadata,
    )
    next_payload["agent_message"] = updated.to_dict()
    next_payload["message_from"] = updated.from_agent
    next_payload["question"] = updated.content
    next_payload["agent"] = updated.to_agent_id
    return next_payload


def _received_agent_message_for_tool(payload: dict[str, Any], *, expected_agent: str, expected_tool: str) -> AgentMessage:
    raw_message = payload.get("agent_message")
    if not isinstance(raw_message, (dict, AgentMessage)):
        raise RuntimeError("Tool execution requires a received AgentMessage from the From-Content-To bus.")
    message = message_from_payload(
        payload,
        default_to_agent=_agent_display_name(str(payload.get("agent") or expected_agent)),
        default_content=str(payload.get("question") or payload.get("query") or ""),
    )
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    if message.to_agent_id != expected_agent:
        raise RuntimeError("Tool execution requires an AgentMessage addressed to the executing Agent.")
    if str(metadata.get("tool") or "") != expected_tool:
        raise RuntimeError("Crawler collection requires the receiving Agent to select delegate_crawler on a received From-Content-To AgentMessage before tool execution.")
    return message


def _trace_agent_message(add_trace: Any, message: AgentMessage, status: str = "received") -> None:
    add_trace("message", status, message.to_dict())


def _agent_message_response_trace(request: AgentMessage, reply: AgentMessage) -> dict[str, Any]:
    return {
        "request": request.to_dict(),
        "reply": reply.to_dict(),
    }


def _agent_message_payload(payload: dict[str, Any], message: AgentMessage) -> dict[str, Any]:
    next_payload = dict(payload)
    next_payload["agent"] = message.to_agent_id
    next_payload["question"] = message.content
    next_payload["message_from"] = message.from_agent
    next_payload["agent_message"] = message.to_dict()
    next_payload.setdefault("session_id", message.conversation_id or payload.get("session_id") or "default")
    return next_payload


def _send_agent_message(
    config: AppConfig,
    payload: dict[str, Any],
    *,
    from_agent: str,
    content: str,
    to_agent: str,
    emit: Any | None = None,
    intent: str = "",
    conversation_id: str = "",
    reply_to: str = "",
    requires_reply: bool = True,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deliver one From-Content-To message through the normal Agent runtime."""

    message = make_agent_message(
        from_agent,
        content,
        to_agent,
        intent=intent,
        conversation_id=conversation_id or str(payload.get("session_id") or payload.get("conversation_id") or ""),
        reply_to=reply_to,
        requires_reply=requires_reply,
        metadata=metadata,
    )
    _record_agent_message_event(message)
    result = dispatch_agent_message_graph(
        config,
        payload,
        from_agent=message.from_agent,
        content=message.content,
        to_agent=message.to_agent,
        emit=emit,
        intent=message.intent,
        conversation_id=message.conversation_id,
        reply_to=message.reply_to,
        requires_reply=message.requires_reply,
        metadata=message.metadata,
        agent_delivery=_deliver_agent_message,
    )
    _record_agent_response_event(normalize_session_id(message.conversation_id or payload.get("session_id")), result)
    return result


def _deliver_agent_message(config: AppConfig, payload: dict[str, Any], emit: Any | None = None) -> dict[str, Any]:
    """Run the current Agent implementation as a LangGraph delivery node."""

    return _chat_impl(config, payload, emit=emit)


def _is_context_only_agent_message(message: AgentMessage) -> bool:
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    return message.intent == "mcagent_context_request" or str(metadata.get("tool") or "") == "mcagent_context"


def _mcagent_context_message_reply(
    config: AppConfig,
    payload: dict[str, Any],
    incoming_message: AgentMessage,
    question: str,
    trace: list[dict[str, Any]],
    add_trace: Any,
) -> dict[str, Any]:
    metadata = incoming_message.metadata if isinstance(incoming_message.metadata, dict) else {}
    request_context = payload.get("mcagent_context_request") if isinstance(payload.get("mcagent_context_request"), dict) else {}
    collection_target = str(metadata.get("collection_target") or request_context.get("collection_target") or question or "").strip()
    focus = _mcagent_context_focus(str(request_context.get("focus") or question), collection_target)
    add_trace(
        "delegate",
        "suppressed_for_context_reply",
        {
            "reason": "CrawlerAgent asked MCagent for local context only. MCagent replies with local context over the same From-Content-To message bus and must not recursively start another Crawler job.",
            "request_message_id": incoming_message.message_id,
        },
    )
    add_trace("retrieve", "mcagent_context_light_search", {"focus": focus, "transport": "_send_agent_message"})
    started = time.time()
    retrieval_error = ""
    try:
        rough_results = Retriever(config).search(focus, top_k=16, session_summary=_session_summary(payload))
    except Exception as exc:  # noqa: BLE001 - context reply should report objective local retrieval blockers.
        rough_results = []
        retrieval_error = f"{type(exc).__name__}: {exc}"
        add_trace("retrieve", "mcagent_context_light_error", {"error": retrieval_error[:500]})
    selected = _dedupe_results(rough_results, 8)
    selected = _filter_mcagent_context_evidence(focus, selected, {"verdict": "ok"})
    focus_terms = _focus_terms_for_question(focus)
    source_dicts = [_result_to_dict(item) for item in selected]
    if selected:
        evidence_lines: list[str] = []
        for item in selected[:6]:
            lines = _evidence_lines_from_text(item.text, focus_terms, limit=3)
            if not lines:
                lines = [item.text[:220]]
            clean_lines = [_clean_evidence_line(line) for line in lines]
            clean_lines = [line for line in clean_lines if line][:2]
            if clean_lines:
                evidence_lines.append(f"- {item.title}: {'；'.join(clean_lines)[:420]}")
            else:
                evidence_lines.append(f"- {item.title}: {item.source_path}")
        answer = (
            "MCagent 已通过 From-Content-To 消息收到 CrawlerAgent 的本地上下文请求。\n\n"
            f"检索主题：{focus}\n\n"
            "本地已有证据候选：\n"
            + "\n".join(evidence_lines)
            + "\n\n可能仍需 CrawlerAgent 自行核查/补充：公开项目页、版本/下载页、完整模组列表、任务线/玩法路线、更新日志或配置说明。"
        )
    else:
        blocker = f"\n本地检索阻塞：{retrieval_error}\n" if retrieval_error else ""
        answer = (
            "MCagent 已通过 From-Content-To 消息收到 CrawlerAgent 的本地上下文请求。\n\n"
            f"检索主题：{focus}\n\n"
            f"{blocker}"
            "本地资料库没有找到可直接交付的证据候选。CrawlerAgent 应自行去公开来源补充项目页、版本/下载页、模组列表、玩法路线和配置说明。"
        )
    add_trace(
        "retrieve",
        "mcagent_context_light_done",
        {"results": len(rough_results), "selected": len(selected), "elapsed_ms": round((time.time() - started) * 1000)},
    )
    response = {
        "answer": answer,
        "sources": source_dicts,
        "context": format_context(selected) if selected else "",
        "agent": "mcagent_rag",
        "evidence": {
            "verdict": "ok" if selected else "insufficient",
            "selected_count": len(selected),
            "candidate_count": len(rough_results),
            "transport": "_send_agent_message",
            "mode": "mcagent_context_reply",
        },
        "session_id": str(payload.get("session_id") or incoming_message.conversation_id or ""),
    }
    return _with_trace(response, trace)


def _is_collection_request_agent_message(message: AgentMessage) -> bool:
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    return message.to_agent_id == "crawler_agent" and (
        message.intent == "collection_request" or str(metadata.get("tool") or "") in {"collection_request", "delegate_crawler"}
    )


def _handle_direct_answer_route(
    *,
    config: AppConfig,
    agent: str,
    model: str,
    original_question: str,
    question: str,
    tool_decision: dict[str, Any],
    route_confirmation: dict[str, Any],
    action_plan: list[dict[str, Any]],
    executor: AgentToolExecutor,
    run: Any,
    session_summary: dict[str, Any],
    add_trace: Any,
) -> dict[str, Any]:
    completeness = _tool_route_completeness_review(
        config,
        agent=agent,
        model=model,
        original_question=original_question,
        selected_tool="direct_answer",
        tool_answer=str(tool_decision.get("reason") or ""),
        tool_decision=tool_decision,
        route_confirmation=route_confirmation,
        action_plan=action_plan,
    )
    if completeness.get("missing_side_effect"):
        add_trace("plan", "route_completeness_gap", completeness)
        if str(completeness.get("tool") or "").strip() == "delegate_crawler" and str(completeness.get("action") or "").strip() == "execute_selected_tool":
            collection_target = str(completeness.get("collection_target") or tool_decision.get("collection_target") or original_question or question).strip()
            add_trace(
                "decide",
                "direct_answer_missing_side_effect_not_executed",
                {
                    "reason": "Completeness review found a possible missing side effect, but runtime will not add delegate_crawler after the Agent selected direct_answer.",
                    "collection_target": collection_target,
                    "required_agent_selection": "delegate_crawler or planned_workflow with delegate_crawler",
                },
            )
    response = executor.direct_answer(run, session_summary=session_summary)
    _append_session(
        {"session_id": run.payload.get("session_id") if hasattr(run, "payload") else None},
        original_question,
        str(response.get("answer") or ""),
        list(response.get("sources") or []),
    )
    return response


def _handle_crawler_audit_route(
    *,
    agent: str,
    original_question: str,
    tool_decision: dict[str, Any],
    route_confirmation: dict[str, Any],
    executor: AgentToolExecutor,
    run: Any,
    session_summary: dict[str, Any],
    trace: list[dict[str, Any]],
    add_trace: Any,
) -> dict[str, Any]:
    audit_confirmation = _reuse_route_confirmation(
        route_confirmation,
        proposed_tool="crawler_audit",
        proposed_goal=str(tool_decision.get("reason") or "Read recent Crawler audit details."),
    )
    add_trace("audit", "next_step_confirmed", audit_confirmation)
    if not bool(audit_confirmation.get("proceed", True)):
        suggested_tool = str(audit_confirmation.get("suggested_tool") or audit_confirmation.get("tool") or "").strip()
        if suggested_tool in {"answer", "direct_answer"}:
            return executor.direct_answer(run, session_summary=session_summary, mode="direct_after_audit_cancelled")
    audit_answer = _recent_crawler_audit_answer(original_question) or {
        "answer": "我没有在最近任务历史中找到能匹配这轮问题的 Crawler 自审记录。",
        "sources": [],
        "context": "",
        "agent": agent,
    }
    add_trace("answer", "recent_crawler_audit", {"source": "jobs", "job_id": (audit_answer.get("job") or {}).get("id")})
    return _with_trace(audit_answer, trace)


def _handle_status_route(
    *,
    route_confirmation: dict[str, Any],
    executor: AgentToolExecutor,
    run: Any,
    add_trace: Any,
) -> dict[str, Any]:
    status_confirmation = _reuse_route_confirmation(
        route_confirmation,
        proposed_tool="status",
        proposed_goal="读取采集、入库和后台任务状态。",
    )
    add_trace("status", "next_step_confirmed", status_confirmation)
    return executor.status(run)


def _handle_temporary_extract_route(
    *,
    config: AppConfig,
    agent: str,
    model: str,
    temperature: float,
    max_tokens: int | None,
    original_question: str,
    question: str,
    tool_decision: dict[str, Any],
    route_confirmation: dict[str, Any],
    executor: AgentToolExecutor,
    run: Any,
    session_summary: dict[str, Any],
    trace: list[dict[str, Any]],
    add_trace: Any,
) -> dict[str, Any]:
    collection_question = str(tool_decision.get("collection_target") or original_question or question).strip()
    extract_confirmation = _reuse_route_confirmation(
        route_confirmation,
        proposed_tool="temporary_extract",
        proposed_goal=f"临时读取并总结公开网页，不保存到本地：{collection_question}",
    )
    add_trace("extract", "next_step_confirmed", extract_confirmation)
    if not bool(extract_confirmation.get("proceed", True)):
        suggested_tool = str(extract_confirmation.get("suggested_tool") or extract_confirmation.get("tool") or "").strip()
        if suggested_tool == "delegate_crawler":
            add_trace(
                "extract",
                "delegate_suggestion_not_executed",
                {
                    "reason": "Temporary extract confirmation suggested delegate_crawler, but runtime will not add a persistent side effect after the Agent selected temporary_extract.",
                    "required_agent_selection": "delegate_crawler or planned_workflow with delegate_crawler",
                },
            )
        return executor.direct_answer(run, session_summary=session_summary, mode="direct_after_extract_cancelled")

    extractor = CrawlerTemporaryExtractService()

    def summarize(question_text: str, url: str, page_text: str) -> str:
        return _generate_temporary_extract_summary(
            config,
            question_text,
            url,
            page_text,
            model,
            temperature,
            max_tokens,
        )

    def review_summarize(question_text: str, url: str, page_text: str, first_answer: str, missing_terms: list[str], excerpt: str) -> str:
        add_trace(
            "extract",
            "temporary_summary_reviewing",
            {
                "missing_terms": missing_terms,
                "first_answer_chars": len(first_answer),
                "excerpt_chars": len(excerpt),
                "saved_to_local": False,
            },
        )
        return _review_temporary_extract_summary(
            config,
            question_text,
            url,
            page_text,
            first_answer,
            missing_terms,
            excerpt,
            model,
            temperature,
            max_tokens,
        )

    def choose_url(question_text: str, target_text: str, candidates: list[dict[str, Any]]) -> str:
        add_trace(
            "extract",
            "temporary_url_discovering",
            {
                "candidate_count": len(candidates),
                "candidates": [
                    {"title": str(item.get("title") or "")[:120], "url": item.get("url")}
                    for item in candidates[:5]
                ],
                "saved_to_local": False,
            },
        )
        selected_url = _choose_temporary_extract_url(
            config,
            question=question_text,
            collection_target=target_text,
            candidates=candidates,
            model=model,
            temperature=temperature,
        )
        add_trace("extract", "temporary_url_selected", {"url": selected_url, "saved_to_local": False})
        return selected_url

    try:
        result = extractor.run(
            question=original_question,
            collection_target=collection_question,
            summarize=summarize,
            review_summarize=review_summarize,
            choose_url=choose_url,
        )
        add_trace(
            "extract",
            "temporary_url_extracted",
            {
                "url": result.url,
                "status_code": result.status_code,
                "content_type": result.content_type,
                "text_chars": result.text_chars,
                "saved_to_local": False,
            },
        )
        return _with_trace(result.to_response(agent=agent), trace)
    except Exception as exc:  # noqa: BLE001
        add_trace("extract", "temporary_url_failed", {"error": f"{type(exc).__name__}: {exc}", "saved_to_local": False})
        return _with_trace(
            {
                "answer": f"CrawlerAgent 临时读取网页失败，且没有保存到本地。失败原因：{type(exc).__name__}: {exc}",
                "sources": [],
                "context": "",
                "agent": agent,
                "temporary_extract": {"saved_to_local": False, "error": f"{type(exc).__name__}: {exc}"},
            },
            trace,
        )


def _handle_local_corpus_inventory_route(
    *,
    config: AppConfig,
    payload: dict[str, Any],
    agent: str,
    model: str,
    original_question: str,
    question: str,
    tool_decision: dict[str, Any],
    route_confirmation: dict[str, Any],
    action_plan: list[dict[str, Any]],
    executor: AgentToolExecutor,
    run: Any,
    session_summary: dict[str, Any],
    trace: list[dict[str, Any]],
    add_trace: Any,
) -> dict[str, Any]:
    inventory_confirmation = _reuse_route_confirmation(
        route_confirmation,
        proposed_tool="local_corpus_inventory",
        proposed_goal=str(tool_decision.get("reason") or "Inspect local corpus coverage and representative indexed documents."),
    )
    add_trace("retrieve", "inventory_next_step_confirmed", inventory_confirmation)
    if not bool(inventory_confirmation.get("proceed", True)):
        return executor.direct_answer(run, session_summary=session_summary, mode="direct_after_inventory_cancelled")

    add_trace("retrieve", "inventory_scanning", {"db_path": str(config.paths.db_path)})
    inventory = _local_corpus_inventory_answer(config, question)
    add_trace(
        "retrieve",
        "inventory_done",
        {
            "sources": len(inventory.get("sources") or []),
            "context_chars": len(str(inventory.get("context") or "")),
        },
    )
    completeness = _tool_route_completeness_review(
        config,
        agent=agent,
        model=model,
        original_question=original_question,
        selected_tool="local_corpus_inventory",
        tool_answer=str(inventory.get("answer") or ""),
        tool_decision=tool_decision,
        route_confirmation=inventory_confirmation,
        action_plan=action_plan,
    )
    if completeness.get("missing_side_effect"):
        add_trace("plan", "route_completeness_gap", completeness)
        if (
            str(completeness.get("tool") or "").strip() == "delegate_crawler"
            and str(completeness.get("action") or "").strip() == "execute_selected_tool"
            and _agent_selected_delegate(action_plan, "local_corpus_inventory", tool_decision)
        ):
            collection_question = str(completeness.get("collection_target") or tool_decision.get("collection_target") or original_question or question).strip()
            delegation = _prepare_and_start_crawler_delegation(
                config,
                payload,
                active_agent=agent,
                model=model,
                original_question=original_question,
                current_question=question,
                collection_question=collection_question,
                session_summary=session_summary,
                gap_summary=str(inventory.get("answer") or "")[:4000],
                planning_instruction=(
                    "MCagent already ran the Agent-selected local inventory tool. "
                    "CrawlerAgent should read the inventory summary, identify the remaining gaps itself, collect public usable sources, self-audit them, and deliver to the requested target."
                ),
                delivery_target=str(completeness.get("delivery_target") or tool_decision.get("delivery_target") or payload.get("delivery_target") or "MCagent/RAG").strip(),
                add_trace=add_trace,
                action_plan=action_plan,
            )
            answer = str(inventory.get("answer") or "").rstrip() + delegation.note
            source_dicts = list(inventory.get("sources") or [])
            _append_session(payload, original_question, answer, source_dicts)
            response = {
                "answer": answer,
                "sources": source_dicts,
                "context": str(inventory.get("context") or ""),
                "agent": agent,
                **_optional_delegation_payload(delegation),
            }
            add_trace("done", "response_ready", {"sources": len(source_dicts), "delegated": True})
            return _with_trace(response, trace)
        if str(completeness.get("tool") or "").strip() == "delegate_crawler":
            add_trace(
                "plan",
                "inventory_missing_side_effect_not_executed",
                {
                    "reason": "Completeness review suggested delegate_crawler, but the original Agent-selected inventory route did not select that side-effect tool.",
                    "required_agent_selection": "delegate_crawler or planned_workflow with delegate_crawler",
                },
            )
    _append_session(payload, original_question, str(inventory.get("answer") or ""), list(inventory.get("sources") or []))
    add_trace("done", "response_ready", {"sources": len(inventory.get("sources") or [])})
    return _with_trace(inventory, trace)


def _handle_mcagent_inventory_planned_workflow_route(
    *,
    config: AppConfig,
    payload: dict[str, Any],
    agent: str,
    model: str,
    original_question: str,
    question: str,
    tool_decision: dict[str, Any],
    action_plan: list[dict[str, Any]],
    session_summary: dict[str, Any],
    trace: list[dict[str, Any]],
    add_trace: Any,
) -> dict[str, Any]:
    add_trace(
        "plan",
        "executing_agent_selected_step",
        {
            "tool": "local_corpus_inventory",
            "reason": "planned_workflow action_plan selected by the Agent; runtime is executing the selected tool rather than substituting a RAG path.",
        },
    )
    inventory = _local_corpus_inventory_answer(config, question)
    add_trace(
        "retrieve",
        "inventory_done",
        {
            "sources": len(inventory.get("sources") or []),
            "context_chars": len(str(inventory.get("context") or "")),
        },
    )
    answer = str(inventory.get("answer") or "").strip()
    source_dicts = list(inventory.get("sources") or [])
    context = str(inventory.get("context") or "")
    if _action_plan_has_tool(action_plan, "crawler_audit"):
        add_trace(
            "plan",
            "executing_agent_selected_step",
            {
                "tool": "crawler_audit",
                "reason": "planned_workflow action_plan selected by the Agent; runtime is reading the objective recent Crawler audit before delegation.",
            },
        )
        audit_answer = _recent_crawler_audit_answer(original_question)
        if audit_answer:
            add_trace("audit", "recent_crawler_audit", {"source": "jobs", "job_id": (audit_answer.get("job") or {}).get("id")})
            audit_text = str(audit_answer.get("answer") or "").strip()
            if audit_text:
                answer = answer.rstrip() + "\n\n近期 Crawler 自审摘要：\n" + audit_text
        else:
            add_trace("audit", "recent_crawler_audit_missing", {"source": "jobs"})

    delegated_job = None
    created = False
    delegated_requested_by = ""
    delegated_delivery_target = ""
    delegated_task = ""
    delegated_handoff_brief = ""
    if _action_plan_has_tool(action_plan, "delegate_crawler"):
        collection_question = str(tool_decision.get("collection_target") or original_question or question).strip()
        add_trace(
            "plan",
            "executing_agent_selected_step",
            {
                "tool": "delegate_crawler",
                "reason": "planned_workflow action_plan selected by the Agent after local corpus inventory.",
            },
        )
        delegation = _prepare_and_start_crawler_delegation(
            config,
            payload,
            active_agent=agent,
            model=model,
            original_question=original_question,
            current_question=question,
            collection_question=collection_question,
            session_summary=session_summary,
            gap_summary=answer[:4000],
            planning_instruction=(
                "MCagent first inspected local corpus coverage because its Agent-selected planned_workflow requested local_corpus_inventory. "
                "CrawlerAgent should read the inventory summary, decide which gaps are real, collect citeable public/local evidence, self-audit sources, and deliver usable artifacts to MCagent/RAG."
            ),
            delivery_target=str(tool_decision.get("delivery_target") or payload.get("delivery_target") or "MCagent/RAG").strip(),
            add_trace=add_trace,
            action_plan=action_plan,
        )
        delegated_job = delegation.job
        created = delegation.created
        delegated_requested_by = delegation.plan.requested_by
        delegated_delivery_target = delegation.plan.delivery_target
        delegated_task = delegation.plan.collection_question
        delegated_handoff_brief = delegation.plan.handoff_brief
        answer = answer.rstrip() + delegation.note

    plan_text = _format_action_plan_for_user(action_plan)
    if plan_text and not answer.lstrip().startswith("执行计划："):
        answer = plan_text + "\n\n" + answer
    _append_session(payload, original_question, answer, source_dicts)
    response: dict[str, Any] = {
        "answer": answer,
        "sources": source_dicts,
        "context": context,
        "agent": agent,
    }
    if delegated_task:
        response["delegation"] = {
            "requested_by": delegated_requested_by or "user_via_mcagent",
            "delivery_target": delegated_delivery_target or "MCagent/RAG",
            "task": delegated_task or str(tool_decision.get("collection_target") or original_question or question).strip(),
            "handoff_brief": delegated_handoff_brief,
        }
    if delegated_job is not None:
        response["job"] = _job_to_dict(delegated_job)
        response["collaboration"] = _collaboration_dialog_for(
            delegated_task or question,
            delegated_job,
            created,
            requested_by=delegated_requested_by or "user_via_mcagent",
            delivery_target=delegated_delivery_target or "MCagent/RAG",
        )
    add_trace("done", "response_ready", {"sources": len(source_dicts), "planned_workflow_executed": True})
    return _with_trace(response, trace)


def _handle_delegate_crawler_route(
    *,
    config: AppConfig,
    payload: dict[str, Any],
    agent: str,
    model: str,
    original_question: str,
    question: str,
    tool_decision: dict[str, Any],
    route_confirmation: dict[str, Any],
    action_plan: list[dict[str, Any]],
    collection_request_agent_message: bool,
    router: LlmAgentToolRouterService,
    executor: AgentToolExecutor,
    run: Any,
    session_summary: dict[str, Any],
    trace: list[dict[str, Any]],
    add_trace: Any,
) -> dict[str, Any]:
    collection_question = str(tool_decision.get("collection_target") or original_question or question).strip()
    if collection_request_agent_message:
        delegate_confirmation = route_confirmation
    else:
        delegate_confirmation = router.confirm_next_step(
            config,
            payload,
            agent=agent,
            model=model,
            original_question=original_question,
            session_summary=session_summary,
            proposed_tool="delegate_crawler",
            proposed_goal=f"把采集目标交给 CrawlerAgent：{collection_question}",
            context={"collection_target": collection_question, "delivery_target": tool_decision.get("delivery_target") or ""},
        )
    add_trace("delegate", "next_step_confirmed", delegate_confirmation)
    if not bool(delegate_confirmation.get("proceed", True)):
        suggested_tool = str(delegate_confirmation.get("suggested_tool") or delegate_confirmation.get("tool") or "").strip()
        if (
            agent == "crawler_agent"
            and suggested_tool == "mcagent_context"
            and _action_plan_has_tool(action_plan, "mcagent_context")
            and _action_plan_has_tool(action_plan, "delegate_crawler")
        ):
            add_trace(
                "delegate",
                "confirmation_reconciled",
                {
                    "reason": "The confirmation model asked to run mcagent_context first; this planned Crawler job already runs mcagent_context as its first internal task, so keep the job instead of cancelling into a chat-turn answer.",
                    "suggested_tool": suggested_tool,
                    "action_plan": action_plan,
                },
            )
        elif suggested_tool != "delegate_crawler":
            return executor.direct_answer(run, session_summary=session_summary, mode="direct_after_delegate_cancelled")

    delegation = _prepare_and_start_crawler_delegation(
        config,
        payload,
        active_agent=agent,
        model=model,
        original_question=original_question,
        current_question=question,
        collection_question=collection_question,
        session_summary=session_summary,
        planning_instruction=str(tool_decision.get("planning_instruction") or "").strip(),
        delivery_target=str(tool_decision.get("delivery_target") or "").strip(),
        add_trace=add_trace,
        action_plan=action_plan,
    )
    if delegation.job is None:
        answer = ""
    elif agent == "crawler_agent" and delegation.plan.requested_by == "user":
        answer = "我是 CrawlerAgent。采集任务已启动。" if delegation.created else "我是 CrawlerAgent。已有采集任务在运行。"
    else:
        answer = "Crawler 多源采集任务已启动。" if delegation.created else "Crawler 已有任务在运行。"
    answer += delegation.note
    return _with_trace(
        {
            "answer": answer,
            "sources": [],
            "agent": agent,
            **_optional_delegation_payload(delegation),
        },
        trace,
    )


def _handle_crawler_action_plan_delegate_route(
    *,
    config: AppConfig,
    payload: dict[str, Any],
    agent: str,
    model: str,
    original_question: str,
    question: str,
    tool_decision: dict[str, Any],
    action_plan: list[dict[str, Any]],
    session_summary: dict[str, Any],
    trace: list[dict[str, Any]],
    add_trace: Any,
) -> dict[str, Any]:
    collection_question = str(tool_decision.get("collection_target") or original_question or question).strip()
    context_answer = ""
    context_gap_summary = ""
    context_sources: list[dict[str, Any]] = []
    if _action_plan_has_tool(action_plan, "mcagent_context"):
        add_trace(
            "plan",
            "executing_agent_selected_step",
            {
                "tool": "mcagent_context",
                "reason": "CrawlerAgent selected mcagent_context before delegate_crawler; runtime executes that selected no-persistence inter-agent step before starting the background collection job.",
                "action_plan": action_plan,
            },
        )
        context_payload = dict(payload)
        context_payload.update(
            {
                "agent": "crawler_agent",
                "query": collection_question or original_question,
                "question": original_question,
                "collection_target": collection_question or original_question,
                "source_question": original_question,
                "delivery_target": str(tool_decision.get("delivery_target") or payload.get("delivery_target") or "MCagent/RAG"),
                "model": model,
            }
        )
        context_plan = {
            "delivery_target": str(tool_decision.get("delivery_target") or payload.get("delivery_target") or "MCagent/RAG"),
            "collection_target": collection_question or original_question,
        }
        context_result = _run_mcagent_context_tool(config, context_payload, context_plan, session_summary)
        context_answer = str(context_result.get("mcagent_answer") or "").strip()
        context_gap_summary = str(context_result.get("mcagent_gap_summary") or context_answer or "").strip()
        context_sources = [
            {
                "title": "MCagent context reply",
                "source_path": str(context_result.get("export_dir") or ""),
                "snippet": _tail_text(context_gap_summary, 900),
                "source": "mcagent_context",
            }
        ] if context_gap_summary else []
        add_trace(
            "plan",
            "mcagent_context_completed",
            {
                "source": "mcagent_context",
                "local_sources": int(context_result.get("mcagent_source_count") or 0),
                "export_dir": str(context_result.get("export_dir") or ""),
                "transport": "_send_agent_message",
            },
        )
    add_trace(
        "plan",
        "executing_agent_selected_step",
        {
            "tool": "delegate_crawler",
            "reason": "CrawlerAgent selected a planned_workflow with a Crawler background collection step; runtime preserves the selected action_plan for the background Crawler job.",
            "action_plan": action_plan,
        },
    )
    delegation = _prepare_and_start_crawler_delegation(
        config,
        payload,
        active_agent=agent,
        model=model,
        original_question=original_question,
        current_question=question,
        collection_question=collection_question,
        session_summary=session_summary,
        gap_summary=context_gap_summary,
        planning_instruction=(
            "Execute the CrawlerAgent-selected action_plan inside the background Crawler job. "
            "Do not collapse inter-agent steps into a chat-turn RAG shortcut; if synchronous mcagent_context has already collected local context, read that gap summary and continue collection from it."
        ),
        delivery_target=str(tool_decision.get("delivery_target") or payload.get("delivery_target") or "").strip(),
        add_trace=add_trace,
        action_plan=action_plan,
    )
    answer_parts: list[str] = []
    if context_gap_summary:
        answer_parts.append(
            "我是 CrawlerAgent。已先通过 From-Content-To 向 MCagent 询问本地资料缺口。\n\n"
            "MCagent 返回的本地上下文/缺口：\n"
            + _tail_text(context_answer or context_gap_summary, 1400)
        )
    answer_parts.append("已按本轮计划启动后台采集任务。" if delegation.job is not None else "")
    answer = "\n\n".join(part for part in answer_parts if part).strip()
    answer += delegation.note
    return _with_trace(
        {
            "answer": answer,
            "sources": context_sources,
            "agent": agent,
            **_optional_delegation_payload(delegation, selected_action_plan=action_plan),
        },
        trace,
    )


def _handle_no_retrieval_results(
    *,
    config: AppConfig,
    payload: dict[str, Any],
    agent: str,
    model: str,
    original_question: str,
    question: str,
    tool_decision: dict[str, Any],
    action_plan: list[dict[str, Any]],
    planned_delegate: bool,
    evidence_question: str,
    session_summary: dict[str, Any],
    trace: list[dict[str, Any]],
    add_trace: Any,
) -> dict[str, Any]:
    if planned_delegate:
        collection_question = str(tool_decision.get("collection_target") or original_question or question).strip()
        gap_summary = (
            "MCagent/RAG local retrieval produced no usable evidence for this requested context check.\n"
            f"Evidence question: {evidence_question}\n"
            "CrawlerAgent should treat the local gap as broad and collect public data that can fill MCagent/RAG."
        )
        delegation = _prepare_and_start_crawler_delegation(
            config,
            payload,
            active_agent=agent,
            model=model,
            original_question=original_question,
            current_question=question,
            collection_question=collection_question,
            session_summary=session_summary,
            gap_summary=gap_summary,
            planning_instruction=(
                "No local MCagent/RAG evidence was available after the planned context check. "
                "CrawlerAgent should independently plan public-source collection and deliver results to MCagent/RAG."
            ),
            delivery_target=str(tool_decision.get("delivery_target") or payload.get("delivery_target") or "MCagent/RAG").strip(),
            add_trace=add_trace,
            action_plan=action_plan,
        )
        base_answer = "MCagent/RAG 本地没有检索到可用证据；这个空缺会作为后续采集上下文。"
        if agent == "crawler_agent":
            answer = _crawler_agent_context_delegation_answer(base_answer, delegation.note)
        else:
            answer = base_answer + delegation.note
        return _with_trace(
            {
                "answer": answer,
                "sources": [],
                "context": "",
                "agent": agent,
                **_optional_delegation_payload(delegation),
            },
            trace,
        )
    add_trace("done", "insufficient_evidence", {"reason": "no_retrieval_results", "delegated": False})
    answer = (
        "本地资料库没有检索到可用证据。本轮不会自动通知 Crawler。\n\n"
        "如果需要补库，请明确让 MCagent 转达给 CrawlerAgent，或切换到 CrawlerAgent 直接委托采集。"
    )
    return _with_trace({"answer": answer, "sources": [], "context": "", "agent": agent}, trace)


def _select_mcagent_rag_evidence_or_respond(
    *,
    config: AppConfig,
    payload: dict[str, Any],
    agent: str,
    model: str,
    original_question: str,
    question: str,
    tool_decision: dict[str, Any],
    action_plan: list[dict[str, Any]],
    planned_delegate: bool,
    evidence_question: str,
    rough_results: list[SearchResult],
    retrieval_plan: Any | None,
    final_k: int,
    session_summary: dict[str, Any],
    trace: list[dict[str, Any]],
    add_trace: Any,
) -> RagEvidenceRouteResult:
    evidence_result = EvidenceWorkflowService(
        prefer_parent_topic_results=_prefer_parent_topic_results,
        modpack_manifest_results=_modpack_manifest_results,
        supplement_local_modpack_manifest_results=_supplement_local_modpack_manifest_results,
        supplement_project_keyword_results=_supplement_project_keyword_results,
        supplement_raw_html_results=lambda cfg, query, results, limit: _supplement_raw_html_results(cfg, query, results, limit=limit),
        ensure_modpack_mod_list_context=_ensure_modpack_mod_list_context,
        fallback_theme_results=_fallback_theme_results,
        dedupe_results=_dedupe_results,
    ).select(
        config,
        evidence_question=evidence_question,
        rough_results=rough_results,
        retrieval_plan=retrieval_plan,
        final_k=final_k,
        add_trace=add_trace,
    )
    selected = evidence_result.selected
    evidence_report = evidence_result.report
    if _needs_general_grounded_answer(evidence_question):
        before_supplement = len(selected)
        selected = _supplement_project_keyword_results(config, evidence_question, selected, final_k)
        if len(selected) != before_supplement:
            add_trace(
                "decide",
                "guide_mechanics_evidence_supplemented",
                {"before": before_supplement, "after": len(selected)},
            )
    selected_before_entity_filter = len(selected)
    selected = _filter_answer_evidence_with_recovery(evidence_question, selected, rough_results, final_k)
    if selected_before_entity_filter and selected and len(selected) != selected_before_entity_filter:
        add_trace(
            "decide",
            "entity_evidence_recovered",
            {
                "before": selected_before_entity_filter,
                "after": len(selected),
                "candidate_count": len(rough_results),
            },
        )
    if evidence_report.selected_count != len(selected):
        evidence_report.selected_count = len(selected)
        if not selected:
            evidence_report.verdict = "insufficient"
            evidence_report.reasons = _dedupe_strings(
                [*evidence_report.reasons, "Filtered retrieved evidence because it did not match the requested subject entity."]
            )
    if evidence_report.verdict != "ok":
        local_fact_answer = _local_modpack_archive_fact_answer(original_question, selected) or _local_version_install_answer(original_question, selected)
        if local_fact_answer:
            source_dicts = [_result_to_dict(item) for item in selected]
            context = format_context(selected)
            add_trace(
                "answer",
                "local_fact_answer",
                {
                    "reason": "version_install_evidence_before_general_evidence_threshold",
                    "sources": len(selected),
                    "evidence_verdict": evidence_report.verdict,
                },
            )
            sources = format_sources(selected)
            if sources and not local_fact_answer.rstrip().endswith(sources):
                local_fact_answer = local_fact_answer.rstrip() + "\n\nSources:\n" + sources
            _append_session(payload, original_question, local_fact_answer, source_dicts)
            return RagEvidenceRouteResult(
                selected=selected,
                evidence_report=evidence_report,
                response=_with_trace(
                    {
                        "answer": local_fact_answer,
                        "sources": source_dicts,
                        "context": context,
                        "agent": agent,
                        "evidence": evidence_report.to_dict(),
                    },
                    trace,
                ),
            )
    if evidence_report.verdict != "ok":
        if planned_delegate:
            collection_question = str(tool_decision.get("collection_target") or original_question or question).strip()
            gap_summary = (
                "MCagent 按计划检索本地资料，但证据筛选仍不足。\n"
                + "\n".join(f"- {reason}" for reason in evidence_report.reasons)
            )
            planning_instruction = (
                "MCagent 已先尝试本地检索，但证据不足；CrawlerAgent 应阅读 handoff_brief、mcagent_gap_summary "
                "和会话摘要，自行判断真正缺口、规划来源，采集后按 MCagent/RAG 可检索格式入库。"
            )
            delegation = _prepare_and_start_crawler_delegation(
                config,
                payload,
                active_agent=agent,
                model=model,
                original_question=original_question,
                current_question=question,
                collection_question=collection_question,
                session_summary=session_summary,
                gap_summary=gap_summary,
                planning_instruction=planning_instruction,
                delivery_target=str(tool_decision.get("delivery_target") or payload.get("delivery_target") or "").strip(),
                add_trace=add_trace,
                action_plan=action_plan,
            )
            answer = _insufficient_evidence_answer(question)
            answer += "\n\nMCagent 已按计划把完整上下文交接给 CrawlerAgent。"
            answer += delegation.note
            return RagEvidenceRouteResult(
                selected=selected,
                evidence_report=evidence_report,
                response=_with_trace(
                    {
                        "answer": answer,
                        "sources": [_result_to_dict(item) for item in selected],
                        "context": "",
                        "agent": agent,
                        "evidence": evidence_report.to_dict(),
                        **_optional_delegation_payload(delegation),
                    },
                    trace,
                ),
            )
        answer = _insufficient_evidence_answer(question)
        answer += "\n\n证据判断：\n" + "\n".join(f"- {reason}" for reason in evidence_report.reasons)
        answer += (
            "\n\n本轮不会自动通知 Crawler。只有当 Agent 在工具选择或 planned workflow 阶段明确选择 Crawler 委托时，"
            "才会启动采集任务。"
        )
        return RagEvidenceRouteResult(
            selected=selected,
            evidence_report=evidence_report,
            response=_with_trace(
                {
                    "answer": answer,
                    "sources": [_result_to_dict(item) for item in selected],
                    "context": "",
                    "agent": agent,
                    "evidence": evidence_report.to_dict(),
                },
                trace,
            ),
        )
    return RagEvidenceRouteResult(selected=selected, evidence_report=evidence_report)


def _handle_rag_answer_generation_route(
    *,
    config: AppConfig,
    payload: dict[str, Any],
    agent: str,
    model: str,
    original_question: str,
    question: str,
    retrieval_note: str,
    evidence_question: str,
    selected: list[SearchResult],
    tool_decision: dict[str, Any],
    route_confirmation: dict[str, Any],
    action_plan: list[dict[str, Any]],
    route_intent: str,
    planned_workflow: bool,
    planned_delegate: bool,
    executor: AgentToolExecutor,
    run: Any,
    session_summary: dict[str, Any],
    trace: list[dict[str, Any]],
    add_trace: Any,
) -> dict[str, Any]:
    source_dicts = [_result_to_dict(item) for item in selected]
    context = format_context(selected)
    delegated_job = None
    created = False
    delegated_requested_by = ""
    delegated_delivery_target = ""
    delegated_task = ""
    delegated_handoff_brief = ""
    if agent == "retriever_only" or bool(payload.get("no_llm")):
        answer, context = executor.retriever_only_answer(context)
    else:
        answer = _local_modpack_archive_fact_answer(original_question, selected)
        if not answer:
            answer = (
                ""
                if (_is_modpack_overview_question(original_question) or _needs_general_grounded_answer(original_question))
                else _local_version_install_answer(original_question, selected)
            )
        if answer:
            selected = _filter_fact_answer_sources(original_question, selected, answer)
            source_dicts = [_result_to_dict(item) for item in selected]
            context = format_context(selected)
            add_trace("answer", "local_fact_answer", {"reason": "deterministic_fact_evidence", "sources": len(selected)})
        if not answer:
            answer_question = _answer_question_for_user(original_question, question, retrieval_note)
            answer_confirmation = {
                "proceed": True,
                "tool": "final_answer_llm",
                "goal": f"基于已筛选证据组织最终回答：{answer_question}",
                "reason": "Evidence has already been selected inside the Agent-selected RAG workflow; generate the final answer without a duplicate LLM confirmation call.",
                "planner": "agent_route_execution",
            }
            add_trace("answer", "next_step_confirmed", answer_confirmation)
            answer, context = executor.grounded_answer(
                run,
                answer_question=answer_question,
                selected=selected,
                retrieval_note=retrieval_note,
                evidence_question=evidence_question,
                repair_question=question,
            )
        if planned_delegate:
            collection_question = str(tool_decision.get("collection_target") or original_question or question).strip()
            planning_instruction = ""
            if answer.strip():
                planning_instruction = (
                    "MCagent 已先检索本地资料并总结了现有资料与缺口；CrawlerAgent 应阅读 mcagent_gap_summary，"
                    "自行判断真正缺口、规划来源，采集后按 MCagent/RAG 可检索格式入库。"
                )
            delegation = _prepare_and_start_crawler_delegation(
                config,
                payload,
                active_agent=agent,
                model=model,
                original_question=original_question,
                current_question=question,
                collection_question=collection_question,
                session_summary=session_summary,
                gap_summary=answer[:4000] if answer.strip() else "",
                planning_instruction=planning_instruction,
                delivery_target=str(tool_decision.get("delivery_target") or payload.get("delivery_target") or "").strip(),
                add_trace=add_trace,
                action_plan=action_plan,
            )
            delegated_job = delegation.job
            created = delegation.created
            delegated_requested_by = delegation.plan.requested_by
            delegated_delivery_target = delegation.plan.delivery_target
            delegated_task = delegation.plan.collection_question
            delegated_handoff_brief = delegation.plan.handoff_brief
            if agent == "crawler_agent":
                answer = _crawler_agent_context_delegation_answer(answer, delegation.note)
            else:
                answer = answer.rstrip() + delegation.note
        elif agent == "mcagent_rag" and not bool(payload.get("no_llm")):
            completeness = _tool_route_completeness_review(
                config,
                agent=agent,
                model=model,
                original_question=original_question,
                selected_tool="local_rag_search+final_answer_llm",
                tool_answer=answer,
                tool_decision=tool_decision,
                route_confirmation=route_confirmation,
                action_plan=action_plan,
            )
            if (
                completeness.get("missing_side_effect")
                and str(completeness.get("tool") or "").strip() == "delegate_crawler"
                and str(completeness.get("action") or "").strip() == "execute_selected_tool"
            ):
                collection_question = str(completeness.get("collection_target") or tool_decision.get("collection_target") or original_question or question).strip()
                add_trace(
                    "plan",
                    "post_answer_route_completeness_gap_not_executed",
                    {
                        **completeness,
                        "reason": "Post-answer review found a possible missing delegation, but runtime will not execute a new side effect after the Agent-selected answer path.",
                        "collection_target": collection_question,
                        "required_agent_selection": "delegate_crawler or planned_workflow with delegate_crawler",
                    },
                )

    plan_text = _format_action_plan_for_user(action_plan) if planned_workflow else ""
    if plan_text and not answer.lstrip().startswith("执行计划："):
        answer = plan_text + "\n\n" + answer
    sources = format_sources(selected)
    if sources and not answer.rstrip().endswith(sources):
        answer = answer.rstrip() + "\n\n来源：\n" + sources
    protocol_review = _final_answer_protocol_review(
        config,
        agent=agent,
        model=model,
        original_question=original_question,
        answer=answer,
        tool_decision=tool_decision,
        action_plan=action_plan,
        planned_delegate=planned_delegate,
        delegated_job_started=delegated_job is not None,
    )
    if protocol_review.get("violation"):
        add_trace("answer", "protocol_violation_detected", protocol_review)
        tool = str(protocol_review.get("tool") or "").strip()
        action = str(protocol_review.get("action") or "").strip()
        side_effect_executed = False
        delegation: CrawlerDelegationRun | None = None
        if (
            tool == "delegate_crawler"
            and action == "execute_selected_tool"
            and delegated_job is None
            and _agent_selected_delegate(action_plan, route_intent, tool_decision)
        ):
            collection_question = str(
                protocol_review.get("collection_target")
                or tool_decision.get("collection_target")
                or original_question
                or question
            ).strip()
            if collection_question:
                delegation = _prepare_and_start_crawler_delegation(
                    config,
                    payload,
                    active_agent=agent,
                    model=model,
                    original_question=original_question,
                    current_question=question,
                    collection_question=collection_question,
                    session_summary=session_summary,
                    gap_summary=answer[:4000] if answer.strip() else "",
                    planning_instruction=(
                        "The final-answer protocol review found that the answer described a required crawler side effect "
                        "before the selected tool had actually run. Send one From-Content-To AgentMessage to CrawlerAgent; "
                        "CrawlerAgent must decide how to inspect, collect, judge, and store evidence."
                    ),
                    delivery_target=str(
                        protocol_review.get("delivery_target")
                        or tool_decision.get("delivery_target")
                        or payload.get("delivery_target")
                        or "MCagent/RAG"
                    ).strip(),
                    add_trace=add_trace,
                    action_plan=action_plan,
                )
                delegated_job = delegation.job
                created = delegation.created
                delegated_requested_by = delegation.plan.requested_by
                delegated_delivery_target = delegation.plan.delivery_target
                delegated_task = delegation.plan.collection_question
                delegated_handoff_brief = delegation.plan.handoff_brief
                side_effect_executed = True
                add_trace(
                    "answer",
                    "protocol_violation_executed_delegation",
                    {
                        "tool": tool,
                        "action": action,
                        "job_id": delegated_job.id,
                        "job_status": delegated_job.status,
                    },
                )
        elif tool == "delegate_crawler" and action == "execute_selected_tool" and delegated_job is None:
            add_trace(
                "answer",
                "protocol_violation_side_effect_not_executed",
                {
                    "reason": "Protocol review found a possible unexecuted delegate_crawler side effect, but runtime will not start it unless the Agent selected that tool before final answer.",
                    "required_agent_selection": "delegate_crawler or planned_workflow with delegate_crawler",
                },
            )
        cleaned = _strip_unexecuted_side_effect_claim_lines(_strip_pseudo_tool_call_blocks(answer))
        if cleaned != answer or action == "execute_selected_tool":
            add_trace(
                "answer",
                "pseudo_tool_text_removed",
                {
                    "reason": protocol_review.get("reason"),
                    "tool": tool,
                    "action": action,
                    "side_effect_executed": side_effect_executed,
                },
            )
            answer = cleaned or "模型最终回答包含未执行的工具调用文本，运行时已阻止它作为普通答案返回。本轮没有启动新的副作用工具。"
            if side_effect_executed and delegation is not None:
                answer = (cleaned or "The model final answer contained an unexecuted tool-call claim, so the runtime removed that text before returning the answer.").rstrip()
                answer = answer + "\n\nMCagent sent the required task to CrawlerAgent through the From-Content-To message channel. " + delegation.note
            elif not cleaned:
                answer = "The model final answer contained an unexecuted tool-call claim, so the runtime removed that text before returning the answer."
    _append_session(payload, original_question, answer, source_dicts)
    response: dict[str, Any] = {"answer": answer, "sources": source_dicts, "context": context, "agent": agent}
    if delegated_job is not None:
        response["job"] = _job_to_dict(delegated_job)
        response["collaboration"] = _collaboration_dialog(question, delegated_job, created, reason="模型回答暴露资料缺口，MCagent 追加多源补库。")
        if delegated_requested_by or delegated_delivery_target or delegated_task or delegated_handoff_brief:
            response["delegation"] = {
                "requested_by": delegated_requested_by or "mcagent",
                "delivery_target": delegated_delivery_target or "MCagent/RAG",
                "task": delegated_task or question,
                "handoff_brief": delegated_handoff_brief,
            }
    add_trace("done", "response_ready", {"sources": len(source_dicts)})
    return _with_trace(response, trace)


def _reuse_route_confirmation(route_confirmation: dict[str, Any], *, proposed_tool: str, proposed_goal: str) -> dict[str, Any]:
    value = dict(route_confirmation or {})
    current_tool = str(value.get("tool") or value.get("suggested_tool") or "").strip()
    if current_tool and current_tool != proposed_tool:
        value = {
            "proceed": True,
            "tool": proposed_tool,
            "goal": proposed_goal,
            "reason": "Runtime route changed after side-effect boundary review; use the side-effect-free tool selected by that review without another LLM call.",
        }
    else:
        value.setdefault("proceed", True)
        value.setdefault("tool", proposed_tool)
        value.setdefault("goal", proposed_goal)
        value.setdefault("reason", "Reusing the Agent route confirmation for this already-selected next step.")
    value["reused_route_confirmation"] = True
    return value


def _action_plan_has_tool(action_plan: list[dict[str, Any]], tool: str) -> bool:
    wanted = tool.strip().lower()
    return any(str(step.get("tool") or "").strip().lower() == wanted for step in action_plan)


def _format_action_plan_for_user(action_plan: list[dict[str, Any]]) -> str:
    if not action_plan:
        return ""
    lines = ["执行计划："]
    for index, step in enumerate(action_plan[:8], start=1):
        goal = str(step.get("goal") or "").strip()
        tool = str(step.get("tool") or "").strip()
        if goal and tool:
            lines.append(f"{index}. {goal}（工具：{tool}）")
        elif goal:
            lines.append(f"{index}. {goal}")
        elif tool:
            lines.append(f"{index}. 调用 {tool}")
    return "\n".join(lines).strip()


PSEUDO_TOOL_CALL_PATTERN = re.compile(
    r"(?<![\w.])(?P<tool>delegate_crawler|mcagent_context|local_corpus_inventory|local_rag_search|crawler_audit|temporary_extract|status)\s*\(",
    flags=re.I,
)


def _answer_contains_pseudo_tool_call(answer: str) -> bool:
    return bool(PSEUDO_TOOL_CALL_PATTERN.search(str(answer or "")))


SIDE_EFFECT_PROMISE_PATTERN = re.compile(
    r"(?:"
    r"(?=.*(?:Agent|agent|工具|tool|CrawlerAgent|MCagent|MCAgent|另一个\s*Agent|后台|任务|工作流))"
    r"(?=.*(?:已|已经|正在|现在|马上|将|会|为你|给你|帮你))"
    r"(?=.*(?:转达|转交|委托|交给|通知|启动|创建|发起|调用|执行|保存|入库|采集|抓取|爬取|补充|补齐))"
    r")",
    flags=re.I,
)


def _answer_may_claim_unexecuted_side_effect(answer: str) -> bool:
    """Return whether a final answer deserves LLM protocol review for side-effect claims."""

    text = str(answer or "")
    if not text.strip():
        return False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if len(line) < 8:
            continue
        if re.search(r"(如果|如需|可以|可让|建议|下一步|你可以|需要的话).{0,20}(Agent|工具|Crawler|MCagent)", line, flags=re.I):
            continue
        if SIDE_EFFECT_PROMISE_PATTERN.search(line):
            return True
    return False


def _strip_pseudo_tool_call_blocks(answer: str) -> str:
    text = str(answer or "")
    lines = text.splitlines()
    cleaned: list[str] = []
    skipping = False
    paren_balance = 0
    for line in lines:
        if not skipping and PSEUDO_TOOL_CALL_PATTERN.search(line):
            skipping = True
            paren_balance = line.count("(") - line.count(")")
            continue
        if skipping:
            paren_balance += line.count("(") - line.count(")")
            if paren_balance <= 0:
                skipping = False
            continue
        cleaned.append(line)
    stripped = "\n".join(cleaned).strip()
    return re.sub(r"\n{3,}", "\n\n", stripped)


def _strip_unexecuted_side_effect_claim_lines(answer: str) -> str:
    text = str(answer or "")
    cleaned: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line and _answer_may_claim_unexecuted_side_effect(line):
            continue
        cleaned.append(raw_line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned)).strip()


def _final_answer_protocol_review(
    config: AppConfig,
    *,
    agent: str,
    model: str,
    original_question: str,
    answer: str,
    tool_decision: dict[str, Any],
    action_plan: list[dict[str, Any]],
    planned_delegate: bool,
    delegated_job_started: bool = False,
) -> dict[str, Any]:
    """Ask the active Agent to judge whether a final answer violated tool protocol."""

    has_pseudo_tool_call = _answer_contains_pseudo_tool_call(answer)
    may_claim_side_effect = (not delegated_job_started) and _answer_may_claim_unexecuted_side_effect(answer)
    if not has_pseudo_tool_call and not may_claim_side_effect:
        return {"violation": False, "action": "allow", "reason": "No tool-call syntax or unexecuted side-effect claim in final answer.", "tool": ""}
    try:
        client, label = _selected_llm_client(config, model, 0.0, agent=agent, timeout_seconds=RUNTIME_REVIEW_LLM_TIMEOUT_SECONDS)
        prompt = (
            "你是当前 Agent 的最终回答协议审计器。只判断最终回答是否把工具调用写成了普通文本，"
            "或者是否声称已经/即将执行跨 Agent 交付、后台任务、采集、保存、入库等真实副作用但运行时并未执行。不要回答用户问题。\n"
            "协议：所有副作用工具调用必须在工具选择/计划阶段执行，不能在最终回答里伪造如 delegate_crawler(...) 的文本。\n"
            "协议：所有跨 Agent 沟通必须通过 AgentMessage(from_agent, content, to_agent) 真实发送，最终回答不能只用自然语言声称“已转达/会交给另一个 Agent”。\n"
            "如果 final_answer 含有伪工具调用或未执行的副作用承诺，并且用户原话或已有工具计划确实要求这个副作用，"
            "action=execute_selected_tool；如果用户没有要求该副作用，action=remove_pseudo_call，不要启动副作用；如果只是条件性建议而非承诺，action=allow。\n"
            "只输出 JSON。\n"
            f"active_agent: {agent}\n"
            f"original_user_message: {original_question}\n"
            f"tool_decision: {json.dumps(tool_decision or {}, ensure_ascii=False)}\n"
            f"action_plan: {json.dumps(action_plan or [], ensure_ascii=False)}\n"
            f"planned_delegate: {planned_delegate}\n"
            f"delegated_job_started: {delegated_job_started}\n"
            f"has_pseudo_tool_call: {has_pseudo_tool_call}\n"
            f"may_claim_unexecuted_side_effect: {may_claim_side_effect}\n"
            f"final_answer:\n{str(answer or '')[:5000]}\n"
            'JSON schema: {"violation":true, "tool":"delegate_crawler|mcagent_context|...", '
            '"action":"execute_selected_tool|remove_pseudo_call|allow", "reason":"一句理由", '
            '"collection_target":"如果要执行 delegate_crawler，写完整自然语言目标"}'
        )
        raw = client.chat(
            [
                {"role": "system", "content": "只输出合法 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=700,
        )
        value = json_object_from_llm_text(raw)
        return {
            "violation": bool(value.get("violation", True)),
            "tool": str(value.get("tool") or "").strip(),
            "action": str(value.get("action") or "remove_pseudo_call").strip(),
            "reason": str(value.get("reason") or "Final answer contained pseudo tool-call syntax.").strip()[:700],
            "collection_target": str(value.get("collection_target") or "").strip(),
            "planner": label,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "violation": True,
            "tool": "",
            "action": "remove_pseudo_call",
            "reason": f"Final answer contained tool protocol risk; protocol review failed closed: {type(exc).__name__}: {exc}",
            "collection_target": "",
            "planner": "runtime_fallback",
        }


def _tool_route_completeness_review(
    config: AppConfig,
    *,
    agent: str,
    model: str,
    original_question: str,
    selected_tool: str,
    tool_answer: str,
    tool_decision: dict[str, Any],
    route_confirmation: dict[str, Any],
    action_plan: list[dict[str, Any]],
) -> dict[str, Any]:
    """Ask the active Agent whether an early-return tool covered the whole user request."""

    try:
        client, label = _selected_llm_client(config, model, 0.0, agent=agent, timeout_seconds=RUNTIME_REVIEW_LLM_TIMEOUT_SECONDS)
        prompt = (
            "你是当前 Agent 的工具路线完整性审计器。不要回答用户问题，只判断本轮已执行工具是否覆盖了用户原始请求中的所有可执行动作。\n"
            "协议：用户、MCagent、CrawlerAgent 的跨 Agent 沟通必须通过 AgentMessage(from_agent, content, to_agent) 真实发送。"
            "如果用户请求包含多个动作，例如先盘点/检索，再交给另一个 Agent 采集/保存/入库，而 selected_tool 只完成了第一步，"
            "你必须标记 missing_side_effect=true，并给出下一步真实工具。不要按关键词触发；按语义判断请求是否真的要求副作用。\n"
            "如果用户只是询问状态、盘点本地资料、解释能力、或条件性建议，不要启动副作用。\n"
            "只输出 JSON。\n"
            f"active_agent: {agent}\n"
            f"original_user_message: {original_question}\n"
            f"selected_tool: {selected_tool}\n"
            f"tool_decision: {json.dumps(tool_decision or {}, ensure_ascii=False)}\n"
            f"route_confirmation: {json.dumps(route_confirmation or {}, ensure_ascii=False)}\n"
            f"action_plan: {json.dumps(action_plan or [], ensure_ascii=False)}\n"
            f"tool_answer_preview:\n{str(tool_answer or '')[:4000]}\n"
            'JSON schema: {"missing_side_effect":true, "tool":"delegate_crawler|...", '
            '"action":"execute_selected_tool|allow", "reason":"一句理由", '
            '"collection_target":"如果要执行 delegate_crawler，写完整自然语言目标", '
            '"delivery_target":"human|MCagent/RAG|CrawlerAgent"}'
        )
        raw = client.chat(
            [
                {"role": "system", "content": "只输出合法 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=700,
        )
        value = json_object_from_llm_text(raw)
        return {
            "missing_side_effect": bool(value.get("missing_side_effect", False)),
            "tool": str(value.get("tool") or "").strip(),
            "action": str(value.get("action") or "allow").strip(),
            "reason": str(value.get("reason") or "").strip()[:700],
            "collection_target": str(value.get("collection_target") or "").strip(),
            "delivery_target": str(value.get("delivery_target") or "").strip(),
            "planner": label,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "missing_side_effect": False,
            "tool": "",
            "action": "allow",
            "reason": f"Route completeness review failed open: {type(exc).__name__}: {exc}",
            "collection_target": "",
            "delivery_target": "",
            "planner": "runtime_fallback",
        }


def _question_looks_transport_garbled(question: str) -> bool:
    compact = re.sub(r"\s+", "", question)
    if len(compact) < 4:
        return False
    question_marks = compact.count("?")
    cjk = len(re.findall(r"[\u4e00-\u9fff]", compact))
    letters = len(re.findall(r"[A-Za-z]", compact))
    if question_marks >= 4 and cjk == 0 and question_marks > letters:
        return True
    if "?" * 4 in compact and cjk == 0:
        return True
    return False


def _recent_crawler_audit_answer(question: str) -> dict[str, Any] | None:
    terms = _primary_fact_subject_terms(question)
    with JOBS_LOCK:
        jobs = [asdict(JOBS[job_id]) for job_id in JOBS_ORDER if job_id in JOBS and JOBS[job_id].kind == "crawler"]
    for job in jobs:
        _sanitize_job_planned_tasks_for_display(job)
        job["readable"] = _job_readable_summary(job)
    if not jobs:
        return {
            "answer": "没有找到可用的 Crawler 任务自审记录。本轮只是查询历史状态，不会新开采集任务。",
            "sources": [],
            "context": "",
            "agent": "mcagent_rag",
        }
    chosen: dict[str, Any] | None = None
    candidates: list[tuple[int, dict[str, Any]]] = []
    for job in jobs:
        haystack = _crawler_job_identity_haystack(job)
        if terms and not any(term.lower() in haystack for term in terms):
            continue
        readable = job.get("readable") if isinstance(job.get("readable"), dict) else {}
        audit = readable.get("self_audit") if isinstance(readable.get("self_audit"), dict) else {}
        if not audit:
            continue
        counts = audit.get("counts") if isinstance(audit.get("counts"), dict) else {}
        activity = int(counts.get("accepted") or 0) + int(counts.get("rejected") or 0) + int(counts.get("pending_review") or 0)
        if activity <= 0 and str(job.get("status") or "") in {"stopped", "queued", "running"}:
            continue
        score = activity
        if str(job.get("status") or "") == "succeeded":
            score += 10
        elif str(job.get("status") or "") == "stopped":
            score -= 8
        if audit.get("ingest_status") in {"done", "running"}:
            score += 5
        elif audit.get("ingest_status") in {"skipped", "", None}:
            score -= 3
        candidates.append((score, job))
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        chosen = candidates[0][1]
    if chosen is None:
        return {
            "answer": "没有找到匹配这个主题且包含有效接受/拒绝记录的 Crawler 自审报告。本轮只是查询历史状态，不会新开采集任务。",
            "sources": [],
            "context": "",
            "agent": "mcagent_rag",
        }
    readable = chosen.get("readable") if isinstance(chosen.get("readable"), dict) else {}
    audit = readable.get("self_audit") if isinstance(readable.get("self_audit"), dict) else {}
    if not audit:
        return None
    counts = audit.get("counts") if isinstance(audit.get("counts"), dict) else {}
    lines = [
        f"Crawler 最近任务自审：{readable.get('target') or chosen.get('title') or chosen.get('id')}",
        f"- 任务ID：{chosen.get('id')}；状态：{chosen.get('status')}",
        f"- 统计：接受 {counts.get('accepted') or 0} 个，拒绝 {counts.get('rejected') or 0} 个，待复核 {counts.get('pending_review') or 0} 个；入库状态：{audit.get('ingest_status') or 'skipped'}",
    ]
    if audit.get("ingest_note"):
        lines.append(f"- 入库判断：{audit.get('ingest_note')}")

    def append_sources(title: str, items: Any, reason_keys: tuple[str, ...]) -> None:
        if not isinstance(items, list) or not items:
            lines.append(f"- {title}：无")
            return
        lines.append(f"- {title}：")
        for item in items[:6]:
            if not isinstance(item, dict):
                continue
            reason = next((str(item.get(key) or "") for key in reason_keys if item.get(key)), "")
            query = str(item.get("query") or item.get("url") or "").strip()
            status = str(item.get("status") or "").strip()
            status_part = f"；状态：{status}" if status else ""
            query_part = f"；目标：{query}" if query else ""
            reason_part = f"；原因：{reason}" if reason else ""
            lines.append(f"  - {item.get('source') or 'source'}{status_part}{query_part}{reason_part}")

    append_sources("接受来源", audit.get("accepted_sources"), ("accepted_reason", "reason", "next_action"))
    append_sources("拒绝/受限来源", audit.get("rejected_sources"), ("rejected_reason", "reason", "next_action"))
    append_sources("待复核来源", audit.get("pending_review_sources"), ("reason", "next_action"))
    if audit.get("principle"):
        lines.append(f"- 原则：{audit.get('principle')}")
    return {
        "answer": "\n".join(lines),
        "sources": [],
        "context": "",
        "agent": "mcagent_rag",
        "job": chosen,
    }


def _scalar_value_haystack(value: Any) -> str:
    values: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, str):
            if item:
                values.append(item)
        elif isinstance(item, (int, float, bool)) and item is not None:
            values.append(str(item))

    walk(value)
    return "\n".join(values).lower()


def _crawler_job_identity_haystack(job: dict[str, Any]) -> str:
    readable = job.get("readable") if isinstance(job.get("readable"), dict) else {}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
    parts: list[str] = [
        str(job.get("title") or ""),
        str(job.get("summary") or ""),
        str(readable.get("target") or ""),
        str(readable.get("headline") or ""),
        str(plan.get("topic") or ""),
        str(plan.get("target_hint") or ""),
        str(plan.get("question") or ""),
        str(plan.get("collection_target") or ""),
    ]
    for task in result.get("planned_tasks") or []:
        if not isinstance(task, dict):
            continue
        parts.append(str(task.get("query") or ""))
        parts.append(str(task.get("reason") or ""))
    return "\n".join(item for item in parts if item).lower()


def _chat(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    fields = _user_message_fields(payload)
    timeout_seconds = _chat_runtime_timeout_seconds(config, payload, fields)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_send_agent_message, config, payload, **fields)
    try:
        return future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError:
        future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        return _chat_timeout_result(config, payload, fields, timeout_seconds)
    finally:
        if future.done():
            executor.shutdown(wait=False, cancel_futures=True)


def _chat_runtime_timeout_seconds(config: AppConfig, payload: dict[str, Any], fields: dict[str, str]) -> float:
    timeout_value = str(payload.get("chat_timeout") or payload.get("chat_timeout_seconds") or "").strip()
    if timeout_value:
        try:
            return min(max(0.01, float(timeout_value)), 900.0)
        except ValueError:
            pass
    agent = str(payload.get("agent") or fields.get("to_agent") or "mcagent_rag")
    model = str(payload.get("model") or payload.get("model_profile_id") or "auto").strip() or "auto"
    profile_model = "" if model in {"auto", "default"} else model
    profile = resolve_profile_from_model(config, profile_model, agent=agent)
    try:
        profile_timeout = float((profile or {}).get("timeout_seconds") or DEFAULT_CHAT_RUNTIME_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        profile_timeout = float(DEFAULT_CHAT_RUNTIME_TIMEOUT_SECONDS)
    overhead = 90.0 if agent == "crawler_agent" else 60.0
    if _payload_requests_crawler_collection(payload, fields):
        overhead = max(overhead, 120.0)
    return min(max(float(DEFAULT_CHAT_RUNTIME_TIMEOUT_SECONDS), profile_timeout + overhead), 900.0)


def _payload_requests_crawler_collection(payload: dict[str, Any], fields: dict[str, str]) -> bool:
    text = "\n".join(
        str(item or "")
        for item in (
            payload.get("question"),
            payload.get("content"),
            fields.get("content"),
            fields.get("to_agent"),
            payload.get("agent"),
        )
    )
    return bool(re.search(r"CrawlerAgent|crawler_agent|Crawler|采集|爬取|抓取|获取|补库|入库|collect|crawl|scrape", text, flags=re.I))


def _chat_timeout_diagnostics(config: AppConfig, payload: dict[str, Any], fields: dict[str, str], timeout_seconds: float) -> dict[str, Any]:
    agent = str(payload.get("agent") or fields.get("to_agent") or "mcagent_rag")
    raw_to_agent = str(fields.get("to_agent") or agent)
    model = str(payload.get("model") or payload.get("model_profile_id") or "auto").strip() or "auto"
    profile_model = "" if model in {"auto", "default"} else model
    profile = resolve_profile_from_model(config, profile_model, agent=agent)
    configured_timeout = None
    profile_id = ""
    profile_label = ""
    if profile:
        configured_timeout = profile.get("timeout_seconds")
        profile_id = str(profile.get("id") or "")
        profile_label = str(profile.get("name") or profile.get("model") or profile_id)
    return {
        "from_agent": fields.get("from_agent"),
        "to_agent": _agent_display_name(agent),
        "to_agent_raw": raw_to_agent,
        "to_agent_id": agent,
        "active_agent": agent,
        "intent": fields.get("intent"),
        "requested_model": model,
        "profile_id": profile_id,
        "profile_label": profile_label,
        "profile_timeout_seconds": configured_timeout,
        "chat_runtime_timeout_seconds": timeout_seconds,
        "side_effect_status": "unknown; inspect /api/jobs for any background job created before timeout",
    }


def _chat_timeout_result(config: AppConfig, payload: dict[str, Any], fields: dict[str, str], timeout_seconds: float) -> dict[str, Any]:
    diagnostics = _chat_timeout_diagnostics(config, payload, fields, timeout_seconds)
    agent = str(diagnostics.get("active_agent") or payload.get("agent") or fields.get("to_agent") or "mcagent_rag")
    answer = (
        f"这轮 Agent 对话超过 {timeout_seconds} 秒还没有返回，运行时已停止等待前端请求继续挂起。\n\n"
        "这通常表示工具选择、下一步确认、本地检索或模型调用耗时过长。"
        "本次响应不会声称已经完成未确认的副作用；请查看任务列表或 /api/jobs 确认是否已有后台 Crawler 任务被创建。"
    )
    return {
        "answer": answer,
        "sources": [],
        "context": "",
        "agent": agent,
        "timed_out": True,
        "timeout_seconds": timeout_seconds,
        "diagnostics": diagnostics,
        "trace": [
            {
                "stage": "runtime",
                "status": "chat_timeout",
                "detail": diagnostics,
            }
        ],
    }


def _relay_user_crawler_request_via_message_bus(
    config: AppConfig,
    payload: dict[str, Any],
    *,
    original_question: str,
    question: str,
    model: str,
    trace: list[dict[str, Any]],
    add_trace: Any,
    session_summary: dict[str, Any],
) -> dict[str, Any]:
    collection_question = _clean_crawler_task_question(original_question or question)
    delivery_target = str(payload.get("delivery_target") or _infer_delivery_target(original_question, session_summary) or "MCagent/RAG")
    message_payload = dict(payload)
    message_payload["agent"] = "crawler_agent"
    message_payload["model"] = model
    message_payload["requested_by"] = "user_via_mcagent"
    message_payload["delivery_target"] = delivery_target
    message_payload["preserve_crawler_request"] = True
    message_payload["session_summary"] = {
        **dict(session_summary or {}),
        "requested_by": "user_via_mcagent",
        "handoff_from": "MCagent",
        "original_user_request": original_question,
        "collection_target": collection_question,
        "task_goal": collection_question,
        "delivery_target": delivery_target,
        "message_transport": "From-Content-To",
    }
    metadata = {
        "tool": "collection_request",
        "requested_by": "user_via_mcagent",
        "delivery_target": delivery_target,
    }
    add_trace(
        "message",
        "explicit_user_handoff_relayed",
        {
            "reason": "User explicitly asked MCagent to relay a collection request to CrawlerAgent; MCagent sends one From-Content-To AgentMessage and lets CrawlerAgent choose tools.",
            "to_agent": "CrawlerAgent",
            "delivery_target": delivery_target,
            "collection_question": collection_question,
        },
    )
    response = _send_agent_message(
        config,
        message_payload,
        from_agent="MCagent",
        content=collection_question,
        to_agent="CrawlerAgent",
        intent="collection_request",
        conversation_id=str(payload.get("session_id") or ""),
        metadata=metadata,
    )
    answer = str(response.get("answer") or "").strip()
    if answer and "转达" not in answer[:80]:
        answer = "MCagent 已通过 From-Content-To 消息把你的请求转达给 CrawlerAgent。\n\n" + answer
        response["answer"] = answer
    response.setdefault("agent", "mcagent_rag")
    response["delegation"] = {
        "requested_by": "user_via_mcagent",
        "delivery_target": delivery_target,
        "task": collection_question,
        "handoff_brief": "",
    }
    job_data = response.get("job") if isinstance(response.get("job"), dict) else {}
    if job_data:
        response.setdefault(
            "collaboration",
            [
                {"speaker": "User", "state": "请求", "text": original_question},
                {"speaker": "MCagent", "state": "转达", "text": "通过 From-Content-To 消息把采集目标交给 CrawlerAgent。"},
                {"speaker": "Crawler", "state": "接收", "text": f"任务 {job_data.get('id')}，状态 {job_data.get('status')}。"},
            ],
        )
    response["trace"] = [*trace, *list(response.get("trace") or [])] if isinstance(response.get("trace"), list) else list(trace)
    return response


def _chat_impl(config: AppConfig, payload: dict[str, Any], emit: Any | None = None) -> dict[str, Any]:
    run = build_agent_execution_context(config, payload, token_resolver=_answer_max_tokens, emit=emit)
    original_question = run.original_question
    question = run.question
    agent = run.agent
    model = run.model
    temperature = run.temperature
    max_tokens = run.max_tokens
    trace = run.trace.steps
    add_trace = run.add_trace
    if not question:
        return run.response({"answer": "问题不能为空。", "sources": [], "context": "", "agent": agent})
    incoming_message = _incoming_agent_message(payload, agent=agent, question=question)
    _trace_agent_message(add_trace, incoming_message)
    context_only_agent_message = agent == "mcagent_rag" and incoming_message.from_agent == "CrawlerAgent" and _is_context_only_agent_message(incoming_message)
    collection_request_agent_message = agent == "crawler_agent" and _is_collection_request_agent_message(incoming_message)
    if _question_looks_transport_garbled(question):
        add_trace("done", "invalid_encoding", {"reason": "question contains too many question marks"})
        return run.response(
            {
                "answer": "这条消息看起来在传输或终端输入时发生了编码损坏，问题内容变成了大量问号。请在网页里重新发送原始中文问题，或确认调用方按 UTF-8 发送请求。为了避免污染资料库，本次不会触发 Crawler。",
                "sources": [],
                "context": "",
                "agent": agent,
            },
        )
    retrieval_note = ""
    session_summary = _session_summary(payload)
    session_summary = _session_summary_with_received_message(session_summary, incoming_message)
    if (
        agent == "mcagent_rag"
        and incoming_message.from_agent_id == "user"
        and _user_explicitly_asked_mcagent_to_tell_crawler(original_question)
        and not _forbids_crawler_handoff(original_question)
    ):
        return _relay_user_crawler_request_via_message_bus(
            config,
            payload,
            original_question=original_question,
            question=question,
            model=model,
            trace=trace,
            add_trace=add_trace,
            session_summary=session_summary,
        )
    if agent == "mcagent_rag":
        contextual_question, retrieval_note, rewritten = _contextualize_question(payload, question)
        if rewritten:
            question = contextual_question
            run.question = question
            add_trace("observe", "contextualized", {"original": original_question, "rewritten": question})
    if context_only_agent_message:
        return _mcagent_context_message_reply(config, payload, incoming_message, question, trace, add_trace)
    if collection_request_agent_message:
        add_trace(
            "message",
            "collection_request_received_for_agent_decision",
            {
                "reason": "CrawlerAgent received a collection_request AgentMessage. The message is context only; CrawlerAgent must still choose the next tool from its own catalog.",
                "from_agent": incoming_message.from_agent,
                "to_agent": incoming_message.to_agent,
                "intent": incoming_message.intent,
                "metadata": incoming_message.metadata,
            },
        )
    router = LlmAgentToolRouterService(
        select_client=_selected_llm_client,
        action_plan_has_tool=_action_plan_has_tool,
    )
    route = router.route(run, session_summary=session_summary)
    tool_decision = route.tool_decision
    route_confirmation = route.route_confirmation if hasattr(route, "route_confirmation") else {}
    route_intent = route.route_intent
    action_plan = route.action_plan
    rag_focus = route.rag_focus
    planned_workflow = route.planned_workflow
    planned_delegate = route.planned_delegate
    executor = AgentToolExecutor(
        generate_direct_answer=_generate_direct_answer,
        generate_direct_answer_stream=_generate_direct_answer_stream,
        generate_grounded_answer=_generate_grounded_answer,
        generate_grounded_answer_stream=_generate_grounded_answer_stream,
        repair_answer=_repair_list_answer,
        status_answer=_crawler_monitor_answer,
    )
    if context_only_agent_message and planned_delegate:
        planned_delegate = False
        action_plan = [step for step in action_plan if str(step.get("tool") or "") != "delegate_crawler"]
        add_trace(
            "delegate",
            "suppressed_for_context_reply",
            {
                "reason": "CrawlerAgent asked MCagent for local context only. MCagent may use its own read tools to answer, but this reply must not recursively start another Crawler job.",
                "request_message_id": incoming_message.message_id,
            },
        )
    if route_intent == "mcagent_context":
        proposed_collection = _clean_inter_agent_collection_target(original_question, str(tool_decision.get("collection_target") or ""))
        if agent == "crawler_agent" and collection_request_agent_message and _action_plan_has_tool(action_plan, "delegate_crawler"):
            route_intent = "planned_workflow"
            planned_workflow = True
            planned_delegate = True
            add_trace(
                "decide",
                "mcagent_context_preserved_inside_crawler_workflow",
                {
                    "reason": "CrawlerAgent selected mcagent_context and delegate_crawler in its own action_plan; runtime preserves that CrawlerAgent plan inside the background job.",
                    "planned_delegate": planned_delegate,
                    "action_plan": action_plan,
                },
            )
        elif agent == "crawler_agent" and _action_plan_has_tool(action_plan, "delegate_crawler"):
            route_intent = "planned_workflow"
            add_trace(
                "decide",
                "mcagent_context_deferred_to_background_workflow",
                {
                    "reason": "CrawlerAgent selected mcagent_context as a step inside a planned workflow that also delegates collection; run both steps inside the background Crawler job instead of answering from chat-turn RAG.",
                    "planned_delegate": planned_delegate,
                    "action_plan": action_plan,
                },
            )
        else:
            route_intent = "answer"
            if not rag_focus:
                rag_focus = _mcagent_context_focus(original_question, str(tool_decision.get("collection_target") or ""))
                tool_decision["rag_focus"] = rag_focus
            add_trace("decide", "mcagent_context_selected", {"rag_focus": rag_focus, "planned_delegate": planned_delegate, "action_plan": action_plan})
    if route_intent == "delegate_crawler":
        proposed_collection = str(tool_decision.get("collection_target") or original_question or question).strip()
        if _should_use_temporary_extract_without_persistence(agent, original_question, proposed_collection, str(tool_decision.get("delivery_target") or "")):
            route_intent = "temporary_extract"
            add_trace(
                "decide",
                "side_effect_boundary_corrected",
                {
                    "from_tool": "delegate_crawler",
                    "to_tool": "temporary_extract",
                    "reason": "User requested immediate URL reading/summarization without local persistence; background collection would have filesystem side effects.",
                    "collection_target": proposed_collection,
                },
            )
    if route_intent == "router_error":
        return executor.router_error(run, tool_decision)
    if route_intent == "direct_answer":
        return _handle_direct_answer_route(
            config=config,
            agent=agent,
            model=model,
            original_question=original_question,
            question=question,
            tool_decision=tool_decision,
            route_confirmation=route_confirmation,
            action_plan=action_plan,
            executor=executor,
            run=run,
            session_summary=session_summary,
            add_trace=add_trace,
        )
    if route_intent == "crawler_audit":
        return _handle_crawler_audit_route(
            agent=agent,
            original_question=original_question,
            tool_decision=tool_decision,
            route_confirmation=route_confirmation,
            executor=executor,
            run=run,
            session_summary=session_summary,
            trace=trace,
            add_trace=add_trace,
        )
    if route_intent == "local_corpus_inventory":
        return _handle_local_corpus_inventory_route(
            config=config,
            payload=payload,
            agent=agent,
            model=model,
            original_question=original_question,
            question=question,
            tool_decision=tool_decision,
            route_confirmation=route_confirmation,
            action_plan=action_plan,
            executor=executor,
            run=run,
            session_summary=session_summary,
            trace=trace,
            add_trace=add_trace,
        )
    if (
        agent == "mcagent_rag"
        and planned_workflow
        and _action_plan_has_tool(action_plan, "local_corpus_inventory")
    ):
        return _handle_mcagent_inventory_planned_workflow_route(
            config=config,
            payload=payload,
            agent=agent,
            model=model,
            original_question=original_question,
            question=question,
            tool_decision=tool_decision,
            action_plan=action_plan,
            session_summary=session_summary,
            trace=trace,
            add_trace=add_trace,
        )
    if route_intent == "temporary_extract":
        return _handle_temporary_extract_route(
            config=config,
            agent=agent,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            original_question=original_question,
            question=question,
            tool_decision=tool_decision,
            route_confirmation=route_confirmation,
            executor=executor,
            run=run,
            session_summary=session_summary,
            trace=trace,
            add_trace=add_trace,
        )
    if route_intent == "delegate_crawler":
        return _handle_delegate_crawler_route(
            config=config,
            payload=payload,
            agent=agent,
            model=model,
            original_question=original_question,
            question=question,
            tool_decision=tool_decision,
            route_confirmation=route_confirmation,
            action_plan=action_plan,
            collection_request_agent_message=collection_request_agent_message,
            router=router,
            executor=executor,
            run=run,
            session_summary=session_summary,
            trace=trace,
            add_trace=add_trace,
        )
    if agent == "crawler_agent" and _action_plan_has_tool(action_plan, "delegate_crawler"):
        return _handle_crawler_action_plan_delegate_route(
            config=config,
            payload=payload,
            agent=agent,
            model=model,
            original_question=original_question,
            question=question,
            tool_decision=tool_decision,
            action_plan=action_plan,
            session_summary=session_summary,
            trace=trace,
            add_trace=add_trace,
        )
    if route_intent == "status":
        return _handle_status_route(route_confirmation=route_confirmation, executor=executor, run=run, add_trace=add_trace)

    if (
        agent == "mcagent_rag"
        and route_intent == "answer"
        and not planned_delegate
        and not bool(payload.get("no_llm"))
    ):
        completeness = _tool_route_completeness_review(
            config,
            agent=agent,
            model=model,
            original_question=original_question,
            selected_tool="local_rag_search",
            tool_answer="Agent selected local RAG/evidence search as the next step; retrieval has not executed yet.",
            tool_decision=tool_decision,
            route_confirmation=route_confirmation,
            action_plan=action_plan,
        )
        if (
            completeness.get("missing_side_effect")
            and str(completeness.get("tool") or "").strip() == "delegate_crawler"
            and str(completeness.get("action") or "").strip() == "execute_selected_tool"
        ):
            collection_target = str(completeness.get("collection_target") or tool_decision.get("collection_target") or original_question or question).strip()
            add_trace(
                "plan",
                "route_completeness_gap_not_executed",
                {
                    **completeness,
                    "reason": "Completeness review found a possible post-RAG delegation need, but runtime will not add delegate_crawler after the Agent selected local RAG.",
                    "collection_target": collection_target,
                    "required_agent_selection": "delegate_crawler or planned_workflow with delegate_crawler",
                },
            )

    rag_retrieval = RagRetrievalService(
        adaptive_rough_k=_adaptive_rough_k,
        adaptive_final_k=_adaptive_final_context_k,
        planning_summary=_retrieval_planning_summary,
        combined_question=_combined_retrieval_question,
        supplement_results=lambda cfg, query, results, limit: _supplement_raw_html_results(cfg, query, results, limit=limit),
        dedupe_results=_dedupe_results,
    )
    retrieval_preparation = rag_retrieval.prepare(config, agent=agent, question=question, rag_focus=rag_focus)
    evidence_question = retrieval_preparation.evidence_question
    rough_k = retrieval_preparation.rough_k
    final_k = retrieval_preparation.final_k
    use_retrieval_planner = False
    if agent == "mcagent_rag":
        retrieval_confirmation = {
            "proceed": True,
            "tool": "local_rag_search",
            "goal": f"Search local evidence to answer: {evidence_question}",
            "reason": "Executing the Agent-selected answer/RAG route without a duplicate LLM confirmation call.",
            "planner": "agent_route_execution",
        }
        add_trace("retrieve", "next_step_confirmed", retrieval_confirmation)
        use_retrieval_planner = not bool(rag_focus)

    retrieval_result = rag_retrieval.retrieve(
        config,
        agent=agent,
        original_question=original_question,
        question=question,
        session_summary=session_summary,
        preparation=retrieval_preparation,
        use_planner=use_retrieval_planner,
        add_trace=add_trace,
    )
    retrieval_plan = retrieval_result.retrieval_plan
    rough_results = retrieval_result.rough_results
    selected = retrieval_result.selected
    if not rough_results:
        return _handle_no_retrieval_results(
            config=config,
            payload=payload,
            agent=agent,
            model=model,
            original_question=original_question,
            question=question,
            tool_decision=tool_decision,
            action_plan=action_plan,
            planned_delegate=planned_delegate,
            evidence_question=evidence_question,
            session_summary=session_summary,
            trace=trace,
            add_trace=add_trace,
        )

    evidence_report = None
    if agent == "mcagent_rag":
        evidence_route = _select_mcagent_rag_evidence_or_respond(
            config=config,
            payload=payload,
            agent=agent,
            model=model,
            original_question=original_question,
            question=question,
            tool_decision=tool_decision,
            action_plan=action_plan,
            planned_delegate=planned_delegate,
            evidence_question=evidence_question,
            rough_results=rough_results,
            retrieval_plan=retrieval_plan,
            final_k=final_k,
            session_summary=session_summary,
            trace=trace,
            add_trace=add_trace,
        )
        selected = evidence_route.selected
        evidence_report = evidence_route.evidence_report
        if evidence_route.response is not None:
            return evidence_route.response

    return _handle_rag_answer_generation_route(
        config=config,
        payload=payload,
        agent=agent,
        model=model,
        original_question=original_question,
        question=question,
        retrieval_note=retrieval_note,
        evidence_question=evidence_question,
        selected=selected,
        tool_decision=tool_decision,
        route_confirmation=route_confirmation,
        action_plan=action_plan,
        route_intent=route_intent,
        planned_workflow=planned_workflow,
        planned_delegate=planned_delegate,
        executor=executor,
        run=run,
        session_summary=session_summary,
        trace=trace,
        add_trace=add_trace,
    )


class MCagentHandler(BaseHTTPRequestHandler):
    server_version = "MCagentWeb/0.2"

    def _config(self) -> AppConfig:
        return self.server.config  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[web] {self.address_string()} - {format % args}")

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._do_get()
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            _send_json(self, {"error": f"{type(exc).__name__}: {exc}"}, status=500)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._do_post()
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            _send_json(self, {"error": f"{type(exc).__name__}: {exc}"}, status=500)

    def _do_get(self) -> None:
        request_path = self.path.split("?", 1)[0]
        if request_path in {"/", "/index.html"}:
            _send_text(self, (WEB_DIR / "index.html").read_text(encoding="utf-8"), "text/html; charset=utf-8", cache_control="no-store")
            return
        if request_path in {"/settings", "/settings.html"}:
            _send_text(self, (WEB_DIR / "settings.html").read_text(encoding="utf-8"), "text/html; charset=utf-8", cache_control="no-store")
            return
        if request_path.startswith("/static/"):
            name = request_path.removeprefix("/static/")
            path = (STATIC_DIR / name).resolve()
            if not str(path).startswith(str(STATIC_DIR.resolve())) or not path.exists():
                _send_json(self, {"error": "not found"}, status=404)
                return
            content_type = "text/css; charset=utf-8" if path.suffix == ".css" else "application/javascript; charset=utf-8"
            _send_text(self, path.read_text(encoding="utf-8"), content_type, cache_control="no-store")
            return
        if request_path == "/api/status":
            _send_json(self, _status_payload(self._config()))
            return
        if request_path == "/api/jobs":
            _send_json(self, _jobs_payload())
            return
        if request_path == "/api/models":
            _send_json(self, {"models": _available_models(self._config())})
            return
        if request_path == "/api/llm-profiles":
            _send_json(self, profiles_payload(self._config()))
            return
        if request_path == "/api/agents":
            _send_json(self, {"agents": AGENTS})
            return
        if request_path == "/api/crawler/summary":
            _send_json(self, _recent_crawler_manifest_summary(self._config().paths.source_dir, limit=20))
            return
        _send_json(self, {"error": "not found"}, status=404)

    def _do_post(self) -> None:
        payload = _read_body(self)
        config = self._config()
        request_path = self.path.split("?", 1)[0]
        if request_path == "/api/chat":
            _send_json(self, _chat(config, payload))
            return
        if request_path == "/api/agent-message":
            _send_json(self, _send_agent_message(
                config,
                payload,
                from_agent=str(payload.get("from_agent") or payload.get("from") or "User"),
                content=str(payload.get("content") or payload.get("message") or payload.get("question") or ""),
                to_agent=str(payload.get("to_agent") or payload.get("to") or payload.get("agent") or "MCagent"),
                intent=str(payload.get("intent") or ""),
                conversation_id=str(payload.get("session_id") or payload.get("conversation_id") or ""),
                reply_to=str(payload.get("reply_to") or ""),
                requires_reply=bool(payload.get("requires_reply", True)),
                metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
            ))
            return
        if request_path == "/api/chat/stream":
            _send_sse_headers(self)

            def emit(event: str, data: Any) -> None:
                _write_sse(self, event, data)

            try:
                result = _send_agent_message(config, payload, emit=emit, **_user_message_fields(payload))
                emit("response", result)
                emit("done", {"ok": True})
            except BrokenPipeError:
                return
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                emit("error", {"error": f"{type(exc).__name__}: {exc}"})
            return
        if request_path == "/api/search":
            query = str(payload.get("query") or "")
            top_k = int(payload.get("top_k") or _adaptive_preview_k(query))
            _send_json(self, {"results": _search(config, query, top_k)})
            return
        if request_path == "/api/crawler/plan":
            question = str(payload.get("question") or payload.get("query") or "")
            include_completed = bool(payload.get("include_completed"))
            max_tasks = int(payload.get("max_tasks") or 16)
            session_summary = payload.get("session_summary") if isinstance(payload.get("session_summary"), dict) else None
            if include_completed:
                plan = plan_crawler_tasks(question, config.paths.source_dir, max_tasks=max_tasks, include_completed=True)
            else:
                plan = plan_crawler_tasks_resilient(question, config.paths.source_dir, max_tasks=max_tasks, session_summary=session_summary)
            plan.setdefault("toolsets", toolsets_payload())
            _send_json(self, plan)
            return
        if request_path == "/api/crawler/summary":
            limit = int(payload.get("limit") or 20)
            query = str(payload.get("query") or "")
            _send_json(self, _recent_crawler_manifest_summary(config.paths.source_dir, limit=max(1, min(limit, 100)), query=query))
            return
        if request_path == "/api/collaboration/start":
            _send_json(self, _chat(config, payload | {"agent": "mcagent_rag"}))
            return
        if request_path == "/api/llm-profiles":
            _send_json(self, save_profiles_payload(config, payload))
            return
        if request_path == "/api/llm-profiles/test":
            raw_profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
            existing = profile_by_id(config, str(raw_profile.get("id") or payload.get("id") or "")) if raw_profile else None
            try:
                _send_json(self, test_profile_connection(raw_profile, existing=existing))
            except Exception as exc:  # noqa: BLE001 - surface connection failure to the settings UI.
                _send_json(self, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=200)
            return
        if request_path == "/api/ingest":
            result = _ingest_after_crawl(config)
            _send_json(self, {"stats": result["stats"], "knowledge_map": result["knowledge_map"], "status": _status_payload(config)})
            return
        if request_path == "/api/jobs/start-ingest":
            job, created = _start_job("ingest", "Import crawler exports", lambda item: _run_ingest_job(item, config))
            _send_json(self, {"job": _job_to_dict(job), "created": created}, status=202 if created else 409)
            return
        if request_path == "/api/jobs/stop":
            job = _request_job_stop(str(payload.get("id") or ""))
            if job is None:
                _send_json(self, {"error": "job not found"}, status=404)
                return
            _send_json(self, {"job": _job_to_dict(job)}, status=202)
            return
        if request_path == "/api/session/delete":
            _send_json(self, _delete_session(str(payload.get("session_id") or "default")))
            return
        if request_path == "/api/session":
            session_id = normalize_session_id(payload.get("session_id"))
            history = SESSION_STORE.history(session_id)
            _send_json(self, {"session_id": session_id, "history": history})
            return
        if request_path == "/api/session/context":
            session_id = normalize_session_id(payload.get("session_id"))
            agent = str(payload.get("agent") or "mcagent_rag")
            context = SESSION_STORE.context(session_id, agent=agent, summary=_session_summary(payload | {"session_id": session_id}))
            _send_json(self, context.to_dict())
            return
        _send_json(self, {"error": "not found"}, status=404)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local MCagent web UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", help="Path to config JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    server = ThreadingHTTPServer((args.host, args.port), MCagentHandler)
    server.config = config  # type: ignore[attr-defined]
    print(f"MCagent web UI: http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping MCagent web UI.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
