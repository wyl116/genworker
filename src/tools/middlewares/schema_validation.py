"""
Schema validation middleware - validates tool input against JSON Schema.
"""
from typing import Any, Callable, Mapping

from src.common.logger import get_logger

from ..formatters import ToolResult
from ..mcp.tool import Tool
from ..pipeline import ToolCallContext

logger = get_logger()


class SchemaValidationMiddleware:
    """
    Validates tool_input against the tool's declared parameter schema.

    Checks:
    - All required params are present.
    - No unexpected params (if strict mode).
    - Basic type validation for declared params.
    """

    def __init__(
        self,
        tool_registry: Mapping[str, Tool] | None = None,
        strict: bool = False,
    ):
        self._tool_registry = tool_registry or {}
        self._strict = strict

    async def process(
        self, ctx: ToolCallContext, next_fn: Callable[[], Any]
    ) -> ToolResult:
        """Validate schema and delegate or reject."""
        tool = self._tool_registry.get(ctx.tool_name) if self._tool_registry else None

        # If tool not found in registry, skip validation and delegate
        if tool is None:
            return await next_fn()

        errors = _validate_params(tool, ctx.tool_input, self._strict)
        if errors:
            msg = (
                f"Schema validation failed for tool '{ctx.tool_name}': "
                + "; ".join(errors)
            )
            logger.warning(f"[SchemaValidation] {msg}")
            return ToolResult(content=msg, is_error=True)

        return await next_fn()


def _validate_params(
    tool: Tool, tool_input: dict[str, Any], strict: bool
) -> list[str]:
    """
    Validate tool_input against tool's declared parameters.

    Returns a list of error messages (empty if valid).
    """
    errors: list[str] = []
    params_schema = dict(tool.parameters)

    # Extract properties if nested
    properties = params_schema.get("properties", params_schema)

    # Check required params
    for param_name in tool.required_params:
        if param_name not in tool_input:
            errors.append(f"Missing required parameter: '{param_name}'")

    # Strict mode: reject unknown params
    if strict and properties:
        known_params = set(properties.keys())
        for key in tool_input:
            if key not in known_params:
                errors.append(f"Unknown parameter: '{key}'")

    # Basic type checking
    for param_name, param_value in tool_input.items():
        param_def = properties.get(param_name)
        if param_def is None or not isinstance(param_def, dict):
            continue

        expected_type = param_def.get("type")
        if expected_type and not _check_type(param_value, expected_type):
            errors.append(
                f"Parameter '{param_name}' expected type '{expected_type}', "
                f"got '{type(param_value).__name__}'"
            )

    return errors


def _check_type(value: Any, expected: str) -> bool:
    """Check if value matches the expected JSON Schema type."""
    type_map: dict[str, tuple[type, ...]] = {
        "string": (str,),
        "integer": (int,),
        "number": (int, float),
        "boolean": (bool,),
        "array": (list, tuple),
        "object": (dict,),
    }
    allowed = type_map.get(expected)
    if allowed is None:
        return True  # Unknown type, skip check
    return isinstance(value, allowed)
