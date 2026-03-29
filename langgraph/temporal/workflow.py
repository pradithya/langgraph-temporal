"""Temporal Workflow that executes a LangGraph graph.

The LangGraphWorkflow replaces the PregelLoop as the execution orchestrator.
It reuses LangGraph's actual channel objects and core algorithms
(`prepare_next_tasks`, `apply_writes`) for behavioral equivalence.

All non-deterministic operations are delegated to Activities.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, cast

from temporalio import workflow
from temporalio.common import RetryPolicy as TemporalRetryPolicy
from temporalio.workflow import ParentClosePolicy

from langgraph.temporal.config import (
    ActivityOptions,
    ConditionalEdgeInput,
    NodeActivityInput,
    NodeActivityOutput,
    RestoredState,
    RetryPolicyConfig,
    StateQueryResult,
    StateUpdatePayload,
    StreamEvent,
    StreamQueryResult,
    SubAgentConfig,
    WorkflowInput,
    WorkflowOutput,
)

# These imports are safe for Temporal Workflow determinism -- they are
# pure in-memory logic with no I/O side effects.
with workflow.unsafe.imports_passed_through():
    from langgraph.channels.base import BaseChannel
    from langgraph.checkpoint.base import Checkpoint
    from langgraph.managed.base import ManagedValueMapping
    from langgraph.pregel._algo import (
        PregelTaskWrites,
        apply_writes,
        prepare_next_tasks,
    )
    from langgraph.pregel._checkpoint import (
        channels_from_checkpoint,
        create_checkpoint,
        empty_checkpoint,
    )
    from langgraph.types import All, PregelTask

    from langgraph.temporal.activities import evaluate_conditional_edge
    from langgraph.temporal.converter import GraphRegistry

CONTINUE_AS_NEW_THRESHOLD = 500


def increment(current: int | None, channel: Any) -> int:
    """Simple version incrementer for `apply_writes`."""
    return (current or 0) + 1


def _to_temporal_retry_policy(config: RetryPolicyConfig) -> TemporalRetryPolicy:
    """Convert a RetryPolicyConfig to a Temporal RetryPolicy."""
    return TemporalRetryPolicy(
        initial_interval=timedelta(seconds=config.initial_interval_seconds),
        backoff_coefficient=config.backoff_coefficient,
        maximum_interval=timedelta(seconds=config.max_interval_seconds),
        maximum_attempts=config.max_attempts,
        non_retryable_error_types=(
            config.non_retryable_error_types if config.non_retryable_error_types else []
        ),
    )


def _get_channel_values(channels: dict[str, BaseChannel]) -> dict[str, Any]:
    """Extract current values from channel objects."""
    from langgraph._internal._typing import MISSING

    result: dict[str, Any] = {}
    for name, channel in channels.items():
        try:
            val = channel.get()
            if val is not MISSING:
                result[name] = val
        except Exception:
            pass
    return result


@workflow.defn
class LangGraphWorkflow:
    """Temporal Workflow that executes a LangGraph graph.

    Mirrors the PregelLoop.tick() / after_tick() cycle using the actual
    `prepare_next_tasks` and `apply_writes` functions from
    `langgraph.pregel._algo`.
    """

    def __init__(self) -> None:
        self.channels: dict[str, BaseChannel] = {}
        self.checkpoint: Checkpoint = empty_checkpoint()
        self.step: int = 0
        self.status: str = "pending"
        self.interrupts: list[dict[str, Any]] = []
        self.resume_values: list[Any] = []
        self.stream_buffer: list[Any] = []
        self._resume_event = asyncio.Event()
        self._graph_ref: str = ""
        self._managed: ManagedValueMapping = {}
        self._trigger_to_nodes: dict[str, list[str]] = {}
        self._node_task_queues: dict[str, str] = {}
        self._node_activity_options: dict[str, ActivityOptions] = {}
        self._node_retry_policies: dict[str, RetryPolicyConfig] = {}
        self._sticky_task_queue: str | None = None
        self._use_worker_affinity: bool = False
        self._subagent_config: SubAgentConfig | None = None

    @workflow.run
    async def run(self, input: WorkflowInput) -> WorkflowOutput:
        """Main Workflow execution loop.

        Mirrors the PregelLoop.tick() / after_tick() cycle using the
        ACTUAL function signatures from `langgraph.pregel._algo`.
        """
        self._graph_ref = input.graph_definition_ref
        graph = GraphRegistry.get_instance().get(self._graph_ref)

        self._trigger_to_nodes = dict(graph.trigger_to_nodes)  # type: ignore[arg-type]
        self._managed = {}

        # Store per-node configuration from workflow input
        self._node_task_queues = input.node_task_queues or {}
        self._node_activity_options = input.node_activity_options or {}
        self._node_retry_policies = input.node_retry_policies or {}
        self._sticky_task_queue = input.sticky_task_queue
        self._use_worker_affinity = input.use_worker_affinity
        self._subagent_config = input.subagent_config

        # Worker affinity: discover a worker-specific task queue.
        # If use_worker_affinity is set and we don't already have a
        # sticky queue (e.g., from continue-as-new), call the
        # get_available_task_queue activity on the shared queue.
        # Whichever worker picks it up returns its unique queue name.
        if input.use_worker_affinity and not self._sticky_task_queue:
            from langgraph.temporal.activities import get_available_task_queue

            self._sticky_task_queue = await workflow.execute_activity(
                get_available_task_queue,
                start_to_close_timeout=timedelta(seconds=10),
            )

        # Initialize or restore channels and checkpoint
        if input.restored_state:
            self.checkpoint = cast(Any, input.restored_state.checkpoint)
            self.step = input.restored_state.step
            channels, _ = channels_from_checkpoint(graph.channels, self.checkpoint)
            self.channels = dict(channels)
        else:
            self.checkpoint = empty_checkpoint()
            self.step = 0
            channels, _ = channels_from_checkpoint(graph.channels, self.checkpoint)
            self.channels = dict(channels)

            # Apply initial input as writes to the __start__ channel,
            # which triggers the __start__ node via trigger_to_nodes.
            if input.input_data is not None:
                input_channel: str = cast(str, graph.input_channels)
                input_writes = PregelTaskWrites(
                    path=("__input__",),
                    name="__input__",
                    writes=[
                        (input_channel, input.input_data),
                    ],
                    triggers=[],
                )
                apply_writes(
                    self.checkpoint,
                    self.channels,
                    [input_writes],
                    increment,
                    self._trigger_to_nodes,
                )

        max_steps = input.recursion_limit
        steps_in_this_run = 0
        pending_writes: list[tuple[str, str, Any]] = []

        # Build a minimal RunnableConfig for prepare_next_tasks
        config = {
            "configurable": {
                "thread_id": workflow.info().workflow_id,
            }
        }

        self.status = "running"

        # Main loop (equivalent to PregelLoop)
        while self.step < max_steps:
            # prepare_next_tasks with for_execution=False returns
            # dict[str, PregelTask] - node names and trigger info
            # without constructing full executable tasks.
            next_task_dict = prepare_next_tasks(  # type: ignore[call-overload]
                self.checkpoint,
                pending_writes,
                graph.nodes,
                self.channels,
                self._managed,
                config,
                self.step,
                max_steps,
                for_execution=False,
                trigger_to_nodes=self._trigger_to_nodes,
            )

            if not next_task_dict:
                self.status = "done"
                break

            next_tasks: list[PregelTask] = list(next_task_dict.values())

            # interrupt_before check
            if input.interrupt_before and self._should_interrupt_nodes(
                next_tasks, input.interrupt_before
            ):
                self.status = "interrupted"
                self.interrupts = [
                    {"node": t.name, "type": "before"} for t in next_tasks
                ]
                await self._wait_for_resume()

            # Execute node Activities (parallel where applicable)
            results = await self._execute_nodes(next_tasks, graph, input)

            # Process Child Workflow requests (e.g., sub-agent dispatch)
            await self._process_child_workflow_requests(results, input)

            # Handle interrupted nodes
            interrupted_results = [r for r in results if r.interrupts]
            if interrupted_results:
                self.status = "interrupted"
                self.interrupts = []
                for r in interrupted_results:
                    self.interrupts.extend(r.interrupts or [])
                await self._wait_for_resume()

                # Re-execute interrupted nodes with resume values
                non_interrupted = [r for r in results if not r.interrupts]
                resumed = await self._execute_resumed_nodes(
                    interrupted_results, graph, input
                )
                results = non_interrupted + resumed

            # Handle PUSH tasks (dynamic Send from nodes)
            push_sends = self._extract_push_sends(results)
            if push_sends:
                push_results = await self._execute_push_tasks(push_sends, graph, input)
                results.extend(push_results)

            # Handle Command.goto routing from node results
            goto_sends = self._extract_command_gotos(results)
            if goto_sends:
                goto_results = await self._execute_push_tasks(goto_sends, graph, input)
                results.extend(goto_results)

            # Wrap results as WritesProtocol-compatible objects.
            # Use the node's trigger channels from the graph definition
            # so that apply_writes updates versions_seen correctly.
            task_writes = [
                PregelTaskWrites(
                    path=tuple(r.task_path) if r.task_path else (r.node_name,),
                    name=r.node_name,
                    writes=list(r.writes),
                    triggers=list(
                        graph.nodes[r.node_name].triggers
                        if r.node_name in graph.nodes
                        else r.triggers
                    ),
                )
                for r in results
                if r.writes
            ]

            if task_writes:
                apply_writes(
                    self.checkpoint,
                    self.channels,
                    task_writes,
                    increment,
                    self._trigger_to_nodes,
                )

            # Emit stream events
            self._emit_stream_events(results)

            # interrupt_after check
            if input.interrupt_after and self._should_interrupt_nodes(
                next_tasks, input.interrupt_after
            ):
                self.status = "interrupted"
                self.interrupts = [
                    {"node": t.name, "type": "after"} for t in next_tasks
                ]
                await self._wait_for_resume()

            # Advance checkpoint versions so prepare_next_tasks
            # does not re-trigger nodes that have already run.
            self.checkpoint = create_checkpoint(
                self.checkpoint, self.channels, self.step
            )
            self.step += 1
            steps_in_this_run += 1
            pending_writes = []

            # Continue-as-new to prevent event history bloat
            if steps_in_this_run >= CONTINUE_AS_NEW_THRESHOLD:
                checkpoint_data = create_checkpoint(
                    self.checkpoint, self.channels, self.step
                )
                workflow.continue_as_new(
                    WorkflowInput(
                        graph_definition_ref=input.graph_definition_ref,
                        input_data=None,
                        recursion_limit=max_steps,
                        interrupt_before=input.interrupt_before,
                        interrupt_after=input.interrupt_after,
                        restored_state=RestoredState(
                            checkpoint=cast(Any, checkpoint_data),
                            step=self.step,
                            sticky_task_queue=self._sticky_task_queue,
                        ),
                        node_task_queues=input.node_task_queues,
                        node_activity_options=input.node_activity_options,
                        node_retry_policies=input.node_retry_policies,
                        sticky_task_queue=self._sticky_task_queue,
                        use_worker_affinity=input.use_worker_affinity,
                        subagent_config=self._subagent_config,
                    )
                )

        if self.status == "running":
            self.status = "done"

        return WorkflowOutput(
            channel_values=_get_channel_values(self.channels),
            step=self.step,
        )

    def _should_interrupt_nodes(
        self,
        tasks: list[PregelTask],
        interrupt_nodes: list[str] | All,
    ) -> bool:
        """Check if any tasks match the interrupt node list."""
        if interrupt_nodes == "*":
            return True
        return any(t.name in interrupt_nodes for t in tasks)

    def _task_queue_for_node(self, node_name: str) -> str | None:
        """Get the task queue for a node, respecting affinity configuration.

        Precedence:
        1. Sticky task queue (if configured) -- overrides everything for affinity
        2. Per-node task queue override (from node_task_queues config)
        3. None (use workflow's default task queue)
        """
        if self._sticky_task_queue:
            return self._sticky_task_queue
        if self._node_task_queues:
            return self._node_task_queues.get(node_name)
        return None

    def _activity_options_for_node(self, node_name: str) -> ActivityOptions:
        """Get Activity options for a node, falling back to defaults."""
        if self._node_activity_options:
            return self._node_activity_options.get(
                node_name,
                ActivityOptions(),
            )
        return ActivityOptions()

    def _retry_policy_for_node(self, node_name: str) -> TemporalRetryPolicy | None:
        """Get Temporal RetryPolicy for a node, if configured."""
        if not self._node_retry_policies:
            return None
        config = self._node_retry_policies.get(node_name)
        if config is None:
            return None
        return _to_temporal_retry_policy(config)

    async def _execute_single_activity(
        self,
        activity_input: NodeActivityInput,
        node_name: str,
    ) -> NodeActivityOutput:
        """Execute a single node Activity with per-node configuration.

        If worker affinity is enabled and the pinned worker is unavailable
        (schedule-to-close timeout), falls back to re-discovering a new
        worker via `get_available_task_queue` and retries once.
        """
        result = await self._dispatch_activity(activity_input, node_name)
        if result is not None:
            return result

        # Fallback: pinned worker timed out, re-discover and retry
        workflow.logger.warn(
            f"Activity '{node_name}' failed on sticky queue "
            f"'{self._sticky_task_queue}', re-discovering worker"
        )
        await self._rediscover_worker()
        result = await self._dispatch_activity(activity_input, node_name)
        if result is not None:
            return result

        # Both attempts failed — raise the original error
        raise RuntimeError(f"Activity '{node_name}' failed after worker re-discovery")

    async def _dispatch_activity(
        self,
        activity_input: NodeActivityInput,
        node_name: str,
    ) -> NodeActivityOutput | None:
        """Dispatch an Activity, returning None on schedule-to-close timeout
        when worker affinity is enabled (signals the pinned worker is gone)."""
        from temporalio.exceptions import ActivityError

        opts = self._activity_options_for_node(node_name)
        task_queue = self._task_queue_for_node(node_name)
        retry_policy = self._retry_policy_for_node(node_name)

        kwargs: dict[str, Any] = {
            "start_to_close_timeout": opts.start_to_close_timeout,
        }
        if opts.heartbeat_timeout is not None:
            kwargs["heartbeat_timeout"] = opts.heartbeat_timeout
        if opts.schedule_to_close_timeout is not None:
            kwargs["schedule_to_close_timeout"] = opts.schedule_to_close_timeout
        if task_queue is not None:
            kwargs["task_queue"] = task_queue
        if retry_policy is not None:
            kwargs["retry_policy"] = retry_policy

        try:
            return await workflow.execute_activity(
                f"{node_name}",
                activity_input,
                result_type=NodeActivityOutput,
                **kwargs,
            )
        except ActivityError:
            # Only fallback if worker affinity is active — the failure
            # likely means the pinned worker is gone.
            if self._use_worker_affinity and self._sticky_task_queue:
                return None
            raise

    async def _rediscover_worker(self) -> None:
        """Clear the sticky queue and discover a new worker.

        Called when the pinned worker appears to be unavailable. The
        Workflow re-runs `get_available_task_queue` on the shared queue
        to find a different worker.
        """
        from langgraph.temporal.activities import get_available_task_queue

        self._sticky_task_queue = None
        self._sticky_task_queue = await workflow.execute_activity(
            get_available_task_queue,
            start_to_close_timeout=timedelta(seconds=10),
        )

    def _is_subgraph_node(self, node_name: str, graph: Any) -> bool:
        """Check if a node is a subgraph (compiled Pregel) node."""
        node = graph.nodes.get(node_name)
        if node is None:
            return False
        bound = getattr(node, "bound", None)
        if bound is None:
            return False
        # Check if the bound runnable is itself a Pregel graph
        from langgraph.pregel import Pregel

        return isinstance(bound, Pregel)

    async def _execute_subgraph_as_child_workflow(
        self,
        task: PregelTask,
        graph: Any,
    ) -> NodeActivityOutput:
        """Execute a subgraph node as a Temporal Child Workflow.

        Maps LangGraph subgraph composition to Temporal's Child Workflow
        pattern, providing full durability for nested graph execution.
        """
        channel_values = _get_channel_values(self.channels)
        node = graph.nodes[task.name]
        subgraph = node.bound

        # Register the subgraph in the GraphRegistry
        sub_ref = GraphRegistry.get_instance().register(subgraph)

        parent_wf_id = workflow.info().workflow_id
        child_wf_id = f"{parent_wf_id}/{task.name}"

        child_input = WorkflowInput(
            graph_definition_ref=sub_ref,
            input_data=channel_values,
            recursion_limit=25,
        )

        try:
            result: WorkflowOutput = await workflow.execute_child_workflow(
                LangGraphWorkflow.run,
                child_input,
                id=child_wf_id,
            )

            # Convert child workflow output to NodeActivityOutput
            writes = [(k, v) for k, v in result.channel_values.items()]
            return NodeActivityOutput(
                node_name=task.name,
                writes=writes,
                triggers=[],
                task_path=cast(Any, task.path),
            )
        finally:
            GraphRegistry.get_instance().unregister(sub_ref)

    async def _execute_nodes(
        self,
        tasks: list[PregelTask],
        graph: Any,
        wf_input: WorkflowInput,
    ) -> list[NodeActivityOutput]:
        """Execute node Activities, possibly in parallel."""
        sorted_tasks = sorted(tasks, key=lambda t: t.path)
        channel_values = _get_channel_values(self.channels)

        # Separate subgraph nodes from regular nodes
        coroutines: list[Any] = []
        for t in sorted_tasks:
            if self._is_subgraph_node(t.name, graph):
                coroutines.append(self._execute_subgraph_as_child_workflow(t, graph))
            else:
                # The __start__ node's writers expect the raw user input dict
                # (the value from the __start__ channel), not the full channel
                # state.  Passing the full state would nest it under __start__
                # and the _get_updates mapper would fail to distribute values
                # to individual state channels.
                if t.name == "__start__":
                    node_state = channel_values.get("__start__", channel_values)
                else:
                    node_state = channel_values
                activity_input = NodeActivityInput(
                    node_name=t.name,
                    input_state=node_state,
                    graph_definition_ref=self._graph_ref,
                    resume_values=None,
                    task_path=cast(Any, t.path),
                    triggers=[],
                )
                coroutines.append(self._execute_single_activity(activity_input, t.name))

        if len(coroutines) == 1:
            return [await coroutines[0]]
        else:
            results = await asyncio.gather(*coroutines)
            return list(results)

    async def _execute_resumed_nodes(
        self,
        interrupted_results: list[NodeActivityOutput],
        graph: Any,
        wf_input: WorkflowInput,
    ) -> list[NodeActivityOutput]:
        """Re-execute interrupted nodes with resume values."""
        channel_values = _get_channel_values(self.channels)
        results = []

        for r in interrupted_results:
            activity_input = NodeActivityInput(
                node_name=r.node_name,
                input_state=channel_values,
                graph_definition_ref=self._graph_ref,
                resume_values=self.resume_values.copy(),
                task_path=r.task_path,
                triggers=list(r.triggers),
            )
            result = await self._execute_single_activity(activity_input, r.node_name)
            results.append(result)

        return results

    async def _execute_push_tasks(
        self,
        push_sends: list[dict[str, Any]],
        graph: Any,
        wf_input: WorkflowInput,
    ) -> list[NodeActivityOutput]:
        """Execute dynamic Send (PUSH) tasks."""
        channel_values = _get_channel_values(self.channels)

        activity_inputs = [
            NodeActivityInput(
                node_name=send["node"],
                input_state=channel_values,
                graph_definition_ref=self._graph_ref,
                send_input=send.get("arg"),
                task_path=(send["node"], "push"),
            )
            for send in push_sends
        ]

        if len(activity_inputs) == 1:
            result = await self._execute_single_activity(
                activity_inputs[0], activity_inputs[0].node_name
            )
            return [result]
        else:
            results = await asyncio.gather(
                *[
                    self._execute_single_activity(ai, ai.node_name)
                    for ai in activity_inputs
                ]
            )
            return list(results)

    def _extract_push_sends(
        self, results: list[NodeActivityOutput]
    ) -> list[dict[str, Any]]:
        """Extract dynamic Send (PUSH) tasks from Activity results."""
        push_sends: list[dict[str, Any]] = []
        for result in results:
            if result.push_sends:
                push_sends.extend(result.push_sends)
        return push_sends

    def _extract_command_gotos(
        self, results: list[NodeActivityOutput]
    ) -> list[dict[str, Any]]:
        """Extract Command.goto targets from Activity results.

        When a node returns Command(goto=...), the goto targets should be
        scheduled as additional tasks in the same superstep.
        """
        goto_sends: list[dict[str, Any]] = []
        for result in results:
            if result.command and result.command.get("goto"):
                goto = result.command["goto"]
                goto_list = goto if isinstance(goto, list) else [goto]
                for target in goto_list:
                    if isinstance(target, dict) and "node" in target:
                        # Send-style goto: {"node": "name", "arg": value}
                        goto_sends.append(target)
                    elif isinstance(target, str):
                        # Simple node name goto
                        goto_sends.append({"node": target, "arg": None})
        return goto_sends

    async def _process_child_workflow_requests(
        self,
        results: list[NodeActivityOutput],
        wf_input: WorkflowInput,
    ) -> None:
        """Process Child Workflow requests from Activity results.

        Iterates over results, dispatches Child Workflows for any
        `child_workflow_requests`, and injects result/error writes back
        into the originating result's writes list.
        """
        for result in results:
            if not result.child_workflow_requests:
                continue

            # Dispatch all child workflows concurrently
            child_outputs = await asyncio.gather(
                *[
                    self._dispatch_child_workflow(req, wf_input, idx)
                    for idx, req in enumerate(result.child_workflow_requests)
                ],
                return_exceptions=True,
            )

            # Inject results back as writes
            for req, child_output in zip(
                result.child_workflow_requests, child_outputs, strict=True
            ):
                tool_call_id = req.get("tool_call_id", "")
                if isinstance(child_output, BaseException):
                    subagent_type = req.get("subagent_type", "unknown")
                    content = f"Sub-agent '{subagent_type}' failed: {child_output}"
                else:
                    # Extract last message from child workflow output
                    messages = child_output.channel_values.get("messages", [])
                    if messages:
                        last_msg = messages[-1]
                        content = (
                            last_msg if isinstance(last_msg, str) else str(last_msg)
                        )
                    else:
                        content = ""

                result.writes.append(
                    (
                        "messages",
                        {
                            "type": "tool",
                            "content": content,
                            "tool_call_id": tool_call_id,
                        },
                    )
                )

    async def _dispatch_child_workflow(
        self,
        req: dict[str, Any],
        wf_input: WorkflowInput,
        index: int,
    ) -> WorkflowOutput:
        """Dispatch a single Child Workflow for a sub-agent request."""
        subagent_type = req.get("subagent_type", "unknown")
        graph_ref = req.get("graph_definition_ref", wf_input.graph_definition_ref)
        initial_state = req.get("initial_state", {})

        parent_wf_id = workflow.info().workflow_id
        child_wf_id = f"{parent_wf_id}/subagent/{subagent_type}/{self.step}_{index}"

        # Determine task queue and timeout from subagent config
        task_queue = workflow.info().task_queue
        execution_timeout = timedelta(minutes=30)
        child_sticky_queue: str | None = None

        if self._subagent_config:
            if self._subagent_config.task_queue:
                task_queue = self._subagent_config.task_queue
            if self._subagent_config.execution_timeout_seconds:
                execution_timeout = timedelta(
                    seconds=self._subagent_config.execution_timeout_seconds
                )
            child_sticky_queue = self._subagent_config.sticky_task_queue

        child_input = WorkflowInput(
            graph_definition_ref=graph_ref,
            input_data=initial_state,
            recursion_limit=wf_input.recursion_limit,
            sticky_task_queue=child_sticky_queue,
        )

        result: WorkflowOutput = await workflow.execute_child_workflow(
            LangGraphWorkflow.run,
            child_input,
            id=child_wf_id,
            task_queue=task_queue,
            execution_timeout=execution_timeout,
            parent_close_policy=ParentClosePolicy.TERMINATE,
        )
        return result

    async def _resolve_conditional_edges(
        self,
        tasks: list[PregelTask],
        graph: Any,
    ) -> list[PregelTask]:
        """Evaluate conditional edge functions as Local Activities.

        For nodes that are sources of conditional edges, evaluate the
        conditional function to determine actual targets. Local Activities
        run on the same worker as the Workflow for minimal latency.
        """
        # Check if graph has conditional edge sources
        if not hasattr(graph, "branches") or not graph.branches:
            return tasks

        resolved: list[PregelTask] = []
        channel_values = _get_channel_values(self.channels)

        for task in tasks:
            # Check if this node has conditional edges
            if task.name in graph.branches:
                result = await workflow.execute_local_activity(
                    evaluate_conditional_edge,
                    ConditionalEdgeInput(
                        source_node=task.name,
                        graph_definition_ref=self._graph_ref,
                        channel_state=channel_values,
                    ),
                    start_to_close_timeout=timedelta(seconds=10),
                )
                if result is not None:
                    # Conditional edge resolved; task scheduling is already
                    # handled by prepare_next_tasks via trigger_to_nodes
                    resolved.append(task)
                else:
                    resolved.append(task)
            else:
                resolved.append(task)

        return resolved

    def _emit_stream_events(self, results: list[NodeActivityOutput]) -> None:
        """Emit stream events from Activity results."""
        channel_values = _get_channel_values(self.channels)
        # Emit "values" mode: full state after step
        self.stream_buffer.append(
            StreamEvent(
                mode="values",
                data=channel_values,
                step=self.step,
            )
        )
        # Emit "updates" mode: per-node updates
        for result in results:
            if result.writes:
                self.stream_buffer.append(
                    StreamEvent(
                        mode="updates",
                        data=dict(result.writes),
                        node_name=result.node_name,
                        step=self.step,
                    )
                )
            # Emit "custom" mode: custom data from StreamWriter
            if result.custom_data:
                for data in result.custom_data:
                    self.stream_buffer.append(
                        StreamEvent(
                            mode="custom",
                            data=data,
                            node_name=result.node_name,
                            step=self.step,
                        )
                    )

    async def _wait_for_resume(self) -> None:
        """Wait for a resume Signal (zero resource consumption)."""
        self._resume_event = asyncio.Event()
        self.resume_values = []
        await workflow.wait_condition(lambda: self._resume_event.is_set())
        self.status = "running"

    @workflow.signal
    async def resume_signal(self, value: Any) -> None:
        """Signal handler for human-in-the-loop resume."""
        self.resume_values.append(value)
        self._resume_event.set()

    @workflow.signal
    async def update_state_signal(self, update: StateUpdatePayload) -> None:
        """Signal handler for external state updates.

        Uses `apply_writes` from `_algo.py` to maintain correct channel
        lifecycle semantics.
        """
        task_writes = PregelTaskWrites(
            path=("__update__",),
            name="__update__",
            writes=list(update.writes),
            triggers=[],
        )
        apply_writes(
            self.checkpoint,
            self.channels,
            [task_writes],
            increment,
            self._trigger_to_nodes,
        )

    @workflow.query
    def get_current_state(self) -> StateQueryResult:
        """Query handler for retrieving current Workflow state."""
        return StateQueryResult(
            channel_values=_get_channel_values(self.channels),
            channel_versions=dict(self.checkpoint.get("channel_versions", {})),
            versions_seen=dict(self.checkpoint.get("versions_seen", {})),
            step=self.step,
            status=self.status,
            interrupts=self.interrupts,
        )

    @workflow.query
    def get_stream_buffer(self, cursor: int = 0) -> StreamQueryResult:
        """Query handler for polling stream events with cursor-based pagination."""
        events = self.stream_buffer[cursor:]
        return StreamQueryResult(
            events=events,
            next_cursor=len(self.stream_buffer),
        )
