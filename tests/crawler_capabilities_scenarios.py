from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.crawler_capabilities import (  # noqa: E402
    capability_catalog_prompt,
    default_sources_for_context,
    is_domain_source,
    looks_like_minecraft_context,
    normalize_source,
    task_preflight,
)


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def test_registry_exposes_profiles_groups_and_domain_plugins() -> None:
    prompt = capability_catalog_prompt()
    assert_true("profiles", "Profiles:" in prompt and "research" in prompt and "full" in prompt)
    assert_true("general_groups", "group:discovery" in prompt and "group:browser" in prompt and "group:artifact" in prompt)
    assert_true("domain_group", "domain:minecraft" in prompt)
    assert_true("tool_contract", "CrawlerAgent decides relevance" in prompt)


def test_default_sources_are_general_until_minecraft_context_exists() -> None:
    general_sources = default_sources_for_context("Collect Python requests official docs and GitHub releases.")
    assert_true("general_web", "web_discovery" in general_sources and "playwright" in general_sources)
    assert_true("general_topic_discovery", "topic_discovery" in general_sources)
    assert_true("no_mc_default", all(not is_domain_source(source, "minecraft") for source in general_sources))
    mc_sources = default_sources_for_context("Collect Utopian Journey Minecraft modpack data.")
    assert_true("mc_domain", "mcmod" in mc_sources and "modrinth" in mc_sources)
    assert_true("archive_requires_planner_intent", "modpack_download" not in mc_sources)


def test_mixed_chinese_minecraft_terms_are_domain_context() -> None:
    assert_equal("mcwiki_chinese_suffix", looks_like_minecraft_context("\u83b7\u53d6MC\u767e\u79d1\u4e0a\u7684\u4e2d\u6587\u9879\u76ee\u4ecb\u7ecd"), True)
    assert_equal("mod_chinese", looks_like_minecraft_context("\u6a21\u7ec4 \u519c\u592b\u4e50\u4e8b"), True)
    mcmod_preflight = task_preflight(
        {
            "source": "mcmod",
            "query": "\u519c\u592b\u4e50\u4e8b",
            "reason": "\u83b7\u53d6MC\u767e\u79d1\u4e0a\u7684\u4e2d\u6587\u9879\u76ee\u4ecb\u7ecd\u3001\u7248\u672c\u548c\u4e0b\u8f7d\u4fe1\u606f",
        },
        context_text="Collect Farmer's Delight public web evidence for MCagent/RAG.",
    )
    assert_equal("mcmod_preflight", mcmod_preflight["valid"], True)
    mcmod_tool_context = task_preflight(
        {
            "source": "mcmod",
            "query": "\u519c\u592b\u4e50\u4e8b(Farmer's Delight) \u73a9\u6cd5 \u6559\u7a0b",
            "reason": "CrawlerAgent selected the registered MC百科 source for this candidate.",
        },
        context_text="Collect Farmer's Delight public evidence for MCagent/RAG.",
    )
    assert_equal("mcmod_tool_context_valid", mcmod_tool_context["valid"], True)


def test_aliases_and_preflight_contracts_are_objective() -> None:
    assert_equal("alias", normalize_source("mc百科"), "mcmod")
    fetch_preflight = task_preflight({"source": "fetch_url", "query": "Python requests docs"})
    assert_equal("fetch_invalid", fetch_preflight["valid"], False)
    assert_true("fetch_url_required", "url_required" in fetch_preflight["issues"])
    placeholder_preflight = task_preflight({"source": "fetch_url", "query": "use_artifact_url"})
    assert_equal("placeholder_invalid", placeholder_preflight["valid"], False)
    assert_true("placeholder_url", "placeholder_url_query" in placeholder_preflight["issues"])
    save_preflight = task_preflight({"source": "save_artifact", "query": "save notes"})
    assert_equal("save_invalid", save_preflight["valid"], False)
    assert_true("save_content_required", any(str(issue).startswith("requires_any:") for issue in save_preflight["issues"]))
    local_output_only_preflight = task_preflight({"source": "search_local_files", "query": "Farmer's Delight", "output_dir": r"D:\tmp\out"})
    assert_equal("local_output_only_invalid", local_output_only_preflight["valid"], False)
    assert_true("local_path_required", any(str(issue) == "requires_any:path|root" for issue in local_output_only_preflight["issues"]))
    local_path_preflight = task_preflight({"source": "search_local_files", "query": "Farmer's Delight", "path": r"D:\magic\MC_Agent\docs"})
    assert_equal("local_path_valid", local_path_preflight["valid"], True)
    domain_preflight = task_preflight({"source": "mcmod", "query": "Python requests"}, context_text="Collect Python requests docs")
    assert_equal("domain_invalid", domain_preflight["valid"], False)
    assert_true("domain_mismatch", "domain_mismatch:minecraft" in domain_preflight["issues"])
    valid_fetch = task_preflight({"source": "fetch_url", "query": "https://docs.python-requests.org/"})
    assert_equal("valid_fetch", valid_fetch["valid"], True)
    general_context = task_preflight({"source": "mcagent_context", "query": "Python requests local coverage gaps"}, context_text="Collect Python requests docs")
    assert_equal("mcagent_context_general", general_context["valid"], True)
    topic_discovery = task_preflight({"source": "topic_discovery", "query": "Python requests release docs"}, context_text="Collect Python requests docs")
    assert_equal("topic_discovery_general", topic_discovery["valid"], True)


if __name__ == "__main__":
    test_registry_exposes_profiles_groups_and_domain_plugins()
    test_default_sources_are_general_until_minecraft_context_exists()
    test_mixed_chinese_minecraft_terms_are_domain_context()
    test_aliases_and_preflight_contracts_are_objective()
    print("crawler_capabilities_scenarios passed")
