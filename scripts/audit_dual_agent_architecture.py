from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def contains(path: Path, *needles: str) -> bool:
    text = read_text(path)
    return all(needle in text for needle in needles)


def audit(root: Path = ROOT) -> dict[str, Any]:
    graphs = root / "mcagent" / "graphs"
    files = {
        "conversation_graph": graphs / "runtime.py",
        "mcagent_graph": graphs / "mcagent.py",
        "crawler_graph": graphs / "crawler.py",
        "legacy_adapter": graphs / "legacy_adapter.py",
        "route_result_contract": graphs / "route_result_contract.py",
        "agent_message": root / "mcagent" / "agent_message.py",
        "agent_runtime": root / "mcagent" / "agent_runtime.py",
        "crawler_capabilities": root / "mcagent" / "crawler_capabilities.py",
        "web_server": root / "mcagent" / "web_server.py",
    }
    checks = [
        {
            "id": "agent_message_triplet",
            "status": "pass"
            if contains(files["agent_message"], "class AgentMessage", "from_agent", "content", "to_agent")
            else "fail",
            "evidence": str(files["agent_message"].relative_to(root)),
        },
        {
            "id": "conversation_routes_by_to_agent",
            "status": "pass"
            if contains(files["conversation_graph"], "message.to_agent_id", "run_mcagent_graph", "run_crawler_graph")
            else "fail",
            "evidence": str(files["conversation_graph"].relative_to(root)),
        },
        {
            "id": "mcagent_subgraph_exists",
            "status": "pass"
            if contains(files["mcagent_graph"], "build_mcagent_graph", 'payload["agent"] = "mcagent_rag"', "blocked_capability_groups")
            else "fail",
            "evidence": str(files["mcagent_graph"].relative_to(root)),
        },
        {
            "id": "crawler_subgraph_exists",
            "status": "pass"
            if contains(files["crawler_graph"], "build_crawler_graph", 'payload["agent"] = "crawler_agent"', "candidate_domain_toolsets")
            else "fail",
            "evidence": str(files["crawler_graph"].relative_to(root)),
        },
        {
            "id": "mcagent_blocks_public_web_tools",
            "status": "pass"
            if contains(files["mcagent_graph"], "web_search", "browser", "download", "public_web_ingest")
            else "fail",
            "evidence": str(files["mcagent_graph"].relative_to(root)),
        },
        {
            "id": "crawler_general_and_domain_tools_split",
            "status": "pass"
            if contains(files["agent_runtime"], "CRAWLER_GENERAL_COLLECTION_TOOL_NAMES", "CRAWLER_DOMAIN_COLLECTION_TOOL_NAMES")
            and contains(files["crawler_capabilities"], "GENERAL_TOOL_GROUPS", "DOMAIN_TOOL_GROUPS")
            else "fail",
            "evidence": f"{files['agent_runtime'].relative_to(root)}; {files['crawler_capabilities'].relative_to(root)}",
        },
        {
            "id": "explicit_legacy_runtime_adapter",
            "status": "pass"
            if contains(files["legacy_adapter"], "deliver_via_legacy_runtime", "legacy_web_server_runtime", "does not choose tools")
            and contains(files["mcagent_graph"], "legacy_adapter", "deliver_via_legacy_runtime")
            and contains(files["crawler_graph"], "legacy_adapter", "deliver_via_legacy_runtime")
            and contains(files["conversation_graph"], "mcagent_graph.legacy_adapter", "crawler_graph.legacy_adapter")
            else "fail",
            "evidence": f"{files['legacy_adapter'].relative_to(root)}; {files['conversation_graph'].relative_to(root)}",
        },
        {
            "id": "explicit_runtime_request_contracts",
            "status": "pass"
            if contains(files["mcagent_graph"], "prepare_runtime_request", "mcagent_local_runtime_request", "MCagent LLM")
            and contains(files["crawler_graph"], "prepare_runtime_request", "crawler_collection_runtime_request", "CrawlerAgent LLM")
            and contains(files["legacy_adapter"], "runtime_request", "runtime_request_id", "contract_kind")
            else "fail",
            "evidence": f"{files['mcagent_graph'].relative_to(root)}; {files['crawler_graph'].relative_to(root)}; {files['legacy_adapter'].relative_to(root)}",
        },
        {
            "id": "explicit_route_input_contracts",
            "status": "pass"
            if contains(files["mcagent_graph"], "prepare_route_input_contract", "mcagent_route_input_contract", "It does not select a tool")
            and contains(files["crawler_graph"], "prepare_route_input_contract", "crawler_route_input_contract", "It does not select a source")
            and contains(files["legacy_adapter"], "route_input_contract_id")
            else "fail",
            "evidence": f"{files['mcagent_graph'].relative_to(root)}; {files['crawler_graph'].relative_to(root)}; {files['legacy_adapter'].relative_to(root)}",
        },
        {
            "id": "explicit_message_preflight_contracts",
            "status": "pass"
            if contains(files["mcagent_graph"], "prepare_message_preflight_contract", "context_only_agent_message", "It does not reply")
            and contains(files["crawler_graph"], "prepare_message_preflight_contract", "collection_request_agent_message", "start a background job")
            and contains(files["legacy_adapter"], "message_preflight_contract_id")
            else "fail",
            "evidence": f"{files['mcagent_graph'].relative_to(root)}; {files['crawler_graph'].relative_to(root)}; {files['legacy_adapter'].relative_to(root)}",
        },
        {
            "id": "explicit_contextual_question_contracts",
            "status": "pass"
            if contains(files["mcagent_graph"], "prepare_contextual_question_contract", "mcagent_contextual_question_contract", "rewrite_executed")
            and contains(files["legacy_adapter"], "contextual_question_contract_id")
            else "fail",
            "evidence": f"{files['mcagent_graph'].relative_to(root)}; {files['legacy_adapter'].relative_to(root)}",
        },
        {
            "id": "explicit_source_planning_contracts",
            "status": "pass"
            if contains(files["crawler_graph"], "prepare_source_planning_contract", "crawler_source_planning_input_contract", "It does not choose sources")
            and contains(files["legacy_adapter"], "source_planning_contract_id")
            and contains(files["route_result_contract"], "source_planning_contract_id")
            else "fail",
            "evidence": f"{files['crawler_graph'].relative_to(root)}; {files['legacy_adapter'].relative_to(root)}; {files['route_result_contract'].relative_to(root)}",
        },
        {
            "id": "explicit_route_result_contracts",
            "status": "pass"
            if contains(files["mcagent_graph"], "prepare_route_result_contract", "mcagent_route_result_contract")
            and contains(files["crawler_graph"], "prepare_route_result_contract", "crawler_route_result_contract")
            and contains(files["route_result_contract"], "result_shape", "It does not select tools", "change the response")
            else "fail",
            "evidence": f"{files['mcagent_graph'].relative_to(root)}; {files['crawler_graph'].relative_to(root)}; {files['route_result_contract'].relative_to(root)}",
        },
        {
            "id": "legacy_runtime_coupling_visible",
            "status": "warn"
            if contains(files["legacy_adapter"], "legacy_web_server_runtime")
            and contains(files["web_server"], "def _chat_impl")
            else "pass",
            "evidence": "Graph subnodes now delegate through an explicit legacy adapter; core execution still reaches web_server._chat_impl during migration.",
        },
    ]
    counts = {status: sum(1 for item in checks if item["status"] == status) for status in ("pass", "warn", "fail")}
    return {
        "subject": "dual_agent_architecture",
        "summary": {
            "is_two_agent_shaped": counts["fail"] == 0,
            "pass": counts["pass"],
            "warn": counts["warn"],
            "fail": counts["fail"],
        },
        "checks": checks,
        "notes": [
            "This audit reports objective structure only.",
            "It does not decide whether a live LLM turn should call a tool or whether evidence is sufficient.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit objective dual-agent architecture facts.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()
    report = audit(ROOT)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        summary = report["summary"]
        print(
            f"Dual-agent shaped: {summary['is_two_agent_shaped']} "
            f"(pass={summary['pass']} warn={summary['warn']} fail={summary['fail']})"
        )
        for item in report["checks"]:
            print(f"[{item['status']}] {item['id']}: {item['evidence']}")
    return 1 if report["summary"]["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
