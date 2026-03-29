"""Helper for creating properly configured Temporal Workers.

Creates Workers with all necessary Workflow and Activity registrations
for executing LangGraph graphs on Temporal.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import TracebackType
from typing import Any

from langgraph.pregel import Pregel
from temporalio.client import Client as TemporalClient
from temporalio.worker import Worker

from langgraph.temporal.activities import (
    dynamic_execute_node,
    evaluate_conditional_edge,
    execute_node,
    get_available_task_queue,
    set_worker_task_queue,
)
from langgraph.temporal.converter import GraphRegistry
from langgraph.temporal.workflow import LangGraphWorkflow


class WorkerGroup:
    """Manages multiple Temporal Workers as a single async context manager.

    Used for worker-affinity mode where two workers are needed:
    one on the shared queue (Workflows + discovery Activity) and one on
    a worker-specific queue (node execution Activities).
    """

    def __init__(self, workers: list[Worker]) -> None:
        self._workers = workers

    async def __aenter__(self) -> WorkerGroup:
        for w in self._workers:
            await w.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        for w in reversed(self._workers):
            await w.__aexit__(exc_type, exc_val, exc_tb)

    async def run(self) -> None:
        """Run all workers. Blocks until shutdown."""
        import asyncio

        await asyncio.gather(*[w.run() for w in self._workers])


def _resolve_worker_queue(
    task_queue: str,
    queue_file: Path | str | None,
) -> str:
    """Resolve the worker-specific queue name.

    If `queue_file` is provided and exists, read the persisted queue name
    (so a restarted worker re-registers on the same queue). Otherwise
    generate a new one and persist it if a path was given.
    """
    if queue_file is not None:
        path = Path(queue_file)
        if path.exists():
            stored = path.read_text().strip()
            if stored:
                return stored
        # Generate and persist
        queue = f"{task_queue}-worker-{uuid.uuid4().hex[:12]}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(queue)
        return queue

    return f"{task_queue}-worker-{uuid.uuid4().hex[:12]}"


def create_worker(
    graph: Pregel,
    client: TemporalClient,
    task_queue: str = "langgraph-default",
    *,
    use_worker_affinity: bool = False,
    worker_queue_file: Path | str | None = None,
    **kwargs: Any,
) -> Worker | WorkerGroup:
    """Create a Temporal Worker configured for a LangGraph graph.

    Registers the `LangGraphWorkflow` as a Workflow and `execute_node` /
    `evaluate_conditional_edge` as Activities. The graph is registered in
    the `GraphRegistry` for Activity-side lookup.

    When `use_worker_affinity` is True, returns a `WorkerGroup` with two
    workers following the Temporal worker-specific task queues pattern:
    - A shared worker on `task_queue` (Workflows + discovery Activity)
    - A worker-specific worker on a unique queue (node Activities)

    Args:
        graph: A compiled Pregel graph instance.
        client: A Temporal client instance.
        task_queue: The task queue to listen on.
        use_worker_affinity: When True, create a dual-worker setup for
            worker-specific task queue affinity.
        worker_queue_file: Path to persist the worker-specific queue name.
            On restart, the worker re-registers on the same queue so that
            in-flight Activities resume on this worker. If None, a new
            queue name is generated each time (no restart recovery).
        **kwargs: Additional Worker configuration (e.g.,
            `max_concurrent_activities`, `max_concurrent_workflow_tasks`).

    Returns:
        A configured Temporal Worker or WorkerGroup instance.
    """
    # Ensure graph is registered
    GraphRegistry.get_instance().register(graph)

    if not use_worker_affinity:
        return Worker(
            client,
            task_queue=task_queue,
            workflows=[LangGraphWorkflow],
            activities=[
                execute_node,
                dynamic_execute_node,
                evaluate_conditional_edge,
            ],
            **kwargs,
        )

    # Worker-affinity mode: two workers following the Temporal
    # worker-specific task queues pattern.
    worker_specific_queue = _resolve_worker_queue(task_queue, worker_queue_file)
    set_worker_task_queue(worker_specific_queue)

    # Worker 1: shared queue — Workflows + get_available_task_queue
    shared_worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[LangGraphWorkflow],
        activities=[get_available_task_queue],
        **kwargs,
    )

    # Worker 2: worker-specific queue — node execution Activities
    specific_worker = Worker(
        client,
        task_queue=worker_specific_queue,
        activities=[
            execute_node,
            dynamic_execute_node,
            evaluate_conditional_edge,
        ],
    )

    return WorkerGroup([shared_worker, specific_worker])
