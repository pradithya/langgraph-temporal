# Streaming

langgraph-temporal supports streaming execution events from Temporal Workflows back to the client.

## Default: Polling stream backend

The default `PollingStreamBackend` uses Temporal Workflow Queries with cursor-based pagination:

```python
async for event in tg.astream(
    {"message": "hello"},
    config={"configurable": {"thread_id": "stream-1"}},
    stream_mode="values",
):
    print(event)
```

### Stream modes

The `stream_mode` parameter filters events by type:

| Mode | Description |
|---|---|
| `values` | Full channel state after each step |
| `updates` | Only the writes from each node |
| `custom` | Custom data emitted by nodes via `StreamWriter` |
| `messages` | LLM message tokens (for chat UIs) |

## Configuring poll interval

```python
from langgraph.temporal import PollingStreamBackend

tg = TemporalGraph(
    graph,
    client,
    task_queue="my-queue",
    stream_backend=PollingStreamBackend(poll_interval=0.05),  # 50ms
)
```

## Custom stream backends

Implement `StreamBackend` for alternative transport (e.g., Redis Pub/Sub, WebSocket):

```python
from langgraph.temporal import StreamBackend


class RedisStreamBackend(StreamBackend):
    async def publish(self, workflow_id: str, event: Any) -> None:
        await redis.publish(f"stream:{workflow_id}", serialize(event))

    async def subscribe(self, workflow_id: str) -> AsyncIterator[Any]:
        async for msg in redis.subscribe(f"stream:{workflow_id}"):
            yield deserialize(msg)
```

## How it works internally

1. As nodes execute, stream events are buffered inside the Workflow
2. The client polls the `get_stream_buffer` Workflow Query with a cursor
3. New events since the last cursor are returned
4. The cursor advances, and polling continues until the Workflow completes
