from __future__ import annotations

from typing import Any


class CrawlerJobProgressService:
    """Build running job update payloads for crawler loop phases."""

    def planned(self, *, topic: str, task_count: int, plan: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "summary": f"我已经围绕 {topic} 规划了 {task_count} 个采集动作。接下来我会逐个检查来源，记录成功、空结果、跑偏和受限原因。",
            "result": {
                "source": "planner",
                "plan": plan,
                "planned_tasks": tasks,
                "tasks": [],
                "loop": [
                    {"phase": "understand", "status": "done", "note": "我已经理解这轮采集目标和资料缺口。"},
                    {"phase": "plan", "status": "done", "note": "我已经生成覆盖目标、搜索词和候选来源。"},
                    {"phase": "act", "status": "running", "note": "我会按优先级执行工具，并记录每个来源的客观结果。"},
                    {"phase": "verify", "status": "pending", "note": "执行后我会审查记录数、跳过数、错误和是否需要入库。"},
                ],
            },
        }

    def reflecting(self, *, reflection: dict[str, Any], task_results: list[dict[str, Any]], tasks: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "summary": f"我正在根据已有观察决定下一步：{reflection.get('action')}\n理由：{reflection.get('reason')}",
            "result": {
                "source": "planner",
                "tasks": task_results,
                "planned_tasks": tasks,
                "plan": plan,
                "loop": [
                    {"phase": "understand", "status": "done"},
                    {"phase": "reflect", "status": "running", "note": str(reflection.get("reason") or "")},
                    {"phase": "act", "status": "pending", "note": "我已经选择下一步工具动作，工具还没有执行。"},
                    {"phase": "verify", "status": "pending"},
                ],
            },
        }

    def empty_query_blocked(self, *, source_label: str, task_results: list[dict[str, Any]], tasks: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "summary": f"我选到的查询为空，工具层已经拒绝执行；我需要重新规划这个来源。\n来源：{source_label}",
            "result": {
                "source": "planner",
                "tasks": task_results,
                "planned_tasks": tasks,
                "plan": plan,
                "loop": [
                    {"phase": "understand", "status": "done"},
                    {"phase": "reflect", "status": "pending", "note": "上一个被选中的任务没有可执行查询。"},
                    {"phase": "act", "status": "blocked", "note": "工具拒绝执行空查询。"},
                    {"phase": "verify", "status": "pending"},
                ],
            },
        }

    def executing(
        self,
        *,
        index: int,
        task_count: int,
        source_label: str,
        query: str,
        reason: str,
        task_results: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "summary": f"我正在执行第 {index}/{task_count} 个采集动作：{source_label}\n查询：{query}\n理由：{reason}",
            "result": {
                "source": "planner",
                "tasks": task_results,
                "planned_tasks": tasks,
                "plan": plan,
                "loop": [
                    {"phase": "understand", "status": "done"},
                    {"phase": "plan", "status": "done"},
                    {"phase": "act", "status": "running", "note": f"正在执行 {index}/{task_count}: {query}"},
                    {"phase": "verify", "status": "pending"},
                ],
            },
        }

    def reviewing_candidates(self, *, task_results: list[dict[str, Any]], tasks: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "summary": "我正在审查刚发现的候选来源，判断哪些值得继续展开，哪些应该拒绝或跳过。",
            "result": {
                "source": "planner",
                "tasks": task_results,
                "planned_tasks": tasks,
                "plan": plan,
                "loop": [
                    {"phase": "understand", "status": "done"},
                    {"phase": "plan", "status": "done"},
                    {"phase": "act", "status": "running"},
                    {"phase": "reviewing_candidates", "status": "running", "note": "主题发现产生了候选来源，我正在判断哪些值得继续展开。"},
                    {"phase": "verify", "status": "pending"},
                ],
            },
        }

    def replanning(
        self,
        *,
        bad_streak: int,
        replan_count: int,
        max_replans: int,
        task_results: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "summary": f"我连续遇到 {bad_streak} 个空结果、跑偏结果或失败结果，正在重新规划查询和来源（{replan_count}/{max_replans}）。",
            "result": {
                "source": "planner",
                "tasks": task_results,
                "planned_tasks": tasks,
                "plan": plan,
                "loop": [
                    {"phase": "understand", "status": "done"},
                    {"phase": "plan", "status": "done"},
                    {"phase": "act", "status": "running"},
                    {"phase": "replan", "status": "running", "note": "最近的工具结果低价值，我正在调整来源和查询词。"},
                    {"phase": "verify", "status": "pending"},
                ],
            },
        }
