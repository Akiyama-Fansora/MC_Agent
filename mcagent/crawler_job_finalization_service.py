from __future__ import annotations

from typing import Any

from .crawler_self_audit_service import CrawlerSelfAuditService
from .crawler_task_materialization_service import CrawlerTaskMaterializationService


class CrawlerJobFinalizationService:
    """Build the final job update payload for a crawler run."""

    def build(
        self,
        *,
        stop_requested: bool,
        success_count: int,
        candidate_count: int,
        failure_count: int,
        replan_count: int,
        needs_ingest: bool,
        task_results: list[dict[str, Any]],
        planned_tasks: list[dict[str, Any]],
        plan: dict[str, Any],
        collection_summary: dict[str, Any],
    ) -> dict[str, Any]:
        agent_finish_reason = str(plan.get("agent_finish_reason") or "").strip()
        reflection_finish_texts: list[str] = []
        reflections = plan.get("agent_reflections") if isinstance(plan.get("agent_reflections"), list) else []
        for reflection in reflections:
            if not isinstance(reflection, dict) or str(reflection.get("action") or "") != "finish":
                continue
            for key in ("reason", "done_summary"):
                text = str(reflection.get(key) or "").strip()
                if text:
                    reflection_finish_texts.append(text)
        if not agent_finish_reason:
            for text in reversed(reflection_finish_texts):
                if text:
                    agent_finish_reason = text
                    break
        finish_evidence_text = "\n".join([agent_finish_reason, *reflection_finish_texts]).lower()
        gap_probe_finished_with_candidate = (
            candidate_count > 0
            and bool(task_results)
            and "no explicit gap list" in finish_evidence_text
            and "external probe/candidate" in finish_evidence_text
        )
        context_checkpoint_finished_with_candidate = (
            candidate_count > 0
            and bool(task_results)
            and "mcagent/rag context" in finish_evidence_text
            and "external candidate or accepted source" in finish_evidence_text
        )
        reflection_timeout_finished_with_evidence = (
            candidate_count > 0
            and bool(task_results)
            and "reflection timed out" in finish_evidence_text
            and "objective evidence already collected" in finish_evidence_text
        )
        candidate_only_success = candidate_count > 0 and failure_count == 0 and bool(task_results)
        partial_candidate_success = (
            candidate_only_success
            or gap_probe_finished_with_candidate
            or context_checkpoint_finished_with_candidate
            or reflection_timeout_finished_with_evidence
        )
        has_successful_result = bool(success_count or partial_candidate_success)
        status = "succeeded" if has_successful_result else ("stopped" if stop_requested else "failed")
        summary = f"资料补库完成：资料成功 {success_count}，候选发现 {candidate_count}，失败 {failure_count}。"
        stopped_before_tools = bool(agent_finish_reason and not stop_requested and success_count <= 0 and not task_results)
        if stopped_before_tools:
            summary = f"CrawlerAgent stopped before tool execution: {agent_finish_reason}"
        if stop_requested and not has_successful_result:
            summary = f"Crawler 任务已停止：已完成 {len(task_results)}/{len(planned_tasks)} 个任务，资料成功 {success_count}，候选发现 {candidate_count}，失败 {failure_count}。"
        elif stop_requested and has_successful_result:
            summary += " Stop was requested after useful tool output was collected; the completed result is kept."
        elif needs_ingest:
            summary += " 已启动后台入库。"
        display_planned_tasks, blocked_planned_tasks = CrawlerTaskMaterializationService().split_displayable_planned_tasks(planned_tasks)

        result = {
            "source": "planner",
            "success_count": success_count,
            "candidate_count": candidate_count,
            "failure_count": failure_count,
            "replan_count": replan_count,
            "ingest": None,
            "ingest_error": "",
            "ingest_background": bool(needs_ingest and has_successful_result),
            "tasks": task_results,
            "planned_tasks": display_planned_tasks,
            "blocked_planned_tasks": blocked_planned_tasks,
            "plan": plan,
            "collection_summary": collection_summary,
            "loop": [
                {"phase": "understand", "status": "done"},
                {"phase": "plan", "status": "done"},
                {
                    "phase": "act",
                    "status": "blocked" if stopped_before_tools else "done",
                    "note": agent_finish_reason if stopped_before_tools else f"Succeeded {success_count}; failed {failure_count}",
                },
                {
                    "phase": "ingest",
                    "status": "running" if needs_ingest and has_successful_result else "skipped",
                    "note": "Background ingest started." if needs_ingest and has_successful_result else "No new records require ingest.",
                },
                {
                    "phase": "verify",
                    "status": "done" if has_successful_result else ("stopped" if stop_requested else "failed"),
                    "note": "Task results keep records/skipped/errors for the next retry.",
                },
            ],
        }
        result["self_audit"] = CrawlerSelfAuditService().build(task_results, result)
        return {
            "status": status,
            "summary": summary,
            "error": None if has_successful_result or stop_requested else (agent_finish_reason or "all crawler sources failed"),
            "result": result,
        }
