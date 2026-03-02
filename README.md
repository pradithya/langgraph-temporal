# langgraph-temporal

Temporal integration for LangGraph — durable execution for stateful AI agents.

Run your existing LangGraph `StateGraph` as a [Temporal](https://temporal.io/) Workflow with automatic recovery from failures, distributed node execution, and zero-resource human-in-the-loop.

## Installation

```bash
pip install langgraph-temporal
```

**Requirements:** Python >= 3.10, `langgraph >= 1.0.0`, `temporalio >= 1.7.0`

## Quick start

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

## Examples

### Conditional edges

Route execution to different nodes based on state:

```python
from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class RouterState(TypedDict):
    topic: str
    response: str


def classify(state: RouterState) -> dict:
    return {"topic": state["topic"]}


def handle_tech(state: RouterState) -> dict:
    return {"response": f"Tech support for: {state['topic']}"}


def handle_general(state: RouterState) -> dict:
    return {"response": f"General help for: {state['topic']}"}


def route(state: RouterState) -> str:
    return "tech" if "bug" in state["topic"] else "general"


builder = StateGraph(RouterState)
builder.add_node("classify", classify)
builder.add_node("tech", handle_tech)
builder.add_node("general", handle_general)
builder.add_edge(START, "classify")
builder.add_conditional_edges("classify", route, {"tech": "tech", "general": "general"})
builder.add_edge("tech", END)
builder.add_edge("general", END)
graph = builder.compile()

# Run on Temporal
tg = TemporalGraph(graph, client, task_queue="my-queue")
worker = tg.create_worker(workflow_runner=UnsandboxedWorkflowRunner())

async with worker:
    result = await tg.ainvoke(
        {"topic": "bug in login", "response": ""},
        config={"configurable": {"thread_id": "route-1"}},
    )
    print(result["response"])  # "Tech support for: bug in login"
```

### Loop with state accumulation

Loops work naturally — the Temporal Workflow repeats steps until the conditional edge exits:

```python
import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph


class LoopState(TypedDict):
    count: int
    values: Annotated[list[str], operator.add]


def step(state: LoopState) -> dict:
    count = state["count"]
    return {"count": count + 1, "values": [f"step-{count + 1}"]}


def should_continue(state: LoopState) -> str:
    return "step" if state["count"] < 5 else "__end__"


builder = StateGraph(LoopState)
builder.add_node("step", step)
builder.add_edge(START, "step")
builder.add_conditional_edges("step", should_continue, {"step": "step", "__end__": END})
graph = builder.compile()

tg = TemporalGraph(graph, client, task_queue="my-queue")
worker = tg.create_worker(workflow_runner=UnsandboxedWorkflowRunner())

async with worker:
    result = await tg.ainvoke(
        {"count": 0, "values": []},
        config={"configurable": {"thread_id": "loop-1"}},
    )
    print(result["count"])   # 5
    print(result["values"])  # ["step-1", "step-2", "step-3", "step-4", "step-5"]
```

### Human-in-the-loop (interrupt and resume)

Use `interrupt()` inside a node to pause the workflow. The Temporal Workflow blocks on a Signal with zero resource consumption — no process held open, no polling. Resume later by sending a value:

```python
import asyncio
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt


class ReviewState(TypedDict):
    draft: str
    final: str


def write_draft(state: ReviewState) -> dict:
    return {"draft": "Here is the draft proposal."}


def human_review(state: ReviewState) -> dict:
    feedback = interrupt("Please review the draft and provide feedback.")
    return {"final": f"Revised with feedback: {feedback}"}


builder = StateGraph(ReviewState)
builder.add_node("write", write_draft)
builder.add_node("review", human_review)
builder.add_edge(START, "write")
builder.add_edge("write", "review")
builder.add_edge("review", END)
graph = builder.compile()

tg = TemporalGraph(graph, client, task_queue="my-queue")
config = {"configurable": {"thread_id": "review-1"}}
worker = tg.create_worker(workflow_runner=UnsandboxedWorkflowRunner())

async with worker:
    # Start the workflow (will pause at interrupt)
    handle = await tg.astart({"draft": "", "final": ""}, config)

    # Poll until interrupted
    while True:
        state = await tg.get_state(config)
        if state["status"] == "interrupted":
            break
        await asyncio.sleep(0.5)

    print(state["interrupts"])  # Shows the interrupt payload

    # Resume with human feedback
    await tg.resume(config, "Looks good, just fix the typo on page 2.")

    # Wait for completion
    result = await handle.result()
    print(result.channel_values["final"])
```

### Scatter-gather (fan-out / fan-in)

Use `Command(goto=[Send(...)])` to dynamically fan out work to multiple parallel nodes, then gather results:

```python
import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send


class GatherState(TypedDict):
    topics: list[str]
    results: Annotated[list[str], operator.add]
    summary: str


class ResearchInput(TypedDict):
    topic: str


def scatter(state: GatherState) -> Command:
    sends = [Send("research", {"topic": t}) for t in state["topics"]]
    return Command(goto=sends)


def research(state: ResearchInput) -> dict:
    return {"results": [f"findings on {state['topic']}"]}


def gather(state: GatherState) -> dict:
    return {"summary": " | ".join(sorted(state["results"]))}


builder = StateGraph(GatherState)
builder.add_node("scatter", scatter)
builder.add_node("research", research)
builder.add_node("gather", gather)
builder.add_edge(START, "scatter")
builder.add_edge("research", "gather")
builder.add_edge("gather", END)
graph = builder.compile()

tg = TemporalGraph(graph, client, task_queue="my-queue")
worker = tg.create_worker(workflow_runner=UnsandboxedWorkflowRunner())

async with worker:
    result = await tg.ainvoke(
        {"topics": ["AI", "databases", "security"], "results": [], "summary": ""},
        config={"configurable": {"thread_id": "scatter-1"}},
    )
    print(result["results"])
    # ["findings on AI", "findings on databases", "findings on security"]
```

### Querying workflow state

Query the current state of a running or completed workflow at any time:

```python
config = {"configurable": {"thread_id": "my-workflow"}}

# Start workflow without waiting
handle = await tg.astart({"value": ""}, config)

# Query state (works while running or after completion)
state = await tg.get_state(config)
print(state["status"])   # "running", "interrupted", or "done"
print(state["values"])   # Current channel values
print(state["step"])     # Current step number
```

### Streaming

Stream events from the workflow as nodes execute:

```python
async for event in tg.astream(
    {"message": "hello"},
    config={"configurable": {"thread_id": "stream-1"}},
    stream_mode="values",
):
    print(event)
```

Supported stream modes: `"values"` (full state after each node), `"updates"` (only changes), `"custom"` (custom events from nodes).

## Configuration

### Per-node Activity options

Configure timeouts per node for long-running operations:

```python
from datetime import timedelta

from langgraph.temporal import ActivityOptions, TemporalGraph

tg = TemporalGraph(
    graph,
    client,
    task_queue="my-queue",
    node_activity_options={
        "llm_call": ActivityOptions(
            start_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(seconds=30),
        ),
        "quick_transform": ActivityOptions(
            start_to_close_timeout=timedelta(seconds=30),
        ),
    },
)
```

### Per-node task queues

Route specific nodes to dedicated worker pools (e.g., GPU workers):

```python
tg = TemporalGraph(
    graph,
    client,
    task_queue="default-queue",
    node_task_queues={
        "embed": "gpu-queue",
        "rerank": "gpu-queue",
    },
)
```

### Workflow timeouts

```python
tg = TemporalGraph(
    graph,
    client,
    task_queue="my-queue",
    workflow_execution_timeout=timedelta(hours=24),
    workflow_run_timeout=timedelta(hours=1),
)
```

## Running a Temporal server

### For development (Docker Compose)

This package includes a `docker-compose.yml` for running a full Temporal server locally:

```bash
make start_temporal     # Start Temporal server
make stop_temporal      # Stop Temporal server
```

### Running integration tests

```bash
# Against the in-process test server (no Docker needed)
make test_integration

# Against a real Temporal server via Docker
make test_integration_docker
```

## License

MIT
