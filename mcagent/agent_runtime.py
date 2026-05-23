from __future__ import annotations

from dataclasses import dataclass, field
import json
import time
from typing import Any, Iterable


LLM_OWNERSHIP_PRINCIPLES = [
    "LLM owns interpretation, tool choice, reflection, and final answer wording.",
    "Tools provide objective observations only; tools must not invent final answers or subjective decisions.",
    "Agent behavior must be general. Do not encode one-off user test phrases as routing rules.",
    "Every non-trivial action should be observable as: observe -> deliberate -> choose_action -> preflight -> execute_tool -> observe_result -> reflect -> continue_or_finish.",
    "If a task is delegated between agents, pass caller, target, known context, acceptance criteria, delivery target, and failure-reporting expectations.",
]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    result_schema: dict[str, Any] = field(default_factory=dict)
    side_effects: str = "none"
    terminal: bool = False
    llm_final_answer_required: bool = True

    def to_prompt_line(self) -> str:
        effect = f" side_effects={self.side_effects}" if self.side_effects else ""
        inputs = f" inputs={','.join(self.input_schema.keys())}" if self.input_schema else ""
        final = " final_by_llm" if self.llm_final_answer_required else " objective_tool_result"
        return f"- {self.name}: {self.description}{inputs}{effect};{final}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "result_schema": self.result_schema,
            "side_effects": self.side_effects,
            "terminal": self.terminal,
            "llm_final_answer_required": self.llm_final_answer_required,
        }


@dataclass(frozen=True, slots=True)
class AgentRole:
    agent_id: str
    display_name: str
    responsibility: str
    relationship: str

    def to_prompt_text(self) -> str:
        return (
            f"{self.display_name} ({self.agent_id}): {self.responsibility} "
            f"Relationship: {self.relationship}"
        )


@dataclass(slots=True)
class AgentAction:
    action: str
    goal: str
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    next_step_risk: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "tool_name": self.tool_name,
            "goal": self.goal,
            "arguments": self.arguments,
            "reason": self.reason,
            "next_step_risk": self.next_step_risk,
        }


@dataclass(frozen=True, slots=True)
class AgentToolDecision:
    tool: str
    reason: str = ""
    rag_focus: str = ""
    collection_target: str = ""
    delivery_target: str = ""
    action_plan: list[dict[str, Any]] = field(default_factory=list)
    planner: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "reason": self.reason,
            "rag_focus": self.rag_focus,
            "collection_target": self.collection_target,
            "delivery_target": self.delivery_target,
            "action_plan": self.action_plan,
            "planner": self.planner,
        }


TOOL_DECISION_ALIASES = {
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


def normalize_agent_tool_decision(
    value: dict[str, Any],
    *,
    agent_id: str,
    original_question: str,
    planner: str,
) -> AgentToolDecision:
    raw_tool = str(value.get("tool") or "").strip().lower()
    tool = TOOL_DECISION_ALIASES.get(raw_tool, raw_tool)
    allowed_routes = {"answer", "direct_answer", "planned_workflow", "status", "delegate_crawler"}
    allowed_routes.update(tool_names_for_agent(agent_id))
    if agent_id == "crawler_agent" and tool == "answer":
        tool = "direct_answer"
    if tool not in allowed_routes:
        tool = "router_error"

    action_plan: list[dict[str, Any]] = []
    raw_plan = value.get("action_plan")
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

    return AgentToolDecision(
        tool=tool,
        reason=str(
            value.get("reason")
            or (
                f"Agent LLM returned invalid tool {raw_tool!r}; no fallback tool executed."
                if tool == "router_error"
                else "Agent LLM selected tool."
            )
        ).strip()[:500],
        rag_focus=str(value.get("rag_focus") or "").strip()[:500],
        collection_target=str(value.get("collection_target") or original_question).strip(),
        delivery_target=str(value.get("delivery_target") or "").strip(),
        action_plan=action_plan,
        planner=planner,
    )


@dataclass(frozen=True, slots=True)
class AgentLoopEvent:
    stage: str
    status: str
    detail: Any = None
    timestamp: float = field(default_factory=time.time)

    def to_trace_dict(self) -> dict[str, Any]:
        return {
            "time": self.timestamp,
            "stage": self.stage,
            "status": self.status,
            "detail": self.detail,
        }


def make_agent_loop_event(stage: str, status: str, detail: Any = None) -> AgentLoopEvent:
    return AgentLoopEvent(stage=str(stage), status=str(status), detail=detail)


TOOL_RESULT_STATUSES = frozenset(
    {
        "ok",
        "empty",
        "off_topic",
        "duplicate_reused",
        "auth_required",
        "quota_limited",
        "captcha_required",
        "login_required",
        "network_error",
        "timeout",
        "parse_error",
        "execution_error",
        "uncertain",
        "blocked",
        "stopped",
    }
)


@dataclass(frozen=True, slots=True)
class ToolObservation:
    tool_name: str
    status: str
    summary: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False
    suggested_next: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "duplicate_reused"}

    @property
    def bad(self) -> bool:
        return not self.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "status": self.status if self.status in TOOL_RESULT_STATUSES else "execution_error",
            "summary": self.summary,
            "detail": self.detail,
            "retryable": self.retryable,
            "suggested_next": self.suggested_next,
        }


@dataclass(frozen=True, slots=True)
class HandoffContract:
    requested_by: str
    from_agent: str
    to_agent: str
    user_request: str
    task_goal: str
    delivery_target: str
    known_context: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    failure_report: str = "If blocked, report exact failed sources/tools, reason, and next viable path."

    def to_prompt_text(self) -> str:
        criteria = "\n".join(f"- {item}" for item in self.acceptance_criteria if item)
        return (
            f"Requested by: {self.requested_by}\n"
            f"From: {self.from_agent}\n"
            f"To: {self.to_agent}\n"
            f"Original user request: {self.user_request}\n"
            f"Task goal: {self.task_goal}\n"
            f"Delivery target: {self.delivery_target}\n"
            f"Known context: {self.known_context or 'none'}\n"
            f"Acceptance criteria:\n{criteria or '- satisfy the task goal with citeable objective evidence'}\n"
            f"Failure reporting: {self.failure_report}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_by": self.requested_by,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "user_request": self.user_request,
            "task_goal": self.task_goal,
            "delivery_target": self.delivery_target,
            "known_context": self.known_context,
            "acceptance_criteria": self.acceptance_criteria,
            "failure_report": self.failure_report,
        }


def _result_text_for_classification(result: dict[str, Any]) -> str:
    manifest = result.get("manifest_stats") if isinstance(result.get("manifest_stats"), dict) else {}
    parts = [
        result.get("output"),
        result.get("failure_reason"),
        result.get("error"),
        result.get("reason"),
        manifest.get("failure_reason"),
        manifest.get("note"),
        manifest.get("status"),
    ]
    return " ".join(str(item) for item in parts if item).lower()


def _manifest_count(result: dict[str, Any], key: str) -> int:
    manifest = result.get("manifest_stats") if isinstance(result.get("manifest_stats"), dict) else {}
    try:
        return int(manifest.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def classify_crawler_tool_result(result: dict[str, Any]) -> ToolObservation:
    """Classify an objective crawler tool result for Agent reflection.

    This does not decide the user-facing answer. It only normalizes observable
    tool outcomes so CrawlerAgent can reflect and choose the next action.
    """

    source = str(result.get("source") or "unknown")
    records = _manifest_count(result, "records")
    skipped = _manifest_count(result, "skipped")
    errors = _manifest_count(result, "errors")
    returncode = int(result.get("returncode") or 0)
    text = _result_text_for_classification(result)
    detail = {
        "source": source,
        "query": result.get("query"),
        "returncode": returncode,
        "records": records,
        "skipped": skipped,
        "errors": errors,
    }

    def observation(status: str, summary: str, *, retryable: bool = False, suggested_next: str = "") -> ToolObservation:
        return ToolObservation(
            tool_name=source,
            status=status,
            summary=summary,
            detail=detail,
            retryable=retryable,
            suggested_next=suggested_next,
        )

    if result.get("stop_requested") or returncode == 130:
        return observation("stopped", "Tool execution was stopped before completion.", retryable=True, suggested_next="Ask whether to resume or choose a smaller next action.")
    if result.get("empty_query_result"):
        return observation("blocked", "Tool refused to run because the query or target was empty.", retryable=True, suggested_next="Reflect on the goal and choose a concrete query, URL, or file target.")
    if result.get("timed_out") or returncode == 124 or "timed out" in text or "timeout" in text:
        return observation("timeout", "Tool timed out before returning usable data.", retryable=True, suggested_next="Retry with a narrower target, browser path, or lower page/file limit.")
    if bool(result.get("existing_evidence_reused", {}).get("matched")):
        return observation("duplicate_reused", "New fetch was duplicate, but matching existing evidence was found and reused.")
    if result.get("off_topic_result"):
        return observation("off_topic", "Tool returned content, but CrawlerAgent/evidence validation judged it off-topic.", retryable=True, suggested_next="Change source, query, URL, or validation context before retrying.")
    if result.get("uncertain_result"):
        return observation("uncertain", "Tool returned content whose relevance is uncertain.", retryable=True, suggested_next="Ask CrawlerAgent to inspect examples and decide whether to expand, keep, or discard.")
    if result.get("empty_result") or result.get("archive_not_found"):
        return observation("empty", "Tool produced no usable records for this target.", retryable=True, suggested_next="Try a different source, alias, broader/narrower query, browser search, or direct URL.")

    if returncode != 0:
        if any(token in text for token in ("quota", "429", "rate limit", "insufficient credit", "billing", "credit")):
            return observation("quota_limited", "Provider quota or rate limit blocked this tool call.", retryable=False, suggested_next="Use browser extraction, local HTTP fetch, or another public source.")
        if any(token in text for token in ("captcha", "verify you are human", "verification")):
            return observation("captcha_required", "Target appears to require captcha or human verification.", retryable=False, suggested_next="Report the blocker or use an accessible mirror/source.")
        if any(token in text for token in ("login", "sign in", "401", "403", "auth", "permission", "unauthorized", "forbidden")):
            status = "login_required" if "login" in text or "sign in" in text else "auth_required"
            return observation(status, "Target or provider requires authentication.", retryable=False, suggested_next="Use configured credentials, a public source, or report the access limit.")
        if any(token in text for token in ("failed to fetch", "connection", "dns", "ssl", "network", "reset by peer", "proxy")):
            return observation("network_error", "Network or transport error prevented collection.", retryable=True, suggested_next="Retry later or switch to another provider/browser path.")
        if any(token in text for token in ("jsondecode", "parse", "parser", "invalid json", "html parse")):
            return observation("parse_error", "Tool fetched data but failed while parsing it.", retryable=True, suggested_next="Save raw HTML/text and use a more tolerant parser or browser extraction.")
        return observation("execution_error", "Tool command failed.", retryable=True, suggested_next="Inspect output_tail, then retry with adjusted parameters or a different tool.")

    if records > 0:
        return observation("ok", "Tool produced usable records.")
    if skipped > 0:
        return observation("empty", "Tool produced no new records; output was skipped or already filtered.", retryable=True, suggested_next="Inspect skipped reasons and decide whether existing evidence is enough or a new source is needed.")
    return observation("empty", "Tool completed but produced no records.", retryable=True, suggested_next="Try a different source, alias, broader/narrower query, browser search, or direct URL.")


AGENT_ROLES = {
    "mcagent_rag": AgentRole(
        agent_id="mcagent_rag",
        display_name="MCagent",
        responsibility=(
            "Talks with the user first-hand, understands conversation context, "
            "uses local/RAG/status/delegation tools when useful, and writes final answers."
        ),
        relationship="May ask CrawlerAgent for missing evidence; must not turn every message into retrieval.",
    ),
    "crawler_agent": AgentRole(
        agent_id="crawler_agent",
        display_name="CrawlerAgent",
        responsibility=(
            "Collects, verifies, saves, and prepares web/local data. It can serve the user directly "
            "or prepare data for MCagent/RAG depending on the delivery target."
        ),
        relationship="Receives direct human tasks or MCagent handoffs, then plans its own collection loop.",
    ),
    "retriever_only": AgentRole(
        agent_id="retriever_only",
        display_name="Retriever-only mode",
        responsibility="Returns objective local retrieval results without LLM final-answer ownership.",
        relationship="Mode, not a third conversational agent.",
    ),
}


MCAGENT_TOOLS = [
    ToolSpec(
        name="direct_answer",
        description="Answer directly with the LLM when no external tool is needed.",
        input_schema={"question": "original user message", "session_summary": "conversation memory"},
        result_schema={"answer": "LLM-written final answer"},
        terminal=True,
    ),
    ToolSpec(
        name="local_rag_search",
        description="Search local documents, chunks, raw HTML, and manifests for evidence.",
        input_schema={"question": "topic-focused search question"},
        result_schema={"sources": "ranked citeable evidence", "context": "formatted evidence"},
        side_effects="read_local_index",
        llm_final_answer_required=True,
    ),
    ToolSpec(
        name="evidence_select",
        description="Select and validate whether retrieved evidence is enough for the user's question.",
        input_schema={"question": "evidence question", "candidates": "retrieval results"},
        result_schema={"verdict": "ok|insufficient", "reasons": "evidence gaps"},
        side_effects="none",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="final_answer_llm",
        description="Write the user-facing answer from selected evidence and conversation context.",
        input_schema={"question": "user question", "selected_sources": "evidence"},
        result_schema={"answer": "LLM-written grounded response"},
        terminal=True,
    ),
    ToolSpec(
        name="status",
        description="Read crawler/import/background job status when the user asks about system progress.",
        input_schema={"scope": "optional status focus"},
        result_schema={"status": "human-readable current system state"},
        side_effects="read_runtime_state",
        terminal=True,
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="delegate_crawler",
        description="Send a natural-language data collection task to CrawlerAgent with context and delivery target.",
        input_schema={"handoff_contract": "caller, goal, context, acceptance criteria"},
        result_schema={"job_id": "crawler job", "handoff": "agent-to-agent message"},
        side_effects="start_background_job",
        terminal=True,
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="planned_workflow",
        description="Create a short tool plan for compound requests, then execute steps with LLM checkpoints.",
        input_schema={"goal": "compound user goal", "steps": "candidate tool steps"},
        result_schema={"plan": "observable action plan"},
        side_effects="depends_on_steps",
        llm_final_answer_required=True,
    ),
]


CRAWLER_ROUTE_TOOLS = [
    ToolSpec(
        name="direct_answer",
        description="Explain CrawlerAgent capabilities, limitations, or simple non-collection replies with the LLM.",
        input_schema={"question": "original user message", "session_summary": "conversation memory"},
        result_schema={"answer": "LLM-written response"},
        terminal=True,
    ),
    ToolSpec(
        name="status",
        description="Read current crawler/import/background job status.",
        input_schema={"scope": "optional status focus"},
        result_schema={"status": "current crawler state"},
        side_effects="read_runtime_state",
        terminal=True,
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="temporary_extract",
        description="Fetch public URL text for an immediate CrawlerAgent answer when persistence/background collection is not needed; use for direct read/summarize/extract tasks that should not save or ingest.",
        input_schema={"query_or_url": "public URL and extraction/summarization request"},
        result_schema={"extracted_text": "temporary page text", "answer": "LLM-written summary", "saved_to_local": False},
        side_effects="network_only_no_filesystem_persistence",
        terminal=True,
        llm_final_answer_required=True,
    ),
    ToolSpec(
        name="mcagent_context",
        description="Inspect MCagent/RAG local evidence and likely gaps for a topic before answering or before collecting data for MCagent/RAG.",
        input_schema={"question": "topic-focused local MCagent/RAG context or gap question"},
        result_schema={"sources": "local MCagent/RAG evidence", "gap_summary": "LLM-written local context and missing-data summary"},
        side_effects="read_local_index",
        terminal=False,
        llm_final_answer_required=True,
    ),
    ToolSpec(
        name="delegate_crawler",
        description="Accept a human or MCagent collection request and start CrawlerAgent's background collection loop; use when persistence, saving, multi-step collection, or RAG handoff is wanted.",
        input_schema={"collection_target": "natural-language goal", "delivery_target": "human|MCagent/RAG|both"},
        result_schema={"job_id": "crawler job", "handoff": "collection contract"},
        side_effects="start_background_job",
        terminal=True,
        llm_final_answer_required=False,
    ),
]


CRAWLER_COLLECTION_TOOLS = [
    ToolSpec(
        name="plan_collection",
        description="Understand the collection goal, delivery target, coverage goals, and next tool actions.",
        input_schema={"handoff_or_user_goal": "collection goal with context"},
        result_schema={"tasks": "ordered executable crawler tasks"},
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="mcagent_context",
        description="Ask MCagent/RAG for local evidence, coverage, and missing-data gaps for a topic during the CrawlerAgent collection loop.",
        input_schema={"query": "topic or gap question to inspect in MCagent/RAG local context"},
        result_schema={"sources": "local evidence report", "gap_summary": "local coverage and gaps", "manifest": "saved inter-agent context artifact"},
        side_effects="read_local_index_and_write_artifact",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="mcmod",
        description="Search and scrape MC百科 pages, preserving markdown, manifest, and raw HTML when available.",
        input_schema={"query": "short source-specific query"},
        result_schema={"records": "saved pages", "manifest": "metadata"},
        side_effects="network_and_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="modrinth",
        description="Search Modrinth projects and project contents.",
        input_schema={"query": "short project query"},
        result_schema={"records": "project/content markdown"},
        side_effects="network_and_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="browser_collect",
        description="Use a browser for general structured collection, saving XLSX/CSV/JSON/report/raw HTML/screenshot.",
        input_schema={"query_or_url": "target", "fields": "requested fields", "output_dir": "save path"},
        result_schema={"files": "saved structured outputs", "failure_reason": "if blocked"},
        side_effects="browser_network_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="fetch_url",
        description="Fetch one public URL with local HTTP and extract readable text/raw HTML without API keys.",
        input_schema={"query_or_url": "public URL or task containing a URL"},
        result_schema={"markdown": "saved extracted text", "raw_html": "saved raw response", "manifest": "metadata and errors"},
        side_effects="network_and_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="save_artifact",
        description="Save agent-provided content to a local file in txt, md, json, jsonl, csv, or html format.",
        input_schema={"content": "string/object/list to persist", "content_ref": "optional prior artifact id such as latest or r1.1", "format": "txt|md|json|jsonl|csv|html", "path": "file or directory path", "filename": "optional file name"},
        result_schema={"path": "saved file", "manifest": "save metadata", "failure_reason": "if serialization or filesystem write failed"},
        side_effects="filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="read_local_file",
        description="Read one local text file and expose it as a crawler artifact for later summarization or saving.",
        input_schema={"path": "absolute or workspace-relative file path", "max_chars": "optional read limit"},
        result_schema={"markdown": "saved readable copy", "manifest": "metadata and errors"},
        side_effects="read_filesystem_and_write_artifact",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="search_local_files",
        description="Search local text files under a directory or file path and return matching snippets as artifacts.",
        input_schema={"path": "directory or file path", "query": "terms to search", "max_files": "optional match limit"},
        result_schema={"matches": "matching files and snippets", "manifest": "metadata and errors"},
        side_effects="read_filesystem_and_write_artifact",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="playwright",
        description="Use a local browser to render/search pages, preserve text and raw HTML, and diagnose JS-heavy pages.",
        input_schema={"query": "short search or URL task"},
        result_schema={"records": "rendered page evidence"},
        side_effects="browser_network_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="modpack_download",
        description="Discover and download public .mrpack/.zip modpack archives when available.",
        input_schema={"query": "project/download query"},
        result_schema={"downloads": "archive files or failure reason"},
        side_effects="network_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="modpack_internal",
        description="Parse a real local modpack archive for manifest, modlist, quests, KubeJS, recipes, configs, and text.",
        input_schema={"archive_or_query": "downloaded/provided archive"},
        result_schema={"records": "internal files converted for RAG"},
        side_effects="filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="web_discovery",
        description="Discover public web pages and candidate URLs from broad search.",
        input_schema={"query": "short discovery query"},
        result_schema={"candidates": "URLs and snippets"},
        side_effects="network_and_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="finish",
        description="Stop the collection loop when enough useful data or a clear blocker report exists.",
        result_schema={"done_summary": "coverage and gaps"},
        terminal=True,
        llm_final_answer_required=True,
    ),
]


def tools_for_agent(agent_id: str) -> list[ToolSpec]:
    if agent_id == "crawler_agent":
        return list(CRAWLER_ROUTE_TOOLS)
    if agent_id == "retriever_only":
        return [tool for tool in MCAGENT_TOOLS if tool.name == "local_rag_search"]
    return list(MCAGENT_TOOLS)


def collection_tools_for_crawler() -> list[ToolSpec]:
    return list(CRAWLER_COLLECTION_TOOLS)


def tool_names_for_agent(agent_id: str) -> list[str]:
    return [tool.name for tool in tools_for_agent(agent_id)]


def tool_catalog_prompt(agent_id: str, *, include_principles: bool = True) -> str:
    role = AGENT_ROLES.get(agent_id, AGENT_ROLES["mcagent_rag"])
    lines: list[str] = [role.to_prompt_text(), "Available tools:"]
    lines.extend(tool.to_prompt_line() for tool in tools_for_agent(agent_id))
    if include_principles:
        lines.append("Runtime principles:")
        lines.extend(f"- {item}" for item in LLM_OWNERSHIP_PRINCIPLES)
    return "\n".join(lines)


def tool_catalog_json(agent_id: str) -> str:
    return json.dumps([tool.to_dict() for tool in tools_for_agent(agent_id)], ensure_ascii=False)


def crawler_collection_catalog_prompt(*, include_principles: bool = True) -> str:
    role = AGENT_ROLES["crawler_agent"]
    lines: list[str] = [role.to_prompt_text(), "Collection tools:"]
    lines.extend(tool.to_prompt_line() for tool in collection_tools_for_crawler())
    if include_principles:
        lines.append("Runtime principles:")
        lines.extend(f"- {item}" for item in LLM_OWNERSHIP_PRINCIPLES)
    return "\n".join(lines)


def validate_tool_name(agent_id: str, name: str, *, fallback: str = "direct_answer") -> str:
    names = set(tool_names_for_agent(agent_id))
    if name in names:
        return name
    if fallback in names:
        return fallback
    return next(iter(names), fallback)


def build_handoff_contract(
    *,
    requested_by: str,
    from_agent: str,
    to_agent: str,
    user_request: str,
    task_goal: str,
    delivery_target: str,
    known_context: str = "",
    acceptance_criteria: Iterable[str] = (),
) -> HandoffContract:
    return HandoffContract(
        requested_by=requested_by,
        from_agent=from_agent,
        to_agent=to_agent,
        user_request=user_request,
        task_goal=task_goal,
        delivery_target=delivery_target,
        known_context=known_context,
        acceptance_criteria=[str(item).strip() for item in acceptance_criteria if str(item).strip()],
    )
