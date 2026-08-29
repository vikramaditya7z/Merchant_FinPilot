"""The LLM Reasoning Agent for Merchant FinPilot.

PROJECT_RULES 1.2, 1.6, 2.7, 10.8 / ARCHITECTURE.md §8, §9.

Contract
--------
A bounded loop over read-only tools that ends in a structured AgentResponse
and a proposed AgentIntent.

Invariants:
- "LLMs reason. Deterministic systems verify."
- Tool surface is the sole security boundary.
- Zero direct database, SQL, or execution access.
- Every financial number comes from deterministic tools.
- Agent outputs a proposal; authorization and execution remain separate.
"""

from .agent import FinancialAgent
from .contracts import (
    AgentResponse,
    AgentStructuredFinding,
    LLMMessage,
    ToolCallRecord,
    ToolCallRequest,
)
from .parser import AgentParsingError, parse_agent_response
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_incident_prompt
from .provider import (
    GeminiProvider,
    LLMAuthenticationError,
    LLMInvalidResponseError,
    LLMProvider,
    LLMProviderError,
    LLMRateLimitError,
    MockLLMProvider,
)

__all__ = [
    "FinancialAgent",
    "AgentResponse",
    "AgentStructuredFinding",
    "LLMMessage",
    "ToolCallRecord",
    "ToolCallRequest",
    "AgentParsingError",
    "parse_agent_response",
    "PROMPT_VERSION",
    "SYSTEM_PROMPT",
    "build_incident_prompt",
    "LLMProvider",
    "GeminiProvider",
    "MockLLMProvider",
    "LLMProviderError",
    "LLMAuthenticationError",
    "LLMRateLimitError",
    "LLMInvalidResponseError",
]
