# Core Concepts

This page explains how langgraph-temporal maps LangGraph constructs to Temporal primitives.

## Architecture overview

```
┌─────────────────────────────────────────────────┐
│  Your Code                                      │
│                                                 │
│  tg = TemporalGraph(graph, client)              │
│  result = await tg.ainvoke(input, config)       │
└──────────────────────┬──────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────┐
│  Temporal Server                                │
│                                                 │
│  ┌───────────────────────────────────────────┐  │
│  │  LangGraphWorkflow (Temporal Workflow)     │  │
│  │                                           │  │
│  │  for step in range(recursion_limit):      │  │
│  │      tasks = prepare_next_tasks(state)    │  │
│  │      results = execute_activities(tasks)  │  │
│  │      apply_writes(state, results)         │  │
│  └───────────────────────────────────────────┘  │
│                       │                          │
│                       ▼                          │
│  ┌───────────────────────────────────────────┐  │
│  │  execute_node Activity                    │  │
│  │  (runs your node function)                │  │
│  └───────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
```

## Mapping: LangGraph to Temporal

| LangGraph | Temporal | Description |
|---|---|---|
| `StateGraph` | Workflow Type | Your graph definition becomes a Workflow |
| Graph step | Workflow step | Each iteration of the execution loop |
| Node execution | Activity | Each node runs as a Temporal Activity |
| `thread_id` | Workflow ID | Identifies the execution instance |
| Channel state | Workflow state | Managed in-Workflow, persisted via event history |
| `interrupt()` | Signal + Wait | Pauses the Workflow until a Signal is received |
| Conditional edge | Local Activity | Edge evaluation runs as a lightweight Activity |
| Subgraph | Child Workflow | Subgraphs execute as independent Child Workflows |
| Retry policy | Activity retry | Maps to Temporal's built-in retry mechanism |

## Execution model

### The Workflow loop

`LangGraphWorkflow` implements LangGraph's execution loop as a deterministic Temporal Workflow:

1. **Initialize** — Reconstruct channel state from input (or restored state on continue-as-new)
2. **Prepare tasks** — Call `prepare_next_tasks()` to determine which nodes to execute
3. **Execute nodes** — Dispatch each node as a Temporal Activity
4. **Apply writes** — Feed Activity results back into channel state via `apply_writes()`
5. **Check termination** — If no more tasks, return final state
6. **Continue-as-new** — After 500 steps, continue the Workflow with carried-over state to prevent event history bloat

### Activities

Each graph node runs as a Temporal Activity. The Activity:

- Receives the current channel state and node name
- Reconstructs the full `RunnableConfig` (including `CONFIG_KEY_SEND`, `CONFIG_KEY_READ`)
- Invokes the node's bound function
- Returns channel writes, interrupt payloads, and any Command objects

There are two Activity types:

- **Named Activities** (`execute_node`) — for statically defined nodes
- **Dynamic Activities** (`dynamic_execute_node`) — for dynamically dispatched nodes (e.g., via `Send`)

### State management

State lives inside the Workflow, not in an external database. This means:

- No separate checkpoint store is needed
- State is reconstructed from Temporal's event history on replay
- `TemporalCheckpointSaver` provides read access to state via Workflow Queries

### Continue-as-new

Temporal Workflows have an event history size limit. To prevent unbounded growth, the Workflow triggers `continue_as_new()` after 500 steps. The current state is serialized into `RestoredState` and carried to the new Workflow run.

This is transparent to users — the Workflow ID stays the same, and state is preserved.

## Key classes

- [`TemporalGraph`](../reference/graph.md) — the primary user-facing entry point
- [`WorkerGroup`](../reference/worker.md) — manages Temporal Workers
- [`ActivityOptions`](../reference/config.md) — per-node Activity configuration
- [`TemporalCheckpointSaver`](../reference/checkpoint.md) — read state from Temporal
