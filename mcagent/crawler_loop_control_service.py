from __future__ import annotations

from typing import Any

from .agent_runtime import classify_crawler_tool_result


PLANNER_SOURCES = {"planner", "auto", "smart", "orchestrator"}


class CrawlerLoopControlService:
    """Compute objective loop-control signals for CrawlerAgent jobs."""

    def update_bad_streak(self, *, result: dict[str, Any], current_bad_streak: int) -> dict[str, Any]:
        observation = classify_crawler_tool_result(result)
        result.setdefault("observation", observation.to_dict())
        bad_streak = current_bad_streak + 1 if observation.bad else 0
        return {
            "bad": observation.bad,
            "bad_streak": bad_streak,
            "observation": observation.to_dict(),
        }

    def should_replan(
        self,
        *,
        source: str,
        success_count: int,
        bad_streak: int,
        replan_count: int,
        max_replans: int,
        task_count: int,
        max_total_tasks: int,
    ) -> bool:
        return (
            source in PLANNER_SOURCES
            and success_count == 0
            and bad_streak >= 3
            and replan_count < max_replans
            and task_count < max_total_tasks
        )

    def should_replan_after_plan_exhausted(
        self,
        *,
        source: str,
        success_count: int,
        bad_streak: int,
        replan_count: int,
        max_replans: int,
        current_index: int,
        task_count: int,
        max_total_tasks: int,
    ) -> bool:
        return (
            source in PLANNER_SOURCES
            and success_count == 0
            and bad_streak > 0
            and current_index >= task_count
            and replan_count < max_replans
            and task_count < max_total_tasks
        )

    def should_finish_after_useful_low_yield(
        self,
        *,
        source: str,
        success_count: int,
        bad_streak: int,
        executed_count: int,
        min_executed: int = 6,
        bad_streak_threshold: int = 4,
    ) -> bool:
        return (
            source in PLANNER_SOURCES
            and success_count > 0
            and executed_count >= min_executed
            and bad_streak >= bad_streak_threshold
        )

    def should_finish_after_no_success_low_yield(
        self,
        *,
        source: str,
        success_count: int,
        bad_streak: int,
        executed_count: int,
        min_executed: int = 6,
        bad_streak_threshold: int = 3,
    ) -> bool:
        return (
            source in PLANNER_SOURCES
            and success_count <= 0
            and executed_count >= min_executed
            and bad_streak >= bad_streak_threshold
        )

    def should_finish_after_enough_success(
        self,
        *,
        source: str,
        success_count: int,
        executed_count: int,
        task_count: int,
        max_total_tasks: int,
        min_success: int = 3,
        min_executed: int = 8,
    ) -> bool:
        return (
            source in PLANNER_SOURCES
            and success_count >= min_success
            and executed_count >= min_executed
            and task_count >= max_total_tasks
        )

    def should_finish_after_gap_probe_satisfied(
        self,
        *,
        source: str,
        task_results: list[dict[str, Any]],
        candidate_count: int,
        success_count: int,
    ) -> bool:
        if source not in PLANNER_SOURCES or len(task_results) < 2:
            return False
        context_results = [
            item
            for item in task_results
            if isinstance(item, dict)
            and str(item.get("source") or "") == "mcagent_context"
            and int(item.get("returncode") or 0) == 0
        ]
        if not context_results:
            return False
        gap_text = "\n".join(str(item.get("mcagent_gap_summary") or "") for item in context_results).lower()
        no_explicit_gap = "no explicit gap list" in gap_text or "没有明确缺口" in gap_text or "未发现明确缺口" in gap_text
        if not no_explicit_gap:
            return False
        external_results = [
            item
            for item in task_results
            if isinstance(item, dict) and str(item.get("source") or "") != "mcagent_context"
        ]
        if not external_results:
            return False
        return candidate_count > 0 or success_count > 0 or any(
            int((item.get("manifest_stats") or {}).get("records") or 0) > 0
            or int((item.get("manifest_stats") or {}).get("candidates") or 0) > 0
            for item in external_results
            if isinstance(item.get("manifest_stats"), dict)
        )

    def should_finish_after_context_plus_external_checkpoint(
        self,
        *,
        source: str,
        task_results: list[dict[str, Any]],
        candidate_count: int,
        success_count: int,
        bad_streak: int,
        executed_count: int,
        min_executed: int = 4,
        bad_streak_threshold: int = 2,
    ) -> bool:
        if source not in PLANNER_SOURCES or executed_count < min_executed or bad_streak < bad_streak_threshold:
            return False
        if candidate_count <= 0 and success_count <= 0:
            return False
        has_context = any(
            isinstance(item, dict)
            and str(item.get("source") or "") == "mcagent_context"
            and int(item.get("returncode") or 0) == 0
            for item in task_results
        )
        if not has_context:
            return False
        return any(self._external_result_has_material(item) for item in task_results if isinstance(item, dict))

    def should_finish_after_gap_summary_handoff_success(
        self,
        *,
        source: str,
        plan: dict[str, Any],
        task_results: list[dict[str, Any]],
        success_count: int,
        executed_count: int,
    ) -> bool:
        if source not in PLANNER_SOURCES or success_count <= 0 or executed_count < 1:
            return False
        text = "\n".join(str(plan.get(key) or "") for key in ("mcagent_gap_summary", "handoff_brief", "planning_instruction", "delivery_target"))
        text = "\n".join(
            [
                text,
                *[
                    "\n".join(str(item.get(key) or "") for key in ("mcagent_gap_summary", "summary", "query"))
                    for item in task_results
                    if isinstance(item, dict) and str(item.get("source") or "") == "mcagent_context"
                ],
            ]
        )
        lowered = text.lower()
        has_gap_summary = "mcagent_gap_summary" in plan or "mcagent" in lowered and ("gap" in lowered or "缺" in text or "本地" in text)
        delivery_to_rag = "mcagent/rag" in lowered or "rag" in lowered
        return has_gap_summary and delivery_to_rag

    def should_finish_after_rag_success_checkpoint(
        self,
        *,
        source: str,
        plan: dict[str, Any],
        success_count: int,
        executed_count: int,
        min_executed: int = 3,
    ) -> bool:
        if source not in PLANNER_SOURCES or success_count <= 0 or executed_count < min_executed:
            return False
        delivery = str(plan.get("delivery_target") or "").lower()
        if "rag" not in delivery and "mcagent" not in delivery:
            return False
        strategy = str(plan.get("strategy") or "")
        if "fallback_after_llm_planner_error" in strategy:
            return True
        return executed_count >= 4

    def _external_result_has_material(self, item: dict[str, Any]) -> bool:
        if str(item.get("source") or "") == "mcagent_context":
            return False
        manifest = item.get("manifest_stats") if isinstance(item.get("manifest_stats"), dict) else {}
        reused = item.get("existing_evidence_reused") if isinstance(item.get("existing_evidence_reused"), dict) else {}
        return (
            int(manifest.get("records") or 0) > 0
            or int(manifest.get("candidates") or 0) > 0
            or bool(reused.get("matched"))
        )
