from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.crawler_capabilities import (  # noqa: E402
    capability_catalog_prompt,
    default_sources_for_context,
    is_domain_source,
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
    assert_true("no_mc_default", all(not is_domain_source(source, "minecraft") for source in general_sources))
    mc_sources = default_sources_for_context("Collect Utopian Journey Minecraft modpack data and .mrpack archive.")
    assert_true("mc_archive", "modpack_download" in mc_sources and "mcmod" in mc_sources)


def test_aliases_and_preflight_contracts_are_objective() -> None:
    assert_equal("alias", normalize_source("mc百科"), "mcmod")
    fetch_preflight = task_preflight({"source": "fetch_url", "query": "Python requests docs"})
    assert_equal("fetch_invalid", fetch_preflight["valid"], False)
    assert_true("fetch_url_required", "url_required" in fetch_preflight["issues"])
    save_preflight = task_preflight({"source": "save_artifact", "query": "save notes"})
    assert_equal("save_invalid", save_preflight["valid"], False)
    assert_true("save_content_required", any(str(issue).startswith("requires_any:") for issue in save_preflight["issues"]))
    domain_preflight = task_preflight({"source": "mcmod", "query": "Python requests"}, context_text="Collect Python requests docs")
    assert_equal("domain_invalid", domain_preflight["valid"], False)
    assert_true("domain_mismatch", "domain_mismatch:minecraft" in domain_preflight["issues"])
    valid_fetch = task_preflight({"source": "fetch_url", "query": "https://docs.python-requests.org/"})
    assert_equal("valid_fetch", valid_fetch["valid"], True)


if __name__ == "__main__":
    test_registry_exposes_profiles_groups_and_domain_plugins()
    test_default_sources_are_general_until_minecraft_context_exists()
    test_aliases_and_preflight_contracts_are_objective()
    print("crawler_capabilities_scenarios passed")
