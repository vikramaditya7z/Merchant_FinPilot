"""Tool Registry and schema definitions for the LLM Agent boundary.

PROJECT_RULES 1.6, 10.8 / ARCHITECTURE.md §9.

Provides:
- Strict parameter schema validation before tool dispatch.
- Standard JSON Schema definitions formatted for LLM function/tool calling.
- Centralized dispatch returning standardized ToolResult containers.
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ..db.database import Database
from .contracts import ToolErrorCode, ToolResult
from .incident_tools import (
    check_action_eligibility,
    get_baseline_comparison,
    get_failure_breakdown,
    get_incident_summary,
    get_revenue_exposure,
    get_time_series,
)


@dataclass(frozen=True)
class ToolDefinition:
    """Definition and schema of a registered agent tool."""
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[..., ToolResult]


class ToolRegistry:
    """Registry managing available agent tools and their schemas."""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        """Register a tool definition."""
        self._tools[tool.name] = tool

    def get_tool(self, name: str) -> Optional[ToolDefinition]:
        """Get a tool definition by name."""
        return self._tools.get(name)

    def list_tools(self) -> List[str]:
        """List names of all registered tools."""
        return sorted(self._tools.keys())

    def get_schemas(self) -> List[Dict[str, Any]]:
        """Return function calling schemas for all registered tools."""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in self._tools.values()
        ]

    def execute(
        self, db: Database, name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> ToolResult:
        """Safely validate arguments and dispatch to the registered tool.

        Args:
            db: The persistent Database repository.
            name: The registered tool name.
            arguments: Dictionary of arguments passed by the agent.

        Returns:
            A typed ToolResult container.
        """
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.error(
                ToolErrorCode.NOT_FOUND, f"Unknown tool '{name}'"
            )

        args = arguments or {}
        try:
            return tool.handler(db, **args)
        except TypeError as e:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGUMENT, f"Invalid arguments for tool '{name}': {e}"
            )
        except Exception as e:
            return ToolResult.error(
                ToolErrorCode.UNAVAILABLE, f"Internal error executing tool '{name}': {e}"
            )

    def bind(self, db: Any) -> "BoundToolRegistry":
        """Return a bound tool registry executing against a specific database."""
        return BoundToolRegistry(self, db)


class BoundToolRegistry:
    """Tool registry bound to a specific database context."""

    def __init__(self, registry: ToolRegistry, db: Any) -> None:
        self._registry = registry
        self._db = db

    def get_schemas(self) -> List[Dict[str, Any]]:
        return self._registry.get_schemas()

    def list_tools(self) -> List[str]:
        return self._registry.list_tools()

    def get_tool(self, name: str) -> Optional[ToolDefinition]:
        return self._registry.get_tool(name)

    def execute(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> ToolResult:
        return self._registry.execute(self._db, name, arguments)


def create_default_registry() -> ToolRegistry:
    """Create and return a ToolRegistry populated with all default read-only tools."""
    registry = ToolRegistry()

    # 1. get_incident_summary
    registry.register(
        ToolDefinition(
            name="get_incident_summary",
            description=(
                "Retrieve structured information, lifecycle status, severity, time window, "
                "overall traffic counts, failure rate, and revenue risk for a known FinancialIncident."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "incident_id": {
                        "type": "string",
                        "description": "The unique incident identifier (e.g. 'inc_...').",
                    }
                },
                "required": ["incident_id"],
                "additionalProperties": False,
            },
            handler=get_incident_summary,
        )
    )

    # 2. get_failure_breakdown
    registry.register(
        ToolDefinition(
            name="get_failure_breakdown",
            description=(
                "Retrieve deterministic dimensional failure breakdown for an incident along a specified dimension "
                "(payment_method, region, provider, failure_code, failure_category, or hour_of_day)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "incident_id": {
                        "type": "string",
                        "description": "The unique incident identifier.",
                    },
                    "dimension": {
                        "type": "string",
                        "enum": [
                            "payment_method",
                            "region",
                            "provider",
                            "failure_code",
                            "failure_category",
                            "hour_of_day",
                        ],
                        "description": "The dimension along which to partition traffic and failures.",
                    },
                },
                "required": ["incident_id", "dimension"],
                "additionalProperties": False,
            },
            handler=get_failure_breakdown,
        )
    )

    # 3. get_time_series
    registry.register(
        ToolDefinition(
            name="get_time_series",
            description=(
                "Retrieve time-series bucket metrics (traffic, failures, failure rate, failed GMV) "
                "across the incident window at a configurable granularity in minutes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "incident_id": {
                        "type": "string",
                        "description": "The unique incident identifier.",
                    },
                    "granularity_minutes": {
                        "type": "integer",
                        "default": 15,
                        "minimum": 5,
                        "maximum": 60,
                        "description": "Duration of each time bucket in minutes (5 to 60).",
                    },
                },
                "required": ["incident_id"],
                "additionalProperties": False,
            },
            handler=get_time_series,
        )
    )

    # 4. get_baseline_comparison
    registry.register(
        ToolDefinition(
            name="get_baseline_comparison",
            description=(
                "Retrieve deterministic historical baseline comparisons, absolute percentage point deviations, "
                "relative lift ratios, and statistical significance for the overall incident or a specific dimension slice."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "incident_id": {
                        "type": "string",
                        "description": "The unique incident identifier.",
                    },
                    "dimension": {
                        "type": "string",
                        "enum": [
                            "payment_method",
                            "region",
                            "provider",
                            "failure_code",
                            "failure_category",
                            "hour_of_day",
                        ],
                        "description": "Optional dimension to inspect a specific slice.",
                    },
                    "dimension_value": {
                        "type": "string",
                        "description": "Optional value within the dimension to compare against its slice baseline.",
                    },
                },
                "required": ["incident_id"],
                "additionalProperties": False,
            },
            handler=get_baseline_comparison,
        )
    )

    # 5. get_revenue_exposure
    registry.register(
        ToolDefinition(
            name="get_revenue_exposure",
            description=(
                "Retrieve exact deterministic financial exposure: failed GMV in integer paise, "
                "excess failed transactions, mean ticket size, revenue at risk, and recoverability assessment."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "incident_id": {
                        "type": "string",
                        "description": "The unique incident identifier.",
                    }
                },
                "required": ["incident_id"],
                "additionalProperties": False,
            },
            handler=get_revenue_exposure,
        )
    )

    # 6. check_action_eligibility
    registry.register(
        ToolDefinition(
            name="check_action_eligibility",
            description=(
                "Deterministically pre-check whether a hypothetical remediation action (ROUTE_UPDATE, CIRCUIT_BREAKER, "
                "RETRY_ROUTING, MERCHANT_NOTIFICATION) is eligible for consideration on an incident without executing it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "incident_id": {
                        "type": "string",
                        "description": "The unique incident identifier.",
                    },
                    "action_type": {
                        "type": "string",
                        "enum": [
                            "ROUTE_UPDATE",
                            "CIRCUIT_BREAKER",
                            "RETRY_ROUTING",
                            "MERCHANT_NOTIFICATION",
                        ],
                        "description": "The type of remediation action being queried.",
                    },
                    "target_dimension": {
                        "type": "string",
                        "enum": ["payment_method", "provider", "region"],
                        "description": "Optional target dimension for routing / circuit breaking.",
                    },
                    "target_value": {
                        "type": "string",
                        "description": "Optional target slice value (e.g. 'acquirer_b', 'upi').",
                    },
                },
                "required": ["incident_id", "action_type"],
                "additionalProperties": False,
            },
            handler=check_action_eligibility,
        )
    )

    return registry
