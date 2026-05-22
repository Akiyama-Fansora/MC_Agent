from __future__ import annotations

from typing import Any


class CrawlerJobProgressService:
    """Build running job update payloads for crawler loop phases."""

    def planned(self, *, topic: str, task_count: int, plan: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "summary": f"Crawler planned {topic}: {task_count} collection tasks. Next: probe, verify, retry with alternate sources if needed, then ingest.",
            "result": {
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
        }

    def reflecting(self, *, reflection: dict[str, Any], task_results: list[dict[str, Any]], tasks: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "summary": f"CrawlerAgent 正在思考下一步：{reflection.get('action')}\n理由：{reflection.get('reason')}",
            "result": {
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
        }

    def empty_query_blocked(self, *, source_label: str, task_results: list[dict[str, Any]], tasks: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "summary": f"CrawlerAgent 选择了一个空查询，工具层已拒绝执行，等待 CrawlerAgent 重新规划。\n来源：{source_label}",
            "result": {
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
            "summary": f"多源补库运行中：{index}/{task_count} {source_label}\n查询：{query}\n原因：{reason}",
            "result": {
                "source": "planner",
                "tasks": task_results,
                "planned_tasks": tasks,
                "plan": plan,
                "loop": [
                    {"phase": "understand", "status": "done"},
                    {"phase": "plan", "status": "done"},
                    {"phase": "act", "status": "running", "note": f"Executing {index}/{task_count}: {query}"},
                    {"phase": "verify", "status": "pending"},
                ],
            },
        }

    def reviewing_candidates(self, *, task_results: list[dict[str, Any]], tasks: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "summary": "Crawler 正在审核主题发现候选：由 Crawler LLM 判断哪些候选值得继续采集。",
            "result": {
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
            "summary": f"Crawler detected {bad_streak} empty/off-topic/failed results. Replanning queries and sources ({replan_count}/{max_replans}).",
            "result": {
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
        }
