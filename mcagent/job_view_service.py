from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable

from .agent_runtime import classify_crawler_tool_result
from .crawler_self_audit_service import CrawlerSelfAuditService


SourceLabelFn = Callable[[str], str]


def _safe_count(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class JobReadableViewService:
    """Build a human-readable job view for API/UI payloads."""

    source_label: SourceLabelFn

    def build(self, job: dict[str, Any]) -> dict[str, Any]:
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
        tasks = result.get("tasks") if isinstance(result.get("tasks"), list) else []
        planned = result.get("planned_tasks") if isinstance(result.get("planned_tasks"), list) else []
        blocked_planned = result.get("blocked_planned_tasks") if isinstance(result.get("blocked_planned_tasks"), list) else []
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
        target = self._display_target(plan, str(job.get("title") or ""))
        goals = [str(item) for item in (plan.get("coverage_goals") or []) if str(item).strip()]
        model_prior = plan.get("model_prior") if isinstance(plan.get("model_prior"), dict) else {}
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
        planner_error = str(plan.get("planner_error") or "").strip()
        fallback_used = bool(planner_error or "fallback_after_llm_planner_error" in str(plan.get("strategy") or ""))
        inter_agent_messages = self._inter_agent_messages(tasks)
        useful_outputs = self._useful_outputs(tasks)
        blocked_outputs = self._blocked_outputs(tasks)
        self_audit = CrawlerSelfAuditService().build(tasks, result)
        timeline = self._timeline(
            plan=plan,
            tasks=tasks,
            planned=planned,
            observations=observations,
            reflections=reflections,
            result=result,
            status=status,
        )

        return {
            "title": str(job.get("title") or ""),
            "status": status,
            "status_label": self._status_label(status),
            "target": target,
            "delivery_target": str(plan.get("delivery_target") or ""),
            "coverage_goals": goals[:5],
            "model_prior": self._model_prior_summary(model_prior),
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
            "planner_error": planner_error,
            "fallback_used": fallback_used,
            "planner_warning": (
                f"Crawler LLM 规划失败，当前使用兜底计划。失败原因：{planner_error}"
                if fallback_used and planner_error
                else ("Crawler LLM 规划失败，当前使用兜底计划。" if fallback_used else "")
            ),
            "inter_agent_messages": inter_agent_messages,
            "useful_outputs": useful_outputs,
            "blocked_outputs": blocked_outputs,
            "self_audit": self_audit,
            "self_audit_summary": self._self_audit_summary(self_audit),
            "blocked_planned_tasks": self._blocked_planned_tasks(blocked_planned),
            "plain_summary": self._plain_summary(
                status=status,
                success_count=success_count,
                useful_outputs=useful_outputs,
                blocked_outputs=blocked_outputs,
                empty=empty,
                off_topic=off_topic,
            ),
            "timeline": timeline,
        }

    def _model_prior_summary(self, prior: dict[str, Any]) -> dict[str, Any]:
        if not prior:
            return {}
        return {
            "target": str(prior.get("target") or ""),
            "aliases": [str(item) for item in list(prior.get("aliases") or [])[:6] if str(item).strip()],
            "likely_source_graph": [str(item) for item in list(prior.get("likely_source_graph") or [])[:8] if str(item).strip()],
            "search_leads": [str(item) for item in list(prior.get("search_leads") or [])[:8] if str(item).strip()],
            "verification_questions": [str(item) for item in list(prior.get("verification_questions") or [])[:6] if str(item).strip()],
            "evidence_status": str(prior.get("evidence_status") or "hypothesis_only"),
            "allowed_use": str(prior.get("allowed_use") or "planning_only"),
            "forbidden_use": str(prior.get("forbidden_use") or ""),
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
            next_action = f"我正在执行第 {current_index}/{total_tasks} 个采集动作：{self.source_label(str(current_task.get('source') or ''))} · {query_label}"
        elif status == "succeeded":
            next_action = "我已完成采集；如果有新资料，会继续入库或已经完成入库。"
        elif status == "failed":
            next_action = "我本轮没有拿到可入库资料，需要重新规划更短、更准的查询词。"
        elif status == "stopped":
            next_action = "我已停止这轮采集。"
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

    def _display_target(self, plan: dict[str, Any], title: str) -> str:
        raw = str(plan.get("topic") or plan.get("target_hint") or "").strip()
        question = str(plan.get("question") or "").strip()
        if self._looks_like_target_fragment(raw):
            extracted = self._extract_named_target(question)
            return extracted or title or raw
        return raw or self._extract_named_target(question) or title

    def _looks_like_target_fragment(self, value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return True
        if len(text) <= 14 and any(mark in text for mark in ("）", ")", "；", ";", "：", ":")):
            return True
        return text in {"具体名称与功能简介）", "具体名称与功能简介", "latest downloaded modpack"}

    def _extract_named_target(self, question: str) -> str:
        text = re.sub(r"\s+", " ", str(question or "")).strip()
        quoted = re.search(r"[“\"'「『](.{2,80}?)[”\"'」』]", text)
        if quoted:
            target = quoted.group(1).strip()
            if target:
                return target
        if "乌托邦探险之旅" in text:
            return "乌托邦探险之旅（Utopian Journey）"
        match = re.search(r"([\u4e00-\u9fffA-Za-z0-9 _.-]{2,40}?)(?:整合包|modpack)", text, flags=re.I)
        if match:
            target = match.group(1).strip(" ，。；;：:")
            if target:
                return f"{target}整合包"
        return ""

    def _health_text(self, *, success_count: int, empty: int, off_topic: int, failure_count: int, latest_observation: dict[str, Any]) -> str:
        latest = str(latest_observation.get("status") or "")
        if latest == "quota_limited":
            return "最近一次工具结果显示额度不足，需要更换来源或等待额度恢复。"
        if latest == "login_required":
            return "最近一次工具结果需要登录，Crawler 应切换公开来源或说明登录限制。"
        if latest == "captcha_required":
            return "最近一次工具结果需要验证码，Crawler 应切换来源或使用浏览器辅助采集。"
        if success_count:
            return f"已拿到 {success_count} 个可入库候选。"
        if empty or off_topic or failure_count:
            return f"暂未拿到稳定候选：空结果 {empty}，跑偏 {off_topic}，失败 {failure_count}。"
        return "Crawler 正在规划或刚开始执行。"

    def _inter_agent_messages(self, tasks: list[Any]) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for item in tasks:
            if not isinstance(item, dict):
                continue
            exchange = item.get("agent_message_exchange")
            if not isinstance(exchange, dict):
                continue
            for key in ("request", "reply"):
                message = exchange.get(key)
                if not isinstance(message, dict):
                    continue
                from_agent = str(message.get("from_agent") or "").strip()
                to_agent = str(message.get("to_agent") or "").strip()
                content = str(message.get("content") or "").strip()
                if from_agent and to_agent and content:
                    messages.append(
                        {
                            "from_agent": from_agent,
                            "to_agent": to_agent,
                            "content": content,
                            "intent": str(message.get("intent") or "").strip(),
                        }
                    )
        return messages[-8:]

    def _useful_outputs(self, tasks: list[Any]) -> list[dict[str, str]]:
        outputs: list[dict[str, str]] = []
        for item in tasks:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "").strip()
            observation = item.get("observation") if isinstance(item.get("observation"), dict) else {}
            status = str(observation.get("status") or "").strip()
            if source == "mcagent_context" or status not in {"ok", "duplicate_reused"}:
                continue
            stats = item.get("manifest_stats") if isinstance(item.get("manifest_stats"), dict) else {}
            records = _safe_count(stats.get("records"))
            usable_records = _safe_count(stats.get("usable_records"), default=records) if stats.get("usable_records") is not None else records
            if records > 0 and usable_records <= 0:
                continue
            outputs.append(
                {
                    "source": self.source_label(source),
                    "status": status,
                    "status_label": "新增可用资料" if status == "ok" else "复用已有资料",
                    "query": str(item.get("query") or "").strip(),
                    "records": str(records),
                    "export_dir": str(item.get("export_dir") or "").strip(),
                }
            )
        return outputs[:8]

    def _blocked_outputs(self, tasks: list[Any]) -> list[dict[str, str]]:
        outputs: list[dict[str, str]] = []
        important = {"auth_required", "login_required", "captcha_required", "quota_limited", "empty", "off_topic", "records_pending_review"}
        for item in tasks:
            if not isinstance(item, dict):
                continue
            observation = item.get("observation") if isinstance(item.get("observation"), dict) else {}
            status = str(observation.get("status") or "").strip()
            if status not in important:
                continue
            outputs.append(
                {
                    "source": self.source_label(str(item.get("source") or "")),
                    "status": status,
                    "status_label": status,
                    "query": str(item.get("query") or "").strip(),
                    "summary": str(observation.get("summary") or "").strip(),
                }
            )
        return outputs[-6:]

    def _self_audit_summary(self, audit: dict[str, Any]) -> str:
        counts = audit.get("counts") if isinstance(audit.get("counts"), dict) else {}
        accepted = int(counts.get("accepted") or 0)
        rejected = int(counts.get("rejected") or 0)
        pending = int(counts.get("pending_review") or 0)
        ingest_status = str(audit.get("ingest_status") or "skipped")
        ingest_label = {
            "running": "入库中",
            "done": "已入库",
            "failed": "入库失败",
            "skipped": "未入库",
        }.get(ingest_status, ingest_status)
        return f"Crawler 自审：接受 {accepted} 个来源，拒绝 {rejected} 个来源，待复核 {pending} 个来源；入库状态：{ingest_label}。"

    def _blocked_planned_tasks(self, tasks: list[Any]) -> list[dict[str, str]]:
        outputs: list[dict[str, str]] = []
        for item in tasks:
            if not isinstance(item, dict):
                continue
            outputs.append(
                {
                    "source": self.source_label(str(item.get("source") or "")),
                    "query": str(item.get("query") or "").strip(),
                    "reason": str(item.get("reason") or "").strip(),
                    "blocked_reason": str(item.get("blocked_reason") or "").strip(),
                    "blocked_message": str(item.get("blocked_message") or "").strip(),
                }
            )
        return outputs[:8]

    def _plain_summary(
        self,
        *,
        status: str,
        success_count: int,
        useful_outputs: list[dict[str, str]],
        blocked_outputs: list[dict[str, str]],
        empty: int,
        off_topic: int,
    ) -> str:
        if status in {"queued", "running"}:
            return "Crawler 正在采集。默认只显示关键进展，完整过程已折叠到详情。"
        if useful_outputs:
            return f"本轮拿到/复用了 {len(useful_outputs)} 类可用资料；另有 {empty} 个空结果、{off_topic} 个跑偏结果已折叠。自审详情会列出接受、拒绝、待复核和入库状态。"
        if success_count:
            return f"本轮记录到 {success_count} 个可用候选，但没有形成新的外部资料摘要。"
        if blocked_outputs:
            return "本轮没有稳定补到新资料，主要受空结果、跑偏或访问限制影响。自审详情会列出受限来源、拒绝原因和下一步动作。"
        return "本轮采集已结束。"

    def _timeline(
        self,
        *,
        plan: dict[str, Any],
        tasks: list[Any],
        planned: list[Any],
        observations: list[dict[str, Any]],
        reflections: list[Any],
        result: dict[str, Any],
        status: str,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        topic = str(plan.get("topic") or plan.get("target_hint") or plan.get("question") or "").strip()
        if topic or planned:
            planner_error = str(plan.get("planner_error") or "").strip()
            strategy = str(plan.get("strategy") or "").strip()
            fallback_used = bool(planner_error or "fallback_after_llm_planner_error" in strategy)
            events.append(
                {
                    "type": "plan",
                    "label": "规划",
                    "status": "warning" if fallback_used else ("ok" if planned else "pending"),
                    "title": topic or "CrawlerAgent 正在规划任务",
                    "text": (
                        f"Crawler LLM 规划失败，当前使用兜底计划。失败原因：{planner_error}"
                        if fallback_used and planner_error
                        else (f"已规划 {len(planned)} 个采集动作。" if planned else "等待 CrawlerAgent 规划可执行采集动作。")
                    ),
                }
            )

        for index, item in enumerate(planned, start=1):
            if not isinstance(item, dict):
                continue
            executed = tasks[index - 1] if index - 1 < len(tasks) and isinstance(tasks[index - 1], dict) else {}
            observation = observations[index - 1] if index - 1 < len(observations) else {}
            source = self.source_label(str(item.get("source") or executed.get("source") or ""))
            query = str(item.get("query") or executed.get("query") or "").strip()
            reason = str(item.get("reason") or "").strip()
            status_text = str(observation.get("status") or ("running" if status == "running" and index == len(tasks) + 1 else "pending"))
            text_parts = [part for part in (query, reason, str(observation.get("summary") or "").strip()) if part]
            events.append(
                {
                    "type": "task",
                    "label": f"采集 {index}",
                    "status": status_text,
                    "title": source or f"采集动作 {index}",
                    "text": "；".join(text_parts),
                }
            )

        for index, item in enumerate(reflections, start=1):
            if not isinstance(item, dict):
                continue
            reason = str(item.get("reason") or "").strip()
            action = str(item.get("action") or "").strip()
            if not reason and not action:
                continue
            events.append(
                {
                    "type": "reflection",
                    "label": f"反思 {index}",
                    "status": action or "reflection",
                    "title": "CrawlerAgent 判断",
                    "text": reason or action,
                }
            )

        replan_count = int(result.get("replan_count") or 0)
        if replan_count:
            events.append(
                {
                    "type": "replan",
                    "label": "重规划",
                    "status": "retry",
                    "title": f"已重规划 {replan_count} 次",
                    "text": "CrawlerAgent 根据空结果、跑偏、失败或重复情况调整采集策略。",
                }
            )
        if result.get("ingest_background"):
            events.append({"type": "ingest", "label": "入库", "status": "running", "title": "后台入库处理中", "text": "采集结果已交给后台导入。"})
        if result.get("ingest"):
            events.append({"type": "ingest", "label": "入库", "status": "ok", "title": "后台入库已完成", "text": "新资料已写入本地库或完成处理。"})
        if result.get("ingest_error"):
            events.append({"type": "ingest", "label": "入库", "status": "failed", "title": "后台入库失败", "text": str(result.get("ingest_error") or "")})
        return events[:80]
