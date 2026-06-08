from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from typing import Any
import urllib.request


BASE = os.environ.get("MCAGENT_BASE", "http://127.0.0.1:8765").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def request_json(method: str, path: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    return json.loads(body or "{}")


def stream_chat(payload: dict[str, Any], timeout: int) -> tuple[list[dict[str, Any]], dict[str, Any], float]:
    started = time.time()
    req = urllib.request.Request(
        BASE + "/api/chat/stream",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    events: list[dict[str, Any]] = []
    current_event = "message"
    data_lines: list[str] = []
    final: dict[str, Any] | None = None
    with urllib.request.urlopen(req, timeout=timeout) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            if line.startswith("event:"):
                current_event = line.split(":", 1)[1].strip()
                continue
            if line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
                continue
            if line.strip() or not data_lines:
                continue
            value = json.loads("\n".join(data_lines))
            events.append({"event": current_event, "data": value})
            if current_event == "response":
                final = value
                break
            current_event = "message"
            data_lines = []
    if final is None:
        raise AssertionError("stream ended without response")
    return events, final, time.time() - started


def answer_text(final: dict[str, Any]) -> str:
    message = final.get("agent_message")
    if isinstance(message, dict) and message.get("content"):
        return str(message.get("content") or "")
    return str(final.get("answer") or "")


def job_id_from_response(final: dict[str, Any], events: list[dict[str, Any]]) -> str:
    job = final.get("job") if isinstance(final.get("job"), dict) else {}
    if job.get("id"):
        return str(job.get("id"))
    for event in events:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
        if detail.get("job_id"):
            return str(detail.get("job_id"))
    return ""


def recent_job(job_id: str) -> dict[str, Any] | None:
    jobs = request_json("GET", "/api/jobs", timeout=20).get("jobs") or []
    for job in jobs:
        if isinstance(job, dict) and str(job.get("id") or "") == job_id:
            return job
    return None


def wait_job(job_id: str, timeout: int) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        job = recent_job(job_id)
        if isinstance(job, dict):
            last = job
            if str(job.get("status") or "") in {"succeeded", "failed", "stopped"}:
                return job
        time.sleep(5)
    raise AssertionError(f"job timeout: {job_id}: {json.dumps(last, ensure_ascii=False, default=str)[:2000]}")


def compact_trace(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for event in events:
        if event.get("event") != "trace" or not isinstance(event.get("data"), dict):
            continue
        data = event["data"]
        detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
        decision = detail.get("decision") if isinstance(detail.get("decision"), dict) else {}
        output.append(
            {
                "stage": data.get("stage"),
                "status": data.get("status"),
                "detail": {
                    "tool": detail.get("tool"),
                    "from_agent": detail.get("from_agent"),
                    "to_agent": detail.get("to_agent"),
                    "job_id": detail.get("job_id"),
                    "decision": {
                        key: decision.get(key)
                        for key in ("tool", "reason", "collection_target", "delivery_target", "action_plan")
                        if key in decision
                    },
                },
            }
        )
    return output[-20:]


def job_sources(job: dict[str, Any]) -> list[str]:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    tasks = result.get("tasks") if isinstance(result.get("tasks"), list) else []
    return [str(item.get("source") or "") for item in tasks if isinstance(item, dict)]


def main() -> int:
    health = request_json("GET", "/api/health", timeout=10)
    if not health.get("ok"):
        raise SystemExit(f"backend unhealthy: {health}")
    session = f"general-crawler-{int(time.time())}"
    question = (
        "CrawlerAgent 请采集 FastAPI 官方文档中关于 BackgroundTasks 的公开资料，"
        "重点包括用途、基本用法、依赖注入中的使用方式、限制或注意事项。"
        "这是非 Minecraft 主题，请使用通用网页/URL 工具，不要使用 MC 百科、Modrinth 或整合包工具；交付给 human。"
    )
    events, final, elapsed = stream_chat(
        {
            "session_id": session,
            "agent": "crawler_agent",
            "question": question,
            "max_tokens": 900,
            "max_tasks": 6,
        },
        timeout=360,
    )
    text = answer_text(final)
    job_id = job_id_from_response(final, events)
    if not job_id:
        raise AssertionError(f"no crawler job returned: {text[:1000]}")
    job = wait_job(job_id, 720)
    sources = job_sources(job)
    blocked_domain = {"mcmod", "modrinth", "modpack_download", "modpack_internal", "mediawiki", "ftbwiki", "createwiki"}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    readable = job.get("readable") if isinstance(job.get("readable"), dict) else {}
    audit = readable.get("self_audit") if isinstance(readable.get("self_audit"), dict) else {}
    counts = audit.get("counts") if isinstance(audit.get("counts"), dict) else {}
    accepted = int(counts.get("accepted") or 0)
    pending = int(counts.get("pending_review") or 0)
    checks = {
        "job_succeeded": job.get("status") == "succeeded",
        "uses_general_sources": any(source in {"web_discovery", "fetch_url", "playwright", "browser_collect", "topic_discovery"} for source in sources),
        "no_minecraft_sources": not any(source in blocked_domain for source in sources),
        "self_audit_visible": bool(audit),
        "accepted_or_pending": accepted + pending > 0,
        "topic_mentions_fastapi": "fastapi" in json.dumps(job, ensure_ascii=False, default=str).lower(),
    }
    report = {
        "session_id": session,
        "question": question,
        "answer": text,
        "elapsed_seconds": round(elapsed, 3),
        "job_id": job_id,
        "job_status": job.get("status"),
        "sources": sources,
        "semantic_checks": checks,
        "job_summary": job.get("summary"),
        "self_audit_summary": readable.get("self_audit_summary"),
        "self_audit": audit,
        "trace": compact_trace(events),
        "passed": all(checks.values()),
        "result_summary": {
            "success_count": result.get("success_count"),
            "candidate_count": result.get("candidate_count"),
            "failure_count": result.get("failure_count"),
        },
    }
    report_dir = ROOT / "runtime" / "general_crawler_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"general_crawler_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"report": str(path), "passed": report["passed"], "job_id": job_id}, ensure_ascii=False, indent=2), flush=True)
    if not report["passed"]:
        raise AssertionError(json.dumps(checks, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
