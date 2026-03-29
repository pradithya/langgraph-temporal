# langgraph-temporal

<p align="center">
  <img src="https://raw.githubusercontent.com/pradithya/langgraph-temporal/main/docs/logo.png" alt="langgraph-temporal" width="400">
</p>

**Temporal integration for LangGraph — durable execution for stateful AI agents.**

Run your existing LangGraph `StateGraph` as a [Temporal](https://temporal.io/) Workflow with automatic recovery from failures, distributed node execution, and zero-resource human-in-the-loop.

!!! warning "Experimental"
    This project is experimental. Use at your own risk.

---

## Why langgraph-temporal?

LangGraph is great for building stateful AI agents, but its default execution model has limitations:

- **No durability** — if the process crashes, all progress is lost
- **No distributed execution** — nodes run in a single process
- **HITL blocks a process** — waiting for human approval ties up compute resources

langgraph-temporal solves these by executing your graph as a Temporal Workflow:

| Feature | Vanilla LangGraph | With langgraph-temporal |
|---|---|---|
| Process crash recovery | Lost progress | Automatic resume |
| Node execution | Single process | Distributed workers |
| Human-in-the-loop | Blocks a process | Zero-resource wait |
| Audit trail | None | Full event history |
| Observability | Manual | Temporal UI + metrics |

## Minimal example

```python
import asyncio
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from temporalio.client import Client as TemporalClient
from temporalio.worker import UnsandboxedWorkflowRunner

from langgraph.temporal import TemporalGraph


class State(TypedDict):
    message: str


def greet(state: State) -> dict:
    return {"message": f"Hello, {state['message']}!"}


async def main():
    # 1. Build a normal LangGraph
    builder = StateGraph(State)
    builder.add_node("greet", greet)
    builder.add_edge(START, "greet")
    builder.add_edge("greet", END)
    graph = builder.compile()

    # 2. Connect to Temporal and wrap the graph
    client = await TemporalClient.connect("localhost:7233")
    tg = TemporalGraph(graph, client, task_queue="my-queue")

    # 3. Start a Worker and invoke
    worker = tg.create_worker(workflow_runner=UnsandboxedWorkflowRunner())
    async with worker:
        result = await tg.ainvoke(
            {"message": "world"},
            config={"configurable": {"thread_id": "greeting-1"}},
        )

    print(result)  # {"message": "Hello, world!"}


asyncio.run(main())
```

## Next steps

- [Installation](getting-started/installation.md) — install the package
- [Quick Start](getting-started/quickstart.md) — run your first Temporal-backed graph
- [Core Concepts](guides/concepts.md) — understand how it works
- [API Reference](reference/graph.md) — full API documentation
