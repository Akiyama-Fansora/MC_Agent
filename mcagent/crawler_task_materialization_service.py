from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .crawler_capabilities import task_preflight


TaskIdentity = Callable[[dict[str, Any]], tuple[str, str]]
SourceAlias = Callable[[str], str]


class CrawlerTaskMaterializationService:
    """Normalize executable crawler tasks returned by planner/review LLM calls."""

    def task_identities(self, tasks: list[dict[str, Any]], *, identity_fn: TaskIdentity) -> list[tuple[str, str]]:
        return [identity_fn(task) for task in tasks]

    def existing_brief(self, tasks: list[dict[str, Any]], *, identity_fn: TaskIdentity) -> list[dict[str, str]]:
        return [{"source": source, "query": query} for source, query in self.task_identities(tasks, identity_fn=identity_fn)]

    def replan_session_summary(
        self,
        *,
        question: str,
        plan: dict[str, Any],
        failure_summary: list[dict[str, Any]],
        existing_tasks: list[dict[str, Any]],
        identity_fn: TaskIdentity,
    ) -> dict[str, Any]:
        return {
            "mode": "mid_job_replan",
            "previous_topic": plan.get("topic") or plan.get("target_hint") or question,
            "delivery_target": plan.get("delivery_target"),
            "cleaning_policy": plan.get("cleaning_policy"),
            "coverage_goals": plan.get("coverage_goals") or [],
            "success_criteria": plan.get("success_criteria") or [],
            "recent_failures": failure_summary,
            "already_planned_tasks": self.existing_brief(existing_tasks, identity_fn=identity_fn),
            "instruction": (
                "Previous crawler tasks were empty, off-topic, or failed. "
                "Revise the plan with short alternative queries and different sources. "
                "Do not repeat already planned source/query pairs. "
                "Keep delivery requirements separate from the data target."
            ),
        }

    def replan_question(self, *, question: str) -> str:
        return (
            "Replan crawler collection for this target. "
            "Use short search queries, alternate sources, and avoid repeated attempts. "
            f"Target: {question}"
        )

    def materialize_replan_tasks(
        self,
        *,
        new_plan: dict[str, Any],
        existing_tasks: list[dict[str, Any]],
        identity_fn: TaskIdentity,
        source_alias_fn: SourceAlias,
        max_new_tasks: int,
    ) -> list[dict[str, Any]]:
        seen = set(self.task_identities(existing_tasks, identity_fn=identity_fn))
        new_tasks: list[dict[str, Any]] = []
        for task in list(new_plan.get("tasks") or []):
            if not isinstance(task, dict):
                continue
            identity = identity_fn(task)
            if not identity[1] or identity in seen:
                continue
            seen.add(identity)
            cloned = dict(task)
            cloned["source"] = source_alias_fn(str(cloned.get("source") or "web_discovery"))
            if not task_preflight(cloned, check_domain=False).get("valid"):
                continue
            cloned["reason"] = f"mid-job replan after empty/off-topic results; {cloned.get('reason') or ''}".strip()
            new_tasks.append(cloned)
            if len(new_tasks) >= max_new_tasks:
                break
        return new_tasks

    def record_replan(
        self,
        *,
        plan: dict[str, Any],
        task_results_count: int,
        failure_summary: list[dict[str, Any]],
        new_tasks: list[dict[str, Any]],
        new_plan: dict[str, Any],
    ) -> None:
        replans = plan.setdefault("replans", [])
        if isinstance(replans, list):
            replans.append(
                {
                    "at_result_count": task_results_count,
                    "failure_summary": failure_summary,
                    "new_tasks": new_tasks,
                    "planner": new_plan.get("strategy") or new_plan.get("planner_model") or new_plan.get("raw_plan", {}).get("_planner_model"),
                }
            )

    def materialize_topic_review_tasks(
        self,
        *,
        review_plan: dict[str, Any],
        existing_tasks: list[dict[str, Any]],
        identity_fn: TaskIdentity,
        source_alias_fn: SourceAlias,
        max_new_tasks: int,
    ) -> list[dict[str, Any]]:
        seen = set(self.task_identities(existing_tasks, identity_fn=identity_fn))
        new_tasks: list[dict[str, Any]] = []
        for task in list(review_plan.get("tasks") or []):
            if not isinstance(task, dict):
                continue
            source = source_alias_fn(str(task.get("source") or ""))
            query = str(task.get("query") or "").strip()
            if source == "topic_discovery" or not query:
                continue
            cloned = dict(task)
            cloned["source"] = source
            cloned["query"] = query
            if not task_preflight(cloned, check_domain=False).get("valid"):
                continue
            cloned["reason"] = f"Crawler LLM reviewed topic discovery candidates; {cloned.get('reason') or ''}".strip()
            identity = identity_fn(cloned)
            if identity in seen:
                continue
            seen.add(identity)
            new_tasks.append(cloned)
            if len(new_tasks) >= max_new_tasks:
                break
        return new_tasks

    def filter_executable_reflection_tasks(self, tasks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Drop tasks that are objectively missing required tool inputs.

        This does not judge source relevance or usefulness. It only prevents the
        executor from running a tool call whose required local input is absent.
        """

        executable: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            preflight = task_preflight(task, check_domain=False)
            if not preflight.get("valid"):
                cloned = dict(task)
                cloned["blocked_reason"] = ",".join(preflight.get("issues") or []) or "crawler_capability_preflight_failed"
                cloned["blocked_message"] = str(preflight.get("message") or "")
                cloned["preflight"] = preflight
                blocked.append(cloned)
                continue
            executable.append(task)
        return executable, blocked

    def split_displayable_planned_tasks(self, tasks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Hide objectively non-executable planned tasks from normal progress.

        The blocked tasks are still returned separately for audit. This keeps
        historical job views from implying Crawler can run modpack_internal
        before a real archive/manifest path exists.
        """

        displayable: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            cloned = dict(task)
            preflight = task_preflight(cloned, check_domain=False)
            if not preflight.get("valid"):
                cloned["blocked_reason"] = ",".join(preflight.get("issues") or []) or "crawler_capability_preflight_failed"
                cloned["blocked_message"] = str(preflight.get("message") or "")
                cloned["preflight"] = preflight
                blocked.append(cloned)
                continue
            displayable.append(cloned)
        return displayable, blocked

    @staticmethod
    def _modpack_internal_without_input(task: dict[str, Any]) -> bool:
        source = str(task.get("source") or "").strip().lower()
        if source != "modpack_internal":
            return False
        for key in ("zip", "archive", "archive_path", "manifest_path", "path"):
            if str(task.get(key) or "").strip():
                return False
        return True

    def fallback_topic_tasks(
        self,
        *,
        seed_queries: list[Any],
        existing_tasks: list[dict[str, Any]],
        identity_fn: TaskIdentity,
        max_new_tasks: int,
    ) -> list[dict[str, Any]]:
        seen = set(self.task_identities(existing_tasks, identity_fn=identity_fn))
        new_tasks: list[dict[str, Any]] = []
        for index, query_value in enumerate(seed_queries):
            query = str(query_value).strip()
            if not query:
                continue
            source = "mcmod" if index < 10 else "web_discovery"
            task: dict[str, Any] = {
                "source": source,
                "query": query,
                "reason": "topic discovery seed expanded from existing local documents",
                "priority": 95 - index,
                "search_limit": 8,
                "max_urls": 6,
            }
            identity = identity_fn(task)
            if identity in seen:
                continue
            seen.add(identity)
            new_tasks.append(task)
            if len(new_tasks) >= max_new_tasks:
                break
        return new_tasks
