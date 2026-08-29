"""LLM Provider abstraction and Gemini API implementation.

PROJECT_RULES 1.6, 10.9 / ARCHITECTURE.md §8.

Provides:
- Abstract LLMProvider interface.
- Pure-Python GeminiProvider using standard library urllib.request (zero SDK dependency).
- MockLLMProvider for deterministic offline unit and scenario testing.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Sequence

from .contracts import LLMMessage, ToolCallRequest


class LLMProviderError(Exception):
    """Base error for LLM provider failures."""


class LLMAuthenticationError(LLMProviderError):
    """Missing or invalid API credentials."""


class LLMRateLimitError(LLMProviderError):
    """Rate limit or quota exceeded."""


class LLMInvalidResponseError(LLMProviderError):
    """Malformed or unparseable response from LLM."""


class LLMProvider(ABC):
    """Abstract interface for LLM interaction."""

    @property
    @abstractmethod
    def model_id(self) -> str:
        """Identifier of the model."""
        pass

    @abstractmethod
    def generate_turn(
        self,
        messages: Sequence[LLMMessage],
        tool_schemas: Sequence[Dict[str, Any]],
        temperature: float = 0.0,
    ) -> LLMMessage:
        """Generate a single conversation turn from the LLM.

        Args:
            messages: History of messages in the conversation.
            tool_schemas: List of JSON Schema definitions for available tools.
            temperature: Sampling temperature (0.0 for deterministic reasoning).

        Returns:
            An LLMMessage containing either tool_calls or text content.
        """
        pass


def clean_gemini_schema(schema: Any) -> Any:
    """Recursively transform a JSON Schema into a Gemini REST API-compliant Schema.

    Google's Schema protobuf rejects OpenAPI/JSON Schema keywords like 'additionalProperties',
    'minimum', 'maximum', and 'default'. This function strips unsupported fields while
    preserving core types, properties, enum constraints, and required fields.
    """
    if not isinstance(schema, dict):
        return schema

    ALLOWED_KEYS = {
        "type",
        "format",
        "description",
        "nullable",
        "enum",
        "properties",
        "required",
        "items",
    }

    cleaned: Dict[str, Any] = {}
    for key, val in schema.items():
        if key not in ALLOWED_KEYS:
            continue
        if key == "properties" and isinstance(val, dict):
            cleaned["properties"] = {
                prop_name: clean_gemini_schema(prop_val)
                for prop_name, prop_val in val.items()
            }
        elif key == "items" and isinstance(val, dict):
            cleaned["items"] = clean_gemini_schema(val)
        else:
            cleaned[key] = val

    return cleaned


class GeminiProvider(LLMProvider):
    """Gemini REST API provider using pure Python standard library."""

    DEFAULT_MODEL = "gemini-2.5-flash"
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,
        timeout_seconds: int = 30,
    ) -> None:
        if api_key is not None:
            self._api_key = api_key if api_key.strip() else None
        else:
            env_key = os.environ.get("GEMINI_API_KEY")
            self._api_key = env_key.strip() if env_key and env_key.strip() else None
        self._model_name = model_name or os.environ.get("GEMINI_MODEL", self.DEFAULT_MODEL)
        self._timeout_seconds = timeout_seconds

    @property
    def model_id(self) -> str:
        return self._model_name

    def generate_turn(
        self,
        messages: Sequence[LLMMessage],
        tool_schemas: Sequence[Dict[str, Any]],
        temperature: float = 0.0,
    ) -> LLMMessage:
        if not self._api_key:
            raise LLMAuthenticationError(
                "Gemini API key not found. Set GEMINI_API_KEY environment variable or pass api_key."
            )

        endpoint = f"{self.BASE_URL}/{self._model_name}:generateContent"

        # Format request payload
        contents: List[Dict[str, Any]] = []
        system_instruction: Optional[Dict[str, Any]] = None

        for msg in messages:
            if msg.role == "system":
                system_instruction = {"parts": [{"text": msg.content or ""}]}
            elif msg.role == "user":
                contents.append({"role": "user", "parts": [{"text": msg.content or ""}]})
            elif msg.role in ("assistant", "model"):
                if msg.raw_parts:
                    contents.append({"role": "model", "parts": list(msg.raw_parts)})
                else:
                    parts: List[Dict[str, Any]] = []
                    if msg.content:
                        parts.append({"text": msg.content})
                    for tc in msg.tool_calls:
                        parts.append({
                            "functionCall": {
                                "name": tc.tool_name,
                                "args": tc.arguments,
                            }
                        })
                    contents.append({"role": "model", "parts": parts or [{"text": ""}]})
            elif msg.role == "tool":
                # Gemini functionResponse format
                call_name = msg.tool_name or "unknown_function"
                response_data = json.loads(msg.content) if msg.content else {}
                contents.append({
                    "role": "user",
                    "parts": [{
                        "functionResponse": {
                            "name": call_name,
                            "response": {"output": response_data},
                        }
                    }]
                })

        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
            },
        }

        if system_instruction:
            payload["systemInstruction"] = system_instruction

        if tool_schemas:
            func_declarations = []
            for s in tool_schemas:
                decl = {
                    "name": s["name"],
                    "description": s.get("description", ""),
                    "parameters": clean_gemini_schema(s.get("parameters", {})),
                }
                func_declarations.append(decl)
            payload["tools"] = [{"functionDeclarations": func_declarations}]

        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=data_bytes,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self._api_key,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout_seconds) as response:
                resp_bytes = response.read()
                resp_json = json.loads(resp_bytes.decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw_err = e.read().decode("utf-8", errors="replace")
            err_body = self._sanitize(raw_err)
            if e.code == 429:
                raise LLMRateLimitError(f"Gemini API rate limit exceeded: {err_body}") from e
            elif e.code in (401, 403):
                raise LLMAuthenticationError(f"Gemini API authentication failed: {err_body}") from e
            raise LLMProviderError(f"Gemini API HTTP error {e.code}: {err_body}") from e
        except urllib.error.URLError as e:
            raise LLMProviderError(f"Gemini API network connection error: {self._sanitize(str(e.reason))}") from e
        except Exception as e:
            raise LLMProviderError(f"Unexpected error communicating with Gemini API: {self._sanitize(str(e))}") from e

        return self._parse_gemini_response(resp_json)

    def _sanitize(self, text: str) -> str:
        """Sanitize text to guarantee API key is never exposed."""
        if self._api_key and self._api_key in text:
            return text.replace(self._api_key, "[REDACTED_API_KEY]")
        return text

    def _parse_gemini_response(self, resp_json: Dict[str, Any]) -> LLMMessage:
        candidates = resp_json.get("candidates", [])
        if not candidates:
            raise LLMInvalidResponseError(f"Gemini response has no candidates: {resp_json}")

        first_candidate = candidates[0]
        content_obj = first_candidate.get("content", {})
        parts = content_obj.get("parts", [])

        text_content_list: List[str] = []
        tool_calls: List[ToolCallRequest] = []

        for idx, p in enumerate(parts):
            if "text" in p:
                text_content_list.append(p["text"])
            elif "functionCall" in p:
                fc = p["functionCall"]
                tool_calls.append(
                    ToolCallRequest(
                        call_id=f"call_{idx}_{fc.get('name')}",
                        tool_name=fc.get("name", ""),
                        arguments=fc.get("args", {}),
                    )
                )

        combined_text = "\n".join(text_content_list).strip() if text_content_list else None
        return LLMMessage(
            role="model",
            content=combined_text,
            tool_calls=tuple(tool_calls),
            raw_parts=tuple(parts) if parts else None,
        )


class MockLLMProvider(LLMProvider):
    """Deterministic, scriptable mock LLM provider for unit and scenario testing."""

    def __init__(
        self,
        model_id: str = "mock-gemini-2.5-flash",
        scripted_turns: Optional[Sequence[LLMMessage]] = None,
        handler: Optional[Callable[[Sequence[LLMMessage], Sequence[Dict[str, Any]]], LLMMessage]] = None,
    ) -> None:
        self._model_id = model_id
        self._scripted_turns = list(scripted_turns) if scripted_turns else []
        self._turn_index = 0
        self._handler = handler
        self.recorded_turns: List[Sequence[LLMMessage]] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    def add_turn(self, turn: LLMMessage) -> None:
        """Add a scripted response turn to the sequence."""
        self._scripted_turns.append(turn)

    def generate_turn(
        self,
        messages: Sequence[LLMMessage],
        tool_schemas: Sequence[Dict[str, Any]],
        temperature: float = 0.0,
    ) -> LLMMessage:
        self.recorded_turns.append(tuple(messages))

        if self._handler is not None:
            return self._handler(messages, tool_schemas)

        if self._turn_index < len(self._scripted_turns):
            turn = self._scripted_turns[self._turn_index]
            self._turn_index += 1
            return turn

        # Default fallback if turns exhausted: return empty final message
        return LLMMessage(
            role="model",
            content=json.dumps({
                "reasoning": "Completed investigation based on verified tool findings.",
                "verified_facts": ["Investigation completed."],
                "findings": [],
                "uncertainty_or_limitations": [],
                "proposed_intent": {
                    "action": "no_action",
                    "reason": "No additional remediation action required based on available evidence.",
                    "evidence_refs": [],
                }
            })
        )
