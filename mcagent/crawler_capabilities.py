from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any


GENERAL_TOOL_GROUPS = {
    "group:discovery": ["web_discovery", "playwright", "topic_discovery"],
    "group:fetch": ["fetch_url"],
    "group:browser": ["playwright", "browser_collect"],
    "group:artifact": ["save_artifact"],
    "group:local": ["read_local_file", "search_local_files"],
    "group:inter_agent": ["mcagent_context"],
}

DOMAIN_TOOL_GROUPS = {
    "domain:minecraft": ["mcmod", "modrinth", "followup", "mediawiki", "ftbwiki", "createwiki", "modpack_download", "modpack_internal"],
}

PROFILE_TOOL_GROUPS = {
    "minimal": ["group:discovery", "group:fetch"],
    "research": ["group:discovery", "group:fetch", "group:browser", "group:artifact", "group:local"],
    "handoff": ["group:inter_agent", "group:discovery", "group:fetch", "group:browser", "group:artifact"],
    "full": ["group:inter_agent", "group:discovery", "group:fetch", "group:browser", "group:artifact", "group:local", "domain:minecraft"],
}


@dataclass(frozen=True, slots=True)
class CrawlerCapability:
    name: str
    group: str
    profile: str
    purpose: str
    side_effects: str
    domain: str = "general"
    requires_query: bool = True
    required_any: tuple[str, ...] = ()
    requires_url: bool = False
    objective_contract: str = "Tool returns objective observations; CrawlerAgent decides relevance, acceptance, retry, deletion, or ingest."
    escalation: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    default_priority: int = 50
    default_limits: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["required_any"] = list(self.required_any)
        value["escalation"] = list(self.escalation)
        value["aliases"] = list(self.aliases)
        return value

    def to_prompt_line(self) -> str:
        req = f" requires_any={','.join(self.required_any)}" if self.required_any else ""
        url = " requires_url" if self.requires_url else ""
        aliases = f" aliases={','.join(self.aliases)}" if self.aliases else ""
        escalation = f" escalation={'>'.join(self.escalation)}" if self.escalation else ""
        return (
            f"- {self.name} [{self.group}; profile={self.profile}; domain={self.domain}]: "
            f"{self.purpose} side_effects={self.side_effects}{req}{url}{aliases}{escalation}; "
            f"{self.objective_contract}"
        )


CAPABILITIES: dict[str, CrawlerCapability] = {
    "mcagent_context": CrawlerCapability(
        name="mcagent_context",
        group="group:inter_agent",
        profile="handoff",
        purpose="Ask MCagent to inspect local MCagent/RAG evidence and gaps, then return an inter-agent transcript artifact.",
        side_effects="read_local_index_and_write_artifact",
        domain="minecraft",
        aliases=("local_context", "rag_gap_check"),
        default_priority=150,
    ),
    "web_discovery": CrawlerCapability(
        name="web_discovery",
        group="group:discovery",
        profile="research",
        purpose="Discover public candidate pages with search, save snippets/pages/manifests for CrawlerAgent review.",
        side_effects="network_and_filesystem",
        escalation=("fetch_url", "playwright"),
        aliases=("search", "public_search", "web"),
        default_priority=62,
        default_limits={"search_limit": 8, "max_urls": 8},
    ),
    "playwright": CrawlerCapability(
        name="playwright",
        group="group:browser",
        profile="research",
        purpose="Render/search pages with a local browser when HTTP/search is empty, JS-rendered, blocked, or loses page structure.",
        side_effects="browser_network_filesystem",
        escalation=("fetch_url", "browser_collect"),
        aliases=("browser", "rendered_extract"),
        default_priority=82,
        default_limits={"search_limit": 6, "max_urls": 4},
    ),
    "fetch_url": CrawlerCapability(
        name="fetch_url",
        group="group:fetch",
        profile="minimal",
        purpose="Fetch one exact public URL with local HTTP and save readable text/raw HTML/manifest.",
        side_effects="network_and_filesystem",
        requires_url=True,
        escalation=("playwright",),
        aliases=("reader", "http_fetch", "url_fetch"),
        default_priority=125,
    ),
    "browser_collect": CrawlerCapability(
        name="browser_collect",
        group="group:browser",
        profile="research",
        purpose="Collect structured rows/fields/tables in browser and save XLSX/CSV/JSON/report/raw HTML/screenshot.",
        side_effects="browser_network_filesystem",
        aliases=("browser_structured", "structured_browser", "product_collect"),
        default_priority=120,
        default_limits={"max_items": 50},
    ),
    "save_artifact": CrawlerCapability(
        name="save_artifact",
        group="group:artifact",
        profile="research",
        purpose="Persist content already held by the agent or referenced from earlier objective tool output.",
        side_effects="filesystem",
        required_any=("content", "content_ref", "artifact_ref"),
        aliases=("save", "artifact"),
        default_priority=110,
    ),
    "read_local_file": CrawlerCapability(
        name="read_local_file",
        group="group:local",
        profile="research",
        purpose="Read one local text file and save a crawler artifact for later review or summarization.",
        side_effects="read_filesystem_and_write_artifact",
        required_any=("path", "file"),
        aliases=("read_file", "local_read"),
        default_priority=108,
    ),
    "search_local_files": CrawlerCapability(
        name="search_local_files",
        group="group:local",
        profile="research",
        purpose="Search a local directory/file for terms and save matching snippets.",
        side_effects="read_filesystem_and_write_artifact",
        required_any=("path", "root"),
        aliases=("grep_local", "local_search"),
        default_priority=106,
        default_limits={"max_files": 25},
    ),
    "topic_discovery": CrawlerCapability(
        name="topic_discovery",
        group="group:discovery",
        profile="research",
        purpose="Mine existing local crawler documents for candidate follow-up topics; CrawlerAgent must review ACCEPT/REJECT before use.",
        side_effects="read_filesystem_and_write_artifact",
        domain="minecraft",
        default_priority=130,
        default_limits={"max_files": 120, "max_queries": 40},
    ),
    "mcmod": CrawlerCapability(
        name="mcmod",
        group="domain:minecraft",
        profile="full",
        purpose="Minecraft-domain MC百科 search/scrape for Chinese mod, modpack, guide, and page evidence.",
        side_effects="network_and_filesystem",
        domain="minecraft",
        aliases=("mc百科",),
        default_priority=100,
        default_limits={"search_limit": 10, "max_urls": 8},
    ),
    "modrinth": CrawlerCapability(
        name="modrinth",
        group="domain:minecraft",
        profile="full",
        purpose="Minecraft-domain Modrinth project metadata and project content discovery.",
        side_effects="network_and_filesystem",
        domain="minecraft",
        default_priority=88,
    ),
    "followup": CrawlerCapability(
        name="followup",
        group="domain:minecraft",
        profile="full",
        purpose="Follow public Source/Wiki/README links from existing Minecraft project metadata.",
        side_effects="network_and_filesystem",
        domain="minecraft",
        default_priority=76,
        default_limits={"max_urls": 16},
    ),
    "mediawiki": CrawlerCapability(
        name="mediawiki",
        group="domain:minecraft",
        profile="full",
        purpose="Minecraft-domain vanilla Minecraft Wiki source.",
        side_effects="network_and_filesystem",
        domain="minecraft",
        default_priority=50,
        default_limits={"search_limit": 8},
    ),
    "ftbwiki": CrawlerCapability(
        name="ftbwiki",
        group="domain:minecraft",
        profile="full",
        purpose="Minecraft-domain FTB Wiki source for large mod mechanics, bosses, and guides.",
        side_effects="network_and_filesystem",
        domain="minecraft",
        default_priority=80,
        default_limits={"search_limit": 8},
    ),
    "createwiki": CrawlerCapability(
        name="createwiki",
        group="domain:minecraft",
        profile="full",
        purpose="Minecraft-domain Create Wiki source for Create mechanics and automation.",
        side_effects="network_and_filesystem",
        domain="minecraft",
        default_priority=80,
        default_limits={"search_limit": 8},
    ),
    "modpack_download": CrawlerCapability(
        name="modpack_download",
        group="domain:minecraft",
        profile="full",
        purpose="Minecraft-domain public .mrpack/.zip archive discovery/download with objective HTTP/download/blocker facts.",
        side_effects="network_filesystem",
        domain="minecraft",
        aliases=("pack_download", "archive_download"),
        default_priority=130,
        default_limits={"search_limit": 8},
    ),
    "modpack_internal": CrawlerCapability(
        name="modpack_internal",
        group="domain:minecraft",
        profile="full",
        purpose="Minecraft-domain parser for a real local modpack archive/manifest; extracts manifest, modlist, quests, KubeJS, recipes, configs, and text.",
        side_effects="filesystem",
        domain="minecraft",
        required_any=("zip", "archive", "archive_path", "manifest_path", "path"),
        aliases=("pack_internal", "modpack_archive"),
        default_priority=145,
    ),
}


ALIASES = {alias: name for name, capability in CAPABILITIES.items() for alias in capability.aliases}
ALIASES.update({name: name for name in CAPABILITIES})


def normalize_source(value: str) -> str:
    source = str(value or "").strip()
    return ALIASES.get(source, source)


def allowed_sources() -> set[str]:
    return set(CAPABILITIES)


def source_defaults() -> dict[str, dict[str, Any]]:
    defaults: dict[str, dict[str, Any]] = {}
    for name, capability in CAPABILITIES.items():
        item = {"priority": capability.default_priority}
        item.update(capability.default_limits)
        defaults[name] = item
    return defaults


def tool_groups_payload() -> dict[str, list[str]]:
    payload = {}
    payload.update(GENERAL_TOOL_GROUPS)
    payload.update(DOMAIN_TOOL_GROUPS)
    return {key: list(value) for key, value in payload.items()}


def profiles_payload() -> dict[str, list[str]]:
    return {key: list(value) for key, value in PROFILE_TOOL_GROUPS.items()}


def capabilities_payload() -> list[dict[str, Any]]:
    return [capability.to_dict() for capability in CAPABILITIES.values()]


def capability_catalog_prompt() -> str:
    lines = [
        "Crawler capability registry:",
        "Profiles:",
        *[f"- {name}: {', '.join(groups)}" for name, groups in PROFILE_TOOL_GROUPS.items()],
        "Tool groups:",
        *[f"- {name}: {', '.join(tools)}" for name, tools in tool_groups_payload().items()],
        "Tools:",
        *[capability.to_prompt_line() for capability in CAPABILITIES.values()],
    ]
    return "\n".join(lines)


def looks_like_minecraft_context(text: str) -> bool:
    value = str(text or "")
    if not value.strip():
        return False
    negative_only = re.search(
        r"(?:不是|非|不要|不用|别用|禁止|避免|not|non[-\s]?|do\s+not|don't|avoid).{0,24}"
        r"(?:minecraft|mc\s*专用|mc\b|mc百科|modrinth|curseforge|modpack|整合包|模组)",
        value,
        flags=re.I,
    ) or re.search(
        r"(?:minecraft|mc\s*专用|mc\b|mc百科|modrinth|curseforge|modpack|整合包|模组).{0,24}"
        r"(?:不是|非|不要|不用|别用|禁止|避免|not|non[-\s]?|do\s+not|don't|avoid)",
        value,
        flags=re.I,
    )
    if negative_only:
        positive_text = re.sub(
            r"(?:不是|非|不要|不用|别用|禁止|避免|not|non[-\s]?|do\s+not|don't|avoid).{0,40}"
            r"(?:minecraft|mc\s*专用|mc\b|mc百科|modrinth|curseforge|modpack|整合包|模组).{0,40}",
            " ",
            value,
            flags=re.I,
        )
        positive_text = re.sub(
            r"(?:minecraft|mc\s*专用|mc\b|mc百科|modrinth|curseforge|modpack|整合包|模组).{0,40}"
            r"(?:不是|非|不要|不用|别用|禁止|避免|not|non[-\s]?|do\s+not|don't|avoid).{0,40}",
            " ",
            positive_text,
            flags=re.I,
        )
        if not re.search(
            r"\b(?:minecraft|mcmod|modrinth|curseforge|modpack|mod list|kubejs|ftb quests|packwiz|mrpack)\b"
            r"|MC百科|整合包|模组|光影|资源包|我的世界",
            positive_text,
            flags=re.I,
        ):
            return False
    return bool(
        re.search(
            r"\b(?:minecraft|mcmod|modrinth|curseforge|modpack|mod list|kubejs|ftb quests|packwiz|mrpack)\b"
            r"|MC百科|整合包|模组|光影|资源包|我的世界",
            value,
            flags=re.I,
        )
    )


def default_sources_for_context(text: str, *, prefer_general_web: bool = False, archive_negated: bool = False) -> list[str]:
    if not looks_like_minecraft_context(text):
        return ["web_discovery", "playwright", "fetch_url", "browser_collect", "save_artifact", "read_local_file", "search_local_files"]
    if prefer_general_web:
        sources = ["web_discovery", "playwright", "fetch_url", "followup", "mcmod", "modrinth"]
    else:
        sources = ["web_discovery", "playwright", "fetch_url", "followup", "mcmod", "modrinth"]
    if archive_negated:
        sources = [source for source in sources if source != "modpack_download"]
    return sources


def is_domain_source(source: str, domain: str) -> bool:
    capability = CAPABILITIES.get(normalize_source(source))
    return bool(capability and capability.domain == domain)


def task_preflight(task: dict[str, Any], *, context_text: str = "", check_domain: bool = True) -> dict[str, Any]:
    source = normalize_source(str(task.get("source") or ""))
    capability = CAPABILITIES.get(source)
    if not capability:
        return {
            "valid": False,
            "source": source,
            "issues": ["unknown_source"],
            "message": f"Unknown crawler source: {source}",
            "objective_contract": "No tool should run until CrawlerAgent chooses a registered source.",
        }
    issues: list[str] = []
    query_value = str(task.get("query") or "").strip()
    if capability.requires_query and not str(task.get("query") or "").strip():
        issues.append("query_required")
    if capability.requires_url and not re.search(r"https?://", query_value, flags=re.I):
        issues.append("url_required")
        if re.fullmatch(r"(?:use_|latest_|previous_|artifact_)?(?:artifact_)?url(?:_\d+)?|use_artifact_url|candidate_url|discovered_url", query_value, flags=re.I):
            issues.append("placeholder_url_query")
    if capability.required_any and not any(str(task.get(key) or "").strip() for key in capability.required_any):
        issues.append(f"requires_any:{'|'.join(capability.required_any)}")
    if check_domain and capability.domain == "minecraft":
        domain_text = "\n".join(
            [
                context_text,
                str(task.get("query") or ""),
                str(task.get("reason") or ""),
            ]
        )
        if not looks_like_minecraft_context(domain_text):
            issues.append("domain_mismatch:minecraft")
    return {
        "valid": not issues,
        "source": source,
        "issues": issues,
        "message": "Executable crawler task." if not issues else "Crawler task is missing objective tool contract requirements.",
        "capability": capability.to_dict(),
        "objective_contract": capability.objective_contract,
    }
