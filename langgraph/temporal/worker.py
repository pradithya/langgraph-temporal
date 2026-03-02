"""Helper for creating properly configured Temporal Workers.

Creates Workers with all necessary Workflow and Activity registrations
for executing LangGraph graphs on Temporal.
"""

from __future__ import annotations

from typing import Any

from langgraph.pregel import Pregel
from temporalio.client import Client as TemporalClient
from temporalio.worker import Worker

from langgraph.temporal.activities import (
    dynamic_execute_node,
    evaluate_conditional_edge,
    execute_node,
)
from langgraph.temporal.converter import GraphRegistry
from langgraph.temporal.workflow import LangGraphWorkflow


def create_worker(
    graph: Pregel,
    client: TemporalClient,
    task_queue: str = "langgraph-default",
    **kwargs: Any,
) -> Worker:
    """Create a Temporal Worker configured for a LangGraph graph.

    Registers the `LangGraphWorkflow` as a Workflow and `execute_node` /
    `evaluate_conditional_edge` as Activities. The graph is registered in
    the `GraphRegistry` for Activity-side lookup.

    Args:
        graph: A compiled Pregel graph instance.
        client: A Temporal client instance.
        task_queue: The task queue to listen on.
        **kwargs: Additional Worker configuration (e.g.,
            `max_concurrent_activities`, `max_concurrent_workflow_tasks`).

    Returns:
        A configured Temporal Worker instance.
    """
    # Ensure graph is registered
    GraphRegistry.get_instance().register(graph)

    return Worker(
        client,
        task_queue=task_queue,
        workflows=[LangGraphWorkflow],
        activities=[execute_node, dynamic_execute_node, evaluate_conditional_edge],
        **kwargs,
    )
