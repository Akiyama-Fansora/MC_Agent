from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .agent_runtime import classify_crawler_tool_result


SourceLabelFn = Callable[[str], str]


@dataclass(slots=True)
class JobReadableViewService:
    """Build a human-readable job view for API/UI payloads."""

    source_label: SourceLabelFn

    def build(self, job: dict[str, Any]) -> dict[str, Any]:
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
        tasks = result.get("tasks") if isinstance(result.get("tasks"), list) else []
        planned = result.get("planned_tasks") if isinstance(result.get("planned_tasks"), list) else []
        reflections = plan.get("agent_reflections") if isinstance(plan.get("agent_reflections"), list) else []
        last_reflection = next((item for item in reversed(reflections) if isinstance(item, dict)), {})

        observations = [
            (item.get("observation") if isinstance(item.get("observation"), dict) else classify_crawler_tool_result(item).to_dict())
            for item in tasks
            if isinstance(item, dict)
        ]
        observation_statuses: dict[str, int] = {}
        for observation in observations:
            status_key = str(observation.get("status") or "unknown")
            observation_statuses[status_key] = observation_statuses.get(status_key, 0) + 1
        latest_observation = observations[-1] if observations else {}

        current_index = self._current_index(job, tasks, planned)
        current_task = planned[current_index - 1] if planned and current_index > 0 else {}
        if not isinstance(current_task, dict):
            current_task = {}

        success_count = int(result.get("success_count") or sum(1 for item in tasks if isinstance(item, dict) and item.get("ingest_deferred")))
        failure_count = int(
            result.get("failure_count")
            or sum(
                1
                for item in tasks
                if isinstance(item, dict)
                and (item.get("empty_result") or item.get("off_topic_result") or int(item.get("returncode") or 0) != 0)
            )
        )
        off_topic = sum(1 for item in tasks if isinstance(item, dict) and item.get("off_topic_result"))
        empty = sum(1 for item in tasks if isinstance(item, dict) and item.get("empty_result"))
        status = str(job.get("status") or "")
        target = str(plan.get("topic") or plan.get("target_hint") or plan.get("question") or "")
        goals = [str(item) for item in (plan.get("coverage_goals") or []) if str(item).strip()]
        current_query = str(current_task.get("query") or "") if current_task else ""
        current_source = self.source_label(str(current_task.get("source") or "")) if current_task else ""
        current_reason = str(current_task.get("reason") or "") if current_task else ""
        next_action = self._next_action(
            status=status,
            current_task=current_task,
            current_index=current_index,
            total_tasks=len(planned),
            current_query=current_query,
            result=result,
        )
        progress_percent = round((current_index / len(planned)) * 100, 1) if planned else 0.0
        health_text = self._health_text(success_count=success_count, empty=empty, off_topic=off_topic, failure_count=failure_count, latest_observation=latest_observation)

        return {
            "title": str(job.get("title") or ""),
            "status": status,
            "status_label": self._status_label(status),
            "target": target,
            "delivery_target": str(plan.get("delivery_target") or ""),
            "coverage_goals": goals[:5],
            "current_index": current_index,
            "total_tasks": len(planned),
            "progress_percent": progress_percent,
            "progress_text": f"第 {current_index or 0} / {len(planned)} 个采集动作" if planned else "等待 Crawler 规划任务",
            "current_source": current_source,
            "current_query": current_query,
            "current_reason": current_reason,
            "agent_reflection": {
                "action": str(last_reflection.get("action") or ""),
                "reason": str(last_reflection.get("reason") or ""),
                "planner": str(last_reflection.get("planner") or ""),
            } if last_reflection else {},
            "success_count": success_count,
            "failure_count": failure_count,
            "off_topic_count": off_topic,
            "empty_count": empty,
            "observation_statuses": observation_statuses,
            "latest_observation": latest_observation,
            "latest_observation_label": str(latest_observation.get("status") or ""),
            "replan_count": int(result.get("replan_count") or 0),
            "summary": str(job.get("summary") or ""),
            "headline": self._headline(status, target, str(job.get("title") or "")),
            "health_text": health_text,
            "next_action": next_action,
        }

    def _current_index(self, job: dict[str, Any], tasks: list[Any], planned: list[Any]) -> int:
        current_index = min(len(tasks) + 1, len(planned)) if planned else len(tasks)
        if str(job.get("status") or "") in {"stopped", "succeeded", "failed"} and planned and tasks:
            current_index = min(len(tasks), len(planned))
        return current_index

    def _next_action(
        self,
        *,
        status: str,
        current_task: dict[str, Any],
        current_index: int,
        total_tasks: int,
        current_query: str,
        result: dict[str, Any],
    ) -> str:
        next_action = "等待 Crawler 规划任务。"
        if status in {"queued", "running"} and current_task:
            query_label = current_query or "等待 CrawlerAgent 给出可执行查询"
            next_action = f"正在执行第 {current_index}/{total_tasks} 个采集任务：{self.source_label(str(current_task.get('source') or ''))} · {query_label}"
        elif status == "succeeded":
            next_action = "采集已完成；如果有新资料，后台会继续入库或已经完成入库。"
        elif status == "failed":
            next_action = "本轮采集失败或没有找到可入库资料，需要 Crawler 重新规划更短、更准的查询词。"
        elif status == "stopped":
            next_action = "任务已停止。"
        if result.get("ingest_background"):
            next_action += " 后台入库正在处理。"
        if result.get("ingest"):
            next_action += " 后台入库已完成。"
        return next_action

    def _status_label(self, status: str) -> str:
        return {
            "queued": "排队中",
            "running": "运行中",
            "succeeded": "已完成",
            "failed": "失败",
            "stopped": "已停止",
        }.get(status, status or "未知")

    def _headline(self, status: str, target: str, title: str) -> str:
        subject = target or title or "Crawler 采集任务"
        return f"{subject} · {self._status_label(status)}"

    def _health_text(self, *, success_count: int, empty: int, off_topic: int, failure_count: int, latest_observation: dict[str, Any]) -> str:
        latest = str(latest_observation.get("status") or "")
        if latest == "quota_limited":
            return "最近一次工具结果显示额度不足，需要换来源或等待额度恢复。"
        if latest == "login_required":
            return "最近一次工具结果需要登录，Crawler 应切换公开来源或说明登录限制。"
        if latest == "captcha_required":
            return "最近一次工具结果需要验证码，Crawler 应切换来源或使用浏览器辅助采集。"
        if success_count:
            return f"已拿到 {success_count} 个可入库候选。"
        if empty or off_topic or failure_count:
            return f"暂未拿到稳定候选：空结果 {empty}，跑偏 {off_topic}，失败 {failure_count}。"
        return "Crawler 正在规划或刚开始执行。"
