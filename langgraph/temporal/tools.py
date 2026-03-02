"""Helper for converting Temporal Activities into LangGraph tools.

Provides `activity_as_tool()` which wraps a Temporal Activity function
as a LangGraph-compatible tool, enabling nodes to invoke Activities
directly with Temporal's durable execution guarantees.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def activity_as_tool(
    activity_fn: Callable[..., Any],
    *,
    name: str | None = None,
    description: str | None = None,
    task_queue: str | None = None,
) -> Callable[..., Any]:
    """Convert a Temporal Activity function into a LangGraph-compatible tool.

    This wraps an `@activity.defn` decorated function so it can be used as
    a tool within LangGraph nodes. When the tool is called inside a node
    Activity, it executes the target Activity via the Temporal worker,
    providing durable execution guarantees for tool invocations.

    Note: The returned tool is a regular callable that invokes the Activity
    function directly (in-process). For cross-worker Activity invocation,
    use Temporal's `workflow.execute_activity()` from within a Workflow.

    Args:
        activity_fn: A function decorated with `@activity.defn`.
        name: Override the tool name (defaults to function name).
        description: Override the tool description (defaults to docstring).
        task_queue: Task queue for Activity execution (for future use).

    Returns:
        A callable that can be used as a LangGraph tool.

    Example:
        ```python
        @activity.defn
        async def search_web(query: str) -> str:
            \"\"\"Search the web for information.\"\"\"
            ...

        tools = [activity_as_tool(search_web)]
        graph.add_node("tools", ToolNode(tools))
        ```
    """
    tool_name: str = name or getattr(activity_fn, "__name__", None) or "activity_tool"
    tool_description: str = description or getattr(activity_fn, "__doc__", None) or ""

    async def tool_wrapper(*args: Any, **kwargs: Any) -> Any:
        return await activity_fn(*args, **kwargs)

    tool_wrapper.__name__ = tool_name
    tool_wrapper.__doc__ = tool_description
    tool_wrapper.__qualname__ = tool_name

    # Preserve original function metadata for introspection
    tool_wrapper._activity_fn = activity_fn  # type: ignore[attr-defined]
    tool_wrapper._task_queue = task_queue  # type: ignore[attr-defined]

    return tool_wrapper
