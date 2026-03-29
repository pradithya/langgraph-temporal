"""Temporal Activities for LangGraph node execution and conditional edge evaluation.

Activities are the non-deterministic boundary in the Temporal integration.
All I/O operations (LLM calls, tool invocations, etc.) happen inside Activities.
"""

from __future__ import annotations

import contextvars
import itertools
from collections.abc import Sequence
from typing import Any, cast

from langchain_core.runnables.config import var_child_runnable_config
from langgraph._internal._constants import (
    CONF,
    CONFIG_KEY_CHECKPOINT_NS,
    CONFIG_KEY_READ,
    CONFIG_KEY_SCRATCHPAD,
    CONFIG_KEY_SEND,
    CONFIG_KEY_STREAM,
    TASKS,
)
from langgraph._internal._scratchpad import PregelScratchpad
from langgraph.channels.untracked_value import UntrackedValue
from langgraph.errors import GraphInterrupt
from langgraph.types import Command, Send
from temporalio import activity
from temporalio.common import RawValue
from temporalio.exceptions import ApplicationError

from langgraph.temporal.config import (
    ConditionalEdgeInput,
    NodeActivityInput,
    NodeActivityOutput,
)
from langgraph.temporal.converter import GraphRegistry

# Generic extension point: context variable for collecting Child Workflow
# requests during Activity execution. Middleware (e.g., TemporalSubAgentMiddleware)
# appends requests here; the Activity collects them into NodeActivityOutput.
_child_workflow_requests_var: contextvars.ContextVar[list[dict[str, Any]]] = (
    contextvars.ContextVar("_child_workflow_requests")
)

# Worker-specific task queue for worker affinity.
# Set at worker startup via `set_worker_task_queue()`. The
# `get_available_task_queue` activity returns this value so the Workflow
# can pin all subsequent Activities to this worker's queue.
_worker_task_queue: str | None = None


def set_worker_task_queue(queue: str) -> None:
    """Set the worker-specific task queue name.

    Called at worker startup. The `get_available_task_queue` activity
    returns this value to the Workflow for worker affinity.
    """
    global _worker_task_queue  # noqa: PLW0603
    _worker_task_queue = queue


@activity.defn
async def get_available_task_queue() -> str:
    """Return this worker's unique task queue name.

    The Workflow calls this activity on the shared distribution queue.
    Whichever worker picks it up returns its own unique queue name. The
    Workflow then dispatches all subsequent Activities to that queue,
    achieving worker affinity (following the Temporal worker-specific
    task queues pattern).
    """
    if _worker_task_queue is None:
        raise ApplicationError(
            "Worker task queue not configured. "
            "Call set_worker_task_queue() at worker startup.",
            non_retryable=True,
        )
    return _worker_task_queue


def _make_counter() -> Any:
    """Create a simple callable counter using itertools.count."""
    return itertools.count(0).__next__


async def _execute_node_impl(input: NodeActivityInput) -> NodeActivityOutput:
    """Core logic for executing a LangGraph node inside a Temporal Activity.

    Reconstructs the full RunnableConfig with:
    - `CONFIG_KEY_SEND`: Callable to emit dynamic Send objects (PUSH tasks)
    - `CONFIG_KEY_READ`: Callable for channel reads within nodes
    - `CONFIG_KEY_SCRATCHPAD`: PregelScratchpad with interrupt counters
      and resume values for `interrupt()` support

    This function is called by both the named `execute_node` activity
    and the dynamic activity handler that provides per-node activity
    type names (e.g. `execute_node:my_node`) for Temporal UI visibility.
    """
    graph = GraphRegistry.get_instance().get(input.graph_definition_ref)
    node = graph.nodes[input.node_name]

    # Collect dynamic Send objects, writes, and custom stream data
    push_sends: list[dict[str, Any]] = []
    all_writes: list[tuple[str, Any]] = []
    custom_data: list[Any] = []

    def send_handler(writes: list[tuple[str, Any]]) -> None:
        """CONFIG_KEY_SEND: Captures writes including Send objects."""
        for chan, val in writes:
            if chan == TASKS:
                if isinstance(val, Send):
                    push_sends.append({"node": val.node, "arg": val.arg})
            else:
                all_writes.append((chan, val))

    def stream_handler(data: Any, mode: str = "custom") -> None:
        """CONFIG_KEY_STREAM: Captures custom stream data from StreamWriter."""
        custom_data.append(data)

    def read_handler(
        select: str | list[str],
        fresh: bool = False,
    ) -> Any:
        """CONFIG_KEY_READ: Provides channel reads for the node.

        Overlays any writes collected so far on top of the input state,
        so that conditional edge writers see the node's output values.
        """
        # Build effective state: input + writes collected so far
        effective = dict(input.input_state)
        for ch, val in all_writes:
            effective[ch] = val
        if isinstance(select, str):
            return effective.get(select)
        return {k: effective.get(k) for k in select}

    # Reconstruct PregelScratchpad for interrupt() support
    scratchpad = PregelScratchpad(
        step=0,
        stop=25,
        call_counter=_make_counter(),
        interrupt_counter=_make_counter(),
        get_null_resume=cast(Any, lambda consume=False: None),
        resume=list(input.resume_values) if input.resume_values else [],
        subgraph_counter=_make_counter(),
    )

    # Build RunnableConfig with required keys
    config: Any = {
        CONF: {
            CONFIG_KEY_SEND: send_handler,
            CONFIG_KEY_READ: read_handler,
            CONFIG_KEY_SCRATCHPAD: scratchpad,
            CONFIG_KEY_STREAM: stream_handler,
            CONFIG_KEY_CHECKPOINT_NS: "",
        }
    }

    # Determine node input
    if input.send_input is not None:
        node_input = input.send_input
    else:
        node_input = input.input_state

    try:
        activity.heartbeat(f"Starting node {input.node_name}")

        # Initialize the child workflow requests context variable so that
        # middleware (e.g., TemporalSubAgentMiddleware) can append requests
        # during node execution.
        _child_workflow_requests_var.set([])

        # Set the runnable config context so that interrupt() and other
        # functions that call get_config() can find it.  This is normally
        # done by the Runnable invoke/ainvoke tracing path, but Activities
        # bypass that machinery.
        var_child_runnable_config.set(config)

        # Execute node.bound (the actual node callable)
        result = await node.bound.ainvoke(node_input, config)

        activity.heartbeat(f"Completed node {input.node_name}")

        # Handle Command objects returned from nodes
        command_data = None
        if isinstance(result, Command):
            command_data = {
                "goto": (
                    [
                        {"node": s.node, "arg": s.arg} if isinstance(s, Send) else s
                        for s in result.goto
                    ]
                    if isinstance(result.goto, (list, tuple))
                    else result.goto
                ),
                "update": result.update,
                "graph": getattr(result, "graph", None),
            }
            # Process Command through writers to get channel writes
            if result.update is not None:
                for writer in node.writers:
                    writer.invoke(result, config)
        else:
            # Run writers (ChannelWrite runnables) to collect writes
            for writer in node.writers:
                writer.invoke(result, config)

        # Filter out UntrackedValue writes
        serializable_writes = _filter_untracked_writes(all_writes, graph)

        # Collect any Child Workflow requests stored during execution
        child_wf_requests = _child_workflow_requests_var.get([])

        return NodeActivityOutput(
            node_name=input.node_name,
            writes=serializable_writes,
            triggers=list(input.triggers),
            task_path=input.task_path,
            push_sends=push_sends if push_sends else None,
            command=command_data,
            custom_data=custom_data if custom_data else None,
            child_workflow_requests=(child_wf_requests if child_wf_requests else None),
        )
    except GraphInterrupt as exc:
        # Node called interrupt() - propagate to Workflow
        # Do NOT apply writes; the node will be re-executed on resume
        # GraphInterrupt stores interrupts in args[0]
        interrupts = exc.args[0] if exc.args else []
        return NodeActivityOutput(
            node_name=input.node_name,
            writes=[],
            triggers=list(input.triggers),
            task_path=input.task_path,
            interrupts=[{"value": i.value, "id": i.id} for i in interrupts],
        )
    except Exception as exc:
        # Evaluate retry_on predicate if the graph has one for this node
        retry_policy = _get_node_retry_policy(graph, input.node_name)
        if retry_policy is not None:
            retry_on = getattr(retry_policy, "retry_on", None)
            if callable(retry_on):
                should_retry = retry_on(exc)
                if not should_retry:
                    raise ApplicationError(
                        str(exc),
                        type=type(exc).__name__,
                        non_retryable=True,
                    ) from exc
        # Re-raise to let Temporal's retry mechanism handle it
        raise


@activity.defn
async def execute_node(input: NodeActivityInput) -> NodeActivityOutput:
    """Execute a single LangGraph node as a Temporal Activity.

    This named activity is kept for backward compatibility and manual
    worker setup.  The Workflow normally dispatches to per-node activity
    type names (e.g. `execute_node:my_node`) which are handled by
    `dynamic_execute_node`.
    """
    return await _execute_node_impl(input)


@activity.defn(dynamic=True)
async def dynamic_execute_node(args: Sequence[RawValue]) -> NodeActivityOutput:
    """Dynamic activity handler for per-node activity type names.

    Handles activity types like `execute_node:greet` so that each
    LangGraph node shows its name in the Temporal UI Event History
    instead of a generic `execute_node` label.
    """
    input = activity.payload_converter().from_payload(
        args[0].payload, NodeActivityInput
    )
    return await _execute_node_impl(input)


@activity.defn
async def evaluate_conditional_edge(
    input: ConditionalEdgeInput,
) -> str | list[str] | None:
    """Evaluate a conditional edge function as a Local Activity.

    This preserves Temporal's determinism boundary while minimizing
    latency (Local Activities run on the same worker as the Workflow).

    In compiled Pregel, conditional edges are stored in `graph.branches`
    as a mapping from source node to branch definitions. Each branch has
    a `path` callable that evaluates the current state to determine the
    next node(s).
    """
    graph = GraphRegistry.get_instance().get(input.graph_definition_ref)

    # Check for branches on this source node
    branches = getattr(graph, "branches", None)
    if not branches or input.source_node not in branches:
        return None

    node_branches = branches[input.source_node]
    results: list[str] = []

    for _branch_name, branch in node_branches.items():
        # Branch has a `path` callable that takes state and returns
        # target node name(s)
        path_fn = getattr(branch, "path", None)
        if path_fn is None:
            continue

        # Evaluate the conditional edge function with current state
        target = path_fn(input.channel_state)

        # Map through the branch's path_map if it exists
        path_map = getattr(branch, "path_map", None)
        if path_map and isinstance(target, str) and target in path_map:
            target = path_map[target]

        if isinstance(target, list):
            results.extend(target)
        elif isinstance(target, str):
            results.append(target)

    if not results:
        return None
    if len(results) == 1:
        return results[0]
    return results


def _get_node_retry_policy(graph: Any, node_name: str) -> Any:
    """Get the LangGraph RetryPolicy for a node, if any."""
    retry_policies = getattr(graph, "retry_policies", None)
    if retry_policies and node_name in retry_policies:
        return retry_policies[node_name]
    # Also check node-level retry_policy attribute
    node = graph.nodes.get(node_name)
    if node is not None:
        return getattr(node, "retry_policy", None)
    return None


def _filter_untracked_writes(
    writes: list[tuple[str, Any]],
    graph: Any,
) -> list[tuple[str, Any]]:
    """Filter out writes to UntrackedValue channels.

    UntrackedValue writes must never appear in Temporal event history.
    """
    return [
        (ch, val)
        for ch, val in writes
        if not isinstance(graph.channels.get(ch), UntrackedValue)
    ]
