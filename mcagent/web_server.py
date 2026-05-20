from __future__ import annotations

import argparse
import concurrent.futures
from dataclasses import asdict, dataclass, field
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
from typing import Any
import sqlite3

from .agent_memory import append_memory_event, memory_summary
from .agent_runtime import build_handoff_contract, tool_catalog_prompt, tool_names_for_agent, validate_tool_name
from .chat import SYSTEM_PROMPT, format_context, format_sources
from .cleaners import _HTMLTextExtractor, normalize_text
from .config import AppConfig, OllamaConfig, PROJECT_ROOT, load_config
from .crawler_llm_planner import plan_crawler_tasks_resilient, plan_crawler_tasks_rule_fallback, reflect_crawler_progress, review_topic_discovery_candidates
from .crawler_planner import CONCEPTS, decompose_crawler_queries, plan_crawler_tasks, toolsets_payload
from .evidence_selector import EvidenceSelector
from .ingest import ingest_exports
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
from .retrieval_planner import plan_retrieval
from .retriever import Retriever
from .schema import SearchResult
from .storage import connect, count_rows


AGENT_CONSOLE_DIR = PROJECT_ROOT / "frontend"
WEB_DIR = Path(os.environ.get("AGENT_CONSOLE_DIR", AGENT_CONSOLE_DIR)).resolve()
STATIC_DIR = WEB_DIR / "static"
MAX_ROUGH_TOP_K = 200
MAX_FINAL_CONTEXT_K = 12
MIN_FINAL_CONTEXT_K = 4
MAX_MODEL_CONTEXT_CHARS = 16000
MAX_SOURCE_CONTEXT_CHARS = 1500
MAX_DEEP_EVIDENCE_CHARS = 900
DEFAULT_ANSWER_MAX_TOKENS = 3000
ANSWER_MAX_TOKENS_CAP = 6000
DEFAULT_CRAWLER_PLANNER_TIMEOUT_SECONDS = 120
MAX_JOBS = 40
GROW_PROGRESS_PATH = PROJECT_ROOT / "runtime" / "grow_knowledge_base_progress.json"
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


SESSIONS: dict[str, list[dict[str, Any]]] = {}
SESSION_SUMMARIES: dict[str, dict[str, Any]] = {}
SESSIONS_LOCK = threading.Lock()
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


def _send_text(handler: BaseHTTPRequestHandler, text: str, content_type: str, status: int = 200) -> None:
    body = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
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
    data["readable"] = _job_readable_summary(data)
    return data


def _job_readable_summary(job: dict[str, Any]) -> dict[str, Any]:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
    tasks = result.get("tasks") if isinstance(result.get("tasks"), list) else []
    planned = result.get("planned_tasks") if isinstance(result.get("planned_tasks"), list) else []
    reflections = plan.get("agent_reflections") if isinstance(plan.get("agent_reflections"), list) else []
    last_reflection = next((item for item in reversed(reflections) if isinstance(item, dict)), {})
    current_index = min(len(tasks) + 1, len(planned)) if planned else len(tasks)
    if str(job.get("status") or "") in {"stopped", "succeeded", "failed"} and planned and tasks:
        current_index = min(len(tasks), len(planned))
    current_task = planned[current_index - 1] if planned and current_index > 0 else {}
    if not isinstance(current_task, dict):
        current_task = {}
    success_count = int(result.get("success_count") or sum(1 for item in tasks if isinstance(item, dict) and item.get("ingest_deferred")))
    failure_count = int(result.get("failure_count") or sum(1 for item in tasks if isinstance(item, dict) and (item.get("empty_result") or item.get("off_topic_result") or int(item.get("returncode") or 0) != 0)))
    off_topic = sum(1 for item in tasks if isinstance(item, dict) and item.get("off_topic_result"))
    empty = sum(1 for item in tasks if isinstance(item, dict) and item.get("empty_result"))
    target = str(plan.get("topic") or plan.get("target_hint") or plan.get("question") or "")
    goals = [str(item) for item in (plan.get("coverage_goals") or []) if str(item).strip()]
    next_action = "等待 Crawler 规划任务。"
    status = str(job.get("status") or "")
    current_query = str(current_task.get("query") or "") if current_task else ""
    if status in {"queued", "running"} and current_task:
        query_label = current_query or "等待 CrawlerAgent 给出可执行查询"
        next_action = f"正在执行第 {current_index}/{len(planned)} 个采集任务：{_source_label(str(current_task.get('source') or ''))} · {query_label}"
    elif status == "succeeded":
        next_action = "采集已完成；如有新资料，后台会继续入库或已完成入库。"
    elif status == "failed":
        next_action = "本轮采集失败或没有找到可入库资料，需要 Crawler 重新规划更短、更准的查询词。"
    elif status == "stopped":
        next_action = "任务已停止。"
    if result.get("ingest_background"):
        next_action += " 后台入库正在处理。"
    if result.get("ingest"):
        next_action += " 后台入库已完成。"
    return {
        "title": str(job.get("title") or ""),
        "status": status,
        "target": target,
        "delivery_target": str(plan.get("delivery_target") or ""),
        "coverage_goals": goals[:5],
        "current_index": current_index,
        "total_tasks": len(planned),
        "current_source": _source_label(str(current_task.get("source") or "")) if current_task else "",
        "current_query": current_query,
        "current_reason": str(current_task.get("reason") or "") if current_task else "",
        "agent_reflection": {
            "action": str(last_reflection.get("action") or ""),
            "reason": str(last_reflection.get("reason") or ""),
            "planner": str(last_reflection.get("planner") or ""),
        } if last_reflection else {},
        "success_count": success_count,
        "failure_count": failure_count,
        "off_topic_count": off_topic,
        "empty_count": empty,
        "replan_count": int(result.get("replan_count") or 0),
        "summary": str(job.get("summary") or ""),
        "next_action": next_action,
    }


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
        return {"jobs": [_job_to_dict(JOBS[job_id]) for job_id in JOBS_ORDER if job_id in JOBS]}


def _running_job(kind: str) -> Job | None:
    for job in JOBS.values():
        if job.kind == kind and job.status in {"queued", "running"} and not job.stop_requested:
            return job
    return None


def _append_job(job: Job) -> None:
    JOBS[job.id] = job
    JOBS_ORDER.insert(0, job.id)
    del JOBS_ORDER[MAX_JOBS:]
    for stale_id in list(JOBS):
        if stale_id not in JOBS_ORDER:
            JOBS.pop(stale_id, None)


def _update_job(job: Job, **changes: Any) -> None:
    with JOBS_LOCK:
        for key, value in changes.items():
            setattr(job, key, value)


def _start_job(kind: str, title: str, target: Any) -> tuple[Job, bool]:
    with JOBS_LOCK:
        running = _running_job(kind)
        if running:
            return running, False
        job = Job(id=f"{int(time.time() * 1000)}-1", kind=kind, title=title)
        _append_job(job)
    thread = threading.Thread(target=target, args=(job,), daemon=True, name=f"mcagent-{kind}")
    thread.start()
    return job, True


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
        if cached and SOURCE_STATUS_CACHE.get("source_dir") == key and now - float(SOURCE_STATUS_CACHE.get("time") or 0) < 5:
            return dict(cached)

    file_count = 0
    manifest_count = 0
    report_count = 0
    total_bytes = 0
    latest: list[dict[str, Any]] = []
    if source_dir.exists():
        latest_heap: list[tuple[float, str, int]] = []
        for path in source_dir.rglob("*"):
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


def _ingest_after_crawl(config: AppConfig) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(4):
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                stats = ingest_exports(config)
            output = "\n".join(part for part in [stdout.getvalue(), stderr.getvalue()] if part)
            return {"stats": asdict(stats), "output": _tail_text(output, 1200), "knowledge_map": _refresh_knowledge_map(), "attempts": attempt + 1}
        except sqlite3.OperationalError as exc:
            last_error = exc
            if "locked" not in str(exc).lower() or attempt == 3:
                break
            time.sleep(1.5 * (attempt + 1))
    raise last_error or RuntimeError("ingest failed")


def _run_background_ingest(job_id: str, config: AppConfig) -> None:
    with INGEST_LOCK:
        try:
            result = _ingest_after_crawl(config)
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
        "reader": "web_discovery",
        "tavily_api": "tavily",
        "tavily_search": "tavily",
        "firecrawl_api": "firecrawl",
        "firecrawl_search": "firecrawl",
        "jina_reader": "jina",
        "jina_search": "jina",
        "browser": "playwright",
        "browser_extract": "playwright",
        "browser_collect": "browser_collect",
        "browser_structured": "browser_collect",
        "structured_browser": "browser_collect",
        "product_collect": "browser_collect",
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
        "tavily": "Tavily Search/Extract",
        "firecrawl": "Firecrawl Search/Scrape",
        "jina": "Jina Reader/Search",
        "playwright": "Playwright 浏览器采集",
        "browser_collect": "浏览器结构化采集",
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
        "tavily": r"D:\magic\MC_Agent\data\crawler_exports\tavily\...",
        "firecrawl": r"D:\magic\MC_Agent\data\crawler_exports\firecrawl\...",
        "jina": r"D:\magic\MC_Agent\data\crawler_exports\jina\...",
        "playwright": r"D:\magic\MC_Agent\data\crawler_exports\playwright\...",
        "browser_collect": r"用户指定目录，或 D:\magic\MC_Agent\data\crawler_exports\browser_collect\...",
        "modpack_download": r"D:\magic\MC_Agent\data\crawler_exports\modpack_download\...",
        "modpack_internal": r"D:\magic\MC_Agent\data\crawler_exports\manual_research\...",
        "topic_discovery": r"D:\magic\MC_Agent\data\crawler_exports\topic_discovery\...",
    }.get(_source_alias(source), r"D:\magic\MC_Agent\data\crawler_exports\...")


def _modpack_archive_for_query(query: str) -> str:
    archive_root = PROJECT_ROOT / "data" / "manual_research"
    archives = list(archive_root.glob("**/pack_archive/*.zip")) + list(archive_root.glob("**/pack_archive/*.mrpack"))
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
        score = sum(1 for token in query_tokens if token.lower() in normalized_haystack)
        scored.append((score, archive))
    scored.sort(key=lambda item: item[0], reverse=True)
    if scored and scored[0][0] > 0:
        return str(scored[0][1])
    return ""


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
            str(int(payload.get("search_limit") or 12)),
            "--max-pages",
            str(int(payload.get("max_urls") or 10)),
        ]
    if source == "tavily":
        return [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_tavily_seed.py"),
            "--query",
            query,
            "--max-results",
            str(int(payload.get("search_limit") or 8)),
            "--max-pages",
            str(int(payload.get("max_urls") or 8)),
            "--search-depth",
            str(payload.get("search_depth") or "advanced"),
        ]
    if source == "firecrawl":
        return [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_firecrawl_seed.py"),
            "--query",
            query,
            "--max-results",
            str(int(payload.get("search_limit") or 8)),
            "--max-pages",
            str(int(payload.get("max_urls") or 8)),
        ]
    if source == "jina":
        return [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_jina_seed.py"),
            "--query",
            query,
            "--max-results",
            str(int(payload.get("search_limit") or 8)),
            "--max-pages",
            str(int(payload.get("max_urls") or 8)),
        ]
    if source == "playwright":
        return [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_playwright_seed.py"),
            "--query",
            query,
            "--max-results",
            str(int(payload.get("search_limit") or 6)),
            "--max-pages",
            str(int(payload.get("max_urls") or 4)),
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
        return [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_modpack_archive_seed.py"),
            "--query",
            query,
            "--limit",
            str(int(payload.get("search_limit") or payload.get("max_urls") or 8)),
        ]
    if source == "modpack_internal":
        archive = str(payload.get("zip") or _modpack_archive_for_query(query) or "").strip()
        if not archive:
            message = {
                "archive_found": False,
                "message": "No matching local modpack archive was found. CrawlerAgent should decide whether to search project pages, Modrinth/CurseForge, public download sources, or ask for an archive.",
            }
            return [sys.executable, "-c", "import json; print(json.dumps(" + repr(message) + ", ensure_ascii=False, indent=2))"]
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "extract_modpack_internals.py"),
            "--zip",
            archive,
        ]
        return command
    if source == "mcmod":
        return [sys.executable, str(PROJECT_ROOT / "scripts" / "fetch_mcmod_seed.py"), "--query", query, "--limit", str(int(payload.get("search_limit") or 10))]
    if source == "modrinth":
        return [
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
            "--include-modpack-contents",
        ]
    return [sys.executable, str(PROJECT_ROOT / "scripts" / "fetch_mediawiki_seed.py"), "--query", query, "--search-limit", str(int(payload.get("search_limit") or 12))]


def _command_timeout(source: str) -> int:
    source = _source_alias(source)
    if source in {"followup", "web_discovery", "tavily", "firecrawl", "jina", "playwright", "browser_collect", "modpack_download"}:
        return 360
    if source in {"modrinth", "mcmod"}:
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
            if process.poll() is not None:
                stdout, _ = process.communicate()
                output = _tail_text(stdout or "")
                returncode = process.returncode
                break
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
    return {
        "manifest_path": str(manifest_path) if manifest_path.exists() else "",
        "records": len(records),
        "skipped": len(skipped),
        "errors": len(errors),
        "downloads": len(downloads),
        "candidates": len(candidates),
        "status": str(data.get("status") or ""),
        "note": str(data.get("note") or ""),
        "failure_reason": str(data.get("failure_reason") or ""),
        "next_action": str(data.get("next_action") or ""),
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
        if "duplicate" not in reason or not previous_path or not Path(previous_path).exists():
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
    for record in records[:5]:
        if isinstance(record, dict):
            record_samples.append(
                {
                    "title": record.get("title"),
                    "url": record.get("url"),
                    "path": record.get("path"),
                    "chars": record.get("chars"),
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
    for result in task_results:
        source = _source_alias(str(result.get("source") or ""))
        entry = by_source.setdefault(source, {"source": source, "tasks": 0, "records": 0, "skipped": 0, "errors": 0, "empty": 0, "off_topic": 0, "uncertain": 0, "failed": 0})
        entry["tasks"] += 1
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
            for sample in brief.get("record_samples", []):
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
        next_actions.append("Crawler 判断失败原因：" + failure_reasons[0])
    if total_records == 0:
        next_actions.append("本轮没有产生可入库 records，应换更短的实体词、直达 URL 或浏览器兜底。")
    if empty_tasks:
        next_actions.append("存在空结果源，下一轮应减少整句查询，改用实体名、别名、英文名和单项关键词。")
    if off_topic_tasks:
        next_actions.append("存在跑偏结果，下一轮应加强标题/URL/正文主题校验，必要时限定站点。")
    if uncertain_tasks:
        next_actions.append("存在相关性不确定结果，下一轮应交给 Crawler LLM 复判或补抓更明确的组件/上下文证据。")
    if duplicate_count:
        next_actions.append("发现重复内容较多，后续应优先抓新教程页、表格页、配方页，而不是重复项目首页。")
    if low_relevance_count:
        next_actions.append("公开搜索低相关结果较多，建议用站点限定或 MC 专门来源优先。")
    if total_errors:
        next_actions.append("部分源返回错误，检查 API key、额度或该源可用性。")
    if not next_actions:
        next_actions.append("本轮有可用新增资料，可让 MCagent 重新检索验证回答质量。")
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
    parts = [str(record.get(key) or "") for key in ("title", "url", "path", "snippet", "description")]
    path = record.get("path")
    if path:
        try:
            parts.append(Path(str(path)).read_text(encoding="utf-8", errors="replace")[:12000])
        except OSError:
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
        samples.append(
            {
                "title": str(record.get("title") or "")[:160],
                "url": str(record.get("url") or "")[:240],
                "excerpt": normalize_text(text)[:900],
            }
        )
    config = load_config()
    client, _label = client_for_agent(config, "crawler_agent", temperature=0.0, timeout_seconds=90)
    prompt = (
        "You are CrawlerAgent judging crawler evidence for a RAG knowledge base.\n"
        "Important: a modpack can include component mods/items/systems. A useful page does NOT need to mention the modpack name if the task query or context indicates it is a component to collect.\n"
        "If the task query is a known or plausible component name such as TACZ, FTB Quests, SlashBlade, a boss name, an item name, or a system name, judge the page by whether it explains that component. Do not require the page to also contain the modpack name.\n"
        "Classify records as useful if they are direct project pages OR plausible component/system/tutorial pages for the target. Reject broad unrelated noise.\n"
        "Output only compact JSON: {\"matched\": true/false, \"reason\": \"direct|component|noise|uncertain\", \"matched_indexes\": [0], \"notes\": \"...\"}\n"
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
    return {
        "matched": bool(value.get("matched")),
        "reason": str(value.get("reason") or "llm_judged"),
        "matched_indexes": [int(item) for item in matched_indexes if str(item).isdigit()][:8],
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
    if not terms:
        return {"matched": True, "reason": "no_terms_available", "matched_records": len(records), "records": len(records), "terms": []}
    matched_records = 0
    examples: list[dict[str, str]] = []
    task_terms = _task_query_terms_for_validation(task_query)
    for record in records:
        raw_text = _record_text_for_validation(record)
        text = raw_text.lower()
        hits = [term for term in terms if term.lower() in text]
        direct_target = any(term in {"落幕曲", "Closing", "Song"} for term in hits) and len(hits) >= 2
        if direct_target:
            matched_records += 1
            if len(examples) < 3:
                examples.append({"title": str(record.get("title") or ""), "url": str(record.get("url") or ""), "hits": ", ".join(hits[:6])})
    if matched_records:
        return {
            "matched": True,
            "reason": "topic_match",
            "matched_records": matched_records,
            "records": len(records),
            "terms": terms,
            "examples": examples,
        }
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
                llm_examples.append({"title": str(record.get("title") or ""), "url": str(record.get("url") or ""), "hits": "Crawler LLM component/direct judgement"})
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
    return {
        "matched": bool(llm_judgement.get("matched")) if isinstance(llm_judgement, dict) else False,
        "reason": str(llm_judgement.get("reason") or "uncertain") if isinstance(llm_judgement, dict) else "uncertain",
        "matched_records": matched_records,
        "records": len(records),
        "terms": terms,
        "task_terms": task_terms,
        "examples": llm_examples,
        "component_candidates": component_candidates,
        "note": "Component candidates are observable hints only. Crawler LLM must judge whether they belong to the target collection before ingest.",
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
        ("tavily", focused_query, "Tavily 搜索并提取正文 Markdown"),
        ("firecrawl", focused_query, "Firecrawl 搜索/抓取正文 Markdown"),
        ("jina", focused_query, "Jina Reader/Search 免费兜底"),
        ("playwright", focused_query, "Playwright 浏览器搜索/渲染，保存正文与 raw HTML"),
        ("web_discovery", focused_query, "公开搜索兜底发现资料源"),
    ]
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
                extra["search_limit"] = 8
            elif source == "modrinth":
                extra.update({"mods": 16, "modpacks": 5, "resourcepacks": 3, "shaders": 1})
            elif source == "followup":
                extra["max_urls"] = 12
            elif source == "web_discovery":
                extra.update({"search_limit": 8, "max_urls": 8})
            elif source == "tavily":
                extra.update({"search_limit": 8, "max_urls": 8, "search_depth": "advanced"})
            elif source == "firecrawl":
                extra.update({"search_limit": 8, "max_urls": 8})
            elif source == "jina":
                extra.update({"search_limit": 8, "max_urls": 8})
            elif source == "playwright":
                extra.update({"search_limit": 6, "max_urls": 4})
            elif source in {"mediawiki", "ftbwiki", "createwiki"}:
                extra["search_limit"] = 8
            tasks.append({"source": source, "query": item_query, "reason": reason, "priority": 50, **extra})
    expanded: list[dict[str, Any]] = []
    for task in tasks:
        source = _source_alias(str(task.get("source") or ""))
        if source in {"mcmod", "tavily", "firecrawl", "jina", "web_discovery", "playwright"} and short_queries:
            limit = 5 if source == "mcmod" else 2
            for index, short_query in enumerate(short_queries[:limit]):
                cloned = dict(task)
                cloned["query"] = short_query
                cloned["priority"] = int(cloned.get("priority") or 50) - index
                cloned["reason"] = f"{cloned.get('reason') or ''}；短查询拆分"
                expanded.append(cloned)
        else:
            expanded.append(task)
    tasks = expanded
    priority = {"mcmod": 100, "modrinth": 90, "ftbwiki": 85, "createwiki": 85, "playwright": 82, "followup": 74, "jina": 72, "web_discovery": 70, "tavily": 66, "firecrawl": 65, "mediawiki": 50}
    for task in tasks:
        source = _source_alias(str(task.get("source") or ""))
        if source == "mcmod":
            task.setdefault("search_limit", 8)
        elif source == "modrinth":
            task.setdefault("mods", 16)
            task.setdefault("modpacks", 5)
            task.setdefault("resourcepacks", 3)
            task.setdefault("shaders", 1)
        elif source == "followup":
            task["max_urls"] = min(int(task.get("max_urls") or 12), 12)
        elif source == "web_discovery":
            task.setdefault("search_limit", 8)
            task["max_urls"] = min(int(task.get("max_urls") or 8), 8)
        elif source == "tavily":
            task.setdefault("search_limit", 8)
            task["max_urls"] = min(int(task.get("max_urls") or 8), 8)
            task.setdefault("search_depth", "advanced")
        elif source == "firecrawl":
            task.setdefault("search_limit", 8)
            task["max_urls"] = min(int(task.get("max_urls") or 8), 8)
        elif source == "jina":
            task.setdefault("search_limit", 8)
            task["max_urls"] = min(int(task.get("max_urls") or 8), 8)
        elif source == "playwright":
            task.setdefault("search_limit", 6)
            task["max_urls"] = min(int(task.get("max_urls") or 4), 4)
        elif source in {"mediawiki", "ftbwiki", "createwiki"}:
            task.setdefault("search_limit", 8)
    tasks.sort(key=lambda item: priority.get(_source_alias(str(item.get("source") or "")), 0), reverse=True)
    return tasks[:max(1, max_tasks)]


def _crawler_task_identity(task: dict[str, Any]) -> tuple[str, str]:
    source = _source_alias(str(task.get("source") or ""))
    query = re.sub(r"\s+", " ", str(task.get("query") or "").strip()).lower()
    return source, query


def _crawler_bad_result(result: dict[str, Any]) -> bool:
    return (
        int(result.get("returncode") or 0) != 0
        or bool(result.get("empty_result"))
        or bool(result.get("off_topic_result"))
        or bool(result.get("uncertain_result"))
    )


def _crawler_failure_summary(task_results: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for result in task_results[-limit:]:
        manifest_stats = result.get("manifest_stats") if isinstance(result.get("manifest_stats"), dict) else {}
        topic_validation = result.get("topic_validation") if isinstance(result.get("topic_validation"), dict) else {}
        reason = "failed"
        if result.get("empty_result"):
            reason = "empty_result"
        elif result.get("off_topic_result"):
            reason = "off_topic_result"
        elif result.get("uncertain_result"):
            reason = "uncertain_result"
        elif int(result.get("returncode") or 0) != 0:
            reason = "command_failed"
        summary.append(
            {
                "source": result.get("source"),
                "query": result.get("query"),
                "reason": reason,
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
    tried = [_crawler_task_identity(task) for task in existing_tasks]
    session_summary = {
        "mode": "mid_job_replan",
        "previous_topic": plan.get("topic") or plan.get("target_hint") or question,
        "delivery_target": plan.get("delivery_target"),
        "cleaning_policy": plan.get("cleaning_policy"),
        "coverage_goals": plan.get("coverage_goals") or [],
        "success_criteria": plan.get("success_criteria") or [],
        "recent_failures": _crawler_failure_summary(task_results),
        "already_planned_tasks": [{"source": source, "query": query} for source, query in tried if source or query],
        "instruction": (
            "Previous crawler tasks were empty, off-topic, or failed. "
            "Revise the plan with short alternative queries and different sources. "
            "Do not repeat already planned source/query pairs. "
            "Keep delivery requirements separate from the data target."
        ),
    }
    replan_question = (
        "Replan crawler collection for this target. "
        "Use short search queries, alternate sources, and avoid repeated attempts. "
        f"Target: {question}"
    )
    new_plan = plan_crawler_tasks_resilient(
        replan_question,
        config.paths.source_dir,
        max_tasks=max(1, max_new_tasks),
        session_summary=session_summary,
    )
    seen = set(tried)
    new_tasks: list[dict[str, Any]] = []
    for task in list(new_plan.get("tasks") or []):
        if not isinstance(task, dict):
            continue
        identity = _crawler_task_identity(task)
        if not identity[1] or identity in seen:
            continue
        seen.add(identity)
        cloned = dict(task)
        cloned["source"] = _source_alias(str(cloned.get("source") or "web_discovery"))
        cloned["reason"] = f"mid-job replan after empty/off-topic results; {cloned.get('reason') or ''}".strip()
        new_tasks.append(cloned)
        if len(new_tasks) >= max_new_tasks:
            break
    if new_tasks:
        replans = plan.setdefault("replans", [])
        if isinstance(replans, list):
            replans.append(
                {
                    "at_result_count": len(task_results),
                    "failure_summary": session_summary["recent_failures"],
                    "new_tasks": new_tasks,
                    "planner": new_plan.get("strategy") or new_plan.get("planner_model") or new_plan.get("raw_plan", {}).get("_planner_model"),
                }
            )
    return new_tasks


def _fallback_tasks_from_topic_discovery(result: dict[str, Any], existing_tasks: list[dict[str, Any]], *, max_new_tasks: int = 16) -> list[dict[str, Any]]:
    manifest_path = (result.get("manifest_stats") or {}).get("manifest_path") if isinstance(result.get("manifest_stats"), dict) else ""
    if not manifest_path:
        return []
    data = _read_json_file(Path(str(manifest_path)))
    seed_queries = data.get("seed_queries") if isinstance(data.get("seed_queries"), list) else []
    seen = {_crawler_task_identity(task) for task in existing_tasks}
    new_tasks: list[dict[str, Any]] = []
    for index, query_value in enumerate(seed_queries):
        query = str(query_value).strip()
        if not query:
            continue
        source = "mcmod" if index < 10 else "tavily"
        task = {
            "source": source,
            "query": query,
            "reason": "topic discovery seed expanded from existing local documents",
            "priority": 95 - index,
            "search_limit": 8,
            "max_urls": 6,
        }
        if source == "tavily":
            task["search_depth"] = "advanced"
        identity = _crawler_task_identity(task)
        if identity in seen:
            continue
        seen.add(identity)
        new_tasks.append(task)
        if len(new_tasks) >= max_new_tasks:
            break
    return new_tasks


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
    existing_brief = [{"source": source, "query": query} for source, query in (_crawler_task_identity(task) for task in existing_tasks)]
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
    seen = {_crawler_task_identity(task) for task in existing_tasks}
    new_tasks: list[dict[str, Any]] = []
    for task in list(plan.get("tasks") or []):
        if not isinstance(task, dict):
            continue
        source = _source_alias(str(task.get("source") or ""))
        query = str(task.get("query") or "").strip()
        if source == "topic_discovery" or not query:
            continue
        cloned = dict(task)
        cloned["source"] = source
        cloned["query"] = query
        cloned["reason"] = f"Crawler LLM reviewed topic discovery candidates; {cloned.get('reason') or ''}".strip()
        identity = _crawler_task_identity(cloned)
        if identity in seen:
            continue
        seen.add(identity)
        new_tasks.append(cloned)
        if len(new_tasks) >= max_new_tasks:
            break
    if new_tasks:
        return new_tasks
    return _fallback_tasks_from_topic_discovery(result, existing_tasks, max_new_tasks=max_new_tasks)


def _run_crawler_job(job: Job, payload: dict[str, Any], config: AppConfig) -> None:
    source = _source_alias(str(payload.get("source") or "planner"))
    question = str(payload.get("source_question") or payload.get("question") or payload.get("query") or "").strip()
    _update_job(job, status="running", started_at=time.time(), summary="Crawler job started.")
    try:
        plan: dict[str, Any] = {}
        if source in {"planner", "auto", "smart", "orchestrator"}:
            session_summary = payload.get("session_summary") if isinstance(payload.get("session_summary"), dict) else None
            max_tasks = int(payload.get("max_tasks") or 16)
            if job.stop_requested:
                _update_job(job, status="stopped", ended_at=time.time(), summary="Crawler 任务已在规划前停止。", error=None)
                return
            if not bool(payload.get("include_completed")):
                plan = _plan_crawler_with_job_timeout(job, question, config, max_tasks, session_summary)
                if plan.get("stopped"):
                    _update_job(job, status="stopped", ended_at=time.time(), summary="Crawler 任务已在规划阶段停止。", error=None, result={"source": "planner", "plan": plan, "planned_tasks": [], "tasks": []})
                    return
                tasks = list(plan.get("tasks") or [])
            else:
                plan = plan_crawler_tasks(question, config.paths.source_dir, max_tasks=max_tasks, include_completed=True)
                tasks = list(plan.get("tasks") or [])
            if job.stop_requested:
                _update_job(job, status="stopped", ended_at=time.time(), summary="Crawler 任务已在规划后停止。", error=None, result={"source": "planner", "plan": plan, "planned_tasks": tasks, "tasks": []})
                return
            if not tasks:
                tasks = _all_source_tasks(question, config, include_completed=True, session_summary=session_summary, max_tasks=max_tasks)
                plan = {"strategy": "fallback_all_source_tasks", "tasks": tasks}
            _update_job(
                job,
                summary=f"Crawler planned {plan.get('topic') or question}: {len(tasks)} collection tasks. Next: probe, verify, retry with alternate sources if needed, then ingest.",
                result={
                    "source": "planner",
                    "plan": plan,
                    "planned_tasks": tasks,
                    "tasks": [],
                    "loop": [
                        {"phase": "understand", "status": "done", "note": "Understand caller, target entity, and missing evidence."},
                        {"phase": "plan", "status": "done", "note": "Crawler LLM produced coverage goals, short queries, and source-specific tasks."},
                        {"phase": "act", "status": "running", "note": "Execute tasks by priority; record returncode, export_dir, and errors for each task."},
                        {"phase": "verify", "status": "pending", "note": "Verify records/skipped/errors and try automatic ingest."},
                    ],
                },
            )
        else:
            tasks = [{"source": source, "query": str(payload.get("query") or question), "reason": "single source request"}]
        task_results: list[dict[str, Any]] = []
        success_count = 0
        candidate_count = 0
        failure_count = 0
        index = 0
        bad_streak = 0
        replan_count = 0
        needs_ingest = False
        max_replans = int(payload.get("max_replans") or 2)
        initial_task_limit = int(payload.get("max_tasks") or len(tasks) or 16)
        max_total_tasks = max(len(tasks), min(32, initial_task_limit + 12))
        while index < len(tasks):
            if job.stop_requested:
                break
            if source in {"planner", "auto", "smart", "orchestrator"}:
                pending_tasks = list(tasks[index:])
                reflection = reflect_crawler_progress(
                    question,
                    plan,
                    task_results,
                    pending_tasks,
                    session_summary=session_summary,
                    max_new_tasks=max(1, min(4, max_total_tasks - len(tasks))),
                )
                plan.setdefault("agent_reflections", []).append(
                    {
                        "at_index": index,
                        "action": reflection.get("action"),
                        "selected_index": reflection.get("selected_index"),
                        "reason": reflection.get("reason"),
                        "planner": reflection.get("planner"),
                        "tasks": reflection.get("tasks") or [],
                    }
                )
                _update_job(
                    job,
                    summary=f"CrawlerAgent 正在思考下一步：{reflection.get('action')}\n理由：{reflection.get('reason')}",
                    result={
                        "source": "planner",
                        "tasks": task_results,
                        "planned_tasks": tasks,
                        "plan": plan,
                        "loop": [
                            {"phase": "understand", "status": "done"},
                            {"phase": "reflect", "status": "running", "note": str(reflection.get("reason") or "")},
                            {"phase": "act", "status": "pending", "note": "CrawlerAgent selected the next tool action; executor has not run it yet."},
                            {"phase": "verify", "status": "pending"},
                        ],
                    },
                )
                action = str(reflection.get("action") or "execute_pending")
                new_tasks = [task for task in list(reflection.get("tasks") or []) if isinstance(task, dict)]
                if action in {"add_tasks", "replan"} and not new_tasks and len(tasks) < max_total_tasks:
                    remaining_slots = max(0, max_total_tasks - len(tasks))
                    new_tasks = _replan_crawler_tasks(
                        question,
                        config,
                        plan,
                        task_results,
                        tasks,
                        max_new_tasks=min(6, remaining_slots),
                    )
                    if new_tasks:
                        plan.setdefault("agent_reflections", []).append(
                            {
                                "at_index": index,
                                "action": "replan_tasks_generated",
                                "reason": "CrawlerAgent requested replan/add_tasks without executable tasks, so the executor asked the Crawler planning LLM to materialize the next tool actions.",
                                "planner": "Crawler replan LLM",
                                "tasks": new_tasks,
                            }
                        )
                if action in {"add_tasks", "replan"} and new_tasks:
                    seen_identities = {_crawler_task_identity(task) for task in tasks}
                    inserted: list[dict[str, Any]] = []
                    for new_task in new_tasks:
                        identity = _crawler_task_identity(new_task)
                        if identity in seen_identities or len(tasks) >= max_total_tasks:
                            continue
                        inserted.append(new_task)
                        seen_identities.add(identity)
                    if inserted:
                        tasks[index:index] = inserted
                        continue
                if action == "finish":
                    plan["agent_finish_reason"] = str(reflection.get("done_summary") or reflection.get("reason") or "")
                    break
                selected_offset = int(reflection.get("selected_index") or 0)
                if selected_offset > 0 and index + selected_offset < len(tasks):
                    tasks[index], tasks[index + selected_offset] = tasks[index + selected_offset], tasks[index]
            task = tasks[index]
            index += 1
            task_source = _source_alias(str(task.get("source") or "mediawiki"))
            task_query = str(task.get("query") or question).strip()
            if not task_query:
                result = {
                    "source": task_source,
                    "returncode": 2,
                    "command": [],
                    "output": "Crawler executor refused to run an empty query. This objective failure is returned to CrawlerAgent for reflection/replanning.",
                    "timeout_seconds": 0,
                    "timed_out": False,
                    "export_dir": "",
                    "query": "",
                    "reason": str(task.get("reason") or ""),
                    "manifest_stats": {"records": 0, "skipped": 0, "errors": 0},
                    "empty_query_result": True,
                    "empty_result": True,
                }
                task_results.append(result)
                failure_count += 1
                bad_streak += 1
                _update_job(
                    job,
                    summary=f"CrawlerAgent 选择了一个空查询，工具层已拒绝执行，等待 CrawlerAgent 重新规划。\n来源：{_source_label(task_source)}",
                    result={
                        "source": "planner",
                        "tasks": task_results,
                        "planned_tasks": tasks,
                        "plan": plan,
                        "loop": [
                            {"phase": "understand", "status": "done"},
                            {"phase": "reflect", "status": "pending", "note": "Previous selected task had an empty query."},
                            {"phase": "act", "status": "blocked", "note": "Tool execution refused empty query."},
                            {"phase": "verify", "status": "pending"},
                        ],
                    },
                )
                continue
            task_payload = dict(payload)
            task_payload.update({"source": task_source, "query": task_query, "question": question})
            for key in (
                "search_limit",
                "max_urls",
                "mods",
                "modpacks",
                "resourcepacks",
                "shaders",
                "search_depth",
                "max_files",
                "max_queries",
                "max_items",
                "output_dir",
                "start_url",
                "timeout_ms",
                "fields",
            ):
                value = task.get(key)
                if value is not None:
                    task_payload[key] = value
            _update_job(
                job,
                summary=f"多源补库运行中：{index}/{len(tasks)} {_source_label(task_source)}\n查询：{task_payload['query']}\n原因：{task.get('reason') or ''}",
                result={
                    "source": "planner",
                    "tasks": task_results,
                    "planned_tasks": tasks,
                    "plan": plan,
                    "loop": [
                        {"phase": "understand", "status": "done"},
                        {"phase": "plan", "status": "done"},
                        {"phase": "act", "status": "running", "note": f"Executing {index}/{len(tasks)}: {task_payload['query']}"},
                        {"phase": "verify", "status": "pending"},
                    ],
                },
            )
            result = _run_crawler_command(_round_command(task_source, task_payload), task_source, job=job)
            result["query"] = str(task_payload.get("query") or "")
            result["reason"] = str(task.get("reason") or "")
            result["manifest_stats"] = _crawler_manifest_stats(str(result.get("export_dir") or ""))
            records_loaded = int(result["manifest_stats"].get("records") or 0)
            existing_evidence = _crawler_reusable_duplicate_evidence(
                str(result.get("export_dir") or ""),
                question,
                str(task_payload.get("query") or ""),
                plan,
            ) if result["returncode"] == 0 and records_loaded == 0 and int(result["manifest_stats"].get("skipped") or 0) > 0 else {"matched": False, "records": []}
            if existing_evidence.get("matched"):
                result["existing_evidence_reused"] = existing_evidence
            if result["returncode"] == 0 and records_loaded > 0:
                result["topic_validation"] = _crawler_topic_match(
                    str(result.get("export_dir") or ""),
                    question,
                    str(task_payload.get("query") or ""),
                    plan,
                )
            if task_source == "modpack_download" and result["returncode"] == 0:
                downloads_loaded = int(result["manifest_stats"].get("downloads") or 0)
                if downloads_loaded > 0:
                    success_count += 1
                    result["archive_downloaded"] = True
                    followup_task = {
                        "source": "modpack_internal",
                        "query": str(task_payload.get("query") or question),
                        "reason": "Crawler downloaded a public modpack archive; parse internal manifest/modlist/quests/scripts next.",
                        "priority": 146,
                    }
                    if _crawler_task_identity(followup_task) not in {_crawler_task_identity(existing) for existing in tasks} and len(tasks) < max_total_tasks:
                        tasks.insert(index, followup_task)
                        plan.setdefault("agent_reflections", []).append(
                            {
                                "at_index": index,
                                "action": "add_tasks",
                                "reason": "公开整合包包体已下载，下一步应解析内部文件，而不是继续只搜网页。",
                                "planner": "executor objective result",
                                "tasks": [followup_task],
                            }
                        )
                else:
                    failure_count += 1
                    result["archive_not_found"] = True
                    result["failure_reason"] = result["manifest_stats"].get("failure_reason") or "未发现可公开直接下载的 .mrpack/.zip 整合包包体。"
            elif task_source == "browser_collect" and result["returncode"] == 0 and records_loaded > 0:
                success_count += 1
                delivery_target = str(plan.get("delivery_target") or payload.get("delivery_target") or "").lower()
                if "rag" in delivery_target or "mcagent" in delivery_target:
                    needs_ingest = True
                    result["ingest_deferred"] = "Crawler will ingest structured browser output for MCagent/RAG after the collection loop finishes."
                else:
                    result["ingest_skipped"] = "Structured browser output was saved to the requested directory for the human user."
            elif task_source == "topic_discovery" and result["returncode"] == 0 and records_loaded > 0:
                candidate_count += 1
                result["candidate_only"] = True
                result["ingest_skipped"] = "topic_discovery candidates are reviewed by Crawler LLM before follow-up collection"
            elif result["returncode"] == 0 and bool(result.get("existing_evidence_reused", {}).get("matched")):
                success_count += 1
                result["ingest_skipped"] = "Crawler reused relevant duplicate-skipped evidence that already exists in the local knowledge base."
            elif result["returncode"] == 0 and records_loaded > 0 and bool(result.get("topic_validation", {}).get("matched")):
                success_count += 1
                needs_ingest = True
                result["ingest_deferred"] = "Crawler will ingest once after the collection loop finishes."
            elif result["returncode"] == 0:
                if records_loaded > 0:
                    topic_reason = str(result.get("topic_validation", {}).get("reason") or "")
                    if topic_reason in {"llm_judge_error_uncertain", "uncertain"}:
                        result["uncertain_result"] = True
                    else:
                        result["off_topic_result"] = True
                else:
                    result["empty_result"] = True
                failure_count += 1
            else:
                failure_count += 1
            task_results.append(result)
            if task_source == "topic_discovery" and result["returncode"] == 0:
                remaining_slots = max(0, max_total_tasks - len(tasks))
                _update_job(
                    job,
                    summary="Crawler 正在审核主题发现候选：由 Crawler LLM 判断哪些候选值得继续采集。",
                    result={
                        "source": "planner",
                        "tasks": task_results,
                        "planned_tasks": tasks,
                        "plan": plan,
                        "loop": [
                            {"phase": "understand", "status": "done"},
                            {"phase": "plan", "status": "done"},
                            {"phase": "act", "status": "running"},
                            {"phase": "reviewing_candidates", "status": "running", "note": "Topic discovery produced candidates; Crawler LLM is judging what to expand next."},
                            {"phase": "verify", "status": "pending"},
                        ],
                    },
                )
                discovered_tasks = _llm_tasks_from_topic_discovery(question, config, result, tasks, max_new_tasks=min(16, remaining_slots))
                if discovered_tasks:
                    tasks.extend(discovered_tasks)
                    plan.setdefault("discovery_expansions", []).append(
                        {
                            "at_result_count": len(task_results),
                            "source_query": result.get("query"),
                            "reviewer": "Crawler LLM",
                            "new_tasks": discovered_tasks,
                        }
                    )
                elif result.get("topic_discovery_review_error"):
                    plan.setdefault("discovery_expansions", []).append(
                        {
                            "at_result_count": len(task_results),
                            "source_query": result.get("query"),
                            "reviewer": "Crawler LLM",
                            "error": result.get("topic_discovery_review_error"),
                            "new_tasks": [],
                        }
                    )
            if _crawler_bad_result(result):
                bad_streak += 1
            else:
                bad_streak = 0
            if (
                source in {"planner", "auto", "smart", "orchestrator"}
                and success_count == 0
                and bad_streak >= 3
                and replan_count < max_replans
                and len(tasks) < max_total_tasks
            ):
                replan_count += 1
                _update_job(
                    job,
                    summary=f"Crawler detected {bad_streak} empty/off-topic/failed results. Replanning queries and sources ({replan_count}/{max_replans}).",
                    result={
                        "source": "planner",
                        "tasks": task_results,
                        "planned_tasks": tasks,
                        "plan": plan,
                        "loop": [
                            {"phase": "understand", "status": "done"},
                            {"phase": "plan", "status": "done"},
                            {"phase": "act", "status": "running"},
                            {"phase": "replan", "status": "running", "note": "Recent crawler results were empty, off-topic, or failed. Asking Crawler LLM to revise source/query choices."},
                            {"phase": "verify", "status": "pending"},
                        ],
                    },
                )
                remaining_slots = max(0, max_total_tasks - len(tasks))
                new_tasks = _replan_crawler_tasks(
                    question,
                    config,
                    plan,
                    task_results,
                    tasks,
                    max_new_tasks=min(6, remaining_slots),
                )
                if new_tasks:
                    tasks.extend(new_tasks)
                bad_streak = 0
        status = "stopped" if job.stop_requested else ("succeeded" if success_count else "failed")
        summary = f"资料补库完成：资料成功 {success_count}，候选发现 {candidate_count}，失败 {failure_count}。"
        if job.stop_requested:
            summary = f"Crawler 任务已停止：已完成 {len(task_results)}/{len(tasks)} 个任务，资料成功 {success_count}，候选发现 {candidate_count}，失败 {failure_count}。"
        elif needs_ingest:
            summary += " 已启动后台入库。"
        collection_summary = _crawler_result_summary(task_results, plan)
        _update_job(
            job,
            status=status,
            ended_at=time.time(),
            summary=summary,
            error=None if success_count or job.stop_requested else "all crawler sources failed",
            result={
                "source": "planner",
                "success_count": success_count,
                "candidate_count": candidate_count,
                "failure_count": failure_count,
                "replan_count": replan_count,
                "ingest": None,
                "ingest_error": "",
                "ingest_background": bool(needs_ingest and not job.stop_requested),
                "tasks": task_results,
                "planned_tasks": tasks,
                "plan": plan,
                "collection_summary": collection_summary,
                "loop": [
                    {"phase": "understand", "status": "done"},
                    {"phase": "plan", "status": "done"},
                    {"phase": "act", "status": "done", "note": f"Succeeded {success_count}; failed {failure_count}"},
                    {"phase": "ingest", "status": "running" if needs_ingest and not job.stop_requested else "skipped", "note": "Background ingest started." if needs_ingest and not job.stop_requested else "No new records require ingest."},
                    {"phase": "verify", "status": "done" if success_count else ("stopped" if job.stop_requested else "failed"), "note": "Task results keep records/skipped/errors for the next retry."},
                ],
            },
        )
        if needs_ingest and not job.stop_requested:
            threading.Thread(target=_run_background_ingest, args=(job.id, config), daemon=True).start()
        append_memory_event("crawler_plan_completed", {"job_id": job.id, "question": question, "success_count": success_count, "candidate_count": candidate_count, "failure_count": failure_count, "summary": collection_summary, "tasks": task_results})
    except Exception as exc:  # noqa: BLE001
        _update_job(job, status="failed", ended_at=time.time(), summary=_tail_text(traceback.format_exc()), error=f"{type(exc).__name__}: {exc}")


def _plan_crawler_with_job_timeout(job: Job, question: str, config: AppConfig, max_tasks: int, session_summary: dict[str, Any] | None) -> dict[str, Any]:
    planner_topic = question
    handoff_brief = ""
    if isinstance(session_summary, dict):
        handoff_brief = str(session_summary.get("handoff_brief") or "").strip()
        planner_topic = str(session_summary.get("current_topic") or session_summary.get("target") or question).strip() or question
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(plan_crawler_tasks_resilient, question, config.paths.source_dir, max_tasks=max_tasks, session_summary=session_summary)
    started = time.time()
    last_notice = 0.0
    try:
        while True:
            if job.stop_requested:
                future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                return {"strategy": "stopped_before_planner_finished", "topic": planner_topic, "handoff_brief": handoff_brief, "tasks": [], "stopped": True}
            try:
                return future.result(timeout=0.5)
            except concurrent.futures.TimeoutError:
                elapsed = time.time() - started
                if elapsed - last_notice >= 5:
                    last_notice = elapsed
                    _update_job(
                        job,
                        summary=f"CrawlerAgent 正在理解任务并规划采集动作，已思考 {int(elapsed)} 秒。目标：{planner_topic[:80]}",
                        result={
                            "source": "planner",
                            "plan": {"topic": planner_topic, "handoff_brief": handoff_brief, "delivery_target": (session_summary or {}).get("delivery_target") if isinstance(session_summary, dict) else ""},
                            "planned_tasks": [],
                            "tasks": [],
                            "loop": [
                                {"phase": "understand", "status": "running", "note": "CrawlerAgent is reading the request, memory, and available tools."},
                                {"phase": "plan", "status": "pending"},
                                {"phase": "act", "status": "pending"},
                                {"phase": "verify", "status": "pending"},
                            ],
                        },
                    )
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
    if isinstance(raw, str) and raw.strip().lower() in {"auto", "none", "null", "unlimited", "不限制", "无限制"}:
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
    return DEFAULT_ANSWER_MAX_TOKENS


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


def _selected_llm_client(config: AppConfig, model: str, temperature: float, agent: str = "mcagent_rag") -> tuple[OpenAICompatibleClient, str]:
    profile = resolve_profile_from_model(config, model, agent=agent)
    if profile:
        return client_from_profile(
            profile,
            temperature=temperature,
            timeout_seconds=max(config.ollama.timeout_seconds, int(profile.get("timeout_seconds") or 180)),
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
            timeout_seconds=max(config.ollama.timeout_seconds, int(template.get("timeout_seconds") or 180)),
        )
    endpoint_config = OllamaConfig(
        base_url=config.ollama.base_url,
        model=model or config.ollama.model,
        temperature=temperature,
        timeout_seconds=config.ollama.timeout_seconds,
    )
    return OllamaOpenAIClient(endpoint_config), f"Ollama {endpoint_config.model}"


def _build_answer_prompt(question: str, context: str, retrieval_note: str = "") -> str:
    note = f"\n{retrieval_note}\n" if retrieval_note else ""
    return f"""问题：{question}
{note}
MCagent 可用工具与能力：
- local_rag_search：检索本地资料库，适合回答 Minecraft、模组、整合包、教程、物品、Boss、配方等问题。
- crawler_status：查看 Crawler 采集/入库/任务进度。用户问“状态、进度、监控、入库怎么样”等，应使用这个能力。
- delegate_crawler：把资料缺口交给 CrawlerAgent。用户明确说“叫/让 Crawler 收集/获取/爬取/补库某资料”，或本地证据不足时，应使用这个能力。
- answer_from_evidence：根据检索证据组织最终回答，并标注 [S1]、[S2] 来源。

工具使用原则：
- 先理解用户原始话，再结合会话上下文；不要让改写后的检索词覆盖用户第一手意图。
- 工具函数只负责检索、状态、派单和客观抽取；是否足够回答、如何组织答案，由 MCagent 基于证据判断。
- 如果用户是在下达 Crawler 任务，不要把这句话当普通 RAG 关键词检索。

本地检索资料：
{context}

请只根据以上资料回答，并使用 [S1]、[S2] 等标记引用来源。若资料只能给出部分答案，要明确说明缺口；如果用户问“有哪些/列出/包含什么/前15个”，优先逐行提取资料中的名称列表，不要编造资料外名称。列表类问题要特别注意教程段落里“合成/获得/需要/要求”后面出现的物品名；同一来源里出现多个名称时要尽量全部列出，而不是只摘前几个。"""


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
    if clean.startswith(("source:", "score:", "url:", "Fetched", "Created", "Updated", "Search query:", "Search snippet:")):
        return ""
    if re.match(r"^(Tavily source|Query|Snippet|Search query|Search snippet)\s*[:：*]", clean, flags=re.I):
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
    except Exception as exc:  # noqa: BLE001 - keep chat usable if remote/local LLM times out.
        primary_error = exc
        if model == config.ollama.model or model.startswith("local:ollama:") or bool(context_override):
            answer = f"模型调用失败：{primary_error}\n\n我没有用本地抽取结果替代模型最终回答。当前证据已随来源返回，修复模型连接后可重新生成。"
            return answer, context
        try:
            fallback = OllamaOpenAIClient(config.ollama)
            answer = fallback.chat(messages, temperature=temperature, max_tokens=max_tokens)
        except Exception as fallback_exc:  # noqa: BLE001
            answer = (
                f"模型调用失败：首选模型失败：{primary_error}；本地 Ollama 也失败：{fallback_exc}\n\n"
                "我没有用本地抽取结果替代模型最终回答。当前检索证据会随本次回复返回，修复模型连接后可重新生成。"
            )
        else:
            answer = (
                f"{answer}\n\n\u6a21\u578b\uff1aOllama {config.ollama.model}"
                f"\n\u5907\u6ce8\uff1a\u9996\u9009\u6a21\u578b\u8c03\u7528\u5931\u8d25\uff0c\u5df2\u81ea\u52a8\u964d\u7ea7\u5230\u672c\u5730 Ollama\u3002\u5931\u8d25\u539f\u56e0\uff1a{primary_error}"
            )
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
    extraction_note = _list_extraction_note(evidence_question, results)
    recipe_note = _recipe_extraction_note(evidence_question, results)
    merged_note = "\n".join(part for part in (retrieval_note, relation_note, extraction_note, recipe_note) if part)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_answer_prompt(question, context, merged_note)},
    ]
    return messages, context


def _build_direct_answer_messages(
    original_question: str,
    contextual_question: str,
    session_summary: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    summary_text = json.dumps(session_summary or {}, ensure_ascii=False)
    user_text = (
        f"用户原话：{original_question}\n"
        f"当前会话理解：{contextual_question}\n"
        f"会话摘要：{summary_text}\n\n"
        "请直接自然回复用户。不要声称查过本地资料库，不要编造来源，不要调用 Crawler。"
    )
    return [
        {
            "role": "system",
            "content": (
                "你是 MCagent，一个可以自然对话、也可以在需要时使用工具的资料助手。"
                "本轮已经由 Agent 判断为不需要工具；请简洁、友好、按上下文直接回答。"
            ),
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
) -> str:
    messages = _build_direct_answer_messages(original_question, contextual_question, session_summary)
    client, model_label = _selected_llm_client(config, model, temperature)
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
) -> str:
    messages = _build_direct_answer_messages(original_question, contextual_question, session_summary)
    client, model_label = _selected_llm_client(config, model, temperature)
    chunks = _collect_streaming_answer(client, messages, temperature, max_tokens, emit_delta, emit_thinking)
    answer = "".join(chunks).strip()
    if not answer:
        retry_tokens = min(max((max_tokens or 0) * 4, 1000), 4000)
        chunks = _collect_streaming_answer(client, messages, temperature, retry_tokens, emit_delta, emit_thinking)
        answer = "".join(chunks).strip()
    return f"{answer}\n\n模型：{model_label}" if answer else "我在。"


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
        primary_error = exc
        if model == config.ollama.model or model.startswith("local:ollama:") or bool(context_override):
            answer = f"模型调用失败：{primary_error}\n\n我没有用本地抽取结果替代模型最终回答。当前证据已保留在来源中，可稍后重试模型生成。"
            return answer, context
        try:
            fallback = OllamaOpenAIClient(config.ollama)
            chunks = _collect_streaming_answer(fallback, messages, temperature, max_tokens, emit_delta, emit_thinking)
            answer = "".join(chunks).strip()
            if not answer:
                retry_tokens = min(max((max_tokens or 0) * 4, 4000), 8000)
                chunks = _collect_streaming_answer(fallback, messages, temperature, retry_tokens, emit_delta, emit_thinking)
                answer = "".join(chunks).strip()
            if not answer:
                raise RuntimeError("local Ollama streaming completed without visible answer content")
            answer = (
                f"{answer}\n\n模型：Ollama {config.ollama.model}"
                f"\n备注：首选模型调用失败，已降级到本地 Ollama。失败原因：{primary_error}"
            )
        except Exception as fallback_exc:  # noqa: BLE001
            answer = (
                f"模型调用失败：首选模型失败：{primary_error}；本地 Ollama 也失败：{fallback_exc}\n\n"
                "我没有用工具抽取结果替代模型最终回答。当前检索证据会随本次回复返回，修复模型连接后可重新生成。"
            )
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

    joined = "\n".join(snippets)
    if "FTB" in joined or "任务" in joined:
        add("先跟着整合包内的 FTB 任务线推进，它是当前资料里最明确的新手引导。")
    if "拔刀剑" in joined or "梦想一心" in joined:
        add("前期围绕拔刀剑路线发育；资料中特别提到可以先做需要 Boss 前置的拔刀剑，并利用后续教程推进到梦想一心。")
    if "女仆" in joined or "杀敌数" in joined:
        add("开局送的女仆要利用起来：把需要刷杀敌数的拔刀剑交给女仆，可以减轻前期刷怪压力。")
    if "tacz" in joined.lower() or "枪械" in joined:
        add("枪械/TACZ 也是核心流派之一，打 Boss 或推进战斗内容时可以作为主要输出手段之一。")
    if "下亚" in joined or "亚波伦" in joined or "Boss" in joined:
        add("中后期再考虑 Boss 线；本地资料提到下亚/亚波伦相关打法，但完整 Boss 顺序和掉落仍需要继续补资料。")
    if not steps:
        for line in snippets[:4]:
            if _looks_like_page_title_or_external_download(line):
                continue
            add(line)

    lines = ["基于当前本地资料，落幕曲新手可以这样起步：", ""]
    lines.extend(f"{index}. {step}" for index, step in enumerate(steps[:6], start=1))
    lines.append("")
    lines.append(f"说明：以上依据当前命中的本地资料整理 [S{source_rank}]；更完整的任务章节、装备/饰品推荐和 Boss 顺序还需要继续补库。")
    return "\n".join(lines)


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
    snippets: list[str] = []
    for item in results[:8]:
        text = item.text if fast else _read_result_full_text(item)
        lines = _evidence_lines_from_text(text, focus_terms, limit=4)
        if focus_terms:
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
                if len(snippets) >= limit:
                    return snippets
    return snippets


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


def _evidence_lines_from_text(text: str, focus_terms: list[str], limit: int = 18) -> list[str]:
    if not text:
        return []
    strong_terms = [term for term in focus_terms if len(term) >= 2]
    evidence_markers = (
        "合成", "配方", "材料", "获取", "获得", "掉落", "生成", "刷新", "位置",
        "步骤", "路线", "前置", "要求", "打法", "击败", "打败", "挑战", "击杀", "斩杀", "胜利", "机制", "奖励", "用途",
        "Boss", "BOSS", "boss", "表", "图片", "Table", "Image",
    )
    output: list[str] = []
    active_window = 0
    for raw_line in text.splitlines():
        is_heading = raw_line.lstrip().startswith("#")
        line = _clean_evidence_line(raw_line)
        if not line or _noisy_evidence_line(line):
            continue
        has_focus = any(term.lower() in line.lower() for term in strong_terms)
        if has_focus:
            active_window = 8
        has_marker = any(marker in line for marker in evidence_markers)
        if has_focus or (active_window > 0 and (has_marker or is_heading)):
            if line not in output:
                output.append(line)
                if len(output) >= limit:
                    break
        if active_window > 0:
            active_window -= 1
    return output


def _is_boss_focus(focus_terms: list[str]) -> bool:
    return any(term.lower() in {"boss", "首领", "头目"} or term in {"BOSS", "Boss"} for term in focus_terms)


def _answer_indicates_missing_data(answer: str) -> bool:
    lowered = answer.lower()
    if "本地整合包清单里解析到" in answer:
        return False
    if "本地资料里能明确点名的 Boss/类 Boss 目标有" in answer:
        return False
    if "本地证据抽取兜底" in answer and "本地资料中找到" in answer:
        return False
    if _answer_missing_recipe_details(answer):
        return True
    if _answer_has_partial_list(answer):
        return False
    markers = (
        "本地资料库未找到可靠答案",
        "未找到可靠答案",
        "资料不足",
        "证据不足",
        "无法回答",
        "无法获取",
        "无法确定",
        "未给出",
        "未列出",
        "并未给出",
        "并未列出",
        "没有给出",
        "没有列出",
        "仅列出",
        "endpoint returned",
        "timeout",
        "not enough information",
        "no reliable answer",
        "not found in the local",
    )
    return any(marker.lower() in lowered for marker in markers)


def _answer_missing_recipe_details(answer: str) -> bool:
    recipe_markers = ("合成表", "合成配方", "合成配方表", "具体合成", "具体的合成", "详细配方", "具体配方", "摆放方式", "九宫格", "JEI")
    missing_markers = ("未提供", "未找到", "没有", "并未", "无法提供", "缺少", "未列出")
    return any(recipe in answer for recipe in recipe_markers) and any(marker in answer for marker in missing_markers)


def _answer_has_partial_list(answer: str) -> bool:
    if "模型调用失败" in answer or "endpoint returned" in answer.lower() or "timeout" in answer.lower():
        return False
    if any(name in answer for name in ("幻魔", "雪鸦", "冻樱", "明兽", "天元刀", "天星刀", "梦想一心")):
        return True
    if not any(marker in answer for marker in ("部分", "不完整", "并非", "仅列出", "只覆盖")):
        return False
    names = re.findall(r"[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9：:·'’_-]{1,24}", answer)
    stop = {"本地资料库", "可靠答案", "来源", "模型", "DeepSeek", "Ollama", "补库动作", "当前回答"}
    useful = [name for name in names if name not in stop and not name.startswith("本地")]
    return len(useful) >= 4


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
    for raw_path in raw_files[:1000]:
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
    retriever = Retriever(config)
    existing_docs = {int(item.document_id) for item in results}
    existing_titles = {_canonical_title_key(item.title) for item in results}
    additions: list[SearchResult] = []
    queries = [_same_theme_tutorial_query(question), intent.entity, *intent.keywords[1:8], *intent.search_queries[:4]]
    queries = _dedupe_strings([str(query) for query in queries if query])
    for keyword in queries:
        try:
            candidates = retriever.search(str(keyword), top_k=60)
        except Exception:
            continue
        for item in candidates:
            path = item.source_path.lower().replace("\\", "/")
            title_key = _canonical_title_key(item.title)
            is_tutorial = any(token in item.title for token in ("攻略", "教程", "配置", "制作"))
            same_theme = "落幕曲" in item.title or "closing song" in item.title.lower()
            if "crawler_exports/mediawiki/" in path or int(item.document_id) in existing_docs or title_key in existing_titles:
                continue
            if len(additions) >= max(2, limit) and not is_tutorial and not same_theme:
                continue
            existing_docs.add(int(item.document_id))
            existing_titles.add(title_key)
            additions.append(item)
            if is_tutorial and same_theme:
                break
            break
    return _dedupe_results([*results, *additions], limit=limit)


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
                "- \u53ef\u8f93\u5165\u201c\u72b6\u6001\u201d\u67e5\u770b\u8fdb\u5ea6\u3002"
            )
        if requested_by == "user_via_mcagent":
            return (
                "\n\n\u91c7\u96c6\u4efb\u52a1\uff1a\u6211\u5df2\u628a\u4f60\u7684\u8bf7\u6c42\u8f6c\u8fbe\u7ed9 CrawlerAgent\u3002\n"
                f"- \u4efb\u52a1ID\uff1a{job.id}\n"
                f"- \u8f6c\u8fbe\u76ee\u6807\uff1a{question}\n"
                f"- \u4ea4\u4ed8\u5bf9\u8c61\uff1a{target_text}\n"
                "- Crawler \u4f1a\u81ea\u5df1\u89c4\u5212\u5173\u952e\u8bcd\u548c\u6765\u6e90\uff0c\u91c7\u96c6\u540e\u6309 MCagent/RAG \u53ef\u8bfb\u683c\u5f0f\u6e05\u6d17\u5165\u5e93\u3002\n"
                "- \u53ef\u8f93\u5165\u201c\u72b6\u6001\u201d\u67e5\u770b\u8fdb\u5ea6\u3002"
            )
        return (
            "\n\n\u8865\u5e93\u52a8\u4f5c\uff1aMCagent \u5224\u65ad\u5f53\u524d\u8d44\u6599\u4e0d\u8db3\uff0c\u5df2\u628a\u8d44\u6599\u7f3a\u53e3\u4ea4\u7ed9 CrawlerAgent\u3002\n"
            f"- \u4efb\u52a1ID\uff1a{job.id}\n"
            f"- \u7f3a\u53e3\u4e3b\u9898\uff1a{question}\n"
            "- Crawler \u4f1a\u81ea\u884c\u89c4\u5212\u641c\u7d22\u8bcd\u3001\u9009\u62e9\u6570\u636e\u6e90\u3001\u6293\u53d6 Markdown/manifest/raw HTML\uff0c\u5e76\u5728\u5b8c\u6210\u540e\u81ea\u52a8\u5165\u5e93\u3002\n"
            "- \u53ef\u8f93\u5165\u201c\u72b6\u6001\u201d\u67e5\u770b\u8fdb\u5ea6\u3002"
        )
    prefix = "\u5df2\u6709 Crawler \u4efb\u52a1\u5728\u8fd0\u884c\uff0c\u672c\u6b21\u4e0d\u91cd\u590d\u521b\u5efa\u3002" if requested_by == "user" else "\u5df2\u6709 Crawler \u4efb\u52a1\u5728\u8fd0\u884c\uff0c\u672c\u6b21\u4e0d\u91cd\u590d\u6d3e\u5355\u3002"
    return f"\n\n\u91c7\u96c6\u4efb\u52a1\uff1a{prefix}\n- \u5f53\u524d\u4efb\u52a1ID\uff1a{job.id}\n- \u72b6\u6001\uff1a{job.status}"


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


def _delegate_crawler_for_missing_data(config: AppConfig, payload: dict[str, Any], question: str, plan: dict[str, Any] | None = None) -> tuple[Job, bool]:
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
    crawler_payload["max_tasks"] = int(payload.get("max_tasks") or crawler_payload.get("max_tasks") or 16)
    crawler_payload["requested_by"] = handoff["requested_by"]
    crawler_payload["handoff_from"] = handoff["handoff_from"]
    crawler_payload["original_user_request"] = handoff["original_user_request"]
    crawler_payload["delivery_target"] = str(payload.get("delivery_target") or _infer_delivery_target(collection_question, session_summary))
    explicit_collection_target = str((session_summary or {}).get("collection_target") or "").strip()
    planner_collection_target = explicit_collection_target or collection_question
    planner_summary = dict(session_summary or {})
    planner_summary.update(
        {
            "requested_by": crawler_payload["requested_by"],
            "handoff_from": crawler_payload["handoff_from"],
            "original_user_request": crawler_payload["original_user_request"],
            "delivery_target": crawler_payload["delivery_target"],
            "collection_target": planner_collection_target,
        }
    )
    crawler_payload["session_summary"] = planner_summary
    job, created = _start_job(
        "crawler",
        "Crawler 采集任务" if crawler_payload["delivery_target"].lower() == "human" else "Crawler 多源补库 -> RAG",
        lambda item: _run_crawler_job(item, crawler_payload, config),
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
        client, label = _selected_llm_client(config, model, 0.0)
        prompt = (
            "你是 MCagent 写给 CrawlerAgent 的交接摘要生成器。\n"
            "你的任务不是搜索、不是回答用户、不是拆关键词，而是把这次委托完整说明给 CrawlerAgent。\n"
            "交接摘要必须包含：调用关系、用户原话、转达目标、相关会话背景、已知资料缺口、交付对象、交付要求。\n"
            "如果用户原话依赖上下文，就用 session_summary 和 mcagent_gap_summary 补充背景；如果不依赖上下文，也要保留原始目标。\n"
            "输出 JSON：{\"handoff_brief\":\"给 CrawlerAgent 的完整交接摘要\", \"reason\":\"一句简短理由\"}\n"
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
        value = _json_object_from_llm_text(raw_text)
        resolved = str(value.get("handoff_brief") or "").strip()
        reason = str(value.get("reason") or label).strip()
        if resolved:
            return resolved[:900], reason[:300]
    except Exception:
        pass
    return fallback[:900], "使用会话摘要生成通用委托交接说明。"


def _delegation_handoff(payload: dict[str, Any], original_question: str, cleaned_question: str) -> dict[str, str]:
    agent = str(payload.get("agent") or "mcagent_rag")
    explicit = str(payload.get("requested_by") or "").strip()
    if explicit:
        requested_by = explicit
    elif agent == "crawler_agent":
        requested_by = "user"
    elif _user_explicitly_asked_mcagent_to_tell_crawler(original_question):
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


def _clean_crawler_task_question(question: str) -> str:
    value = str(question).strip()
    value = re.sub(r"^\s*(?:\u8bf7|\u9ebb\u70e6|\u5e2e\u6211|\u5e2e\u5fd9)?\s*(?:\u544a\u8bc9|\u53eb|\u8ba9|\u6d3e|\u901a\u77e5)?\s*(?:MCagent|MCAgent|MC Agent)?\s*(?:\u53bb)?\s*(?:\u544a\u8bc9|\u53eb|\u8ba9|\u6d3e|\u901a\u77e5)?\s*(?:CrawlerAgent|Crawler|\u722c\u866bAgent|\u722c\u866bagent|\u722c\u866b)\s*(?:\u4f60|\u4ed6)?\s*(?:\u8ba9\u4ed6)?\s*(?:\u53bb|\u6765|\u5e2e\u6211|\u5e2e\u5fd9|\u7ee7\u7eed)?\s*(?:\u6536\u96c6|\u91c7\u96c6|\u83b7\u53d6|\u6293\u53d6|\u722c\u53d6|\u8865\u5145|\u8865\u5e93|\u66f4\u65b0\u8d44\u6599)?\s*", "", value, flags=re.I)
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


def _append_session(payload: dict[str, Any], question: str, answer: str, sources: list[dict[str, Any]]) -> None:
    session_id = str(payload.get("session_id") or "default")
    with SESSIONS_LOCK:
        history = SESSIONS.setdefault(session_id, [])
        history.append({"time": time.time(), "question": question, "answer": answer, "sources": sources})
        _update_session_summary_locked(session_id, history[-1])
        del history[:-80]


def _session_history(payload: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
    session_id = str(payload.get("session_id") or "default")
    with SESSIONS_LOCK:
        server_history = list(SESSIONS.get(session_id, []))[-limit:]
    payload_history = _payload_history(payload, limit=limit)
    if not payload_history:
        return server_history
    if not server_history:
        return payload_history
    seen = {(str(item.get("question") or ""), str(item.get("answer") or "")[:120]) for item in server_history}
    merged = list(server_history)
    for item in payload_history:
        key = (str(item.get("question") or ""), str(item.get("answer") or "")[:120])
        if key not in seen:
            merged.append(item)
            seen.add(key)
    return merged[-limit:]


def _session_summary(payload: dict[str, Any]) -> dict[str, Any]:
    explicit = payload.get("session_summary") if isinstance(payload.get("session_summary"), dict) else {}
    session_id = str(payload.get("session_id") or "default")
    with SESSIONS_LOCK:
        summary = dict(SESSION_SUMMARIES.get(session_id) or {})
    if not summary:
        summary = _summary_from_history(_payload_history(payload, limit=20))
    if explicit:
        merged = dict(summary)
        for key, value in explicit.items():
            if isinstance(value, list) and isinstance(merged.get(key), list):
                merged[key] = _merge_limited(list(merged.get(key) or []), [str(item) for item in value], limit=80)
            elif value not in (None, "", []):
                merged[key] = value
        return merged
    return summary


def _payload_history(payload: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
    raw = payload.get("history")
    if not isinstance(raw, list):
        return []
    turns: list[dict[str, Any]] = []
    pending_question = ""
    pending_time = time.time()
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        if role == "user":
            pending_question = text
            try:
                pending_time = float(item.get("time") or time.time() * 1000) / 1000
            except (TypeError, ValueError):
                pending_time = time.time()
            continue
        if role != "assistant" or not pending_question:
            continue
        if text in {"处理中...", "处理 中...", "Processing..."}:
            continue
        turns.append(
            {
                "time": pending_time,
                "question": pending_question,
                "answer": text,
                "sources": item.get("sources") if isinstance(item.get("sources"), list) else [],
            }
        )
        pending_question = ""
        if len(turns) > limit * 2:
            turns = turns[-limit:]
    return turns[-limit:]


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
        summary["topics"] = _merge_limited(summary.get("topics") or [], topics, limit=24)
        summary["names"] = _merge_limited(summary.get("names") or [], names, limit=40)
        summary["gaps"] = _merge_limited(summary.get("gaps") or [], gaps, limit=24)
        summary["entities"] = _merge_limited(summary.get("entities") or [], [*topics[:8], *names[:16]], limit=48)
    return summary


def _delete_session(session_id: str) -> dict[str, Any]:
    session_id = session_id or "default"
    with SESSIONS_LOCK:
        had_history = session_id in SESSIONS
        had_summary = session_id in SESSION_SUMMARIES
        SESSIONS.pop(session_id, None)
        SESSION_SUMMARIES.pop(session_id, None)
    return {"session_id": session_id, "deleted": had_history or had_summary}


def _update_session_summary_locked(session_id: str, turn: dict[str, Any]) -> None:
    summary = SESSION_SUMMARIES.setdefault(session_id, {"topics": [], "entities": [], "names": [], "gaps": [], "turn_count": 0})
    summary["turn_count"] = int(summary.get("turn_count") or 0) + 1
    question = str(turn.get("question") or "")
    answer = _strip_answer_metadata(str(turn.get("answer") or ""))
    topics = _fallback_focus_terms(question)
    names = _extract_context_names(answer, limit=24)
    gaps = _extract_context_gaps(answer, limit=12)
    for source in turn.get("sources") or []:
        if isinstance(source, dict):
            topics.extend(_fallback_focus_terms(str(source.get("title") or "")))
    summary["topics"] = _merge_limited(summary.get("topics") or [], topics, limit=24)
    summary["names"] = _merge_limited(summary.get("names") or [], names, limit=40)
    summary["gaps"] = _merge_limited(summary.get("gaps") or [], gaps, limit=24)
    summary["entities"] = _merge_limited(summary.get("entities") or [], [*topics[:8], *names[:16]], limit=48)


def _merge_limited(existing: list[Any], new_items: list[Any], limit: int) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for item in [*existing, *new_items]:
        value = str(item).strip()
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        output.append(value)
        if len(output) >= limit:
            break
    return output


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


def _is_crawler_status_request(question: str) -> bool:
    normalized = question.strip().lower()
    if _is_crawler_start_request(question):
        return False
    tokens = (
        "状态",
        "进度",
        "监控",
        "入库",
        "status",
        "progress",
    )
    if normalized in {"crawler", "crawleragent"}:
        return True
    return any(token in normalized for token in tokens)


def _is_crawler_start_request(question: str) -> bool:
    lowered = question.lower()
    target_markers = ("crawler", "crawleragent", "爬虫", "爬虫agent", "补库", "资料库")
    explicit_data_markers = ("全网", "联网", "网上", "网络", "网页", "资料", "数据", "文档", "页面", "html", "raw html")
    collection_actions = ("采集", "收集", "抓取", "爬取", "补充", "补库", "更新资料", "crawl", "fetch", "scrape")
    if any(target in lowered for target in target_markers):
        return any(action in lowered for action in ("叫", "让", "派", "去", "帮", *collection_actions)) or any(target in lowered for target in ("补库", "资料库"))
    if any(action in lowered for action in collection_actions) and any(marker in lowered for marker in explicit_data_markers):
        return True
    return False


def _mcagent_route_intent(question: str, agent: str) -> str:
    if agent == "crawler_agent":
        lowered = question.strip().lower()
        if not question.strip() or lowered in {"crawler", "crawleragent"}:
            return "status"
        if _is_crawler_start_request(question):
            return "delegate_crawler"
        if _is_crawler_status_request(question):
            return "status"
        return "delegate_crawler"
    if _is_crawler_start_request(question):
        return "delegate_crawler"
    if _is_crawler_status_request(question):
        return "status"
    return "answer"


def _json_object_from_llm_text(text: str) -> dict[str, Any]:
    stripped = str(text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    match = re.search(r"\{.*\}", stripped, flags=re.S)
    if match:
        stripped = match.group(0)
    value = json.loads(stripped)
    if not isinstance(value, dict):
        raise ValueError("LLM did not return a JSON object")
    return value


def _agent_tool_decision(
    config: AppConfig,
    payload: dict[str, Any],
    *,
    agent: str,
    original_question: str,
    contextual_question: str,
    session_summary: dict[str, Any],
    model: str,
) -> dict[str, Any]:
    """Let the active Agent LLM choose which tool to execute.

    This function is a tool selector, not the answer generator. It must not
    decide final content; it only returns the next tool for the backend to run.
    """
    if agent == "retriever_only" or bool(payload.get("no_llm")):
        return {
            "tool": "answer",
            "reason": "仅检索模式或禁用 LLM，直接进入本地 RAG。",
            "planner": "runtime",
        }
    try:
        client, label = _selected_llm_client(config, model, 0.0)
        catalog = tool_catalog_prompt(agent)
        allowed_tools = "|".join(tool_names_for_agent(agent))
        prompt = (
            "你是当前对话里的 Agent 工具选择器，只决定下一步使用哪个工具，不回答用户问题。\n"
            "参与者：用户、MCagent、CrawlerAgent。\n"
            "下面是本项目统一 Agent Runtime 暴露给当前 Agent 的工具目录。工具目录是能力说明，不是关键词触发规则。\n"
            f"{catalog}\n"
            "角色约束：如果 active_agent 是 crawler_agent，CrawlerAgent 不是问答 RAG 助手。用户直接给 CrawlerAgent 的资料采集、网页抓取、保存文件、补库、给 MCagent/RAG 准备数据等目标，应选择 delegate_crawler。只有用户明确询问 CrawlerAgent 能力、已有任务状态、或不是采集目标的闲聊说明时，才选择 direct_answer 或 status。\n"
            "交付对象判断：如果用户是在 MCagent 对话里要求转达给 CrawlerAgent 收集资料，通常是为了补充 MCagent/RAG 的本地资料库，delivery_target 应选 MCagent/RAG；只有用户明确表示只是要给自己看的摘要或临时结果时才选 human。CrawlerAgent 直接收到用户委托时，也要根据用户是否提到 MCagent/RAG/入库来判断交付对象。\n"
            "重要原则：不要用关键词触发。必须按语义判断。不要把游戏内“获取某物/如何获得”误判成 Crawler 采集任务。\n"
            "当前系统主要服务 Minecraft 资料库。若实体名有泛义但当前对话没有给出其他领域，rag_focus 不能只写裸实体，必须带上 Minecraft/整合包/模组等领域限定；若存在同名歧义，后续回答可说明歧义。\n"
            "委托交接原则：collection_target 不是搜索词，也不是给工具的死规则，而是给 CrawlerAgent 的自然语言任务目标。若任务目标依赖上下文，要把相关背景自然写进目标；不要拆成关键词，也不要丢掉用户原话。\n"
            "如果是复合问答，优先 answer；如果复合任务包含“先查本地资料/总结缺口/再让 Crawler 补”，选择 planned_workflow，并给出 action_plan。\n"
            "如果需要本地 RAG 检索，rag_focus 要写成真正要查的主题问题，去掉“本地资料、缺什么、让 Crawler 去找、状态”等元指令。\n"
            "只输出 JSON，不要 Markdown，不要解释隐藏思考。\n"
            "额外工具 direct_answer：当用户只是问候、闲聊、询问系统能力、要求解释当前行为，或任何不需要本地资料/状态/Crawler 的问题时选择 direct_answer；选择它就不要触发 local_rag_search。\n"
            f"active_agent: {agent}\n"
            f"original_user_message: {original_question}\n"
            f"contextualized_for_retrieval: {contextual_question}\n"
            f"session_summary: {json.dumps(session_summary or {}, ensure_ascii=False)}\n"
            f"JSON schema: {{\"tool\":\"{allowed_tools}\", "
            "\"reason\":\"一句面向开发日志的理由\", "
            "\"rag_focus\":\"需要本地检索时的主题化检索问题\", "
            "\"collection_target\":\"若选择 delegate_crawler，写成完整自然语言采集目标；不要拆搜索词，也不要丢掉用户原话和必要上下文\", "
            "\"delivery_target\":\"human|MCagent/RAG|\", "
            "\"action_plan\":[{\"step\":1,\"tool\":\"local_rag_search|summarize_gaps|delegate_crawler|crawler_status\",\"goal\":\"这一步要完成什么\"}]}"
        )
        raw_text = client.chat(
            [
                {"role": "system", "content": "只输出合法 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=900,
        )
        value = _json_object_from_llm_text(raw_text)
        raw_tool = str(value.get("tool") or "answer").strip().lower()
        tool_aliases = {
            "local_rag_search": "answer",
            "answer_from_evidence": "answer",
            "rag": "answer",
            "chat": "direct_answer",
            "smalltalk": "direct_answer",
            "direct": "direct_answer",
            "answer_and_delegate": "planned_workflow",
            "answer_then_crawler": "planned_workflow",
            "answer_then_delegate": "planned_workflow",
            "plan": "planned_workflow",
            "workflow": "planned_workflow",
            "crawler_status": "status",
            "delegate": "delegate_crawler",
            "crawler": "delegate_crawler",
        }
        tool = tool_aliases.get(raw_tool, raw_tool)
        if agent == "crawler_agent" and tool == "answer":
            tool = "direct_answer"
        tool = validate_tool_name(agent, tool, fallback="answer" if agent != "crawler_agent" else "delegate_crawler")
        raw_plan = value.get("action_plan")
        action_plan: list[dict[str, Any]] = []
        if isinstance(raw_plan, list):
            for index, step in enumerate(raw_plan[:8], start=1):
                if not isinstance(step, dict):
                    continue
                action_plan.append(
                    {
                        "step": int(step.get("step") or index),
                        "tool": str(step.get("tool") or "").strip(),
                        "goal": str(step.get("goal") or step.get("description") or "").strip()[:300],
                    }
                )
        return {
            "tool": tool,
            "reason": str(value.get("reason") or "Agent LLM selected tool.").strip()[:500],
            "rag_focus": str(value.get("rag_focus") or "").strip()[:500],
            "collection_target": str(value.get("collection_target") or original_question).strip(),
            "delivery_target": str(value.get("delivery_target") or "").strip(),
            "action_plan": action_plan,
            "planner": label,
        }
    except Exception as exc:  # noqa: BLE001 - keep chat usable if the router model fails.
        fallback_tool = "delegate_crawler" if agent == "crawler_agent" else "answer"
        return {
            "tool": fallback_tool,
            "reason": f"Agent tool selector failed; conservative fallback to {fallback_tool}: {type(exc).__name__}: {exc}",
            "collection_target": original_question,
            "delivery_target": "",
            "planner": "fallback_after_llm_error",
        }


def _agent_confirm_next_step(
    config: AppConfig,
    payload: dict[str, Any],
    *,
    agent: str,
    model: str,
    original_question: str,
    session_summary: dict[str, Any],
    proposed_tool: str,
    proposed_goal: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask the active Agent LLM to confirm the next tool action.

    This is not a final answer and not a hardcoded router. It is a small
    observable checkpoint so MCagent/Crawler-style flows do not silently jump
    from one tool to the next without the Agent confirming intent.
    """
    if agent == "retriever_only" or bool(payload.get("no_llm")):
        return {"proceed": True, "tool": proposed_tool, "goal": proposed_goal, "reason": "runtime mode confirmed", "planner": "runtime"}
    try:
        client, label = _selected_llm_client(config, model, 0.0)
        catalog = tool_catalog_prompt(agent, include_principles=True)
        allowed_tools = ", ".join(tool_names_for_agent(agent))
        prompt = (
            "你是当前 Agent 的下一步行动确认器。只确认下一步工具动作，不回答用户问题，不写最终答案。\n"
            "必须基于用户原话、会话摘要、上一步决策和当前上下文判断：现在是否应该执行 proposed_tool，目标是否说清楚，是否需要改用另一个允许工具。\n"
            f"当前 Agent 工具目录：\n{catalog}\n"
            f"允许工具名：{allowed_tools}, answer, evidence_select, final_answer_llm。\n"
            "如果 proposed_tool 合理，proceed=true；如果不合理，proceed=false 并给出 suggested_tool。不要拆 Crawler 的搜索词，不要替工具生成最终回答。\n"
            "只输出 JSON。\n"
            "额外工具 direct_answer：当用户只是问候、闲聊、询问系统能力、要求解释当前行为，或任何不需要本地资料/状态/Crawler 的问题时选择 direct_answer；选择它就不要触发 local_rag_search。\n"
            f"active_agent: {agent}\n"
            f"original_user_message: {original_question}\n"
            f"session_summary: {json.dumps(session_summary or {}, ensure_ascii=False)}\n"
            f"proposed_tool: {proposed_tool}\n"
            f"proposed_goal: {proposed_goal}\n"
            f"context: {json.dumps(context or {}, ensure_ascii=False)}\n"
            "JSON schema: {\"proceed\":true, \"tool\":\"工具名\", \"suggested_tool\":\"可选\", \"goal\":\"确认后的下一步目标\", \"reason\":\"一句理由\", \"concern\":\"可选风险\"}"
        )
        raw_text = client.chat(
            [
                {"role": "system", "content": "只输出合法 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=700,
        )
        value = _json_object_from_llm_text(raw_text)
        tool = str(value.get("tool") or proposed_tool).strip() or proposed_tool
        suggested = str(value.get("suggested_tool") or "").strip()
        return {
            "proceed": bool(value.get("proceed", True)),
            "tool": tool,
            "suggested_tool": suggested,
            "goal": str(value.get("goal") or proposed_goal).strip()[:500],
            "reason": str(value.get("reason") or "Agent confirmed next step.").strip()[:500],
            "concern": str(value.get("concern") or "").strip()[:500],
            "planner": label,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "proceed": True,
            "tool": proposed_tool,
            "goal": proposed_goal,
            "reason": f"Next-step confirmation failed; continuing with prior Agent decision: {type(exc).__name__}: {exc}",
            "planner": "fallback_after_confirmation_error",
        }


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
    payload["trace"] = trace
    return payload


def _trace_step(stage: str, status: str, detail: Any = None) -> dict[str, Any]:
    return {"time": time.time(), "stage": stage, "status": status, "detail": detail}


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


def _chat(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    return _chat_impl(config, payload)


def _chat_impl(config: AppConfig, payload: dict[str, Any], emit: Any | None = None) -> dict[str, Any]:
    def add_trace(stage: str, status: str, detail: Any = None) -> dict[str, Any]:
        step = _trace_step(stage, status, detail)
        trace.append(step)
        if emit is not None:
            emit("trace", step)
        return step

    original_question = str(payload.get("question") or payload.get("query") or "").strip()
    question = original_question
    agent = str(payload.get("agent") or "mcagent_rag")
    profile_id = str(payload.get("model_profile_id") or "").strip()
    if profile_id:
        model = f"profile:{profile_id}"
    else:
        raw_model = str(payload.get("model") or "").strip()
        if raw_model:
            model = raw_model
        else:
            assigned = profiles_payload(config).get("assignments", {}).get("crawler_agent" if agent == "crawler_agent" else "mcagent_rag", "")
            model = f"profile:{assigned}" if assigned else config.ollama.model
    temperature = float(payload.get("temperature") if payload.get("temperature") is not None else config.ollama.temperature)
    max_tokens = _answer_max_tokens(payload, question)
    trace: list[dict[str, Any]] = []
    add_trace("observe", "received", {"agent": agent, "question": question})
    if not question:
        return _with_trace({"answer": "问题不能为空。", "sources": [], "context": "", "agent": agent}, trace)
    if _question_looks_transport_garbled(question):
        add_trace("done", "invalid_encoding", {"reason": "question contains too many question marks"})
        return _with_trace(
            {
                "answer": "这条消息看起来在传输或终端输入时发生了编码损坏，问题内容变成了大量问号。请在网页里重新发送原始中文问题，或确认调用方按 UTF-8 发送请求。为了避免污染资料库，本次不会触发 Crawler。",
                "sources": [],
                "context": "",
                "agent": agent,
            },
            trace,
        )
    retrieval_note = ""
    session_summary = _session_summary(payload)
    if agent == "mcagent_rag":
        contextual_question, retrieval_note, rewritten = _contextualize_question(payload, question)
        if rewritten:
            question = contextual_question
            add_trace("observe", "contextualized", {"original": original_question, "rewritten": question})
    tool_decision = _agent_tool_decision(
        config,
        payload,
        agent=agent,
        original_question=original_question,
        contextual_question=question,
        session_summary=session_summary,
        model=model,
    )
    route_intent = str(tool_decision.get("tool") or "answer")
    action_plan = tool_decision.get("action_plan") if isinstance(tool_decision.get("action_plan"), list) else []
    rag_focus = str(tool_decision.get("rag_focus") or "").strip()
    add_trace("decide", "tool_selected", {"tool": route_intent, "original_question": original_question, "decision": tool_decision})
    if action_plan:
        add_trace("plan", "created", {"steps": action_plan})
    if rag_focus:
        add_trace("plan", "rag_focus", {"question": rag_focus})
    route_confirmation = _agent_confirm_next_step(
        config,
        payload,
        agent=agent,
        model=model,
        original_question=original_question,
        session_summary=session_summary,
        proposed_tool=route_intent,
        proposed_goal=str(tool_decision.get("reason") or "确认本轮应执行的工具路径。"),
        context={"tool_decision": tool_decision, "action_plan": action_plan},
    )
    add_trace("decide", "next_step_confirmed", route_confirmation)
    if not bool(route_confirmation.get("proceed", True)):
        suggested_tool = str(route_confirmation.get("suggested_tool") or route_confirmation.get("tool") or "").strip()
        if suggested_tool in {"direct_answer", "answer", "planned_workflow", "status", "delegate_crawler"}:
            route_intent = suggested_tool
    planned_workflow = route_intent == "planned_workflow"
    planned_delegate = planned_workflow and _action_plan_has_tool(action_plan, "delegate_crawler")
    if planned_workflow:
        route_intent = "answer"
    if route_intent == "direct_answer":
        add_trace("answer", "generating", {"model": model, "mode": "direct"})
        try:
            if emit is not None:
                answer = _generate_direct_answer_stream(
                    config,
                    original_question,
                    question,
                    session_summary,
                    model,
                    temperature,
                    max_tokens,
                    lambda chunk: emit("delta", {"text": chunk}),
                    emit_thinking=lambda detail: add_trace("answer", "thinking", detail),
                )
            else:
                answer = _generate_direct_answer(
                    config,
                    original_question,
                    question,
                    session_summary,
                    model,
                    temperature,
                    max_tokens,
                )
        except Exception as exc:  # noqa: BLE001
            answer = f"模型调用失败：{exc}"
        return _with_trace({"answer": answer, "sources": [], "context": "", "agent": agent}, trace)
    if route_intent == "delegate_crawler":
        collection_question = str(tool_decision.get("collection_target") or original_question or question).strip()
        delegate_confirmation = _agent_confirm_next_step(
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
        handoff = _delegation_handoff(payload, original_question, collection_question)
        requested_by = handoff["requested_by"]
        if str(payload.get("delivery_target") or "").strip():
            delivery_target = str(payload.get("delivery_target")).strip()
        elif str(tool_decision.get("delivery_target") or "").strip():
            delivery_target = str(tool_decision.get("delivery_target")).strip()
        elif requested_by in {"mcagent", "user_via_mcagent"}:
            delivery_target = "MCagent/RAG"
        else:
            delivery_target = _infer_delivery_target(original_question, session_summary)
        handoff_brief, brief_reason = _build_delegate_handoff_brief(
            config,
            model=model,
            original_question=original_question,
            collection_target=collection_question,
            session_summary=session_summary,
            requested_by=requested_by,
            delivery_target=delivery_target,
        )
        add_trace("delegate", "handoff_brief", {"brief": handoff_brief, "reason": brief_reason})
        delegate_summary = dict(session_summary or {})
        delegate_summary["handoff_brief"] = handoff_brief
        delegate_summary["handoff_brief_reason"] = brief_reason
        if not delegate_summary.get("current_topic") and (delegate_summary.get("topics") or []):
            delegate_summary["current_topic"] = str((delegate_summary.get("topics") or [""])[0])
        if not delegate_summary.get("missing_evidence") and (delegate_summary.get("gaps") or []):
            delegate_summary["missing_evidence"] = "；".join(str(item) for item in (delegate_summary.get("gaps") or [])[:8])
        delegate_payload = payload | {
            "requested_by": requested_by,
            "handoff_from": handoff["handoff_from"],
            "original_user_request": handoff["original_user_request"],
            "delivery_target": delivery_target,
            "preserve_crawler_request": True,
            "session_summary": delegate_summary,
        }
        job, created = _delegate_crawler_for_missing_data(config, delegate_payload, collection_question)
        answer = "Crawler 多源采集任务已启动。" if created else "Crawler 已有任务在运行。"
        answer += _crawler_delegation_note_for(job, collection_question, created, requested_by=requested_by, delivery_target=delivery_target)
        return _with_trace(
            {
                "answer": answer,
                "sources": [],
                "job": _job_to_dict(job),
                "collaboration": _collaboration_dialog_for(collection_question, job, created, requested_by=requested_by, delivery_target=delivery_target),
                "delegation": {"requested_by": requested_by, "delivery_target": delivery_target, "task": collection_question},
            },
            trace,
        )
    if route_intent == "status":
        status_confirmation = _agent_confirm_next_step(
            config,
            payload,
            agent=agent,
            model=model,
            original_question=original_question,
            session_summary=session_summary,
            proposed_tool="status",
            proposed_goal="读取采集、入库和后台任务状态。",
            context={},
        )
        add_trace("status", "next_step_confirmed", status_confirmation)
        return _with_trace(_crawler_monitor_answer(config), trace)

    retriever = Retriever(config)
    evidence_question = rag_focus or question
    rough_k = _adaptive_rough_k(evidence_question, agent)
    final_k = _adaptive_final_context_k(evidence_question, config, agent)
    retrieval_plan = None
    if agent == "mcagent_rag":
        retrieval_confirmation = _agent_confirm_next_step(
            config,
            payload,
            agent=agent,
            model=model,
            original_question=original_question,
            session_summary=session_summary,
            proposed_tool="local_rag_search",
            proposed_goal=f"检索本地资料库以回答：{evidence_question}",
            context={"evidence_question": evidence_question, "rough_k": rough_k, "final_context_k": final_k},
        )
        add_trace("retrieve", "next_step_confirmed", retrieval_confirmation)
        if not bool(retrieval_confirmation.get("proceed", True)):
            suggested_tool = str(retrieval_confirmation.get("suggested_tool") or retrieval_confirmation.get("tool") or "").strip()
            if suggested_tool in {"answer", "direct_answer", "final_answer_llm"}:
                add_trace("answer", "generating", {"model": model, "mode": "direct_after_retrieval_cancelled"})
                try:
                    if emit is not None:
                        answer = _generate_direct_answer_stream(
                            config,
                            original_question,
                            question,
                            session_summary,
                            model,
                            temperature,
                            max_tokens,
                            lambda chunk: emit("delta", {"text": chunk}),
                            emit_thinking=lambda detail: add_trace("answer", "thinking", detail),
                        )
                    else:
                        answer = _generate_direct_answer(
                            config,
                            original_question,
                            question,
                            session_summary,
                            model,
                            temperature,
                            max_tokens,
                        )
                except Exception as exc:  # noqa: BLE001
                    answer = f"模型调用失败：{exc}"
                return _with_trace({"answer": answer, "sources": [], "context": "", "agent": agent}, trace)
        planning_summary = _retrieval_planning_summary(session_summary, original_question, evidence_question)
        add_trace("retrieve", "planning", {"question": evidence_question, "original": original_question})
        retrieval_plan = plan_retrieval(evidence_question, session_summary=planning_summary, max_queries=10, use_llm=True)
        add_trace("retrieve", "planned", retrieval_plan.to_dict())
    add_trace("retrieve", "searching", {"mode": "planned_adaptive" if retrieval_plan else "adaptive", "rough_k": rough_k, "final_context_k": final_k})
    search_question = _combined_retrieval_question(evidence_question, question, retrieval_plan)
    rough_results = retriever.search(search_question, top_k=rough_k, plan=retrieval_plan, session_summary=session_summary)
    if agent == "mcagent_rag" and len(rough_results) < max(4, final_k // 2):
        rough_results = _supplement_raw_html_results(config, evidence_question, rough_results, limit=24)
    add_trace("retrieve", "done", {"results": len(rough_results), "top": rough_results[0].title if rough_results else ""})
    if not rough_results:
        job, created = _delegate_crawler_for_missing_data(config, payload, question)
        answer = "本地资料库未找到可靠答案。"
        answer += _crawler_delegation_note(job, question, created)
        return _with_trace({"answer": answer, "sources": [], "context": "", "agent": agent, "job": _job_to_dict(job), "collaboration": _collaboration_dialog(question, job, created)}, trace)

    selected = _dedupe_results(rough_results, limit=final_k)
    evidence_report = None
    if agent == "mcagent_rag" and not bool(payload.get("no_llm")):
        add_trace("decide", "selecting_evidence", {"candidates": len(rough_results)})
        selected, evidence_report = EvidenceSelector(final_context_k=final_k).select(evidence_question, rough_results, plan=retrieval_plan)
        selected = _prefer_parent_topic_results(evidence_question, selected, rough_results, final_k)
        modpack_list_selected = _modpack_manifest_results(evidence_question, rough_results, final_k)
        if agent == "mcagent_rag" and not modpack_list_selected:
            modpack_list_selected = _supplement_local_modpack_manifest_results(config, evidence_question, final_k)
        if modpack_list_selected:
            selected = _dedupe_results([*modpack_list_selected, *selected], limit=final_k)
            evidence_report.verdict = "ok"
            evidence_report.reasons = []
            evidence_report.selected_count = len(selected)
        selected = _supplement_project_keyword_results(config, evidence_question, selected, final_k)
        selected = _supplement_raw_html_results(config, evidence_question, selected, limit=final_k)
        selected = _ensure_modpack_mod_list_context(config, evidence_question, selected, rough_results, final_k)
        fallback_selected = _fallback_theme_results(evidence_question, rough_results, final_k)
        if fallback_selected and len(selected) < min(4, final_k):
            selected = _dedupe_results([*fallback_selected, *selected], limit=final_k)
            if evidence_report.verdict != "ok":
                evidence_report.verdict = "ok"
                evidence_report.reasons = []
                evidence_report.selected_count = len(selected)
        add_trace("decide", "evidence_selected", evidence_report.to_dict())
        if evidence_report.verdict != "ok":
            if planned_delegate:
                collection_question = str(tool_decision.get("collection_target") or original_question or question).strip()
                handoff = _delegation_handoff(payload, original_question, collection_question)
                requested_by = handoff["requested_by"]
                delivery_target = str(tool_decision.get("delivery_target") or payload.get("delivery_target") or "").strip()
                if not delivery_target:
                    delivery_target = "MCagent/RAG" if requested_by in {"mcagent", "user_via_mcagent"} else _infer_delivery_target(original_question, session_summary)
                planner_summary = dict(session_summary or {})
                planner_summary["collection_target"] = collection_question
                planner_summary["mcagent_gap_summary"] = (
                    "MCagent 按计划检索本地资料，但证据筛选仍不足。\n"
                    + "\n".join(f"- {reason}" for reason in evidence_report.reasons)
                )
                planner_summary["planning_instruction"] = (
                    "MCagent 已先尝试本地检索，但证据不足；CrawlerAgent 应阅读 handoff_brief、mcagent_gap_summary "
                    "和会话摘要，自行判断真正缺口、规划来源，采集后按 MCagent/RAG 可检索格式入库。"
                )
                handoff_brief, brief_reason = _build_delegate_handoff_brief(
                    config,
                    model=model,
                    original_question=original_question,
                    collection_target=collection_question,
                    session_summary=planner_summary,
                    requested_by=requested_by,
                    delivery_target=delivery_target,
                    mcagent_gap_summary=planner_summary["mcagent_gap_summary"],
                )
                planner_summary["handoff_brief"] = handoff_brief
                planner_summary["handoff_brief_reason"] = brief_reason
                if not planner_summary.get("current_topic") and (planner_summary.get("topics") or []):
                    planner_summary["current_topic"] = str((planner_summary.get("topics") or [""])[0])
                if not planner_summary.get("missing_evidence") and (planner_summary.get("gaps") or []):
                    planner_summary["missing_evidence"] = "；".join(str(item) for item in (planner_summary.get("gaps") or [])[:8])
                add_trace("delegate", "handoff_brief", {"brief": handoff_brief, "reason": brief_reason})
                delegate_payload = payload | {
                    "requested_by": requested_by,
                    "handoff_from": handoff["handoff_from"],
                    "original_user_request": handoff["original_user_request"],
                    "delivery_target": delivery_target,
                    "preserve_crawler_request": True,
                    "session_summary": planner_summary,
                }
                job, created = _delegate_crawler_for_missing_data(config, delegate_payload, collection_question)
                answer = _insufficient_evidence_answer(question)
                answer += "\n\nMCagent 已按计划把完整上下文交接给 CrawlerAgent。"
                answer += _crawler_delegation_note_for(job, collection_question, created, requested_by=requested_by, delivery_target=delivery_target)
                add_trace("delegate", "planned_workflow", {"job_id": job.id, "status": job.status, "task": collection_question})
                return _with_trace(
                    {
                        "answer": answer,
                        "sources": [_result_to_dict(item) for item in selected],
                        "context": "",
                        "agent": agent,
                        "job": _job_to_dict(job),
                        "evidence": evidence_report.to_dict(),
                        "collaboration": _collaboration_dialog_for(collection_question, job, created, requested_by=requested_by, delivery_target=delivery_target),
                        "delegation": {"requested_by": requested_by, "delivery_target": delivery_target, "task": collection_question, "handoff_brief": handoff_brief},
                    },
                    trace,
                )
            job, created = _delegate_crawler_for_missing_data(config, payload, question)
            answer = _insufficient_evidence_answer(question)
            answer += "\n\n证据判断：\n" + "\n".join(f"- {reason}" for reason in evidence_report.reasons)
            answer += _crawler_delegation_note(job, question, created)
            return _with_trace({"answer": answer, "sources": [_result_to_dict(item) for item in selected], "context": "", "agent": agent, "job": _job_to_dict(job), "evidence": evidence_report.to_dict(), "collaboration": _collaboration_dialog(question, job, created)}, trace)

    source_dicts = [_result_to_dict(item) for item in selected]
    context = format_context(selected)
    delegated_job = None
    created = False
    delegated_requested_by = ""
    delegated_delivery_target = ""
    delegated_task = ""
    delegated_handoff_brief = ""
    if agent == "retriever_only" or bool(payload.get("no_llm")):
        answer = "本地检索结果如下，未调用模型：\n\n" + context
    else:
        add_trace("answer", "generating", {"model": model})
        answer_question = _answer_question_for_user(original_question, question, retrieval_note)
        answer_confirmation = _agent_confirm_next_step(
            config,
            payload,
            agent=agent,
            model=model,
            original_question=original_question,
            session_summary=session_summary,
            proposed_tool="final_answer_llm",
            proposed_goal=f"基于已筛选证据组织最终回答：{answer_question}",
            context={"selected_sources": len(selected), "evidence_question": evidence_question},
        )
        add_trace("answer", "next_step_confirmed", answer_confirmation)
        if emit is not None:
            answer, context = _generate_grounded_answer_stream(
                config,
                answer_question,
                selected,
                model,
                temperature,
                max_tokens,
                lambda chunk: emit("delta", {"text": chunk}),
                emit_thinking=lambda detail: add_trace("answer", "thinking", detail),
                retrieval_note=retrieval_note,
                evidence_question=evidence_question,
            )
        else:
            answer, context = _generate_grounded_answer(
                config,
                answer_question,
                selected,
                model,
                temperature,
                max_tokens,
                retrieval_note=retrieval_note,
                evidence_question=evidence_question,
            )
        answer = _repair_list_answer(question, answer, selected)
        if planned_delegate:
            collection_question = str(tool_decision.get("collection_target") or original_question or question).strip()
            handoff = _delegation_handoff(payload, original_question, collection_question)
            requested_by = handoff["requested_by"]
            delivery_target = str(tool_decision.get("delivery_target") or payload.get("delivery_target") or "").strip()
            if not delivery_target:
                delivery_target = "MCagent/RAG" if requested_by in {"mcagent", "user_via_mcagent"} else _infer_delivery_target(original_question, session_summary)
            planner_summary = dict(session_summary or {})
            if answer.strip():
                planner_summary["mcagent_gap_summary"] = answer[:4000]
                planner_summary["planning_instruction"] = (
                    "MCagent 已先检索本地资料并总结了现有资料与缺口；CrawlerAgent 应阅读 mcagent_gap_summary，"
                    "自行判断真正缺口、规划来源，采集后按 MCagent/RAG 可检索格式入库。"
                )
            planner_summary["collection_target"] = collection_question
            handoff_brief, brief_reason = _build_delegate_handoff_brief(
                config,
                model=model,
                original_question=original_question,
                collection_target=collection_question,
                session_summary=planner_summary,
                requested_by=requested_by,
                delivery_target=delivery_target,
                mcagent_gap_summary=answer,
            )
            planner_summary["handoff_brief"] = handoff_brief
            planner_summary["handoff_brief_reason"] = brief_reason
            if not planner_summary.get("current_topic") and (planner_summary.get("topics") or []):
                planner_summary["current_topic"] = str((planner_summary.get("topics") or [""])[0])
            if not planner_summary.get("missing_evidence") and (planner_summary.get("gaps") or []):
                planner_summary["missing_evidence"] = "；".join(str(item) for item in (planner_summary.get("gaps") or [])[:8])
            add_trace("delegate", "handoff_brief", {"brief": handoff_brief, "reason": brief_reason})
            delegate_payload = payload | {
                "requested_by": requested_by,
                "handoff_from": handoff["handoff_from"],
                "original_user_request": handoff["original_user_request"],
                "delivery_target": delivery_target,
                "preserve_crawler_request": True,
                "session_summary": planner_summary,
            }
            delegated_job, created = _delegate_crawler_for_missing_data(config, delegate_payload, collection_question)
            delegated_requested_by = requested_by
            delegated_delivery_target = delivery_target
            delegated_task = collection_question
            delegated_handoff_brief = handoff_brief
            answer = answer.rstrip() + _crawler_delegation_note_for(delegated_job, collection_question, created, requested_by=requested_by, delivery_target=delivery_target)
            add_trace("delegate", "planned_workflow", {"job_id": delegated_job.id, "status": delegated_job.status, "task": collection_question})
        elif _answer_indicates_missing_data(answer):
            delegated_job, created = _delegate_crawler_for_missing_data(config, payload, question)
            answer = answer.rstrip() + _crawler_delegation_note(delegated_job, question, created)
            add_trace("delegate", "answer_marked_missing", {"job_id": delegated_job.id, "status": delegated_job.status})
    plan_text = _format_action_plan_for_user(action_plan) if planned_workflow else ""
    if plan_text and not answer.lstrip().startswith("执行计划："):
        answer = plan_text + "\n\n" + answer
    sources = format_sources(selected)
    if sources and not answer.rstrip().endswith(sources):
        answer = answer.rstrip() + "\n\n来源：\n" + sources
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
            _send_text(self, (WEB_DIR / "index.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
            return
        if request_path in {"/settings", "/settings.html"}:
            _send_text(self, (WEB_DIR / "settings.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
            return
        if request_path.startswith("/static/"):
            name = request_path.removeprefix("/static/")
            path = (STATIC_DIR / name).resolve()
            if not str(path).startswith(str(STATIC_DIR.resolve())) or not path.exists():
                _send_json(self, {"error": "not found"}, status=404)
                return
            content_type = "text/css; charset=utf-8" if path.suffix == ".css" else "application/javascript; charset=utf-8"
            _send_text(self, path.read_text(encoding="utf-8"), content_type)
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
        if request_path == "/api/chat/stream":
            _send_sse_headers(self)

            def emit(event: str, data: Any) -> None:
                _write_sse(self, event, data)

            try:
                result = _chat_impl(config, payload, emit=emit)
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
        if request_path == "/api/jobs/start-crawler":
            crawler_payload = dict(payload)
            if _has_likely_encoding_damage(crawler_payload):
                _send_json(
                    self,
                    {
                        "error": "request text appears to be encoding-damaged; please resend as UTF-8 JSON",
                        "hint": "Do not send Chinese JSON through a misconfigured PowerShell command. Use the web UI or a UTF-8 client.",
                    },
                    status=400,
                )
                return
            crawler_payload.setdefault("agent", "crawler_agent")
            job, created = _delegate_crawler_for_missing_data(config, crawler_payload, str(crawler_payload.get("question") or crawler_payload.get("query") or ""))
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
            session_id = str(payload.get("session_id") or "default")
            with SESSIONS_LOCK:
                history = list(SESSIONS.get(session_id, []))
            _send_json(self, {"session_id": session_id, "history": history})
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
