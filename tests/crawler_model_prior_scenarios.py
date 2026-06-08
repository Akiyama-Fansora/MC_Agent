from __future__ import annotations

from pathlib import Path
import sys
import json


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.crawler_model_prior_service import CrawlerModelPriorService  # noqa: E402
from mcagent.crawler_llm_planner import plan_crawler_tasks_rule_fallback, _sanitize_plan  # noqa: E402
from mcagent.crawler_self_audit_service import CrawlerSelfAuditService  # noqa: E402
from mcagent.job_view_service import JobReadableViewService  # noqa: E402


def assert_true(name: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def test_rule_prior_is_hypothesis_only_and_suggests_verification_leads() -> None:
    prior = CrawlerModelPriorService().rule_prior(
        question="补充农夫乐事 Farmer's Delight 的新手入门、教程、核心玩法资料，交给 MCagent/RAG。",
        target_hint="农夫乐事 Farmer's Delight",
        context_text="Minecraft mod guide beginner progression tutorial",
        session_summary={"delivery_target": "MCagent/RAG"},
        learned_memory={},
    )
    joined = "\n".join(prior["aliases"] + prior["likely_source_graph"] + prior["search_leads"]).lower()
    assert_equal("evidence_status", prior["evidence_status"], "hypothesis_only")
    assert_equal("allowed_use", prior["allowed_use"], "planning_only")
    assert_true("has_farmer_alias", "farmer's delight" in joined)
    assert_true("has_source_graph", "wiki" in joined and ("modrinth" in joined or "curseforge" in joined))
    assert_true("has_verification_boundary", "Do not cite" in prior["forbidden_use"])
    assert_true("no_session_key_alias", "delivery_target" not in [alias.lower() for alias in prior["aliases"]])


def test_prior_leads_spread_aliases_before_source_variants() -> None:
    prior = CrawlerModelPriorService().rule_prior(
        question="问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他",
        target_hint="乌托邦整合包",
        context_text="Minecraft modpack MCagent/RAG gap fill",
        session_summary={"delivery_target": "MCagent/RAG", "requested_by": "user"},
        learned_memory={},
    )
    leads = prior["search_leads"]
    assert_true("cn_alias_early", any("乌托邦探险之旅" in lead for lead in leads[:5]), leads)
    assert_true("en_alias_early", any("Utopian Journey" in lead for lead in leads[:6]), leads)
    assert_true("no_json_key_alias", "delivery_target" not in [alias.lower() for alias in prior["aliases"]], prior["aliases"])


def test_fallback_plan_uses_prior_leads_without_accepting_prior_as_evidence() -> None:
    question = "让 CrawlerAgent 补充农夫乐事 Farmer's Delight 的新手入门、教程和 progression 资料，完成后给 MCagent/RAG 用。"
    plan = plan_crawler_tasks_rule_fallback(
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=12,
        planner_error="unit timeout",
        session_summary={"delivery_target": "MCagent/RAG", "collection_target": "农夫乐事 Farmer's Delight 新手入门"},
    )
    prior = plan.get("model_prior")
    assert_true("prior_present", isinstance(prior, dict), prior)
    assert_equal("prior_status", prior.get("evidence_status"), "hypothesis_only")
    task_queries = "\n".join(str(task.get("query") or "") for task in plan["tasks"]).lower()
    assert_true("prior_leads_in_tasks", "farmer's delight" in task_queries and ("guide" in task_queries or "wiki" in task_queries), task_queries)
    audit = CrawlerSelfAuditService().build([], {"plan": plan})
    assert_equal("prior_not_accepted", audit["counts"]["accepted"], 0)
    assert_equal("prior_not_pending", audit["counts"]["pending_review"], 0)


def test_sanitize_plan_preserves_llm_prior_and_readable_exposes_it_separately() -> None:
    raw_prior = {
        "target": "Playwright Python",
        "aliases": ["Playwright", "Playwright Python"],
        "likely_source_graph": ["official docs", "GitHub repository"],
        "search_leads": ["Playwright Python docs", "Playwright GitHub releases"],
        "verification_questions": ["Which official page verifies the API?"],
    }
    plan = _sanitize_plan(
        {
            "topic": "Playwright Python",
            "delivery_target": "human",
            "model_prior": raw_prior,
            "tasks": [{"source": "web_discovery", "query": "Playwright Python docs", "reason": "verify prior", "priority": 100}],
        },
        "Collect Playwright Python docs and releases for a general RAG corpus.",
        ROOT / "data" / "crawler_exports",
        max_tasks=6,
        session_summary={"delivery_target": "human", "collection_target": "Playwright Python"},
    )
    readable = JobReadableViewService(source_label=lambda value: value).build(
        {
            "id": "job-test",
            "title": "prior test",
            "status": "running",
            "result": {"plan": plan, "planned_tasks": plan["tasks"], "tasks": []},
        }
    )
    assert_equal("prior_status", plan["model_prior"]["evidence_status"], "hypothesis_only")
    assert_true("readable_prior", bool(readable["model_prior"]["search_leads"]), readable["model_prior"])
    assert_true("self_audit_separate", "accepted_sources" in readable["self_audit"] and readable["self_audit"]["counts"]["accepted"] == 0)


def test_llm_prior_merges_rule_source_specific_leads() -> None:
    class FakeClient:
        def chat(self, *_args, **_kwargs):  # noqa: ANN001
            return json.dumps(
                {
                    "target": "Farmer's Delight",
                    "aliases": ["Farmer's Delight"],
                    "likely_source_graph": ["official/project page", "documentation/wiki"],
                    "search_leads": ["Farmer's Delight wiki"],
                    "verification_questions": ["Which page verifies gameplay?"],
                }
            )

    prior = CrawlerModelPriorService(client=FakeClient(), model_label="fake").build(
        question="Collect Farmer's Delight beginner guide, cooking mechanics, versions, and download pages for MCagent/RAG.",
        target_hint="Farmer's Delight",
        context_text="Minecraft mod guide beginner tutorial cooking recipe Modrinth CurseForge",
        session_summary={"delivery_target": "MCagent/RAG"},
        learned_memory={},
    )
    joined = "\n".join(prior["source_specific_leads"] + prior["candidate_urls"]).lower()
    assert_true("fallback_source_specific_merged", "modrinth: farmers-delight" in joined, joined)
    assert_true("guide_candidate_url", "minecraft-guides.com/wiki/farmers-delight" in joined, joined)
    assert_equal("prior_boundary", prior["evidence_status"], "hypothesis_only")


def test_sanitize_plan_drops_placeholder_queries_and_uses_prior_leads() -> None:
    raw_prior = {
        "target": "Farmer's Delight",
        "aliases": ["Farmer's Delight", "FarmersDelight"],
        "likely_source_graph": ["Modrinth project", "wiki/tutorial pages"],
        "search_leads": ["Farmer's Delight beginner guide", "Farmer's Delight wiki", "FarmersDelight Modrinth"],
    }
    plan = _sanitize_plan(
        {
            "topic": "Farmer's Delight",
            "delivery_target": "MCagent/RAG",
            "model_prior": raw_prior,
            "sources": ["mcagent_context", "mcmod", "web_discovery", "playwright"],
            "tasks": [
                {"source": "mcmod", "query": "short query", "reason": "placeholder", "priority": 200},
                {"source": "web_discovery", "query": "query", "reason": "placeholder", "priority": 199},
            ],
        },
        "Ask CrawlerAgent to collect Farmer's Delight beginner guide and progression docs for MCagent/RAG.",
        ROOT / "data" / "crawler_exports",
        max_tasks=4,
        session_summary={"delivery_target": "MCagent/RAG", "collection_target": "Farmer's Delight", "requested_by": "user_via_mcagent"},
    )
    tasks = plan["tasks"]
    task_text = "\n".join(f"{task.get('source')} | {task.get('query')}" for task in tasks).lower()
    assert_true("placeholder_removed", "short query" not in task_text and "\nquery" not in task_text, task_text)
    assert_true("prior_task_survives_low_budget", "farmer's delight beginner guide" in task_text or "farmer's delight wiki" in task_text, task_text)
    assert_true("prior_task_marked", any(task.get("from_model_prior") for task in tasks), tasks)
    assert_equal("prior_boundary", plan["model_prior"]["allowed_use"], "planning_only")


if __name__ == "__main__":
    test_rule_prior_is_hypothesis_only_and_suggests_verification_leads()
    test_prior_leads_spread_aliases_before_source_variants()
    test_fallback_plan_uses_prior_leads_without_accepting_prior_as_evidence()
    test_sanitize_plan_preserves_llm_prior_and_readable_exposes_it_separately()
    test_llm_prior_merges_rule_source_specific_leads()
    test_sanitize_plan_drops_placeholder_queries_and_uses_prior_leads()
    print("crawler_model_prior_scenarios passed")
