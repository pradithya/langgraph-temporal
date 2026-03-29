"""TemporalGraph - the primary user-facing entry point.

Wraps a compiled LangGraph graph for execution on Temporal, preserving
the familiar `invoke()` / `stream()` API.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any

from langgraph.pregel import Pregel
from temporalio.client import Client as TemporalClient
from temporalio.client import WorkflowHandle

from langgraph.temporal.config import (
    ActivityOptions,
    RetryPolicyConfig,
    StateUpdatePayload,
    SubAgentConfig,
    WorkflowInput,
    WorkflowOutput,
)
from langgraph.temporal.converter import GraphRegistry
from langgraph.temporal.streaming import PollingStreamBackend, StreamBackend
from langgraph.temporal.worker import create_worker
from langgraph.temporal.workflow import LangGraphWorkflow


class TemporalGraph:
    """Wraps a compiled LangGraph graph for execution on Temporal.

    This is the primary entry point for using LangGraph with Temporal.
    It preserves the familiar `invoke()` / `stream()` API while executing
    the graph as a Temporal Workflow with durable execution guarantees.

    Args:
        graph: A compiled Pregel graph (output of `StateGraph.compile()`).
        client: A Temporal client instance.
        task_queue: Default task queue for Activities.
        node_task_queues: Per-node task queue overrides.
        node_activity_options: Per-node Activity configuration.
        workflow_execution_timeout: Maximum time for the entire workflow
            execution including retries and continue-as-new.
        workflow_run_timeout: Maximum time for a single workflow run.
        stream_backend: Backend for streaming events.
    """

    def __init__(
        self,
        graph: Pregel,
        client: TemporalClient,
        *,
        task_queue: str = "langgraph-default",
        node_task_queues: dict[str, str] | None = None,
        node_activity_options: dict[str, ActivityOptions] | None = None,
        workflow_execution_timeout: timedelta | None = None,
        workflow_run_timeout: timedelta | None = None,
        stream_backend: StreamBackend | None = None,
    ) -> None:
        self.graph = graph
        self.client = client
        self.task_queue = task_queue
        self.node_task_queues = node_task_queues or {}
        self.node_activity_options = node_activity_options or {}
        self.workflow_execution_timeout = workflow_execution_timeout
        self.workflow_run_timeout = workflow_run_timeout
        self.stream_backend = stream_backend or PollingStreamBackend()

        # Register graph in the GraphRegistry
        self._graph_ref = GraphRegistry.get_instance().register(graph)

    def _get_workflow_id(self, config: dict[str, Any] | None = None) -> str:
        """Get or generate a workflow ID from config."""
        if config and "configurable" in config:
            thread_id = config["configurable"].get("thread_id")
            if thread_id:
                return str(thread_id)
        return f"langgraph-{uuid.uuid4().hex[:12]}"

    def _build_workflow_input(
        self,
        input: Any,
        config: dict[str, Any] | None = None,
        *,
        interrupt_before: list[str] | None = None,
        interrupt_after: list[str] | None = None,
    ) -> WorkflowInput:
        """Build WorkflowInput from user arguments."""
        recursion_limit = 25
        if config and "configurable" in config:
            recursion_limit = config["configurable"].get(
                "recursion_limit", recursion_limit
            )
        if config:
            recursion_limit = config.get("recursion_limit", recursion_limit)

        # Use graph's interrupt configuration as defaults
        ib = interrupt_before
        if ib is None and self.graph.interrupt_before_nodes:
            ib = list(self.graph.interrupt_before_nodes)
        ia = interrupt_after
        if ia is None and self.graph.interrupt_after_nodes:
            ia = list(self.graph.interrupt_after_nodes)

        # Serialize activity options for workflow input
        serialized_options: dict[str, ActivityOptions] | None = None
        if self.node_activity_options:
            serialized_options = dict(self.node_activity_options)

        # Serialize retry policies if configured on the graph
        serialized_retry: dict[str, RetryPolicyConfig] | None = None
        if hasattr(self.graph, "retry_policies") and self.graph.retry_policies:
            serialized_retry = {}
            for node_name, policy in self.graph.retry_policies.items():
                serialized_retry[node_name] = RetryPolicyConfig(
                    initial_interval_seconds=getattr(policy, "initial_interval", 1.0),
                    backoff_coefficient=getattr(policy, "backoff_factor", 2.0),
                    max_interval_seconds=getattr(policy, "max_interval", 100.0),
                    max_attempts=getattr(policy, "max_attempts", 0),
                )

        # Read sticky_task_queue, use_worker_affinity, and subagent_config
        sticky_task_queue: str | None = None
        use_worker_affinity: bool = False
        subagent_config: SubAgentConfig | None = None
        if config and "configurable" in config:
            sticky_task_queue = config["configurable"].get("sticky_task_queue")
            use_worker_affinity = bool(
                config["configurable"].get("use_worker_affinity", False)
            )
            raw_subagent = config["configurable"].get("subagent_config")
            if isinstance(raw_subagent, SubAgentConfig):
                subagent_config = raw_subagent
            elif isinstance(raw_subagent, dict):
                subagent_config = SubAgentConfig(
                    task_queue=raw_subagent.get("task_queue"),
                    sticky_task_queue=raw_subagent.get("sticky_task_queue"),
                    execution_timeout_seconds=raw_subagent.get(
                        "execution_timeout_seconds", 1800.0
                    ),
                )

        return WorkflowInput(
            graph_definition_ref=self._graph_ref,
            input_data=input if isinstance(input, dict) else {"__root__": input},
            recursion_limit=recursion_limit,
            interrupt_before=ib,
            interrupt_after=ia,
            node_task_queues=self.node_task_queues if self.node_task_queues else None,
            node_activity_options=serialized_options,
            node_retry_policies=serialized_retry,
            sticky_task_queue=sticky_task_queue,
            use_worker_affinity=use_worker_affinity,
            subagent_config=subagent_config,
        )

    async def ainvoke(
        self,
        input: Any,
        config: dict[str, Any] | None = None,
        *,
        interrupt_before: list[str] | None = None,
        interrupt_after: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Execute the graph as a Temporal Workflow and return the result.

        Args:
            input: Input to the graph.
            config: RunnableConfig with thread_id and other settings.
            interrupt_before: Node names to pause before executing.
            interrupt_after: Node names to pause after executing.

        Returns:
            The final channel state values.
        """
        workflow_id = self._get_workflow_id(config)
        workflow_input = self._build_workflow_input(
            input,
            config,
            interrupt_before=interrupt_before,
            interrupt_after=interrupt_after,
        )

        result: WorkflowOutput = await self.client.execute_workflow(
            LangGraphWorkflow.run,
            workflow_input,
            id=workflow_id,
            task_queue=self.task_queue,
            execution_timeout=self.workflow_execution_timeout,
            run_timeout=self.workflow_run_timeout,
        )

        return result.channel_values

    def invoke(
        self,
        input: Any,
        config: dict[str, Any] | None = None,
        *,
        interrupt_before: list[str] | None = None,
        interrupt_after: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Execute the graph as a Temporal Workflow and return the result.

        Synchronous wrapper around `ainvoke()`.
        """
        return asyncio.get_event_loop().run_until_complete(
            self.ainvoke(
                input,
                config,
                interrupt_before=interrupt_before,
                interrupt_after=interrupt_after,
                **kwargs,
            )
        )

    async def astart(
        self,
        input: Any,
        config: dict[str, Any] | None = None,
        *,
        interrupt_before: list[str] | None = None,
        interrupt_after: list[str] | None = None,
    ) -> WorkflowHandle:
        """Start a Workflow without waiting for completion.

        Args:
            input: Input to the graph.
            config: RunnableConfig with thread_id and other settings.

        Returns:
            A Temporal WorkflowHandle for querying/signaling the workflow.
        """
        workflow_id = self._get_workflow_id(config)
        workflow_input = self._build_workflow_input(
            input,
            config,
            interrupt_before=interrupt_before,
            interrupt_after=interrupt_after,
        )

        handle = await self.client.start_workflow(
            LangGraphWorkflow.run,
            workflow_input,
            id=workflow_id,
            task_queue=self.task_queue,
            execution_timeout=self.workflow_execution_timeout,
            run_timeout=self.workflow_run_timeout,
        )

        return handle

    async def astream(
        self,
        input: Any,
        config: dict[str, Any] | None = None,
        *,
        stream_mode: str = "values",
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        """Stream graph execution events from a Temporal Workflow.

        Args:
            input: Input to the graph.
            config: RunnableConfig with thread_id and other settings.
            stream_mode: Stream mode (values, updates, custom, messages).

        Yields:
            Stream events matching the requested mode.
        """
        handle = await self.astart(input, config, **kwargs)

        if isinstance(self.stream_backend, PollingStreamBackend):
            async for event in self.stream_backend.poll_stream(
                handle,
                LangGraphWorkflow.get_stream_buffer,
            ):
                if hasattr(event, "mode") and event.mode == stream_mode:
                    yield event.data
                elif not hasattr(event, "mode"):
                    yield event

    def stream(
        self,
        input: Any,
        config: dict[str, Any] | None = None,
        *,
        stream_mode: str = "values",
        **kwargs: Any,
    ) -> Iterator[Any]:
        """Stream graph execution events (synchronous).

        Synchronous wrapper around `astream()`.
        """

        async def _collect() -> list[Any]:
            events = []
            async for event in self.astream(
                input, config, stream_mode=stream_mode, **kwargs
            ):
                events.append(event)
            return events

        events = asyncio.get_event_loop().run_until_complete(_collect())
        yield from events

    async def get_state(self, config: dict[str, Any]) -> dict[str, Any]:
        """Query the Workflow for current state.

        Args:
            config: RunnableConfig with thread_id.

        Returns:
            The current state query result.
        """
        workflow_id = self._get_workflow_id(config)
        handle = self.client.get_workflow_handle(workflow_id)
        result = await handle.query(LangGraphWorkflow.get_current_state)
        return {
            "values": result.channel_values,
            "step": result.step,
            "status": result.status,
            "interrupts": result.interrupts,
        }

    async def update_state(
        self,
        config: dict[str, Any],
        values: dict[str, Any],
        *,
        as_node: str | None = None,
    ) -> None:
        """Send a state update Signal to a running Workflow.

        Args:
            config: RunnableConfig with thread_id.
            values: Channel values to update.
            as_node: Node name to attribute the update to (for trigger tracking).
        """
        workflow_id = self._get_workflow_id(config)
        handle = self.client.get_workflow_handle(workflow_id)
        writes = [(k, v) for k, v in values.items()]
        await handle.signal(
            LangGraphWorkflow.update_state_signal,
            StateUpdatePayload(writes=writes),
        )

    async def get_state_history(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        """Retrieve state history from the Temporal Workflow.

        Currently returns the current state only. In the future, this will
        traverse the continue-as-new chain for full history.

        Args:
            config: RunnableConfig with thread_id.

        Returns:
            List of state snapshots.
        """
        state = await self.get_state(config)
        return [state]

    async def resume(self, config: dict[str, Any], value: Any) -> None:
        """Send a resume Signal to a paused Workflow.

        Args:
            config: RunnableConfig with thread_id.
            value: The resume value to send.
        """
        workflow_id = self._get_workflow_id(config)
        handle = self.client.get_workflow_handle(workflow_id)
        await handle.signal(LangGraphWorkflow.resume_signal, value)

    @classmethod
    async def local(
        cls,
        graph: Pregel,
        *,
        task_queue: str = "langgraph-default",
        **kwargs: Any,
    ) -> TemporalGraph:
        """Create a TemporalGraph using Temporal's local test server.

        This is a convenience factory for local development and testing.
        It starts an in-process Temporal test server, removing the need
        for a full Temporal server deployment.

        Args:
            graph: A compiled Pregel graph instance.
            task_queue: Default task queue name.
            **kwargs: Additional arguments passed to TemporalGraph.

        Returns:
            A TemporalGraph configured with the local test server client.

        Example:
            ```python
            temporal_graph = await TemporalGraph.local(compiled_graph)
            result = await temporal_graph.ainvoke({"messages": ["hello"]})
            ```
        """
        from temporalio.testing import WorkflowEnvironment

        env = await WorkflowEnvironment.start_local()
        return cls(
            graph,
            env.client,
            task_queue=task_queue,
            **kwargs,
        )

    def create_worker(self, **kwargs: Any) -> Any:
        """Create a Temporal Worker configured for this graph.

        Returns:
            A Temporal Worker instance ready to run.
        """
        return create_worker(
            self.graph,
            self.client,
            self.task_queue,
            **kwargs,
        )
