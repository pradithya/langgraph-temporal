# Human-in-the-Loop

langgraph-temporal supports LangGraph's `interrupt()` for human-in-the-loop workflows. The Workflow pauses with **zero resource consumption** — no process is blocked waiting for approval.

## How it works

1. A node calls `interrupt()` during execution
2. The Activity captures the interrupt payload and returns it to the Workflow
3. The Workflow pauses and waits for a `resume_signal`
4. You query the Workflow state to see pending interrupts
5. You send a resume Signal with an approval value
6. The Workflow resumes execution from where it paused

## Using interrupt_before / interrupt_after

You can pause before or after specific nodes without modifying node code:

```python
# Pause before the "deploy" node for approval
result = await tg.ainvoke(
    {"action": "deploy to production"},
    config={"configurable": {"thread_id": "deploy-1"}},
    interrupt_before=["deploy"],
)
```

Or set it at compile time:

```python
graph = builder.compile(interrupt_before=["deploy"])
tg = TemporalGraph(graph, client, task_queue="my-queue")
```

## Querying and resuming

Use `astart()` for non-blocking execution, then query and resume:

```python
# Start the workflow (non-blocking)
handle = await tg.astart(
    {"action": "deploy to production"},
    config={"configurable": {"thread_id": "deploy-1"}},
    interrupt_before=["deploy"],
)

# ... later, check the state
state = await tg.get_state(
    {"configurable": {"thread_id": "deploy-1"}}
)

if state["status"] == "interrupted":
    print("Pending approval:", state["interrupts"])

    # Resume with approval
    await tg.resume(
        {"configurable": {"thread_id": "deploy-1"}},
        "approved",
    )

# Wait for completion
result = await handle.result()
```

## Using interrupt() in nodes

Nodes can call `interrupt()` directly for dynamic approval requests:

```python
from langgraph.types import interrupt


def deploy_node(state: State) -> dict:
    plan = generate_deployment_plan(state)

    # Pause and wait for human approval
    approval = interrupt({"plan": plan, "requires": "approval"})

    if approval == "approved":
        execute_deployment(plan)
        return {"status": "deployed"}
    else:
        return {"status": "cancelled"}
```

The interrupt payload is stored in the Workflow state and can be queried via `get_state()`.

## Zero-resource waiting

Unlike vanilla LangGraph where `interrupt()` blocks a running process, Temporal-backed interrupts:

- **Consume no compute** while waiting — the Worker is free to handle other Workflows
- **Survive restarts** — if the Worker restarts, the Workflow state is preserved
- **Have no timeout** — the Workflow can wait indefinitely for human input
- **Are queryable** — check interrupt status from any process via Temporal Queries
