from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .config import AppConfig, OllamaConfig, PROJECT_ROOT
from .llm import OllamaOpenAIClient, OpenAICompatibleClient


PROFILE_PATH = PROJECT_ROOT / "data" / "llm_profiles.json"
AGENT_IDS = {"mcagent_rag", "crawler_agent"}


def _profile_id(value: str = "") -> str:
    value = str(value or "").strip()
    if value:
        clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
        if clean:
            return clean[:80]
    return "profile-" + uuid.uuid4().hex[:12]


def _normalize_base_url(value: str) -> str:
    text = str(value or "").strip()
    return text.rstrip("/") if text else ""


def _default_profiles(config: AppConfig) -> dict[str, Any]:
    profiles: list[dict[str, Any]] = [
        {
            "id": "ollama-default",
            "name": f"Ollama {config.ollama.model}",
            "provider": "ollama",
            "base_url": config.ollama.base_url,
            "model": config.ollama.model,
            "api_key": "",
            "timeout_seconds": config.ollama.timeout_seconds,
            "builtin": True,
        },
        {
            "id": "deepseek-template",
            "name": "DeepSeek deepseek-v4-pro",
            "provider": "openai-compatible",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-pro",
            "api_key": "",
            "timeout_seconds": 180,
            "builtin": True,
        },
    ]
    return {
        "profiles": profiles,
        "assignments": {
            "mcagent_rag": "ollama-default",
            "crawler_agent": "ollama-default",
        },
    }


def _read_store() -> dict[str, Any]:
    if not PROFILE_PATH.exists():
        return {}
    try:
        value = json.loads(PROFILE_PATH.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _merge_store(config: AppConfig) -> dict[str, Any]:
    default = _default_profiles(config)
    stored = _read_store()
    profiles_by_id: dict[str, dict[str, Any]] = {}
    for item in default.get("profiles", []):
        if isinstance(item, dict):
            profiles_by_id[str(item.get("id"))] = dict(item)
    for item in stored.get("profiles", []) if isinstance(stored.get("profiles"), list) else []:
        if not isinstance(item, dict):
            continue
        profile = _sanitize_profile(item, existing=None)
        profiles_by_id[profile["id"]] = profile
    assignments = dict(default.get("assignments") or {})
    if isinstance(stored.get("assignments"), dict):
        for agent, profile_id in stored["assignments"].items():
            if agent in AGENT_IDS and str(profile_id) in profiles_by_id:
                assignments[agent] = str(profile_id)
    return {"profiles": list(profiles_by_id.values()), "assignments": assignments}


def _sanitize_profile(raw: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    profile_id = _profile_id(str(raw.get("id") or ""))
    api_key = str(raw.get("api_key") or "")
    if not api_key and existing:
        api_key = str(existing.get("api_key") or "")
    model = str(raw.get("model") or "").strip()
    base_url = _normalize_base_url(str(raw.get("base_url") or ""))
    timeout = int(raw.get("timeout_seconds") or (existing or {}).get("timeout_seconds") or 180)
    timeout = max(15, min(timeout, 900))
    return {
        "id": profile_id,
        "name": str(raw.get("name") or model or profile_id).strip()[:120],
        "provider": str(raw.get("provider") or "openai-compatible").strip()[:60],
        "base_url": base_url,
        "model": model,
        "api_key": api_key.strip(),
        "timeout_seconds": timeout,
        "updated_at": time.time(),
    }


def public_profile(profile: dict[str, Any]) -> dict[str, Any]:
    item = {key: value for key, value in profile.items() if key != "api_key"}
    item["key_configured"] = bool(profile.get("api_key"))
    return item


def profiles_payload(config: AppConfig) -> dict[str, Any]:
    store = _merge_store(config)
    return {
        "profiles": [public_profile(item) for item in store["profiles"]],
        "assignments": store["assignments"],
    }


def save_profiles_payload(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    current = _merge_store(config)
    current_by_id = {str(item.get("id")): item for item in current.get("profiles", [])}
    profiles: list[dict[str, Any]] = []
    for raw in payload.get("profiles") if isinstance(payload.get("profiles"), list) else []:
        if not isinstance(raw, dict):
            continue
        profile = _sanitize_profile(raw, existing=current_by_id.get(str(raw.get("id") or "")))
        if not profile["base_url"] or not profile["model"]:
            continue
        profiles.append(profile)
    if not profiles:
        profiles = [item for item in current["profiles"] if not item.get("builtin")]
    profile_ids = {item["id"] for item in profiles}
    assignments: dict[str, str] = {}
    raw_assignments = payload.get("assignments") if isinstance(payload.get("assignments"), dict) else {}
    for agent in AGENT_IDS:
        value = str(raw_assignments.get(agent) or current.get("assignments", {}).get(agent) or "")
        if value in profile_ids:
            assignments[agent] = value
    fallback_id = profiles[0]["id"] if profiles else ""
    for agent in AGENT_IDS:
        assignments.setdefault(agent, fallback_id)
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(
        json.dumps({"profiles": profiles, "assignments": assignments}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return profiles_payload(config)


def profile_by_id(config: AppConfig, profile_id: str) -> dict[str, Any] | None:
    store = _merge_store(config)
    for profile in store["profiles"]:
        if str(profile.get("id")) == str(profile_id):
            return profile
    return None


def assigned_profile(config: AppConfig, agent: str) -> dict[str, Any]:
    store = _merge_store(config)
    profile_id = str(store.get("assignments", {}).get(agent) or "")
    profile = profile_by_id(config, profile_id)
    if profile:
        return profile
    return store["profiles"][0]


def resolve_profile_from_model(config: AppConfig, model: str, agent: str = "mcagent_rag") -> dict[str, Any] | None:
    value = str(model or "").strip()
    if value.startswith("profile:"):
        return profile_by_id(config, value.split(":", 1)[1])
    if value.startswith("llm-profile:"):
        return profile_by_id(config, value.split(":", 1)[1])
    if not value:
        return assigned_profile(config, agent)
    value_lower = value.lower()
    store = _merge_store(config)
    exact_matches = [
        profile
        for profile in store["profiles"]
        if value_lower
        in {
            str(profile.get("id") or "").strip().lower(),
            str(profile.get("model") or "").strip().lower(),
            str(profile.get("name") or "").strip().lower(),
        }
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]
    assigned = assigned_profile(config, agent)
    if exact_matches and assigned in exact_matches:
        return assigned
    return None


def client_from_profile(profile: dict[str, Any], *, temperature: float = 0.0, timeout_seconds: int | None = None) -> tuple[OpenAICompatibleClient, str]:
    endpoint_config = OllamaConfig(
        base_url=_normalize_base_url(str(profile.get("base_url") or "")),
        model=str(profile.get("model") or ""),
        temperature=temperature,
        timeout_seconds=int(timeout_seconds or profile.get("timeout_seconds") or 180),
    )
    label = str(profile.get("name") or profile.get("model") or profile.get("id") or "LLM")
    provider = str(profile.get("provider") or "").lower()
    if provider == "ollama" and not profile.get("api_key"):
        return OllamaOpenAIClient(endpoint_config), label
    return OpenAICompatibleClient(endpoint_config, api_key=str(profile.get("api_key") or ""), provider_label=label), label


def client_for_agent(config: AppConfig, agent: str, *, temperature: float = 0.0, timeout_seconds: int | None = None) -> tuple[OpenAICompatibleClient, str]:
    return client_from_profile(assigned_profile(config, agent), temperature=temperature, timeout_seconds=timeout_seconds)


def test_profile_connection(raw_profile: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    profile = _sanitize_profile(raw_profile, existing=existing)
    client, label = client_from_profile(profile, temperature=0.0, timeout_seconds=min(int(profile.get("timeout_seconds") or 60), 60))
    start = time.time()
    text = client.chat(
        [
            {"role": "system", "content": "Reply with OK only."},
            {"role": "user", "content": "ping"},
        ],
        temperature=0.0,
        max_tokens=32,
    )
    return {
        "ok": True,
        "label": label,
        "model": profile["model"],
        "elapsed_ms": round((time.time() - start) * 1000),
        "sample": text[:120],
    }
