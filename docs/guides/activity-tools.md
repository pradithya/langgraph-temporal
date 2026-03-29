# Activity Tools

`activity_as_tool` wraps a Temporal Activity function as a LangGraph-compatible tool, enabling nodes to call Activities with Temporal's durable execution guarantees.

## Usage

```python
from temporalio import activity
from langgraph.temporal import activity_as_tool


@activity.defn
async def search_web(query: str) -> str:
    """Search the web for information."""
    # ... your search implementation
    return f"Results for: {query}"


# Wrap as a LangGraph tool
tools = [activity_as_tool(search_web)]

# Use in your graph
graph.add_node("tools", ToolNode(tools))
```

## Customizing name and description

```python
tool = activity_as_tool(
    search_web,
    name="web_search",
    description="Search the internet for up-to-date information.",
)
```

## How it works

The wrapper preserves the Activity function's signature and metadata while making it callable as a standard LangGraph tool. When the tool is invoked inside a node Activity, it calls the Activity function directly (in-process).

!!! note
    `activity_as_tool` runs the Activity in-process within the current Activity execution. For cross-worker Activity invocation, use `workflow.execute_activity()` from within a Workflow.
