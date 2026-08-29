"""Typed contracts for the LLM Agent reasoning layer.

PROJECT_RULES 1.6, 4.2, 10.8 / ARCHITECTURE.md §8, §9.

Defines:
- LLM interaction messages and tool call representations.
- Structured AgentResponse container.
- Structured findings and audit summaries.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..domain.intent import AgentIntent


@dataclass(frozen=True)
class ToolCallRequest:
    """A tool call requested by the LLM."""
    call_id: str
    tool_name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMMessage:
    """A turn in the conversation with the LLM."""
    role: str  # "system", "user", "assistant" / "model", "tool"
    content: Optional[str] = None
    tool_calls: Tuple[ToolCallRequest, ...] = ()
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    raw_parts: Optional[Tuple[Dict[str, Any], ...]] = None


@dataclass(frozen=True)
class ToolCallRecord:
    """Auditable record of a tool execution within the agent reasoning loop."""
    call_id: str
    tool_name: str
    arguments: Dict[str, Any]
    success: bool
    result_digest: str
    raw_result: Dict[str, Any]


@dataclass(frozen=True)
class AgentStructuredFinding:
    """A single structured finding derived by the agent citing verified evidence."""
    title: str
    dimension: Optional[str]
    observed_value: Optional[str]
    evidence_ref: Optional[str]
    summary: str


@dataclass(frozen=True)
class AgentResponse:
    """The final structured response produced by the Agent reasoning loop.

    Guarantees:
    - Clear distinction between reasoning, verified facts, findings, and uncertainty.
    - Attached AgentIntent is a proposal only (not authorization or execution).
    - Contains complete audit trail of tool calls used.
    """
    incident_id: str
    reasoning: str
    verified_facts: Tuple[str, ...]
    findings: Tuple[AgentStructuredFinding, ...]
    uncertainty_or_limitations: Tuple[str, ...]
    tool_calls_used: Tuple[ToolCallRecord, ...]
    proposed_intent: Optional[AgentIntent]
    model_id: str
    prompt_version: str
    iterations_count: int
    raw_model_response: Optional[str] = None
