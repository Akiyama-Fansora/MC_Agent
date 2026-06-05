from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

from .config import OllamaConfig


class OpenAICompatibleClient:
    def __init__(self, config: Any, *, api_key: str = "", provider_label: str = "OpenAI-compatible") -> None:
        self.base_url = str(config.base_url)
        self.model = str(config.model)
        self.temperature = float(config.temperature)
        self.timeout_seconds = int(config.timeout_seconds)
        self.api_key = api_key
        self.provider_label = provider_label

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        return self._chat_once_or_retry(messages, temperature=temperature, max_tokens=max_tokens, response_format=response_format)

    def stream_chat(self, messages: list[dict[str, str]], temperature: float | None = None, max_tokens: int | None = None) -> Iterator[str]:
        for event in self.stream_events(messages, temperature=temperature, max_tokens=max_tokens):
            if event.get("type") == "content" and event.get("text"):
                yield str(event["text"])

    def stream_events(self, messages: list[dict[str, str]], temperature: float | None = None, max_tokens: int | None = None) -> Iterator[dict[str, str]]:
        endpoint = self.base_url.rstrip("/") + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "stream": True,
        }
        if max_tokens is not None and max_tokens > 0:
            payload["max_tokens"] = max_tokens
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_text = line[5:].strip()
                    if data_text == "[DONE]":
                        break
                    try:
                        data = json.loads(data_text)
                    except json.JSONDecodeError:
                        continue
                    choice = (data.get("choices") or [{}])[0] if isinstance(data, dict) else {}
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta") or {}
                    if isinstance(delta, dict):
                        content = delta.get("content")
                        if content:
                            yield {"type": "content", "text": str(content)}
                        reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                        if reasoning:
                            yield {"type": "reasoning", "text": ""}
                    message = choice.get("message") or {}
                    if isinstance(message, dict) and message.get("content"):
                        yield {"type": "content", "text": str(message["content"])}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                f"{self.provider_label} endpoint returned HTTP {exc.code} at {endpoint}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Failed to reach {self.provider_label} endpoint at {endpoint}. "
                "Check the service, base URL, model name, and network availability."
            ) from exc

    def _chat_once_or_retry(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        data = self._chat_raw(messages, temperature=temperature, max_tokens=max_tokens, response_format=response_format)
        content = self._content_from_response(data)
        choice = (data.get("choices") or [{}])[0] if isinstance(data, dict) else {}
        finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
        if content or finish_reason != "length":
            return content
        retry_tokens = max((max_tokens or 0) * 3, 3000)
        retry_tokens = min(retry_tokens, 8000)
        retry_data = self._chat_raw(messages, temperature=temperature, max_tokens=retry_tokens, response_format=response_format)
        return self._content_from_response(retry_data)

    def _chat_raw(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        endpoint = self.base_url.rstrip("/") + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "stream": False,
        }
        if max_tokens is not None and max_tokens > 0:
            payload["max_tokens"] = max_tokens
        if response_format:
            payload["response_format"] = response_format
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                f"{self.provider_label} endpoint returned HTTP {exc.code} at {endpoint}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Failed to reach {self.provider_label} endpoint at {endpoint}. "
                "Check the service, base URL, model name, and network availability."
            ) from exc

        data = json.loads(raw)
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected {self.provider_label} response: {raw[:500]}")
        return data

    def _content_from_response(self, data: dict[str, Any]) -> str:
        try:
            return str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected {self.provider_label} response: {json.dumps(data, ensure_ascii=False)[:500]}") from exc


class OllamaOpenAIClient(OpenAICompatibleClient):
    def __init__(self, config: OllamaConfig) -> None:
        super().__init__(config, provider_label="Ollama")
