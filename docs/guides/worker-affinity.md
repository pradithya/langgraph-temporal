# Worker Affinity

Worker affinity ensures that all Activities for a given Workflow execution run on the **same worker**. This is essential when your nodes access local resources (e.g., filesystem, GPU, local database).

## When to use it

- Nodes read/write files on the local disk
- Nodes use a local GPU or device
- Nodes share in-memory state across invocations
- You need filesystem consistency across a multi-step agent

## How it works

langgraph-temporal implements the [Temporal worker-specific task queues pattern](https://github.com/temporalio/samples-python/tree/main/worker_specific_task_queues):

1. `create_worker()` generates a **unique queue name** per worker process
2. It creates **two internal workers**:
    - **Shared worker** on the main queue — handles Workflows and the `get_available_task_queue` discovery Activity
    - **Worker-specific worker** on the unique queue — handles node execution Activities
3. When a Workflow starts, it calls `get_available_task_queue` on the shared queue — whichever worker picks it up returns its unique queue
4. All subsequent node Activities are dispatched to that discovered queue
5. The discovered queue **survives continue-as-new** — the same worker stays pinned

```
Worker A                          Worker B
┌──────────────────────┐         ┌──────────────────────┐
│ Shared: "my-queue"   │         │ Shared: "my-queue"   │
│  - LangGraphWorkflow │         │  - LangGraphWorkflow │
│  - get_avail_queue   │         │  - get_avail_queue   │
│                      │         │                      │
│ Specific: "my-q-a1"  │         │ Specific: "my-q-b2"  │
│  - execute_node      │         │  - execute_node      │
│  - dynamic_exec_node │         │  - dynamic_exec_node │
└──────────────────────┘         └──────────────────────┘
```

## Enabling worker affinity

### On the Worker side

```python
from langgraph.temporal import TemporalGraph

tg = TemporalGraph(graph, client, task_queue="my-queue")

worker = tg.create_worker(
    use_worker_affinity=True,
    workflow_runner=UnsandboxedWorkflowRunner(),
)

async with worker:
    await asyncio.Future()  # run forever
```

The returned object is a `WorkerGroup` managing both internal workers.

### On the client side

No changes needed — affinity is **transparent to the client**:

```python
result = await tg.ainvoke(
    {"message": "hello"},
    config={
        "configurable": {
            "thread_id": "task-1",
            "use_worker_affinity": True,
        }
    },
)
```

## Restart recovery

By default, a new queue name is generated each time a worker starts. If the worker restarts, in-flight Activities on the old queue will fail.

To handle restarts gracefully, persist the queue name to a file:

```python
worker = tg.create_worker(
    use_worker_affinity=True,
    worker_queue_file="/var/run/my-worker/queue.txt",
    workflow_runner=UnsandboxedWorkflowRunner(),
)
```

On restart, the worker reads the persisted queue name and re-registers on the same queue, allowing in-flight Activities to resume.

## Fallback on worker failure

If a pinned worker dies permanently, Activities dispatched to its queue will fail with an `ActivityError`. The Workflow handles this automatically:

1. Catches the `ActivityError`
2. Clears the stale sticky queue
3. Re-calls `get_available_task_queue` to discover a new worker
4. Retries the failed Activity on the new worker

This means your Workflow **never gets stuck** — it falls back to another available worker if the pinned one is gone.
