from __future__ import annotations

from dataclasses import dataclass, field
import json
import time
from typing import Any, Iterable

from .crawler_capabilities import capability_catalog_prompt


LLM_OWNERSHIP_PRINCIPLES = [
    "LLM owns interpretation, tool choice, reflection, and final answer wording.",
    "Tools provide objective observations only; tools must not invent final answers or subjective decisions.",
    "Agent behavior must be general. Do not encode one-off user test phrases as routing rules.",
    "Each Agent must reason from its role identity before choosing tools; role identity is not a keyword trigger.",
    "All User/MCagent/CrawlerAgent communication should be representable as AgentMessage(from_agent, content, to_agent); message delivery does not decide the receiver's next tool.",
    "Every non-trivial action should be observable as: observe -> deliberate -> choose_action -> preflight -> execute_tool -> observe_result -> reflect -> continue_or_finish.",
    "If a task is delegated between agents, pass caller, target, known context, acceptance criteria, delivery target, and failure-reporting expectations.",
]


CRAWLER_RESEARCH_METHOD = [
    "CrawlerAgent is a general-purpose collection agent. Minecraft is only one domain toolset; do not treat MC sites or modpack archive tools as the default for every task.",
    "Choose tools by capability group first: general discovery/search, exact URL fetch, browser-rendered extraction, local file search/read, artifact persistence, then optional domain-specific toolsets such as Minecraft/modpack sources.",
    "Use domain-specific tools only when the target ecosystem justifies them or the user/MCagent handoff explicitly requests that domain. For non-Minecraft targets, prefer web_discovery, fetch_url, playwright, browser_collect, read_local_file/search_local_files, and save_artifact.",
    "Research method for CrawlerAgent: do not start with broad keyword blasting. First identify the target entity, aliases, language variants, official names, version scope, and likely source ecosystem.",
    "Build a source graph before scaling: official/project pages, documentation, repositories, package indexes, download/file pages, dependency/relation pages, changelogs/releases, wiki pages, forum posts, videos, and community mirrors.",
    "Prefer exact URLs and source-specific queries after identity is known. Use broad web discovery only to find candidate source nodes, then crawl those nodes directly.",
    "When a result is empty, duplicate, off-topic, blocked, or low-yield, change the source class or graph node instead of repeating similar generic searches.",
    "For Minecraft modpack archive discovery, use a stable route order: Modrinth API search with project_type:modpack and version files.url for .mrpack; CurseForge public/API file pages when a downloadUrl or direct /files download is objectively visible; GitHub Releases assets via browser_download_url; packwiz repositories by finding pack.toml and index.toml; then forum/community pages that expose direct .mrpack/.zip links.",
    "For Chinese community modpacks, also inspect public install guides and official/server sites: Yuque or docs pages, download guide pages, small public installer metadata, and text endpoints that disclose a final release URL. A cloud-drive page alone is not enough; a small public installer or guide can be evidence only if it exposes a no-login direct .zip/.mrpack URL that tools can probe and download.",
    "After finding a candidate archive route, inspect objective evidence before choosing the next task: page title/source, alias match, file extension, redirect target, content type, filename, size, status code, and whether login/captcha/payment/cloud-drive UI blocks full automation.",
    "Quark/Baidu/123pan/client-only/cloud-drive links are not fully automatic unless a direct public file URL is visible without login, captcha, payment, or manual user action. Record the blocker and switch routes instead of pretending the archive is available.",
    "Only schedule modpack_internal after a real local archive path, manifest path, or downloaded .mrpack/.zip is present in tool output or user context.",
    "For MCagent/RAG delivery, save citeable artifacts with markdown, manifest, source URL, metadata, raw text or raw HTML when available, and a clear coverage/gap summary.",
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
    to_agent: str = ""
    content: str = ""
    intent: str = ""
    action_plan: list[dict[str, Any]] = field(default_factory=list)
    planner: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "reason": self.reason,
            "rag_focus": self.rag_focus,
            "collection_target": self.collection_target,
            "delivery_target": self.delivery_target,
            "to_agent": self.to_agent,
            "content": self.content,
            "intent": self.intent,
            "action_plan": self.action_plan,
            "planner": self.planner,
        }


TOOL_DECISION_ALIASES = {
    "local_rag_search": "answer",
    "answer_from_evidence": "answer",
    "plan": "planned_workflow",
    "planned": "planned_workflow",
    "workflow": "planned_workflow",
}


def _safe_action_step_number(value: Any, fallback: int) -> tuple[int, str]:
    text = str(value or "").strip()
    if not text:
        return fallback, ""
    try:
        return int(text), ""
    except (TypeError, ValueError):
        return fallback, text[:80]


def _normalize_action_step_tool(agent_id: str, raw_step_tool: str, goal: str, allowed_action_tools: set[str]) -> str:
    step_tool = TOOL_DECISION_ALIASES.get(raw_step_tool, raw_step_tool)
    if step_tool in allowed_action_tools:
        return step_tool
    goal_text = str(goal or "").strip()
    lowered_goal = goal_text.lower()
    if agent_id == "mcagent_rag":
        inventory_markers = (
            "local corpus",
            "local library",
            "knowledge base",
            "inventory",
            "coverage",
            "modpack corpus",
            "what local",
            "what the local",
            "本地",
            "资料库",
            "知识库",
            "库存",
            "盘点",
            "覆盖",
            "有哪些资料",
            "有哪些整合包",
            "整合包清单",
            "收录",
        )
        final_markers = (
            "final answer",
            "summarize",
            "summary",
            "organize answer",
            "answer user",
            "整理",
            "总结",
            "最终回答",
            "生成回答",
            "组织回答",
        )
        if any(marker in goal_text for marker in final_markers) or any(marker in lowered_goal for marker in final_markers):
            return "final_answer_llm"
        if any(marker in goal_text for marker in inventory_markers) or any(marker in lowered_goal for marker in inventory_markers):
            return "local_corpus_inventory"
    if agent_id == "crawler_agent":
        context_markers = ("ask mcagent", "mcagent context", "local gap", "问 MCagent", "询问 MCagent", "本地缺口", "本地上下文")
        collect_markers = ("collect", "crawl", "fetch", "save", "ingest", "采集", "爬取", "抓取", "保存", "入库", "补充")
        if any(marker in goal_text for marker in context_markers) or any(marker in lowered_goal for marker in context_markers):
            return "mcagent_context"
        if any(marker in goal_text for marker in collect_markers) or any(marker in lowered_goal for marker in collect_markers):
            return "delegate_crawler"
    return "" if step_tool not in allowed_action_tools else step_tool


def normalize_agent_tool_decision(
    value: dict[str, Any],
    *,
    agent_id: str,
    original_question: str,
    planner: str,
) -> AgentToolDecision:
    raw_tool = str(value.get("tool") or "").strip().lower()
    tool = TOOL_DECISION_ALIASES.get(raw_tool, raw_tool)
    allowed_routes = {"answer", "direct_answer", "planned_workflow", "status"}
    allowed_routes.update(tool_names_for_agent(agent_id))
    if agent_id == "crawler_agent" and tool == "answer":
        tool = "direct_answer"
    if tool not in allowed_routes:
        tool = "router_error"

    action_plan: list[dict[str, Any]] = []
    allowed_action_tools = set(tool_names_for_agent(agent_id)) | {"answer", "direct_answer", "evidence_select", "final_answer_llm"}
    raw_plan = value.get("action_plan")
    if isinstance(raw_plan, list):
        for index, step in enumerate(raw_plan[:8], start=1):
            if not isinstance(step, dict):
                continue
            raw_step_tool = str(step.get("tool") or "").strip().lower()
            goal = str(step.get("goal") or step.get("description") or "").strip()[:300]
            step_tool = _normalize_action_step_tool(agent_id, raw_step_tool, goal, allowed_action_tools)
            step_number, step_label = _safe_action_step_number(step.get("step"), index)
            normalized_step = {
                "step": step_number,
                "tool": step_tool,
                "goal": goal,
            }
            if step_label:
                normalized_step["step_label"] = step_label
            action_plan.append(normalized_step)

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
        to_agent=str(value.get("to_agent") or value.get("to") or "").strip(),
        content=str(value.get("content") or value.get("message") or "").strip(),
        intent=str(value.get("intent") or "").strip(),
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
        "records_pending_review",
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


MCAGENT_OBJECTIVE_TOOL_NAMES = {
    "read_session_memory",
    "search_local_index",
    "inspect_local_corpus",
    "read_indexed_document",
    "select_evidence",
}


CRAWLER_OBJECTIVE_TOOL_NAMES = {
    "list_directory",
    "read_file",
    "search_files_by_name",
    "search_files_by_content",
    "get_file_metadata",
    "extract_text_from_pdf",
    "extract_text_from_docx",
    "extract_text_from_excel",
    "extract_text_from_archive",
    "fetch_url",
    "web_discovery",
    "playwright_snapshot",
    "browser_collect",
    "save_artifact",
}


MCAGENT_OBJECTIVE_TOOLS = [
    ToolSpec(
        name="read_session_memory",
        description="Read local conversation memory: user turns, agent replies, AgentMessage records, tool observations, and user corrections.",
        input_schema={"session_id": "conversation id", "limit": "optional recent event limit"},
        result_schema={"turns": "recent user/agent messages", "events": "AgentMessage/tool observations/preferences"},
        side_effects="read_local_memory",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="search_local_index",
        description="Search the indexed local Minecraft knowledge base and return objective matching chunks/documents with scores, titles, paths, and snippets.",
        input_schema={"query": "topic or evidence query", "limit": "optional result limit"},
        result_schema={"matches": "ranked chunks/documents", "scores": "retrieval scores", "paths": "source paths"},
        side_effects="read_local_index",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="inspect_local_corpus",
        description="Scan indexed local knowledge-base metadata across the whole corpus and return counts, titles, paths, source types, and mechanical entity candidates for Agent judgment.",
        input_schema={"scope": "optional corpus scope such as modpacks, mods, vanilla, resources"},
        result_schema={"counts": "objective counts by bucket", "documents": "representative raw records", "candidate_entities": "mechanically grouped names with evidence"},
        side_effects="read_local_index",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="read_indexed_document",
        description="Read one already indexed local document/chunk/raw artifact by id/path for exact evidence inspection.",
        input_schema={"document_id_or_path": "document id, chunk id, or source path", "max_chars": "optional read limit"},
        result_schema={"text": "raw indexed text", "metadata": "title, path, source, timestamps"},
        side_effects="read_local_index",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="select_evidence",
        description="Objectively package candidate evidence for the Agent to judge sufficiency; it does not decide the final answer.",
        input_schema={"question": "user question", "candidates": "retrieval or corpus records"},
        result_schema={"candidate_sources": "normalized evidence records", "gaps": "observable missing fields"},
        side_effects="none",
        llm_final_answer_required=False,
    ),
]


CRAWLER_OBJECTIVE_TOOLS = [
    ToolSpec(
        name="list_directory",
        description="List files and folders under a local path with optional recursion and shallow metadata.",
        input_schema={"path": "directory path", "recursive": "optional boolean"},
        result_schema={"entries": "names, paths, type, size, mtime"},
        side_effects="read_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="read_file",
        description="Read a local text file or text-like artifact with encoding and line/character limits.",
        input_schema={"path": "file path", "encoding": "optional encoding", "start_line": "optional", "end_line": "optional", "max_chars": "optional"},
        result_schema={"text": "file text", "metadata": "size, mtime, encoding, truncation"},
        side_effects="read_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="search_files_by_name",
        description="Find local files by glob or regex name pattern under a directory.",
        input_schema={"path": "root directory", "pattern": "glob or regex", "recursive": "optional boolean"},
        result_schema={"matches": "file paths and metadata"},
        side_effects="read_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="search_files_by_content",
        description="Search local text files for keywords or regex and return objective snippets with file paths and line numbers.",
        input_schema={"path": "root path", "query": "keyword or regex", "max_files": "optional"},
        result_schema={"matches": "paths, line numbers, snippets"},
        side_effects="read_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="get_file_metadata",
        description="Return local file metadata such as size, mtime, extension, type, and readability.",
        input_schema={"path": "file or directory path"},
        result_schema={"metadata": "objective filesystem metadata"},
        side_effects="read_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="extract_text_from_pdf",
        description="Extract text and page metadata from a local PDF for Agent review.",
        input_schema={"path": "pdf path", "page_range": "optional"},
        result_schema={"pages": "page text and metadata", "errors": "parse issues"},
        side_effects="read_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="extract_text_from_docx",
        description="Extract paragraphs/tables from a local Word document for Agent review.",
        input_schema={"path": "docx path"},
        result_schema={"text": "document text", "tables": "table text when available"},
        side_effects="read_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="extract_text_from_excel",
        description="Extract sheet names, cell ranges, and table-like text from a local spreadsheet.",
        input_schema={"path": "xlsx/csv path", "sheet": "optional"},
        result_schema={"sheets": "rows/cells/metadata"},
        side_effects="read_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="extract_text_from_archive",
        description="Inspect archive entries and optionally extract readable manifest/text files without judging relevance.",
        input_schema={"path": "zip/7z/rar/tar path", "patterns": "optional file filters"},
        result_schema={"entries": "archive listing", "texts": "selected text entries", "errors": "parse issues"},
        side_effects="read_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="fetch_url",
        description="Fetch one exact public URL and return status, headers, title, readable text, raw HTML path, and errors.",
        input_schema={"url": "public URL"},
        result_schema={"status_code": "HTTP status", "title": "page title", "text": "readable text", "raw_html_path": "optional"},
        side_effects="network",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="web_discovery",
        description="Search public web sources and return candidate URLs, titles, snippets, and source engine metadata.",
        input_schema={"query": "search query", "limit": "optional"},
        result_schema={"candidates": "URLs, titles, snippets, engines"},
        side_effects="network",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="playwright_snapshot",
        description="Open or search with a browser and return objective rendered-page observations: snapshot, text, screenshot path, console logs, network list, and action targets.",
        input_schema={"url_or_query": "URL or search task", "max_pages": "optional"},
        result_schema={"snapshot": "rendered page snapshot", "text": "page text", "screenshot_path": "optional", "network": "request list"},
        side_effects="browser_network",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="browser_collect",
        description="Use browser automation to collect structured rows/fields/screenshots from pages chosen by CrawlerAgent.",
        input_schema={"url_or_query": "target", "fields": "optional structured fields"},
        result_schema={"records": "objective rows/fields/files", "blockers": "captcha/login/timeout facts"},
        side_effects="browser_network_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="save_artifact",
        description="Persist content that CrawlerAgent already has into a local artifact; returns path and manifest only.",
        input_schema={"content": "content/ref", "path": "target path", "format": "optional format"},
        result_schema={"path": "saved file", "manifest": "write metadata"},
        side_effects="filesystem_write",
        llm_final_answer_required=False,
    ),
]


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
    for key in ("records", "search_results", "errors", "skipped"):
        value = result.get(key) or manifest.get(key)
        if value:
            parts.append(json.dumps(value, ensure_ascii=False, default=str)[:4000])
    return " ".join(str(item) for item in parts if item).lower()


def _manifest_count(result: dict[str, Any], key: str) -> int:
    manifest = result.get("manifest_stats") if isinstance(result.get("manifest_stats"), dict) else {}
    try:
        return int(manifest.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _result_count(result: dict[str, Any], key: str, *, default: int = 0) -> int:
    try:
        return int(result.get(key) or default)
    except (TypeError, ValueError):
        return default


def _manifest_has_only_empty_records(result: dict[str, Any]) -> bool:
    manifest = result.get("manifest_stats") if isinstance(result.get("manifest_stats"), dict) else {}
    try:
        records = int(manifest.get("records") or 0)
        usable_records = int(manifest.get("usable_records") or 0)
        empty_records = int(manifest.get("empty_records") or 0)
    except (TypeError, ValueError):
        return False
    return records > 0 and usable_records <= 0 and empty_records >= records


def classify_crawler_tool_result(result: dict[str, Any]) -> ToolObservation:
    """Classify an objective crawler tool result for Agent reflection.

    This does not decide the user-facing answer. It only normalizes observable
    tool outcomes so CrawlerAgent can reflect and choose the next action.
    """

    source = str(result.get("source") or "unknown")
    records = _manifest_count(result, "records")
    skipped = _manifest_count(result, "skipped")
    errors = _manifest_count(result, "errors")
    usable_records = _manifest_count(result, "usable_records")
    empty_records = _manifest_count(result, "empty_records")
    returncode = _result_count(result, "returncode")
    text = _result_text_for_classification(result)
    detail = {
        "source": source,
        "query": result.get("query"),
        "returncode": returncode,
        "records": records,
        "usable_records": usable_records,
        "empty_records": empty_records,
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
        return observation("duplicate_reused", "New fetch produced no new file, but CrawlerAgent accepted matching existing evidence for reuse.")
    if result.get("off_topic_result"):
        return observation("off_topic", "Tool returned content, but CrawlerAgent/evidence validation judged it off-topic.", retryable=True, suggested_next="Change source, query, URL, or validation context before retrying.")
    if result.get("uncertain_result"):
        return observation("uncertain", "Tool returned content whose relevance is uncertain.", retryable=True, suggested_next="Ask CrawlerAgent to inspect examples and decide whether to expand, keep, or discard.")
    if result.get("empty_result") or result.get("archive_not_found"):
        return observation("empty", "Tool produced no usable records for this target.", retryable=True, suggested_next="Try a different source, alias, broader/narrower query, browser search, or direct URL.")
    if _manifest_has_only_empty_records(result):
        return observation(
            "records_pending_review",
            "Tool produced record metadata, but objective file/character counts show no saved content bytes.",
            retryable=True,
            suggested_next="Ask CrawlerAgent to reject, rewrite, or retry with a source that produces non-empty evidence.",
        )

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

    validation = result.get("topic_validation") if isinstance(result.get("topic_validation"), dict) else {}
    explicit_review_action = str(
        result.get("crawler_review_action") or validation.get("crawler_review_action") or validation.get("review_action") or validation.get("decision") or ""
    ).strip().lower()
    if records > 0 and explicit_review_action in {"accept", "accepted", "keep", "use", "ingest", "accepted_for_task"}:
        return observation("ok", "CrawlerAgent explicitly reviewed and accepted records as useful evidence.")
    if records > 0 and bool(validation.get("matched")):
        return observation(
            "records_pending_review",
            "Tool produced topically matched records, but CrawlerAgent still needs to judge accept/reject/retry before use.",
            retryable=True,
            suggested_next="Ask CrawlerAgent to inspect matched records and explicitly decide accept, reject, retry, ignore, delete, or ingest.",
        )
    if records > 0:
        return observation(
            "records_pending_review",
            "Tool produced records for CrawlerAgent review; usefulness is not decided by the tool.",
            retryable=True,
            suggested_next="Ask CrawlerAgent to inspect the saved records and choose accept, reject, retry, ignore, or delete for this job.",
        )
    if skipped > 0:
        return observation("records_pending_review", "Tool produced no new records, but skipped outputs may reference existing evidence for CrawlerAgent review.", retryable=True, suggested_next="Inspect skipped reasons and previous_path values, then decide whether existing evidence is enough or a new source is needed.")
    return observation("empty", "Tool completed but produced no records.", retryable=True, suggested_next="Try a different source, alias, broader/narrower query, browser search, or direct URL.")


AGENT_ROLES = {
    "mcagent_rag": AgentRole(
        agent_id="mcagent_rag",
        display_name="MCagent",
        responsibility=(
            "A Minecraft-focused knowledge agent. It talks with the user first-hand, first asks "
            "whether the message is about Minecraft, modpacks, mods, gameplay, items, servers, "
            "MC documentation, or this local Minecraft knowledge base, then uses local/RAG/status/"
            "delegation tools when useful and writes final answers."
        ),
        relationship=(
            "Knows the participants User, MCagent, and CrawlerAgent. May message CrawlerAgent through "
            "the shared From-Content-To AgentMessage bus; must not turn every message into retrieval. "
            "If the message is not Minecraft-related, it can still answer as a normal assistant or explain its boundary."
        ),
    ),
    "crawler_agent": AgentRole(
        agent_id="crawler_agent",
        display_name="CrawlerAgent",
        responsibility=(
            "A general-purpose data collection agent. It collects, verifies, saves, and prepares "
            "web/local data across domains. Minecraft/modpack collection is one optional domain "
            "toolset, not CrawlerAgent's whole identity."
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
        description="Answer directly with the LLM when no external tool is needed, including greetings, capability explanations, and clearly non-Minecraft chat.",
        input_schema={"question": "original user message", "session_summary": "conversation memory"},
        result_schema={"answer": "LLM-written final answer"},
        terminal=True,
    ),
    ToolSpec(
        name="local_rag_search",
        description=(
            "Search the local Minecraft knowledge base documents, chunks, raw HTML, and manifests for citeable evidence "
            "about a specific gameplay/mod/modpack fact, guide, item, recipe, Boss, version, or known topic. "
            "This is not a whole-corpus inventory tool; it only returns relevant snippets and must not be used to claim what the entire local library contains."
        ),
        input_schema={"question": "specific topic-focused evidence question"},
        result_schema={"sources": "ranked citeable evidence", "context": "formatted evidence"},
        side_effects="read_local_index",
        llm_final_answer_required=True,
    ),
    ToolSpec(
        name="local_corpus_inventory",
        description=(
            "Objectively inspect the entire indexed local Minecraft knowledge base, grouping by source/type and exposing raw title, path, snippet, "
            "and mechanical candidate evidence for the Agent to judge. Use when the Agent judges the user is asking what the local library contains, "
            "which modpacks/projects are present, corpus coverage, inventory, or broad local-data scope rather than a specific gameplay fact."
        ),
        input_schema={"question": "coverage or inventory question"},
        result_schema={
            "answer": "objective local corpus inventory observation",
            "metadata.inventory_observation": "raw titles, paths, snippets, bucket counts, and mechanical entity candidates for LLM judgment",
            "sources": "representative indexed documents",
        },
        side_effects="read_local_index",
        terminal=True,
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
        name="crawler_audit",
        description="Read recent Crawler job self-audit details, including accepted/rejected/pending sources, rejection reasons, ingest status, and whether records entered the local knowledge base.",
        input_schema={"question": "audit question about recent crawler work"},
        result_schema={"answer": "objective audit summary", "job": "matched crawler job"},
        side_effects="read_runtime_state",
        terminal=True,
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="agent_message",
        description=(
            "Send one no-persistence From-Content-To AgentMessage to another participant. The current "
            "Agent must semantically decide whether the requested target is User, MCagent, or CrawlerAgent; "
            "a mentioned name can also be an ordinary word/name. This is MCagent's only cross-agent communication path; "
            "message delivery never chooses the receiver's next tool, and CrawlerAgent must decide any later collection action itself."
        ),
        input_schema={"to_agent": "User|MCagent|CrawlerAgent", "content": "message body", "intent": "optional semantic intent"},
        result_schema={"answer": "receiver reply delivered over AgentMessage", "agent_message": "message bus reply"},
        side_effects="agent_message_no_persistence",
        terminal=True,
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="planned_workflow",
        description=(
            "Create a short MCagent-owned plan for compound requests, then execute steps with LLM checkpoints. "
            "If the plan needs CrawlerAgent, the step must use agent_message to CrawlerAgent; MCagent has no separate crawler delegation tool."
        ),
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
        description="Inspect MCagent/RAG local evidence and likely gaps for a topic before answering or before collecting data for MCagent/RAG. If the same request also asks to collect/fill/supplement afterward, choose planned_workflow with mcagent_context followed by delegate_crawler instead of stopping at this single tool.",
        input_schema={"question": "topic-focused local MCagent/RAG context or gap question"},
        result_schema={"sources": "local MCagent/RAG evidence", "gap_summary": "LLM-written local context and missing-data summary"},
        side_effects="read_local_index",
        terminal=False,
        llm_final_answer_required=True,
    ),
    ToolSpec(
        name="agent_message",
        description=(
            "Send one no-persistence From-Content-To AgentMessage to another participant. The current "
            "Agent must semantically decide whether the requested target is User, MCagent, or CrawlerAgent; "
            "a mentioned name can also be an ordinary word/name. Message delivery never chooses the receiver's next tool."
        ),
        input_schema={"to_agent": "User|MCagent|CrawlerAgent", "content": "message body", "intent": "optional semantic intent"},
        result_schema={"answer": "receiver reply delivered over AgentMessage", "agent_message": "message bus reply"},
        side_effects="agent_message_no_persistence",
        terminal=True,
        llm_final_answer_required=False,
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
    ToolSpec(
        name="planned_workflow",
        description="Create a short CrawlerAgent route plan for compound requests, such as first inspecting MCagent/RAG context and then starting background collection. For 'ask MCagent first, then collect/fill/supplement' requests, action_plan must include mcagent_context and delegate_crawler. The LLM owns the plan and must include action_plan steps.",
        input_schema={"goal": "compound user goal", "steps": "candidate route tools such as mcagent_context and delegate_crawler"},
        result_schema={"plan": "observable action plan"},
        side_effects="depends_on_steps",
        llm_final_answer_required=True,
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
        description="Send an inter-agent message from CrawlerAgent to MCagent; MCagent then uses its own local RAG/evidence workflow and replies with local evidence and gaps.",
        input_schema={"query": "topic or gap question CrawlerAgent asks MCagent"},
        result_schema={"mcagent_reply": "MCagent's reply to CrawlerAgent", "sources": "local evidence report", "gap_summary": "local coverage and gaps", "manifest": "saved inter-agent transcript artifact"},
        side_effects="read_local_index_and_write_artifact",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="mcmod",
        description="Minecraft-domain tool: search and scrape MC百科 pages, preserving markdown, manifest, and raw HTML when available. Use only for Minecraft/MC百科 targets.",
        input_schema={"query": "short source-specific query"},
        result_schema={"records": "saved pages", "manifest": "metadata"},
        side_effects="network_and_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="modrinth",
        description="Minecraft-domain tool: search Modrinth projects and project contents. Use only for Minecraft mod/modpack/resource-pack ecosystems.",
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
        description="Use a local browser with Playwright MCP-style evidence: accessibility-like page snapshot, interactive targets, text, raw HTML, screenshot, console logs, and network request list.",
        input_schema={"query": "short search or URL task", "snapshot_depth": "optional page snapshot depth"},
        result_schema={"records": "rendered page evidence with snapshot paths, action targets, console/network artifacts"},
        side_effects="browser_network_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="modpack_download",
        description="Minecraft-domain tool: discover and download public .mrpack/.zip modpack archives when available, including exact direct URLs or pages chosen by CrawlerAgent.",
        input_schema={"query": "project/download query, exact archive URL, or public download/release page URL"},
        result_schema={"candidates": "objective archive candidates", "downloads": "archive files or failure reason", "blockers": "login/captcha/payment/cloud-drive limitations when observed"},
        side_effects="network_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="modpack_internal",
        description="Minecraft-domain tool: parse a real local modpack archive for manifest, modlist, quests, KubeJS, recipes, configs, and text.",
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


CRAWLER_GENERAL_COLLECTION_TOOL_NAMES = {
    "plan_collection",
    "mcagent_context",
    "browser_collect",
    "fetch_url",
    "save_artifact",
    "read_local_file",
    "search_local_files",
    "playwright",
    "web_discovery",
    "finish",
}


CRAWLER_DOMAIN_COLLECTION_TOOL_NAMES = {
    "minecraft": {
        "mcmod",
        "modrinth",
        "modpack_download",
        "modpack_internal",
    }
}


def general_collection_tools_for_crawler() -> list[ToolSpec]:
    return [tool for tool in CRAWLER_COLLECTION_TOOLS if tool.name in CRAWLER_GENERAL_COLLECTION_TOOL_NAMES]


def domain_collection_tools_for_crawler(domain: str) -> list[ToolSpec]:
    names = CRAWLER_DOMAIN_COLLECTION_TOOL_NAMES.get(str(domain or "").strip().lower(), set())
    return [tool for tool in CRAWLER_COLLECTION_TOOLS if tool.name in names]


def tools_for_agent(agent_id: str) -> list[ToolSpec]:
    if agent_id == "crawler_agent":
        return list(CRAWLER_ROUTE_TOOLS)
    if agent_id == "retriever_only":
        return [tool for tool in MCAGENT_TOOLS if tool.name == "local_rag_search"]
    return list(MCAGENT_TOOLS)


def objective_tools_for_agent(agent_id: str) -> list[ToolSpec]:
    if agent_id == "crawler_agent":
        return list(CRAWLER_OBJECTIVE_TOOLS)
    if agent_id == "retriever_only":
        return [tool for tool in MCAGENT_OBJECTIVE_TOOLS if tool.name == "search_local_index"]
    return list(MCAGENT_OBJECTIVE_TOOLS)


def collection_tools_for_crawler() -> list[ToolSpec]:
    return list(CRAWLER_COLLECTION_TOOLS)


def tool_names_for_agent(agent_id: str) -> list[str]:
    return [tool.name for tool in tools_for_agent(agent_id)]


def tool_catalog_prompt(agent_id: str, *, include_principles: bool = True) -> str:
    role = AGENT_ROLES.get(agent_id, AGENT_ROLES["mcagent_rag"])
    lines: list[str] = [
        role.to_prompt_text(),
        "Available tools are split into Agent route tools and objective observation tools.",
        "Route tools the Agent may choose as its next step:",
    ]
    lines.extend(tool.to_prompt_line() for tool in tools_for_agent(agent_id))
    objective_tools = objective_tools_for_agent(agent_id)
    if objective_tools:
        lines.append("Objective observation tools available inside those routes:")
        lines.extend(tool.to_prompt_line() for tool in objective_tools)
        lines.append(
            "Observation-tool boundary: these tools expose facts only. They do not decide whether evidence is enough, "
            "which Agent to message, whether to continue, or what the final answer should say."
        )
    if include_principles:
        lines.append("Runtime principles:")
        lines.extend(f"- {item}" for item in LLM_OWNERSHIP_PRINCIPLES)
    return "\n".join(lines)


def tool_catalog_json(agent_id: str) -> str:
    return json.dumps(
        {
            "route_tools": [tool.to_dict() for tool in tools_for_agent(agent_id)],
            "objective_observation_tools": [tool.to_dict() for tool in objective_tools_for_agent(agent_id)],
        },
        ensure_ascii=False,
    )


def crawler_collection_catalog_prompt(*, include_principles: bool = True) -> str:
    role = AGENT_ROLES["crawler_agent"]
    lines: list[str] = [role.to_prompt_text(), capability_catalog_prompt(), "Collection tools:"]
    lines.extend(tool.to_prompt_line() for tool in collection_tools_for_crawler())
    if include_principles:
        lines.append("Runtime principles:")
        lines.extend(f"- {item}" for item in LLM_OWNERSHIP_PRINCIPLES)
        lines.append("Crawler research method:")
        lines.extend(f"- {item}" for item in CRAWLER_RESEARCH_METHOD)
    return "\n".join(lines)


def compact_crawler_collection_catalog_prompt() -> str:
    """Short planning catalog for latency-sensitive CrawlerAgent LLM calls."""

    role = AGENT_ROLES["crawler_agent"]
    capability_groups = [
        "General discovery/search: web_discovery",
        "Exact public URL extraction: fetch_url",
        "Browser-rendered extraction and structured rows: playwright, browser_collect",
        "Local file inspection: read_local_file, search_local_files",
        "Persistence of already available content/artifacts: save_artifact",
        "Inter-agent local context request: mcagent_context",
        "Minecraft/domain sources when target is MC-related: mcmod, modrinth, followup, mediawiki, ftbwiki, createwiki",
        "Minecraft modpack archive and internals when objective archive/path evidence exists: modpack_download, modpack_internal",
    ]
    tool_lines: list[str] = []
    for tool in collection_tools_for_crawler():
        inputs = ",".join(tool.input_schema.keys()) if tool.input_schema else ""
        effect = tool.side_effects or "none"
        tool_lines.append(f"- {tool.name}: inputs={inputs or 'none'}; side_effects={effect}")
    principles = [
        "LLM chooses tool/query; tools only return objective observations.",
        "Do not invent exact URLs/slugs; use direct URL tasks only when URL is supplied or objectively discovered.",
        "For MCagent/RAG gap handoffs, convert gaps into positive coverage tasks, not literal 'missing/缺口' searches.",
        "For non-Minecraft targets, avoid Minecraft-specific sources.",
        "For structured extraction with fields/output_dir, prefer browser_collect and preserve user path.",
        "For modpack archive goals, use modpack_download before modpack_internal; modpack_internal requires a real local archive/manifest path.",
    ]
    return "\n".join(
        [
            role.to_prompt_text(),
            "Capability groups:",
            *[f"- {item}" for item in capability_groups],
            "Tools:",
            *tool_lines,
            "Planning principles:",
            *[f"- {item}" for item in principles],
        ]
    )


def validate_tool_name(agent_id: str, name: str, *, fallback: str = "router_error") -> str:
    names = set(tool_names_for_agent(agent_id))
    if name in names:
        return name
    unsafe_fallbacks = {"delegate_crawler", "planned_workflow", "temporary_extract"}
    if fallback in unsafe_fallbacks:
        return "router_error"
    if fallback == "router_error" or fallback in names:
        return fallback
    return "router_error"


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
