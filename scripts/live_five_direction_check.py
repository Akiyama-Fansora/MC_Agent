from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


BASE = os.environ.get("MCAGENT_BASE", "http://127.0.0.1:8765").rstrip("/")
TEST_MODEL = os.environ.get("MCAGENT_TEST_MODEL", "").strip()
ROOT = Path(__file__).resolve().parents[1]
RUN_REPORT: list[dict[str, Any]] = []
REPORT_FILE: Path | None = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def request_json(method: str, path: str, payload: dict | None = None, timeout: int = 30) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
    return json.loads(body or "{}")


def post_json(path: str, payload: dict, timeout: int = 30) -> dict:
    return request_json("POST", path, payload, timeout=timeout)


def get_json(path: str, timeout: int = 30) -> dict:
    return request_json("GET", path, timeout=timeout)


def read_stream(payload: dict, timeout: int = 180) -> tuple[list[dict], dict | None, float]:
    if TEST_MODEL and "model" not in payload:
        payload = {**payload, "model": TEST_MODEL}
    started = time.time()
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/api/chat/stream",
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    events: list[dict] = []
    final: dict | None = None
    event = "message"
    data_lines: list[str] = []
    with urllib.request.urlopen(req, timeout=timeout) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
                continue
            if line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
                continue
            if line.strip():
                continue
            if not data_lines:
                event = "message"
                continue
            value = json.loads("\n".join(data_lines))
            events.append({"event": event, "data": value})
            if event == "response":
                final = value
                break
            event = "message"
            data_lines = []
    return events, final, time.time() - started


def chat(session_id: str, agent: str, question: str, *, timeout: int = 180, **extra: object) -> tuple[list[dict], dict, float]:
    payload = {
        "session_id": session_id,
        "agent": agent,
        "question": question,
        "max_tokens": extra.pop("max_tokens", 800),
        **extra,
    }
    print(f"  chat start: session={session_id} agent={agent} timeout={timeout}s", flush=True)
    events, final, elapsed = read_stream(payload, timeout=timeout)
    print(f"  chat done: session={session_id} elapsed={elapsed:.1f}s", flush=True)
    if final is None:
        raise AssertionError(f"no final response for {agent}: {question}")
    return events, final, elapsed


def answer_text(final: dict) -> str:
    agent_message = final.get("agent_message")
    if isinstance(agent_message, dict) and agent_message.get("content"):
        return str(agent_message["content"])
    return str(final.get("answer") or "")


def trace_steps(events: list[dict]) -> list[dict]:
    return [event["data"] for event in events if event.get("event") == "trace" and isinstance(event.get("data"), dict)]


def compact_trace(events: list[dict], limit: int = 30) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for step in trace_steps(events):
        detail = step.get("detail") if isinstance(step.get("detail"), dict) else {}
        compact_detail: dict[str, Any] = {}
        for key in (
            "tool",
            "reason",
            "goal",
            "sources",
            "job_id",
            "status",
            "saved_to_local",
            "from_agent",
            "to_agent",
            "tuple",
            "collection_target",
            "delivery_target",
            "requested_by",
        ):
            if key in detail:
                compact_detail[key] = detail[key]
        if "content" in detail:
            compact_detail["content"] = str(detail.get("content") or "")[:700]
        decision = detail.get("decision") if isinstance(detail.get("decision"), dict) else {}
        if decision:
            compact_detail["decision"] = {
                key: decision.get(key)
                for key in ("tool", "reason", "rag_focus", "collection_target", "delivery_target", "action_plan")
                if key in decision
            }
        steps.append(
            {
                "stage": step.get("stage"),
                "status": step.get("status"),
                "detail": compact_detail,
            }
        )
    return steps[-limit:]


def compact_job_action_chain(job: dict | None) -> dict[str, Any]:
    if not isinstance(job, dict) or not job:
        return {}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
    tasks = result.get("tasks") if isinstance(result.get("tasks"), list) else []
    planned = result.get("planned_tasks") if isinstance(result.get("planned_tasks"), list) else []
    chain: list[dict[str, Any]] = []
    for index, item in enumerate(tasks, start=1):
        if not isinstance(item, dict):
            continue
        entry = {
            "index": index,
            "source": item.get("source"),
            "query": item.get("query"),
            "returncode": item.get("returncode"),
            "output": str(item.get("output") or "")[:900],
            "export_dir": item.get("export_dir"),
            "records": item.get("records"),
            "elapsed_seconds": item.get("elapsed_seconds"),
            "transport": item.get("transport"),
        }
        if item.get("agent_message_exchange"):
            entry["agent_message_exchange"] = item.get("agent_message_exchange")
        if item.get("mcagent_trace"):
            entry["mcagent_trace"] = [
                {
                    "stage": step.get("stage"),
                    "status": step.get("status"),
                    "detail": step.get("detail") if isinstance(step.get("detail"), dict) else {},
                }
                for step in item.get("mcagent_trace", [])[:12]
                if isinstance(step, dict)
            ]
        chain.append(entry)
    return {
        "id": job.get("id"),
        "status": job.get("status"),
        "title": job.get("title"),
        "summary": job.get("summary"),
        "success_count": job.get("success_count") or result.get("success_count"),
        "failure_count": job.get("failure_count") or result.get("failure_count"),
        "candidate_count": job.get("candidate_count") or result.get("candidate_count"),
        "plan": {
            key: plan.get(key)
            for key in ("topic", "target_hint", "strategy", "delivery_target", "coverage_goals", "planner_error")
            if key in plan
        },
        "planned_tasks": [
            {
                "source": item.get("source"),
                "query": item.get("query"),
                "reason": item.get("reason"),
            }
            for item in planned[:20]
            if isinstance(item, dict)
        ],
        "task_results": chain,
        "self_audit": (job.get("readable") or {}).get("self_audit") if isinstance(job.get("readable"), dict) else None,
    }


def inter_agent_exchanges_from_job(job: dict | None) -> list[dict[str, Any]]:
    if not isinstance(job, dict):
        return []
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    tasks = result.get("tasks") if isinstance(result.get("tasks"), list) else []
    exchanges: list[dict[str, Any]] = []
    for item in tasks:
        if not isinstance(item, dict):
            continue
        exchange = item.get("agent_message_exchange")
        if isinstance(exchange, dict):
            exchanges.append(
                {
                    "source": item.get("source"),
                    "transport": item.get("transport"),
                    "exchange": exchange,
                }
            )
    return exchanges


def record_direction(
    *,
    direction: str,
    title: str,
    agent: str,
    question: str,
    final: dict,
    events: list[dict],
    elapsed: float,
    verdict: str,
    semantic_checks: dict[str, bool],
    notes: str = "",
    job: dict | None = None,
) -> None:
    answer = answer_text(final)
    item = {
        "direction": direction,
        "title": title,
        "agent": agent,
        "question": question,
        "answer": answer,
        "elapsed_seconds": round(elapsed, 3),
        "verdict": verdict,
        "semantic_checks": semantic_checks,
        "trace": compact_trace(events),
        "job": job or final.get("job"),
        "job_action_chain": compact_job_action_chain(job or final.get("job")),
        "inter_agent_exchanges": inter_agent_exchanges_from_job(job or final.get("job")),
        "delegation": final.get("delegation"),
        "collaboration": final.get("collaboration"),
        "agent_message": final.get("agent_message"),
        "temporary_extract": final.get("temporary_extract"),
        "source_count": len(final.get("sources") or []),
        "notes": notes,
    }
    RUN_REPORT.append(item)
    if REPORT_FILE is not None:
        write_report(REPORT_FILE)


def replace_direction_record(direction: str, **updates: Any) -> None:
    for item in reversed(RUN_REPORT):
        if item.get("direction") == direction:
            item.update(updates)
            if REPORT_FILE is not None:
                write_report(REPORT_FILE)
            return


def verdict_from_checks(checks: dict[str, bool]) -> str:
    return "passed" if checks and all(bool(value) for value in checks.values()) else "failed"


def pending_verdict(base_checks: dict[str, bool]) -> str:
    return "pending" if base_checks and all(bool(value) for value in base_checks.values()) else "failed"


def planned_action_tools(events: list[dict]) -> list[str]:
    tools: list[str] = []
    for step in trace_steps(events):
        detail = step.get("detail") if isinstance(step.get("detail"), dict) else {}
        plans: list[Any] = []
        if isinstance(detail.get("steps"), list):
            plans.extend(detail.get("steps") or [])
        decision = detail.get("decision") if isinstance(detail.get("decision"), dict) else {}
        if isinstance(decision.get("action_plan"), list):
            plans.extend(decision.get("action_plan") or [])
        for item in plans:
            if isinstance(item, dict):
                tool = str(item.get("tool") or "").strip()
                if tool:
                    tools.append(tool)
    return tools


def executed_plan_tools(events: list[dict]) -> list[str]:
    tools: list[str] = []
    for step in trace_steps(events):
        if step.get("stage") != "plan" or step.get("status") != "executing_agent_selected_step":
            continue
        detail = step.get("detail") if isinstance(step.get("detail"), dict) else {}
        tool = str(detail.get("tool") or "").strip()
        if tool:
            tools.append(tool)
    return tools


def write_report(path: Path | None = None) -> Path:
    if path is None:
        report_dir = ROOT / "runtime" / "live_five_direction_reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / f"five_direction_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"base": BASE, "model": TEST_MODEL, "directions": RUN_REPORT}, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = path.with_suffix(".md")
    lines = [f"# Live Five-Direction Report", "", f"- base: {BASE}", f"- model: {TEST_MODEL or '(default)'}", ""]
    for item in RUN_REPORT:
        lines.extend(
            [
                f"## {item['direction']} {item['title']}",
                "",
                f"- agent: {item['agent']}",
                f"- elapsed_seconds: {item['elapsed_seconds']}",
                f"- verdict: {item['verdict']}",
                f"- semantic_checks: {json.dumps(item['semantic_checks'], ensure_ascii=False)}",
                "",
                "### Question",
                "",
                item["question"],
                "",
                "### Answer",
                "",
                item["answer"],
                "",
                "### Trace",
                "",
                "```json",
                json.dumps(item["trace"], ensure_ascii=False, indent=2),
                "```",
                "",
                "### Job Action Chain",
                "",
                "```json",
                json.dumps(item.get("job_action_chain") or {}, ensure_ascii=False, indent=2),
                "```",
                "",
                "### Inter-Agent Exchanges",
                "",
                "```json",
                json.dumps(item.get("inter_agent_exchanges") or [], ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n五向测试完整问答报告 JSON: {path}", flush=True)
    print(f"五向测试完整问答报告 Markdown: {md_path}", flush=True)
    return path


def has_trace(events: list[dict], stage: str | None = None, status: str | None = None, tool: str | None = None) -> bool:
    for step in trace_steps(events):
        if stage is not None and step.get("stage") != stage:
            continue
        if status is not None and step.get("status") != status:
            continue
        detail = step.get("detail") if isinstance(step.get("detail"), dict) else {}
        if tool is not None and detail.get("tool") != tool:
            continue
        return True
    return False


def message_tuple(events: list[dict]) -> list[str] | None:
    for step in trace_steps(events):
        if step.get("stage") == "message" and step.get("status") == "received":
            detail = step.get("detail") if isinstance(step.get("detail"), dict) else {}
            value = detail.get("tuple")
            if isinstance(value, list) and len(value) == 3:
                return [str(item) for item in value]
    return None


def stop_job(job_id: str) -> None:
    try:
        post_json("/api/jobs/stop", {"id": job_id}, timeout=20)
    except Exception as exc:  # noqa: BLE001
        print(f"WARN stop job {job_id}: {exc}", flush=True)


def find_job(job_id: str) -> dict | None:
    jobs = get_json("/api/jobs", timeout=20).get("jobs") or []
    for job in jobs:
        if isinstance(job, dict) and str(job.get("id")) == str(job_id):
            return job
    return None


def wait_job(job_id: str, timeout: int, label: str) -> dict:
    deadline = time.time() + timeout
    last_status = ""
    while time.time() < deadline:
        job = find_job(job_id)
        if not job:
            time.sleep(2)
            continue
        status = str(job.get("status") or "")
        readable = job.get("readable") if isinstance(job.get("readable"), dict) else {}
        current = readable.get("current") or job.get("title") or ""
        snapshot = f"{status}: {current}"
        if snapshot != last_status:
            print(f"  {label} job {job_id}: {snapshot}", flush=True)
            last_status = snapshot
        if status in {"completed", "succeeded", "failed", "stopped"}:
            return job
        time.sleep(5)
    job = find_job(job_id) or {}
    raise AssertionError(f"{label} job timeout after {timeout}s; last={json.dumps(job, ensure_ascii=False, default=str)[:1200]}")


def require(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}", flush=True)
        return
    raise AssertionError(f"{name}: {detail}")


def run_direction_1(session: str) -> None:
    print("\nD1 MCagent local RAG answer", flush=True)
    q = "\u6839\u636e\u672c\u5730\u8d44\u6599\uff0c\u4e4c\u6258\u90a6\u63a2\u9669\u4e4b\u65c53.0 \u6709\u54ea\u4e9b\u73a9\u6cd5\u6807\u7b7e\u548c\u7279\u8272\uff1f"
    events, final, elapsed = chat(session + "-d1", "mcagent_rag", q, timeout=300, show_context=True, max_tokens=1000)
    text = answer_text(final)
    semantic_checks = {
        "answer_present": bool(text.strip()),
        "no_crawler_job": not final.get("job") and not final.get("delegation"),
        "used_local_retrieval": has_trace(events, "retrieve") or bool(final.get("sources")),
        "topic_grounded": "\u4e4c\u6258\u90a6" in text or "Utopia" in text or "\u4ea4\u6613" in text or "\u63a2\u7d22" in text,
    }
    require("d1_answer_present", bool(text.strip()), text[:300])
    require("d1_no_crawler_job", not final.get("job") and not final.get("delegation"), json.dumps(final.get("delegation"), ensure_ascii=False))
    require("d1_used_local_retrieval", has_trace(events, "retrieve") or bool(final.get("sources")), "no retrieve trace or sources")
    require("d1_topic_grounded", "\u4e4c\u6258\u90a6" in text or "Utopia" in text or "\u4ea4\u6613" in text or "\u63a2\u7d22" in text, text[:500])
    record_direction(direction="D1", title="MCagent local RAG answer", agent="mcagent_rag", question=q, final=final, events=events, elapsed=elapsed, verdict=verdict_from_checks(semantic_checks), semantic_checks=semantic_checks)


def run_direction_2(session: str, stop_jobs: bool, wait_seconds: int) -> None:
    print("\nD2 MCagent delegates Crawler for missing material", flush=True)
    q = "\u73b0\u5728\u4e4c\u6258\u90a6\u6574\u5408\u5305\u4f60\u672c\u5730\u8fd8\u7f3a\u54ea\u4e9b\u8d44\u6599\uff0c\u5217\u51fa\u6765\uff0c\u7136\u540e\u8ba9 Crawler \u53bb\u8865\u5145\u3002"
    events, final, elapsed = chat(session + "-d2", "mcagent_rag", q, timeout=240, max_tokens=900)
    delegation = final.get("delegation") if isinstance(final.get("delegation"), dict) else {}
    planned_tools = planned_action_tools(events)
    semantic_checks = {
        "delegation_created": bool(delegation),
        "requested_by_mcagent": delegation.get("requested_by") == "user_via_mcagent",
        "delivery_to_rag": delegation.get("delivery_target") == "MCagent/RAG",
        "job_started": bool((final.get("job") if isinstance(final.get("job"), dict) else {}).get("id")),
        "answer_mentions_crawler": "Crawler" in answer_text(final) or "采集" in answer_text(final),
        "no_unknown_plan_tools": not any(tool for tool in planned_tools if tool not in {
            "direct_answer",
            "local_rag_search",
            "local_corpus_inventory",
            "evidence_select",
            "final_answer_llm",
            "status",
            "crawler_audit",
            "delegate_crawler",
        }),
    }
    record_direction(
        direction="D2",
        title="MCagent delegates Crawler for missing material",
        agent="mcagent_rag",
        question=q,
        final=final,
        events=events,
        elapsed=elapsed,
        verdict=verdict_from_checks(semantic_checks),
        semantic_checks=semantic_checks,
    )
    require("d2_delegation_created", bool(delegation), answer_text(final)[:500])
    require("d2_requested_by_mcagent", delegation.get("requested_by") == "user_via_mcagent", json.dumps(delegation, ensure_ascii=False))
    require("d2_delivery_to_rag", delegation.get("delivery_target") == "MCagent/RAG", json.dumps(delegation, ensure_ascii=False))
    job = final.get("job") if isinstance(final.get("job"), dict) else {}
    require("d2_job_started", bool(job.get("id")), json.dumps(final, ensure_ascii=False, default=str)[:800])
    require("d2_no_unknown_plan_tools", semantic_checks["no_unknown_plan_tools"], json.dumps(planned_tools, ensure_ascii=False))
    if stop_jobs and job.get("id"):
        stop_job(str(job["id"]))
    if not stop_jobs and job.get("id"):
        try:
            completed = wait_job(str(job["id"]), wait_seconds, "d2")
            result = completed.get("result") if isinstance(completed.get("result"), dict) else {}
            task_results = result.get("tasks") if isinstance(result.get("tasks"), list) else []
            semantic_checks["job_completed"] = completed.get("status") in {"completed", "succeeded"}
            semantic_checks["crawler_actions_visible"] = bool(task_results)
            semantic_checks["delegated_work_executed_in_job"] = bool(task_results) if "delegate_crawler" in planned_tools else True
            semantic_checks["crawler_self_audit_visible"] = bool(((completed.get("readable") or {}).get("self_audit") if isinstance(completed.get("readable"), dict) else None))
            replace_direction_record(
                "D2",
                verdict=verdict_from_checks(semantic_checks),
                semantic_checks=semantic_checks,
                job=completed,
                job_action_chain=compact_job_action_chain(completed),
                inter_agent_exchanges=inter_agent_exchanges_from_job(completed),
                notes="MCagent initial delegation plus completed Crawler background action chain.",
            )
            require("d2_job_completed", semantic_checks["job_completed"], json.dumps(completed, ensure_ascii=False, default=str)[:1200])
            require("d2_crawler_actions_visible", semantic_checks["crawler_actions_visible"], json.dumps(result, ensure_ascii=False, default=str)[:1200])
            require(
                "d2_delegated_work_executed_in_job",
                semantic_checks["delegated_work_executed_in_job"],
                json.dumps({"planned": planned_tools, "task_results": task_results[-5:]}, ensure_ascii=False, default=str)[:1200],
            )
        except Exception as exc:  # noqa: BLE001
            semantic_checks["job_completed"] = False
            replace_direction_record(
                "D2",
                verdict="failed",
                semantic_checks=semantic_checks,
                job=find_job(str(job["id"])) or job,
                job_action_chain=compact_job_action_chain(find_job(str(job["id"])) or job),
                notes=f"MCagent delegated, but Crawler background job did not complete: {type(exc).__name__}: {exc}",
            )
            raise


def run_direction_3(session: str) -> None:
    print("\nD3 direct Crawler technical documentation extraction without saving", flush=True)
    q = (
        "用 Crawler 临时读取 https://docs.python.org/3/library/asyncio-task.html "
        "里关于 TaskGroup、create_task、cancellation 的技术要点，给我结构化总结；"
        "不要保存到本地，不要启动后台采集。"
    )
    events, final, elapsed = chat(session + "-d3", "crawler_agent", q, timeout=240, max_tokens=900)
    text = answer_text(final)
    temp = final.get("temporary_extract") if isinstance(final.get("temporary_extract"), dict) else {}
    semantic_checks = {
        "message_to_crawler": (message_tuple(events) or ["", "", ""])[2] == "CrawlerAgent",
        "no_background_job": not final.get("job") and not final.get("delegation"),
        "not_saved": temp.get("saved_to_local") is False or "\u4e0d\u4fdd\u5b58" in text,
        "technical_content_present": all(term in text for term in ("TaskGroup", "create_task")) and any(term in text.lower() for term in ("cancel", "cancellation", "取消")),
    }
    record_direction(direction="D3", title="direct Crawler technical documentation extraction without saving", agent="crawler_agent", question=q, final=final, events=events, elapsed=elapsed, verdict=verdict_from_checks(semantic_checks), semantic_checks=semantic_checks)
    require("d3_message_goes_to_crawler", (message_tuple(events) or ["", "", ""])[2] == "CrawlerAgent", str(message_tuple(events)))
    require("d3_no_background_job", not final.get("job") and not final.get("delegation"), json.dumps(final.get("delegation"), ensure_ascii=False))
    require("d3_no_save_metadata", temp.get("saved_to_local") is False or "\u4e0d\u4fdd\u5b58" in text, json.dumps(temp, ensure_ascii=False))
    require(
        "d3_answer_mentions_technical_content",
        semantic_checks["technical_content_present"],
        text[:500],
    )


def run_direction_4(session: str, wait_seconds: int) -> None:
    print("\nD4 Crawler asks MCagent gaps, then fills MCagent/RAG", flush=True)
    q = "\u95ee\u4e0bMCAgent\u4e4c\u6258\u90a6\u6574\u5408\u5305\u8fd8\u7f3a\u54ea\u4e9b\u4e1c\u897f \u4f60\u53bb\u7f51\u4e0a\u627e\u8865\u7ed9\u4ed6"
    events, final, elapsed = chat(session + "-d4", "crawler_agent", q, timeout=300, max_tokens=900, max_tasks=8)
    semantic_checks = {
        "message_to_crawler": (message_tuple(events) or ["", "", ""])[2] == "CrawlerAgent",
        "agent_selected_collection_route": has_trace(events, "decide", "tool_selected", "delegate_crawler")
        or has_trace(events, "decide", "tool_selected", "planned_workflow")
        or has_trace(events, "delegate", "next_step_confirmed"),
        "job_started": bool((final.get("job") if isinstance(final.get("job"), dict) else {}).get("id")),
    }
    require("d4_message_goes_to_crawler", (message_tuple(events) or ["", "", ""])[2] == "CrawlerAgent", str(message_tuple(events)))
    require(
        "d4_agent_selected_collection_route",
        has_trace(events, "decide", "tool_selected", "delegate_crawler")
        or has_trace(events, "decide", "tool_selected", "planned_workflow")
        or has_trace(events, "delegate", "next_step_confirmed"),
        json.dumps(trace_steps(events)[-8:], ensure_ascii=False, default=str),
    )
    job = final.get("job") if isinstance(final.get("job"), dict) else {}
    require("d4_job_started", bool(job.get("id")), json.dumps(final, ensure_ascii=False, default=str)[:1000])
    record_direction(
        direction="D4",
        title="Crawler asks MCagent gaps, then fills MCagent/RAG",
        agent="crawler_agent",
        question=q,
        final=final,
        events=events,
        elapsed=elapsed,
        verdict=pending_verdict(semantic_checks),
        semantic_checks=semantic_checks,
        job=job,
        notes="Initial CrawlerAgent chat and job start recorded before waiting for background completion.",
    )
    try:
        completed = wait_job(str(job["id"]), wait_seconds, "d4")
    except Exception as exc:  # noqa: BLE001
        semantic_checks["job_completed"] = False
        replace_direction_record(
            "D4",
            verdict="failed",
            semantic_checks=semantic_checks,
            notes=f"Initial chat succeeded, but the background job did not complete: {type(exc).__name__}: {exc}",
            job=find_job(str(job["id"])) or job,
        )
        raise
    semantic_checks["job_completed"] = completed.get("status") in {"completed", "succeeded"}
    if not semantic_checks["job_completed"]:
        replace_direction_record(
            "D4",
            verdict="failed",
            semantic_checks=semantic_checks,
            notes=f"Initial chat succeeded, but the background job finished with status={completed.get('status')}.",
            job=completed,
            job_action_chain=compact_job_action_chain(completed),
            inter_agent_exchanges=inter_agent_exchanges_from_job(completed),
        )
    require("d4_job_completed", completed.get("status") in {"completed", "succeeded"}, json.dumps(completed, ensure_ascii=False, default=str)[:1200])
    result = completed.get("result") if isinstance(completed.get("result"), dict) else {}
    plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
    require(
        "d4_no_planner_fallback",
        not plan.get("planner_error") and "fallback_after_llm_planner_error" not in str(plan.get("strategy") or ""),
        json.dumps({"strategy": plan.get("strategy"), "planner_error": plan.get("planner_error")}, ensure_ascii=False, default=str),
    )
    task_results = result.get("tasks") if isinstance(result.get("tasks"), list) else []
    saw_context = any(isinstance(item, dict) and item.get("source") == "mcagent_context" for item in task_results)
    semantic_checks["job_used_mcagent_context"] = saw_context
    require("d4_job_used_mcagent_context", saw_context, json.dumps(task_results[-5:], ensure_ascii=False, default=str))
    context_steps = [item for item in task_results if isinstance(item, dict) and item.get("source") == "mcagent_context"]
    if context_steps:
        semantic_checks["mcagent_context_not_timed_out"] = not bool(context_steps[0].get("timed_out"))
        require(
            "d4_mcagent_context_not_timed_out",
            semantic_checks["mcagent_context_not_timed_out"],
            json.dumps(context_steps[0], ensure_ascii=False, default=str)[:1200],
        )
        require(
            "d4_mcagent_context_fast_enough",
            float(context_steps[0].get("elapsed_seconds") or 9999) < 180,
            json.dumps(context_steps[0], ensure_ascii=False, default=str)[:1200],
        )
        semantic_checks["mcagent_context_uses_message_bus"] = context_steps[0].get("transport") == "_send_agent_message"
        require(
            "d4_mcagent_context_uses_message_bus",
            semantic_checks["mcagent_context_uses_message_bus"],
            json.dumps(context_steps[0], ensure_ascii=False, default=str)[:1200],
        )
        require(
            "d4_agent_exchange_visible",
            bool(context_steps[0].get("agent_message_exchange")),
            json.dumps(context_steps[0], ensure_ascii=False, default=str)[:1200],
        )
    external_steps = [item for item in task_results if isinstance(item, dict) and item.get("source") != "mcagent_context"]
    semantic_checks["continued_after_mcagent_context"] = bool(external_steps)
    require("d4_continued_after_mcagent_context", bool(external_steps), json.dumps(task_results, ensure_ascii=False, default=str)[:1200])
    require("d4_has_usable_material", int(completed.get("success_count") or result.get("success_count") or 0) > 0 or "usable" in json.dumps(result, ensure_ascii=False).lower(), json.dumps(result, ensure_ascii=False, default=str)[:1200])
    check_events, final_check, check_elapsed = chat(
        session + "-d4-check",
        "mcagent_rag",
        "\u4e4c\u6258\u90a6\u6574\u5408\u5305\u6a21\u7ec4\u6570\u91cf\u6709\u54ea\u4e9b\u8bf4\u6cd5\uff1f",
        timeout=180,
        show_context=True,
        max_tokens=800,
    )
    check_text = answer_text(final_check)
    semantic_checks["mcagent_can_use_collected_context"] = any(value in check_text + str(final_check.get("context") or "") for value in ["430", "423", "505", "Immersive Aircraft"])
    require("d4_mcagent_can_use_collected_context", semantic_checks["mcagent_can_use_collected_context"], check_text[:700])
    combined_final = dict(final)
    combined_final["followup_check"] = {
        "question": "\u4e4c\u6258\u90a6\u6574\u5408\u5305\u6a21\u7ec4\u6570\u91cf\u6709\u54ea\u4e9b\u8bf4\u6cd5\uff1f",
        "answer": check_text,
        "elapsed_seconds": round(check_elapsed, 3),
        "trace": compact_trace(check_events),
    }
    RUN_REPORT[:] = [item for item in RUN_REPORT if item.get("direction") != "D4"]
    record_direction(direction="D4", title="Crawler asks MCagent gaps, then fills MCagent/RAG", agent="crawler_agent", question=q, final=combined_final, events=events, elapsed=elapsed, verdict=verdict_from_checks(semantic_checks), semantic_checks=semantic_checks, job=completed)


def run_direction_5(session: str, wait_seconds: int, output_dir: Path) -> None:
    print("\nD5 Crawler saves structured public data locally", flush=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    q = (
        "Use Crawler to open https://webscraper.io/test-sites/e-commerce/static/computers/laptops, "
        "extract the first 5 products with name, price, and link, "
        f"then save xlsx, csv, and json outputs to {output_dir}."
    )
    events, final, elapsed = chat(session + "-d5", "crawler_agent", q, timeout=240, max_tokens=900)
    semantic_checks = {
        "message_to_crawler": (message_tuple(events) or ["", "", ""])[2] == "CrawlerAgent",
        "job_started": bool((final.get("job") if isinstance(final.get("job"), dict) else {}).get("id")),
    }
    require("d5_message_goes_to_crawler", (message_tuple(events) or ["", "", ""])[2] == "CrawlerAgent", str(message_tuple(events)))
    job = final.get("job") if isinstance(final.get("job"), dict) else {}
    require("d5_job_started", bool(job.get("id")), json.dumps(final, ensure_ascii=False, default=str)[:1000])
    record_direction(
        direction="D5",
        title="Crawler saves structured public data locally",
        agent="crawler_agent",
        question=q,
        final=final,
        events=events,
        elapsed=elapsed,
        verdict=pending_verdict(semantic_checks),
        semantic_checks=semantic_checks,
        job=job,
        notes=f"Initial CrawlerAgent chat and job start recorded before waiting. output_dir={output_dir}",
    )
    try:
        completed = wait_job(str(job["id"]), wait_seconds, "d5")
    except Exception as exc:  # noqa: BLE001
        semantic_checks["job_completed"] = False
        replace_direction_record(
            "D5",
            verdict="failed",
            semantic_checks=semantic_checks,
            notes=f"Initial chat succeeded, but the background job did not complete: {type(exc).__name__}: {exc}; output_dir={output_dir}",
            job=find_job(str(job["id"])) or job,
        )
        raise
    semantic_checks["job_completed"] = completed.get("status") in {"completed", "succeeded"}
    if not semantic_checks["job_completed"]:
        replace_direction_record(
            "D5",
            verdict="failed",
            semantic_checks=semantic_checks,
            notes=f"Initial chat succeeded, but the background job finished with status={completed.get('status')}; output_dir={output_dir}",
            job=completed,
        )
    require("d5_job_completed", completed.get("status") in {"completed", "succeeded"}, json.dumps(completed, ensure_ascii=False, default=str)[:1200])
    expected = [output_dir / "items.xlsx", output_dir / "items.csv", output_dir / "items.json", output_dir / "manifest.json"]
    semantic_checks["files_exist"] = all(path.exists() for path in expected)
    require("d5_files_exist", semantic_checks["files_exist"], "\n".join(str(path) for path in expected if not path.exists()))
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    semantic_checks["manifest_ok"] = manifest.get("status") == "ok" and int(manifest.get("record_count") or 0) >= 1
    require("d5_manifest_ok", semantic_checks["manifest_ok"], json.dumps(manifest, ensure_ascii=False)[:800])
    semantic_checks["has_expected_fields"] = bool(manifest.get("fields")) or all(key in json.dumps(manifest, ensure_ascii=False).lower() for key in ("name", "price"))
    replace_direction_record(
        "D5",
        verdict=verdict_from_checks(semantic_checks),
        semantic_checks=semantic_checks,
        job=completed,
        notes=f"output_dir={output_dir}",
    )


def main() -> int:
    global REPORT_FILE
    parser = argparse.ArgumentParser(description="Run the live five-direction MCagent/CrawlerAgent test matrix.")
    parser.add_argument("--skip-long", action="store_true", help="Run D1-D3 only; D4-D5 start and wait for background crawler jobs.")
    parser.add_argument("--job-timeout", type=int, default=480, help="Seconds to wait for each long background crawler job.")
    parser.add_argument("--keep-jobs", action="store_true", help="Do not stop short delegation jobs after D2.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "manual_tests" / "live_five_direction_products" / time.strftime("%Y%m%d_%H%M%S"),
        help="Directory used by D5 structured-data save test.",
    )
    parser.add_argument("--report-file", type=Path, default=None, help="Write complete D1-D5 question/answer report to this JSON path.")
    args = parser.parse_args()
    REPORT_FILE = args.report_file
    try:
        health = get_json("/api/status", timeout=10)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SystemExit(f"Backend is not reachable at {BASE}: {exc}") from exc
    require("backend_health", bool(health.get("project_root")) and bool(health.get("database")), json.dumps(health, ensure_ascii=False))

    session = f"live-five-{int(time.time())}"
    try:
        run_direction_1(session)
        run_direction_2(session, stop_jobs=args.skip_long and not args.keep_jobs, wait_seconds=args.job_timeout)
        run_direction_3(session)
        if not args.skip_long:
            run_direction_4(session, args.job_timeout)
            run_direction_5(session, args.job_timeout, args.output_dir)
    finally:
        write_report(args.report_file)
    print("\nLIVE FIVE-DIRECTION CHECK PASSED", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"LIVE FIVE-DIRECTION CHECK FAILED: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
