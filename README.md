# Langgraph Temporal

<p align="center">
  <img src="docs/logo.png" alt="Logo">
</p>


Temporal integration for LangGraph — durable execution for stateful AI agents.

Run your existing LangGraph `StateGraph` as a [Temporal](https://temporal.io/) Workflow with automatic recovery from failures, distributed node execution, and zero-resource human-in-the-loop.

## Installation

```bash
pip install langgraph-temporal
```

**Requirements:** Python >= 3.10, `langgraph >= 1.0.0`, `temporalio >= 1.7.0`

## Quick start

## 1. Run a local Temporal server

A `docker-compose.yml` is provided to run Temporal locally with PostgreSQL and Elasticsearch.

```bash
cp .env.ci .env
make start_temporal
```

## 2. Create langgraph workflow

Take any compiled LangGraph graph, wrap it in `TemporalGraph`, start a Worker, and invoke:

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
    from time import sleep
    sleep(10) # simulate work
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

    # 3. Start a Worker (runs Activities that execute your nodes)
    worker = tg.create_worker(workflow_runner=UnsandboxedWorkflowRunner())

    async with worker:
        result = await tg.ainvoke(
            {"message": "world"},
            config={"configurable": {"thread_id": "greeting-1"}},
        )

    print(result)  # {"message": "Hello, world!"}


asyncio.run(main())
```

> **Note:** LangGraph imports modules restricted by Temporal's sandbox, so
> `UnsandboxedWorkflowRunner()` is required when creating the Worker.

## 3. Open Temporal UI

The Temporal Web UI is available at `http://localhost:8233`

![Demo](docs/demo.gif)


## License

MIT
