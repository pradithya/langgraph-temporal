# langgraph-temporal: Technical Design Document

## 1. Architecture Overview

```
+------------------------------------------------------------------+
|                         Client Application                        |
|                                                                   |
|  graph = StateGraph(State)                                        |
|  graph.add_node("agent", agent_fn)                                |
|  graph.add_node("tools", ToolNode(tools))                         |
|  compiled = graph.compile()                                       |
|                                                                   |
|  temporal_graph = TemporalGraph(compiled, temporal_client, ...)   |
|  result = temporal_graph.invoke(input, config)                    |
+--------------------+---------------------------------------------+
                     |
                     | Temporal Client: start_workflow / signal / query
                     v
+------------------------------------------------------------------+
|                      Temporal Server                              |
|  +------------------------------------------------------------+  |
|  |  Workflow: LangGraphWorkflow                                |  |
|  |                                                             |  |
|  |  1. Deserialize input & initialize channels                 |  |
|  |  2. Loop:                                                   |  |
|  |     a. Determine next nodes (prepare_next_tasks logic)      |  |
|  |     b. Check interrupt_before -> wait_for_signal()          |  |
|  |     c. Execute node Activities (parallel if applicable)     |  |
|  |     d. Apply writes to channel state                        |  |
|  |     e. Check interrupt_after -> wait_for_signal()           |  |
|  |     f. Emit stream events                                   |  |
|  |  3. Return final state                                      |  |
|  +------------------------------------------------------------+  |
|                                                                   |
|  +--------------------+   +--------------------+                  |
|  | Activity:          |   | Activity:          |                  |
|  | execute_node       |   | execute_node       |                  |
|  | ("agent")          |   | ("tools")          |                  |
|  |                    |   |                    |                  |
|  | - Deserialize      |   | - Deserialize      |                  |
|  |   input state      |   |   input state      |                  |
|  | - Run node fn      |   | - Run node fn      |                  |
|  | - Serialize        |   | - Serialize        |                  |
|  |   output writes    |   |   output writes    |                  |
|  +--------------------+   +--------------------+                  |
|                                                                   |
|  +------------------------------------------------------------+  |
|  | Child Workflow: LangGraphSubgraphWorkflow                   |  |
|  | (for subgraph nodes)                                        |  |
|  +------------------------------------------------------------+  |
+------------------------------------------------------------------+
                     |
                     | Task Queue(s)
                     v
+------------------------------------------------------------------+
|                      Temporal Worker(s)                           |
|                                                                   |
|  worker = TemporalGraph.create_worker(                           |
|      compiled_graph,                                              |
|      task_queue="langgraph-default",                              |
|  )                                                                |
|  worker.run()                                                     |
+------------------------------------------------------------------+
```

## 2. Concept Mapping

| LangGraph Concept | Temporal Concept | Notes |
|---|---|---|
| `StateGraph` / `CompiledStateGraph` | Workflow Definition | Graph topology becomes Workflow control flow |
| `Pregel` execution loop | Workflow function body | The `tick()` / `after_tick()` loop becomes a `while` loop in the Workflow |
| Node (callable) | Activity | Non-deterministic work dispatched to workers |
| `PregelExecutableTask` | Activity Execution | One invocation of a node |
| Channel state (`channel_values`) | Workflow local variables | Carried in Workflow memory, updated after Activities |
| `BaseCheckpointSaver` | Temporal Event History | Implicit via replay; explicit via `TemporalCheckpointSaver` |
| `checkpoint["id"]` | Workflow Event ID / custom marker | Monotonic ordering preserved |
| `interrupt()` | `workflow.wait_condition()` + Signal | Zero-resource blocking |
| `Command(resume=...)` | Signal with payload | Client sends Signal to resume |
| `RetryPolicy` | `temporalio.common.RetryPolicy` | Direct mapping of parameters |
| `CachePolicy` | Activity result caching (Temporal memoization) | Activity results are inherently cached by replay |
| Subgraph | Child Workflow | `workflow.execute_child_workflow()` |
| `checkpoint_ns` (namespace) | Child Workflow ID prefix | `parent_wf_id/subgraph_name` |
| `Send` (map-reduce) | Multiple Activity executions or `asyncio.gather` | Fan-out pattern in Workflow; dynamic `Send` from nodes requires PUSH task support |
| `Command` (from nodes) | Activity output with command metadata | Must capture `goto`, `update`, `graph=PARENT` semantics |
| `Overwrite` | Serialized as tagged type in Activity output | Bypasses reducers; must survive Activity boundary |
| `UntrackedValue` | Excluded from Activity output serialization | Must never appear in Temporal event history |
| `CONFIG_KEY_SCRATCHPAD` | Reconstructed per-Activity invocation | Required for `interrupt()` counter and resume value matching |
| `StreamWriter` / stream modes | Side-channel (Redis/SSE) or Temporal Query | See Streaming section |
| `recursion_limit` | Workflow `run_timeout` + step counter | Prevent infinite loops |
| `step_timeout` | Activity `start_to_close_timeout` | Per-step timeout |
| `thread_id` | Workflow ID | 1:1 mapping |
| `run_id` | Workflow Run ID | Temporal provides this automatically |
| `Durability` | N/A (always durable) | Temporal provides full durability by default |

## 3. Component Design

### 3.1 Package Structure

```
libs/langgraph-temporal/
  langgraph/temporal/
    __init__.py            # Public API exports
    graph.py               # TemporalGraph wrapper (invoke, stream, get_state, update_state, resume, local)
    workflow.py            # LangGraphWorkflow definition (uses PregelTaskWrites, not TaskWritesAdapter)
    activities.py          # Node Activity wrappers + conditional edge Local Activities
    checkpoint.py          # TemporalCheckpointSaver
    streaming.py           # Streaming side-channel (PollingStreamBackend)
    converter.py           # GraphRegistry + graph metadata extraction
    worker.py              # Worker creation helpers
    config.py              # Configuration dataclasses (ActivityOptions, RetryPolicyConfig, WorkflowInput, etc.)
    tools.py               # activity_as_tool helper
    encryption.py          # EncryptionCodec (AES-GCM) + FernetEncryptionCodec for PII workloads
    metrics.py             # MetricsReporter with OpenTelemetry integration
    _codec.py              # LargePayloadCodec for >2MB payloads (BlobStore abstraction)
    _serde.py              # Serialization helpers (Overwrite, UntrackedValue handling)
```

**Note:** The following files from the original design were intentionally merged:
- `state.py` → Functionality covered by `_serde.py` and inline in `workflow.py` using `channels_from_checkpoint`
- `channels.py` → Functionality covered by `workflow.py` using actual LangGraph channel objects
- `signals.py` → Signal/Query handlers are inlined in `workflow.py`
- `_scratchpad.py` → PregelScratchpad reconstruction is inlined in `activities.py`

### 3.2 TemporalGraph (Primary Entry Point)

`TemporalGraph` is the user-facing wrapper that makes a compiled LangGraph
graph executable on Temporal. It preserves the `invoke()` / `stream()` API.

```python
# langgraph_temporal/graph.py

from temporalio.client import Client as TemporalClient
from langgraph.pregel.main import Pregel
from langgraph.types import StreamMode, All, Command

class TemporalGraph:
    """Wraps a compiled LangGraph graph for execution on Temporal."""

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
    ):
        self.graph = graph
        self.client = client
        self.task_queue = task_queue
        self.node_task_queues = node_task_queues or {}
        self.node_activity_options = node_activity_options or {}
        self.workflow_execution_timeout = workflow_execution_timeout
        self.workflow_run_timeout = workflow_run_timeout
        self.stream_backend = stream_backend

    def invoke(
        self,
        input: Any,
        config: RunnableConfig | None = None,
        *,
        stream_mode: StreamMode = "values",
        interrupt_before: All | Sequence[str] | None = None,
        interrupt_after: All | Sequence[str] | None = None,
        **kwargs,
    ) -> dict[str, Any] | Any:
        """Execute the graph as a Temporal Workflow and return the result."""
        ...

    def stream(
        self,
        input: Any,
        config: RunnableConfig | None = None,
        *,
        stream_mode: StreamMode | Sequence[StreamMode] | None = None,
        **kwargs,
    ) -> Iterator[Any]:
        """Stream graph execution events from a Temporal Workflow."""
        ...

    async def astart(
        self,
        input: Any,
        config: RunnableConfig | None = None,
    ) -> WorkflowHandle:
        """Start a Workflow without waiting for completion."""
        ...

    def get_state(self, config: RunnableConfig) -> StateSnapshot:
        """Query the Workflow for current state."""
        ...

    def resume(self, config: RunnableConfig, value: Any) -> None:
        """Send a resume Signal to a paused Workflow."""
        ...

    def create_worker(self, **kwargs) -> Worker:
        """Create a Temporal Worker configured for this graph."""
        ...
```

### 3.3 LangGraphWorkflow (Temporal Workflow)

The Workflow is the core of the integration. It replaces the `PregelLoop` as
the execution orchestrator. The Workflow must be **deterministic** -- all
non-deterministic operations are delegated to Activities.

**Critical design decision**: Rather than reimplementing channel update logic,
the Workflow reuses LangGraph's actual channel objects and core algorithms.
Channel classes (`LastValue`, `BinaryOperatorAggregate`, `Topic`, etc.) are
purely in-memory with no I/O and are safe for Temporal Workflow determinism.
The functions `prepare_next_tasks` and `apply_writes` from `_algo.py` are
also pure in-memory logic, operating solely on channel objects and dicts.

The only non-deterministic operations that must run as Activities are:
1. Node function execution (LLM calls, tool invocations, I/O)
2. Conditional edge function evaluation (user-defined callables that may
   depend on external state)

```python
# langgraph_temporal/workflow.py

from temporalio import workflow
from temporalio.workflow import SignalMethod, QueryMethod

# These imports are safe for Temporal Workflow determinism -- they are
# pure in-memory logic with no I/O side effects.
with workflow.unsafe.imports_passed_through():
    from langgraph.pregel._algo import (
        prepare_next_tasks,
        apply_writes,
        should_interrupt,
        WritesProtocol,
    )
    from langgraph.channels.base import BaseChannel
    from langgraph.checkpoint.base import Checkpoint, empty_checkpoint
    from langgraph.pregel._read import PregelNode
    from langgraph.managed.base import ManagedValueMapping
    from langgraph.types import PregelTask, PregelExecutableTask, Send


CONTINUE_AS_NEW_THRESHOLD = 500  # steps before continue-as-new


# NOTE: The actual implementation uses PregelTaskWrites from
# langgraph.pregel._algo instead of a custom TaskWritesAdapter.
# PregelTaskWrites already conforms to WritesProtocol with the
# required path, name, writes, and triggers properties.


@workflow.defn
class LangGraphWorkflow:
    """Temporal Workflow that executes a LangGraph graph."""

    def __init__(self):
        self.channels: dict[str, BaseChannel] = {}
        self.checkpoint: Checkpoint = empty_checkpoint()
        self.step: int = 0
        self.status: str = "pending"
        self.interrupts: list[dict] = []
        self.resume_values: list[Any] = []
        self.pending_resume_for_node: str | None = None
        self.stream_buffer: list[tuple] = []
        self._resume_event = workflow.Event()
        self._graph_def = None  # Set in run()
        self._processes: dict[str, PregelNode] = {}
        self._managed: ManagedValueMapping = {}
        self._trigger_to_nodes: dict[str, list[str]] = {}

    @workflow.run
    async def run(self, input: WorkflowInput) -> WorkflowOutput:
        """Main Workflow execution loop.

        Mirrors the PregelLoop.tick() / after_tick() cycle using the
        ACTUAL function signatures from langgraph.pregel._algo:

        - prepare_next_tasks(checkpoint, pending_writes, processes,
            channels, managed, config, step, stop, *, for_execution)
        - apply_writes(checkpoint, channels, tasks, get_next_version,
            trigger_to_nodes)
        - should_interrupt(checkpoint, interrupt_nodes, tasks)
        """
        # Store graph_def as instance variable for signal/query handlers
        self._graph_def = input.graph_definition
        graph_def = self._graph_def

        # Reconstruct PregelNode process mapping and trigger_to_nodes
        # from the graph definition (these are deterministic, in-memory)
        self._processes = graph_def.build_processes()
        self._trigger_to_nodes = graph_def.trigger_to_nodes
        self._managed = graph_def.managed or {}

        # Initialize or restore channels and checkpoint
        if input.restored_state:
            self.channels = restore_channels(input.restored_state, graph_def)
            self.checkpoint = input.restored_state.checkpoint
            self.step = input.restored_state.step
        else:
            self.channels = initialize_channels(input.input_data, graph_def)
            self.checkpoint = empty_checkpoint()
            # Apply initial input writes to the checkpoint
            self.checkpoint["channel_versions"] = {
                name: 1 for name in self.channels
            }
            self.step = 0

        max_steps = input.recursion_limit
        steps_in_this_run = 0
        pending_writes: list[tuple[str, str, Any]] = []

        # Build a minimal RunnableConfig for prepare_next_tasks
        config: RunnableConfig = {
            "configurable": {
                "thread_id": workflow.info().workflow_id,
            }
        }

        # Main loop (equivalent to PregelLoop)
        while self.step < max_steps:
            # prepare_next_tasks signature:
            # (checkpoint, pending_writes, processes, channels, managed,
            #  config, step, stop, *, for_execution, trigger_to_nodes, ...)
            #
            # With for_execution=False, returns dict[str, PregelTask]
            # which gives us node names and trigger info without
            # constructing full executable tasks (no config/store needed).
            next_task_dict = prepare_next_tasks(
                self.checkpoint,
                pending_writes,
                self._processes,
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

            next_tasks = list(next_task_dict.values())

            # Evaluate conditional edges as Local Activities
            # (user-defined callables may be non-deterministic)
            next_tasks = await self._resolve_conditional_edges(
                next_tasks, graph_def
            )

            if not next_tasks:
                self.status = "done"
                break

            # interrupt_before check
            # should_interrupt signature: (checkpoint, interrupt_nodes, tasks)
            # Note: should_interrupt expects PregelExecutableTask, but we
            # only check node names against interrupt_nodes, so we use a
            # simplified check here instead.
            if input.interrupt_before and self._should_interrupt_nodes(
                next_tasks, input.interrupt_before
            ):
                self.status = "interrupt_before"
                self.interrupts = [
                    {"node": t.name, "type": "before"} for t in next_tasks
                ]
                await self._wait_for_resume()

            # Execute node Activities (parallel where applicable)
            results = await self._execute_nodes(next_tasks, graph_def)

            # Handle interrupted nodes -- do NOT apply writes or advance
            interrupted_results = [r for r in results if r.interrupts]
            if interrupted_results:
                self.status = "interrupted"
                self.interrupts = []
                for r in interrupted_results:
                    self.interrupts.extend(r.interrupts)
                    self.pending_resume_for_node = r.node_name
                await self._wait_for_resume()
                resume_result = await self._execute_node_with_resume(
                    interrupted_results[0], graph_def
                )
                results = [
                    resume_result if r.node_name == resume_result.node_name
                    else r
                    for r in results
                    if not r.interrupts
                ] + [resume_result]

            # Handle PUSH tasks (dynamic Send from nodes)
            push_tasks = self._extract_push_tasks(results)
            if push_tasks:
                push_results = await self._execute_nodes(push_tasks, graph_def)
                results.extend(push_results)

            # Wrap results as WritesProtocol-compatible objects
            task_writes = [
                PregelTaskWrites(
                    path=r.task_path,
                    name=r.node_name,
                    writes=r.writes,
                    triggers=r.triggers or [],
                )
                for r in results
            ]

            # apply_writes signature:
            # (checkpoint, channels, tasks, get_next_version, trigger_to_nodes)
            # Returns: set[str] of updated channel names
            updated_channels = apply_writes(
                self.checkpoint,
                self.channels,
                task_writes,
                increment,  # get_next_version: simple int increment
                self._trigger_to_nodes,
            )

            # Emit stream events
            self._emit_stream_events(results, graph_def)

            # interrupt_after check
            if input.interrupt_after and self._should_interrupt_nodes(
                next_tasks, input.interrupt_after
            ):
                self.status = "interrupt_after"
                self.interrupts = [
                    {"node": t.name, "type": "after"} for t in next_tasks
                ]
                await self._wait_for_resume()

            self.step += 1
            steps_in_this_run += 1
            pending_writes = []  # Clear for next step

            # Continue-as-new to prevent event history bloat
            if steps_in_this_run >= CONTINUE_AS_NEW_THRESHOLD:
                workflow.continue_as_new(
                    WorkflowInput(
                        graph_definition=graph_def,
                        input_data=None,
                        recursion_limit=max_steps,
                        interrupt_before=input.interrupt_before,
                        interrupt_after=input.interrupt_after,
                        restored_state=RestoredState(
                            channels=serialize_channels(self.channels),
                            checkpoint=self.checkpoint,
                            step=self.step,
                        ),
                    )
                )

        return WorkflowOutput(
            channel_state=get_channel_values(self.channels),
            step=self.step,
        )

    def _should_interrupt_nodes(
        self, tasks: list[PregelTask], interrupt_nodes: list[str] | All
    ) -> bool:
        """Simplified interrupt check based on node names.

        We avoid calling should_interrupt() directly because it expects
        PregelExecutableTask (for_execution=True), which requires
        constructing full configs. Instead, we replicate the core logic:
        check if any task name is in the interrupt_nodes list and if
        any channels have been updated since the last interrupt.
        """
        if interrupt_nodes == "*":
            return True
        return any(t.name in interrupt_nodes for t in tasks)

    async def _resolve_conditional_edges(self, tasks, graph_def):
        """Evaluate conditional edge functions as Local Activities.

        **How conditional edges interact with prepare_next_tasks:**

        In LangGraph's compiled graph, conditional edges are resolved
        during compilation into `branches` entries that map source nodes
        to target nodes via callable predicates. The compiled graph's
        `trigger_to_nodes` mapping already encodes the static edges.
        However, the *conditional* part (which specific target to route
        to) requires evaluating user-defined callables at runtime.

        In the Temporal integration, `prepare_next_tasks` determines
        which nodes CAN fire based on channel triggers and versions_seen.
        But for conditional edges, the actual target node depends on
        the conditional function's result. We evaluate these as Local
        Activities AFTER prepare_next_tasks returns the set of possible
        next tasks.

        Specifically:
        1. prepare_next_tasks returns tasks for nodes whose triggers
           have new channel versions
        2. For nodes that are the SOURCE of conditional edges, we
           evaluate the conditional function to determine the actual
           target(s)
        3. The resolved targets replace the original task entries

        Local Activities run on the same worker as the Workflow, avoiding
        the full task queue round-trip. Typical latency: <5ms (vs 20-50ms
        for regular Activities). Note: Local Activities have a hard
        80-second timeout and cannot heartbeat.
        """
        if not graph_def.conditional_edge_sources:
            return tasks

        resolved = []
        for task in tasks:
            if task.name in graph_def.conditional_edge_sources:
                node_writes = self._get_node_writes_for_local_read(task)
                next_nodes = await workflow.execute_local_activity(
                    evaluate_conditional_edge,
                    args=[ConditionalEdgeInput(
                        source_node=task.name,
                        edge_refs=graph_def.conditional_edge_sources[task.name],
                        channel_state=get_channel_values(self.channels),
                        node_writes=node_writes,
                        graph_definition_ref=graph_def.ref,
                    )],
                    start_to_close_timeout=timedelta(seconds=10),
                )
                # next_nodes may be:
                # - str: single target node name
                # - list[str]: multiple target nodes (fan-out)
                # - list[Send]: dynamic Send objects
                # - END: terminal
                if isinstance(next_nodes, list):
                    for target in next_nodes:
                        if isinstance(target, Send):
                            # Dynamic Send from conditional edge
                            resolved.append(PushTask(
                                name=target.node, input_override=target.arg
                            ))
                        elif target != END:
                            resolved.append(task._replace(name=target))
                elif next_nodes and next_nodes != END:
                    resolved.append(task._replace(name=next_nodes))
            else:
                resolved.append(task)
        return resolved

    async def _execute_nodes(self, tasks, graph_def):
        """Execute node Activities, possibly in parallel.

        Tasks are sorted deterministically to match LangGraph's internal
        ordering from prepare_next_tasks, ensuring consistent write
        application order for parallel nodes.
        """
        # Sort tasks to ensure deterministic ordering across replays
        sorted_tasks = sorted(tasks, key=lambda t: t.path)

        if len(sorted_tasks) == 1:
            task = sorted_tasks[0]
            return [await workflow.execute_activity(
                execute_node,
                args=[NodeActivityInput(
                    node_name=task.name,
                    input_state=self._read_channels_for_node(task, graph_def),
                    graph_definition_ref=graph_def.ref,
                    resume_values=None,
                    task_path=task.path,
                )],
                task_queue=self._task_queue_for_node(task.name),
                retry_policy=self._retry_policy_for_node(task.name, graph_def),
                start_to_close_timeout=self._timeout_for_node(task.name),
            )]
        else:
            # Parallel execution with asyncio.gather (order preserved)
            return list(await asyncio.gather(*[
                workflow.execute_activity(
                    execute_node,
                    args=[NodeActivityInput(
                        node_name=t.name,
                        input_state=self._read_channels_for_node(t, graph_def),
                        graph_definition_ref=graph_def.ref,
                        resume_values=None,
                        task_path=t.path,
                    )],
                    task_queue=self._task_queue_for_node(t.name),
                    retry_policy=self._retry_policy_for_node(t.name, graph_def),
                    start_to_close_timeout=self._timeout_for_node(t.name),
                )
                for t in sorted_tasks
            ]))

    async def _execute_node_with_resume(self, interrupted_result, graph_def):
        """Re-execute an interrupted node with resume values injected."""
        return await workflow.execute_activity(
            execute_node,
            args=[NodeActivityInput(
                node_name=interrupted_result.node_name,
                input_state=self._read_channels_for_node_by_name(
                    interrupted_result.node_name, graph_def
                ),
                graph_definition_ref=graph_def.ref,
                resume_values=self.resume_values.copy(),
                task_path=interrupted_result.task_path,
            )],
            task_queue=self._task_queue_for_node(interrupted_result.node_name),
            retry_policy=self._retry_policy_for_node(
                interrupted_result.node_name, graph_def
            ),
            start_to_close_timeout=self._timeout_for_node(
                interrupted_result.node_name
            ),
        )

    async def _wait_for_resume(self):
        """Wait for a resume Signal (zero resource consumption)."""
        self._resume_event = workflow.Event()
        self.resume_values = []  # Clear for new resume cycle
        await workflow.wait_condition(lambda: self._resume_event.is_set())
        self.status = "running"

    def _extract_push_tasks(self, results):
        """Extract dynamic Send (PUSH) tasks from Activity results.

        When a node emits Send objects during execution (via CONFIG_KEY_SEND),
        these become new Activities to execute in the same superstep.
        """
        push_tasks = []
        for result in results:
            if result.push_sends:
                for send in result.push_sends:
                    push_tasks.append(PushTask(
                        name=send.node,
                        input_override=send.arg,
                    ))
        return push_tasks

    @workflow.signal
    async def resume_signal(self, value: Any):
        """Signal handler for human-in-the-loop resume."""
        self.resume_values.append(value)
        self._resume_event.set()

    @workflow.signal
    async def update_state_signal(self, update: StateUpdatePayload):
        """Signal handler for external state updates.

        Uses apply_writes from _algo.py to maintain correct channel
        lifecycle semantics (versions_seen, consume, etc.).
        """
        task_writes = PregelTaskWrites(
            path=("__update__",),
            name="__update__",
            writes=update.writes,
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
            channel_state=get_channel_values(self.channels),
            channel_versions=self.channel_versions,
            step=self.step,
            status=self.status,
            interrupts=self.interrupts,
        )

    @workflow.query
    def get_stream_buffer(self, cursor: int = 0) -> StreamQueryResult:
        """Query handler for polling stream events with cursor-based pagination.

        Returns events starting from the given cursor position.
        """
        events = self.stream_buffer[cursor:]
        return StreamQueryResult(
            events=events,
            next_cursor=len(self.stream_buffer),
        )
```

**Key design decisions in the Workflow:**

1. **Reuse actual LangGraph algorithms with correct signatures**:
   - `prepare_next_tasks(checkpoint, pending_writes, processes, channels,
     managed, config, step, stop, *, for_execution, trigger_to_nodes)` --
     called with `for_execution=False` to get `PregelTask` objects (node
     names and trigger info without full config construction).
   - `apply_writes(checkpoint, channels, tasks, get_next_version,
     trigger_to_nodes)` -- called with a `Checkpoint` TypedDict, actual
     channel objects, `PregelTaskWrites` objects conforming to
     `WritesProtocol`, and the graph's `trigger_to_nodes` mapping.
   - Both are imported via `workflow.unsafe.imports_passed_through()` and
     are pure in-memory logic with no I/O.

2. **`PregelTaskWrites` for `WritesProtocol` conformance**: Activity results
   (`NodeActivityOutput`) are wrapped in `PregelTaskWrites` dataclasses that
   provide the `path`, `name`, `writes`, and `triggers` properties required
   by `apply_writes`. This bridges Temporal's serialized Activity outputs with
   LangGraph's internal protocol.

3. **Simplified interrupt check**: Rather than calling `should_interrupt()`
   (which expects `PregelExecutableTask` from `for_execution=True`), the
   Workflow uses `_should_interrupt_nodes()` that checks node names against
   the interrupt list. This avoids constructing full executable tasks with
   configs, stores, and managers just for the interrupt check.

4. **`Checkpoint` TypedDict maintained in Workflow**: The Workflow maintains a
   proper `Checkpoint` TypedDict (via `empty_checkpoint()`) with
   `channel_versions`, `versions_seen`, and `updated_channels` fields.
   This is updated by `apply_writes` and used by `prepare_next_tasks`.

5. **Conditional edges as Local Activities**: User-defined conditional edge
   functions may be non-deterministic. They are evaluated as Local Activities
   (not regular Activities) for minimal latency (<5ms vs 20-50ms). Note:
   Local Activities have a hard 80-second timeout and cannot heartbeat.
   For conditional edge functions requiring longer execution, provide an
   escape hatch to run as regular Activities.

6. **Deterministic task ordering**: Tasks are sorted by `path` before
   execution to ensure consistent write application order across replays.
   `apply_writes` also sorts internally by `task_path_str(t.path[:3])`.

7. **Interrupt re-execution**: When a node calls `interrupt()`, the Activity
   returns the interrupt payload without applying writes. After resume, the
   node is re-executed with resume values injected into the `PregelScratchpad`.

8. **Continue-As-New**: After `CONTINUE_AS_NEW_THRESHOLD` steps, the Workflow
   serializes its full state (including `Checkpoint` TypedDict) and continues
   as a new Workflow run, preventing event history from exceeding Temporal's
   50,000-event limit.

9. **Stream buffer pagination**: `get_stream_buffer` uses cursor-based
   pagination, allowing clients to track their read position without
   requiring the Workflow to manage buffer cleanup.

10. **`ManagedValueMapping` support**: Managed values are passed to
    `prepare_next_tasks`. In v0.1, managed values requiring external state
    (like `SharedValue`) are not fully supported; the library raises a
    clear error if unsupported managed values are detected. Simple managed
    values (like `IsLastStep`) that only need scratchpad access are supported.

### 3.4 Node Activity

Activities are the bridge between Temporal's durable execution and LangGraph's
node functions.

```python
# langgraph_temporal/activities.py

from temporalio import activity
from dataclasses import dataclass, field

@dataclass
class NodeActivityInput:
    node_name: str
    input_state: dict[str, Any]
    graph_definition_ref: str  # Reference to retrieve graph definition
    resume_values: list[Any] | None = None  # For interrupt resume
    task_path: tuple[str, ...] = ()  # For deterministic ordering
    send_input: Any | None = None  # For dynamic Send (PUSH) tasks

@dataclass
class NodeActivityOutput:
    """Output from a node Activity execution.

    Must provide enough information for the Workflow to construct a
    WritesProtocol-compatible object (via PregelTaskWrites) for
    apply_writes(checkpoint, channels, tasks, get_next_version,
    trigger_to_nodes).
    """
    node_name: str
    writes: list[tuple[str, Any]]  # Channel writes from the node
    triggers: list[str] = field(default_factory=list)  # Channels that triggered this node
    task_path: tuple[str, ...] = ()
    interrupts: list[dict] | None = None
    push_sends: list[dict] | None = None  # Dynamic Send objects emitted
    command: dict | None = None  # Command returned from node (goto, update, graph)

@activity.defn
async def execute_node(input: NodeActivityInput) -> NodeActivityOutput:
    """Execute a single LangGraph node as a Temporal Activity.

    This is the non-deterministic boundary. Everything inside this
    function may perform I/O, call LLMs, execute tools, etc.

    The Activity reconstructs the full RunnableConfig with:
    - CONFIG_KEY_SEND: Callable to emit dynamic Send objects (PUSH tasks)
    - CONFIG_KEY_READ: Callable for conditional edge local reads
    - CONFIG_KEY_SCRATCHPAD: PregelScratchpad with interrupt counters
      and resume values for interrupt() support
    """
    # Retrieve the graph definition and node callable
    graph_def = get_graph_definition(input.graph_definition_ref)
    node = graph_def.nodes[input.node_name]

    # Build the PregelNode's expected input from channel state
    # If this is a PUSH task (dynamic Send), use send_input instead
    if input.send_input is not None:
        node_input = input.send_input
    else:
        node_input = build_node_input(input.input_state, node)

    # Collect dynamic Send objects emitted during execution
    push_sends = []
    all_writes = []

    def send_handler(writes):
        """CONFIG_KEY_SEND: Captures writes including Send objects."""
        for chan, val in writes:
            if chan == PUSH:
                push_sends.append(val)
            else:
                all_writes.append((chan, val))

    # Reconstruct PregelScratchpad for interrupt() support
    scratchpad = PregelScratchpad()
    if input.resume_values:
        scratchpad.resume = input.resume_values

    # Build config with all required keys
    config = build_node_config(
        input,
        graph_def,
        send=send_handler,
        scratchpad=scratchpad,
        local_read=partial(local_read, input.input_state, all_writes),
    )

    # Execute the node
    try:
        activity.heartbeat(f"Starting node {input.node_name}")
        result = await run_node(node, node_input, config)

        # Process result -- handle Command objects
        command_data = None
        if isinstance(result, Command):
            command_data = {
                "goto": result.goto,
                "update": result.update,
                "graph": result.graph,
            }
            # Process Command through ChannelWrite writers
            writes = process_command_writes(result, node, graph_def)
            all_writes.extend(writes)
        else:
            writes = collect_writes(result, node, graph_def)
            all_writes.extend(writes)

        # Strip UntrackedValue writes from output (must not appear in
        # Temporal event history)
        serializable_writes = [
            (chan, val) for chan, val in all_writes
            if not is_untracked_value(chan, graph_def)
        ]

        return NodeActivityOutput(
            node_name=input.node_name,
            writes=serializable_writes,
            task_path=input.task_path,
            push_sends=push_sends if push_sends else None,
            command=command_data,
        )
    except GraphInterrupt as exc:
        # Node called interrupt() -- propagate to Workflow
        # Do NOT apply writes; the node will be re-executed on resume
        # NOTE: GraphInterrupt stores interrupts in args[0], not exc.interrupts
        interrupts = exc.args[0] if exc.args else []
        return NodeActivityOutput(
            node_name=input.node_name,
            writes=[],
            task_path=input.task_path,
            interrupts=[
                {"value": i.value, "id": i.id}
                for i in interrupts
            ],
        )


@activity.defn
async def evaluate_conditional_edge(
    input: ConditionalEdgeInput,
) -> str | list[str | Send] | None:
    """Evaluate a conditional edge function as a Local Activity.

    This preserves Temporal's determinism boundary while minimizing
    latency (Local Activities run on the same worker).
    """
    graph_def = get_graph_definition(input.graph_definition_ref)
    edge_fn = graph_def.resolve_conditional_edge(input.edge_ref)

    # Reconstruct local_read semantics: the conditional edge sees state
    # reflecting only the writes from its triggering node
    state = apply_local_reads(input.channel_state, input.node_writes)
    return edge_fn(state)
```

**Key Activity design decisions:**

1. **`CONFIG_KEY_SEND` reconstruction**: A `send_handler` callable captures
   dynamic `Send` objects (PUSH tasks) and regular writes emitted during
   node execution. These are returned to the Workflow for dispatch.

2. **`PregelScratchpad` reconstruction**: The scratchpad is rebuilt with
   resume values from the Workflow. This enables `interrupt()` within nodes
   to correctly match resume values by index via `scratchpad.interrupt_counter()`.

3. **`Command` handling**: When a node returns a `Command` object, it is
   processed through `ChannelWrite` writers (matching `PregelExecutableTask.writers`
   behavior) to produce the correct channel writes for `goto`, `update`, and
   `graph=PARENT` semantics.

4. **`UntrackedValue` filtering**: Writes to `UntrackedValue` channels are
   stripped from the Activity output before serialization, preserving the
   invariant that these values must never be persisted.

5. **`Overwrite` passthrough**: `Overwrite` values are serialized as tagged
   types in `_serde.py`, allowing the Workflow's channel objects to correctly
   bypass reducers when applying writes.

6. **Interrupt re-execution**: When interrupted, the Activity returns empty
   writes and the interrupt payload. The Workflow does NOT apply writes or
   advance the step. On resume, the Activity is re-invoked with
   `resume_values` populated in the `PregelScratchpad`.

7. **`RetryPolicy.retry_on` predicate support**: When the LangGraph
   `RetryPolicy` specifies a callable `retry_on` predicate, the Activity
   catches exceptions, evaluates the predicate, and re-raises as either
   `ApplicationError` (retryable) or `ApplicationError(non_retryable=True)`.
   This bridges the gap between LangGraph's predicate-based retry and
   Temporal's type-based retry.

### 3.5 TemporalCheckpointSaver

This component bridges LangGraph's `get_state()` / `get_state_history()` APIs
with Temporal's Workflow state.

```python
# langgraph_temporal/checkpoint.py

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)

class TemporalCheckpointSaver(BaseCheckpointSaver):
    """CheckpointSaver that reads state from Temporal Workflows.

    This is a read-oriented adapter. Writes are handled implicitly by
    Temporal's event history. The primary use case is enabling
    get_state() and get_state_history() to work transparently.
    """

    def __init__(self, client: TemporalClient, namespace: str = "default"):
        super().__init__()
        self.client = client
        self.namespace = namespace

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Retrieve current state by querying the Temporal Workflow."""
        thread_id = config["configurable"]["thread_id"]
        handle = self.client.get_workflow_handle(
            workflow_id=thread_id,
        )
        try:
            state = handle.query(LangGraphWorkflow.get_current_state)
        except temporalio.service.RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                return None
            raise  # Propagate connection errors, auth failures, etc.

        checkpoint = Checkpoint(
            v=1,
            id=str(state.step),
            ts=datetime.now(timezone.utc).isoformat(),
            channel_values=state.channel_state,
            channel_versions=state.channel_versions,
            versions_seen=state.versions_seen,
        )
        metadata = CheckpointMetadata(
            source="loop",
            step=state.step,
            parents={},
        )
        return CheckpointTuple(
            config=config,
            checkpoint=checkpoint,
            metadata=metadata,
        )

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """List checkpoints from Temporal Event History.

        Each completed step in the Workflow corresponds to a checkpoint.
        We reconstruct checkpoint state from Activity completion events.
        """
        thread_id = config["configurable"]["thread_id"]
        handle = self.client.get_workflow_handle(workflow_id=thread_id)
        # Iterate Temporal event history
        # Each ActivityTaskCompleted event represents a checkpoint boundary
        ...

    def put(self, config, checkpoint, metadata, new_versions):
        """No-op: Temporal handles persistence via Event History."""
        return config

    def put_writes(self, config, writes, task_id, task_path=""):
        """No-op: writes are persisted as Activity results."""
        pass
```

### 3.6 State Serialization

State must be serializable for transmission between the Workflow and Activities.
LangGraph uses `JsonPlusSerializer` internally. The Temporal integration
must handle serialization at the Activity boundary.

```python
# langgraph_temporal/state.py

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

class StateSerializer:
    """Handles serialization of LangGraph channel state for Temporal."""

    def __init__(self):
        self.serde = JsonPlusSerializer()

    def serialize_channel_state(
        self, channel_values: dict[str, Any]
    ) -> bytes:
        """Serialize channel state for Activity input."""
        return self.serde.dumps(channel_values)

    def deserialize_channel_state(self, data: bytes) -> dict[str, Any]:
        """Deserialize channel state from Activity output."""
        return self.serde.loads(data)

    def serialize_writes(
        self, writes: list[tuple[str, Any]]
    ) -> bytes:
        """Serialize node writes for Activity output."""
        return self.serde.dumps(writes)

    def deserialize_writes(self, data: bytes) -> list[tuple[str, Any]]:
        """Deserialize node writes from Activity output."""
        return self.serde.loads(data)
```

For large payloads, a `TemporalPayloadCodec` wrapping an external blob store
(S3, GCS) should be provided:

```python
# langgraph_temporal/_codec.py

from temporalio.converter import PayloadCodec
from temporalio.api.common.v1 import Payload

class LargePayloadCodec(PayloadCodec):
    """Codec that offloads large payloads to external storage."""

    def __init__(self, store: BlobStore, threshold_bytes: int = 2_000_000):
        self.store = store
        self.threshold = threshold_bytes

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        result = []
        for p in payloads:
            if len(p.data) > self.threshold:
                key = await self.store.put(p.data)
                result.append(Payload(
                    metadata={"encoding": b"blob-store", "key": key.encode()},
                    data=b"",
                ))
            else:
                result.append(p)
        return result

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        result = []
        for p in payloads:
            if p.metadata.get("encoding") == b"blob-store":
                data = await self.store.get(p.metadata["key"].decode())
                result.append(Payload(data=data))
            else:
                result.append(p)
        return result
```

### 3.7 Graph Definition Serialization

The Workflow needs access to the graph's topology (edges, conditional edges,
channel specs) without importing the full graph object (which may contain
non-serializable callables). The solution is a `GraphDefinition` dataclass
that captures the structural information needed by the Workflow.

```python
# langgraph_temporal/converter.py

@dataclass
class GraphDefinition:
    """Serializable representation of a LangGraph graph's topology."""
    node_names: list[str]
    edges: dict[str, list[str]]           # node -> [next_nodes]
    conditional_edges: dict[str, str]      # node -> condition_function_ref
    channel_specs: dict[str, ChannelSpec]  # channel_name -> spec
    input_channels: list[str]
    output_channels: list[str]
    interrupt_before: list[str]
    interrupt_after: list[str]
    node_retry_policies: dict[str, dict]   # node -> RetryPolicy as dict
    node_task_queues: dict[str, str]       # node -> task_queue
    trigger_to_nodes: dict[str, list[str]]

@dataclass
class ChannelSpec:
    """Serializable channel specification."""
    type: str  # "last_value", "binop", "topic", etc.
    value_type: str
    reducer: str | None = None  # Serialized reference to reducer function

def compile_to_definition(graph: Pregel, **overrides) -> GraphDefinition:
    """Extract a serializable GraphDefinition from a compiled Pregel graph."""
    ...
```

**Conditional edges** pose a challenge because they are Python callables. Two
approaches:

1. **Evaluate in Activity**: Run the conditional edge function inside a
   lightweight Activity. The Workflow calls this Activity after each step to
   determine the next nodes.
2. **Pre-register with string references**: Require users to register
   conditional edge functions by name, enabling the Workflow to look them up
   at runtime.

The recommended approach is **(1)** because it requires no API changes.

## 4. Workflow Definition Patterns

### 4.1 Linear Graph

```
START -> A -> B -> C -> END
```

Workflow:
```
execute_activity(A) -> execute_activity(B) -> execute_activity(C)
```

### 4.2 Conditional Branching

```
START -> Agent -> (should_continue) -> Tools -> Agent
                                   \-> END
```

Workflow:
```
while True:
    result = execute_activity(Agent)
    apply_writes(result)
    next_node = execute_activity(evaluate_condition, "should_continue", state)
    if next_node == END:
        break
    result = execute_activity(Tools)
    apply_writes(result)
```

### 4.3 Parallel Execution (Fan-out/Fan-in via Send)

```
START -> (continue_to_jokes) -> [generate_joke x N] -> END
```

Workflow:
```
sends = execute_activity(evaluate_condition, "continue_to_jokes", state)
results = await asyncio.gather(*[
    execute_activity(generate_joke, send.arg) for send in sends
])
apply_writes(results)  # BinaryOperatorAggregate or Topic channel
```

## 5. Human-in-the-Loop Design

### 5.1 Flow Diagram

```
                    Client                   Temporal                 Worker
                      |                         |                       |
                      |-- start_workflow ------->|                       |
                      |                         |-- schedule activity -->|
                      |                         |                       |
                      |                         |<-- activity result ----|
                      |                         |   (interrupt detected) |
                      |                         |                       |
                      |<-- query: interrupts ---|                       |
                      |   "Approve $10k?"       |                       |
                      |                         |    (no resources      |
                      |   (human reviews...)    |     consumed)         |
                      |                         |                       |
                      |-- signal: resume ------>|                       |
                      |   {"approved": true}    |                       |
                      |                         |-- schedule activity -->|
                      |                         |                       |
                      |                         |<-- activity result ----|
                      |                         |                       |
                      |<-- workflow result ------|                       |
```

### 5.2 Interrupt Handling

When a node calls `interrupt(value)`:

1. The Activity catches `GraphInterrupt` and returns the interrupt payload.
2. The Workflow stores the interrupt in `self.interrupts`.
3. The Workflow calls `await workflow.wait_condition(...)`, blocking with zero
   resource consumption.
4. The client queries `get_current_state` to retrieve interrupt details.
5. The client sends a `resume_signal` with the response value.
6. The Workflow wakes up, re-executes the node Activity with the resume value
   injected.

### 5.3 Multiple Interrupts in a Single Node

LangGraph supports multiple `interrupt()` calls in a single node, matched by
order. The Temporal integration handles this by:

1. On first Activity execution, collecting ALL interrupts (the node re-raises
   after the first `interrupt()` call due to `GraphInterrupt`).
2. When resumed with enough values, re-executing the Activity with all resume
   values provided. The Activity reconstructs the resume context so that each
   `interrupt()` call returns the corresponding value.

This matches the existing behavior in `/libs/langgraph/langgraph/pregel/_loop.py`
lines 588-616, where `_pending_interrupts()` tracks interrupt IDs and their
corresponding resume values.

## 6. Streaming Design

Temporal Workflows are not designed for real-time streaming. Three strategies
are available, each with different trade-offs:

### Strategy A: Side-Channel (Recommended)

```
+----------+     +---------+     +--------+
| Activity | --> | Redis   | --> | Client |
| (node)   |     | Pub/Sub |     |        |
+----------+     +---------+     +--------+
```

- Activities publish stream events to an external pub/sub system (Redis,
  Kafka, or SSE endpoint).
- Client subscribes to the channel keyed by Workflow ID.
- Lowest latency, best for token-by-token streaming.
- Requires additional infrastructure.

### Strategy B: Temporal Queries (Polling)

- Workflow accumulates stream events in a buffer.
- Client polls via `workflow.query` at a configurable interval.
- No additional infrastructure needed.
- Higher latency (polling interval), buffer management complexity.

### Strategy C: Temporal Updates (Temporal 1.10+)

- Use Temporal Workflow Updates for request-response streaming.
- Moderate latency, no additional infrastructure.
- Limited by Temporal's update throughput.

The implementation should support all three via a `StreamBackend` abstraction:

```python
# langgraph_temporal/streaming.py

class StreamBackend(ABC):
    @abstractmethod
    async def publish(self, workflow_id: str, event: StreamEvent): ...

    @abstractmethod
    def subscribe(self, workflow_id: str) -> AsyncIterator[StreamEvent]: ...

class RedisStreamBackend(StreamBackend): ...
class PollingStreamBackend(StreamBackend): ...
class TemporalUpdateStreamBackend(StreamBackend): ...
```

## 7. Error Handling and Retry Strategy

### 7.1 RetryPolicy Mapping

```python
def to_temporal_retry_policy(
    lg_policy: LangGraphRetryPolicy,
) -> temporalio.common.RetryPolicy:
    """Map LangGraph RetryPolicy to Temporal RetryPolicy.

    Note: LangGraph's `jitter` is not mapped because Temporal applies
    jitter internally. LangGraph's `retry_on` callable predicate is
    handled inside the Activity (see execute_node), not here.
    """
    return temporalio.common.RetryPolicy(
        initial_interval=timedelta(seconds=lg_policy.initial_interval),
        backoff_coefficient=lg_policy.backoff_factor,
        maximum_interval=timedelta(seconds=lg_policy.max_interval),
        maximum_attempts=lg_policy.max_attempts,
        non_retryable_error_types=[
            "InvalidUpdateError",
            "GraphRecursionError",
            "EmptyInputError",
        ],
    )

# Inside execute_node Activity: handling retry_on predicates
def handle_retry_on(exc: Exception, lg_policy: LangGraphRetryPolicy):
    """Evaluate LangGraph's retry_on predicate and raise appropriate error.

    Temporal's RetryPolicy only supports error type names. When LangGraph
    uses a callable predicate, we evaluate it locally in the Activity
    and convert to Temporal-compatible errors.
    """
    if callable(lg_policy.retry_on):
        if lg_policy.retry_on(exc):
            raise ApplicationError(str(exc), type="RetryableNodeError")
        else:
            raise ApplicationError(
                str(exc), type="NonRetryableNodeError", non_retryable=True
            )
    raise  # Default: let Temporal decide based on error type
```

### 7.2 Error Classification

| LangGraph Error | Temporal Handling |
|---|---|
| `GraphRecursionError` | Non-retryable. Workflow fails. |
| `InvalidUpdateError` | Non-retryable. Activity fails permanently. |
| `GraphInterrupt` | Not an error. Activity returns interrupt payload. |
| `GraphBubbleUp` | Propagated to parent Workflow via Child Workflow failure. |
| `EmptyInputError` | Non-retryable. |
| Rate limit / network errors | Retryable per RetryPolicy. |
| `ParentCommand` | Handled within Activity; command forwarded to Workflow. |

### 7.3 Workflow-Level Timeouts

```python
temporal_graph = TemporalGraph(
    graph,
    client,
    workflow_execution_timeout=timedelta(hours=24),  # Overall limit
    workflow_run_timeout=timedelta(hours=1),          # Per-run limit
)
```

## 8. Subgraph as Child Workflows

### 8.1 Design

When a node contains a subgraph (detected via `node.subgraphs`), the Workflow
executes it as a Child Workflow instead of an Activity.

```python
# Inside LangGraphWorkflow

async def _execute_node(self, task, graph_def):
    if task.name in graph_def.subgraph_nodes:
        # Execute as Child Workflow
        child_input = WorkflowInput(
            graph_definition=graph_def.subgraph_definitions[task.name],
            input_data=self._read_channels_for_node(task, graph_def),
            recursion_limit=self.remaining_steps(),
        )
        result = await workflow.execute_child_workflow(
            LangGraphWorkflow.run,
            child_input,
            id=f"{workflow.info().workflow_id}/{task.name}",
            parent_close_policy=ParentClosePolicy.TERMINATE,
            cancellation_type=ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
        )
        return NodeActivityOutput(
            node_name=task.name,
            writes=result.channel_state_as_writes(),
        )
    else:
        # Execute as Activity
        return await workflow.execute_activity(...)
```

### 8.2 Namespace Mapping

LangGraph uses `checkpoint_ns` with `NS_SEP` (`|`) to track subgraph nesting.
In the Temporal integration:

```
LangGraph:  "parent_node:task_id|child_node:task_id"
Temporal:   Workflow ID "parent_wf_id/parent_node/step_N/child_node"
```

The mapping is:
- Parent Workflow ID = `thread_id`
- Child Workflow ID = `{parent_wf_id}/{subgraph_node_name}/{step}`
- Nested children = `{parent_wf_id}/{node1}/{step1}/{node2}/{step2}/...`

**Important**: The step counter is included in the Child Workflow ID to prevent
Workflow ID collisions when the same subgraph node is invoked multiple times
(e.g., in a loop). Temporal requires unique Workflow IDs within a namespace for
concurrent executions.

## 9. Configuration and Initialization

### 9.1 Full Configuration Example

```python
from langgraph_temporal import TemporalGraph, ActivityOptions
from langgraph_temporal.streaming import RedisStreamBackend
from temporalio.client import Client
from datetime import timedelta

# 1. Build your graph as usual
from langgraph.graph import StateGraph, START, END

class State(TypedDict):
    messages: Annotated[list, add_messages]

builder = StateGraph(State)
builder.add_node("agent", agent_fn)
builder.add_node("tools", ToolNode(tools))
builder.add_edge(START, "agent")
builder.add_conditional_edges("agent", should_continue)
builder.add_edge("tools", "agent")
compiled = builder.compile()

# 2. Connect to Temporal
client = await Client.connect("localhost:7233")

# 3. Wrap with TemporalGraph
temporal_graph = TemporalGraph(
    compiled,
    client,
    task_queue="langgraph-agents",
    node_task_queues={
        "agent": "gpu-workers",       # LLM calls on GPU
        "tools": "cpu-workers",        # Tool execution on CPU
    },
    node_activity_options={
        "agent": ActivityOptions(
            start_to_close_timeout=timedelta(minutes=5),
            heartbeat_timeout=timedelta(seconds=30),
        ),
        "tools": ActivityOptions(
            start_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(minutes=1),
        ),
    },
    workflow_execution_timeout=timedelta(hours=24),
    stream_backend=RedisStreamBackend("redis://localhost:6379"),
)

# 4. Use it exactly like a normal LangGraph graph
result = temporal_graph.invoke(
    {"messages": [("user", "Research quantum computing")]},
    config={"configurable": {"thread_id": "research-001"}},
)

# 5. Start a worker (in a separate process or the same one)
worker = temporal_graph.create_worker()
await worker.run()
```

### 9.2 Worker Setup

```python
# worker.py (run on each worker machine)
from langgraph_temporal import TemporalGraph
from temporalio.client import Client

async def main():
    client = await Client.connect("temporal.mycompany.com:7233")

    # The graph must be importable on the worker
    from my_app.graph import compiled_graph

    temporal_graph = TemporalGraph(compiled_graph, client, task_queue="langgraph-agents")
    worker = temporal_graph.create_worker(
        max_concurrent_activities=10,
        max_concurrent_workflow_tasks=100,
    )
    await worker.run()
```

## 10. Code Examples: Before and After

### Before: Standard LangGraph

```python
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver

builder = StateGraph(State)
builder.add_node("agent", agent_fn)
builder.add_node("tools", ToolNode(tools))
builder.add_edge(START, "agent")
builder.add_conditional_edges("agent", should_continue)
builder.add_edge("tools", "agent")

# Compile with in-memory checkpointing
graph = builder.compile(checkpointer=InMemorySaver())

# Execute
result = graph.invoke(
    {"messages": [("user", "Hello")]},
    config={"configurable": {"thread_id": "t1"}},
)
```

### After: LangGraph on Temporal

```python
from langgraph.graph import StateGraph, START, END
from langgraph_temporal import TemporalGraph
from temporalio.client import Client

builder = StateGraph(State)
builder.add_node("agent", agent_fn)
builder.add_node("tools", ToolNode(tools))
builder.add_edge(START, "agent")
builder.add_conditional_edges("agent", should_continue)
builder.add_edge("tools", "agent")

# Compile WITHOUT checkpointer (Temporal handles durability)
compiled = builder.compile()

# Wrap with Temporal
client = await Client.connect("localhost:7233")
graph = TemporalGraph(compiled, client, task_queue="my-agents")

# Execute -- same API
result = graph.invoke(
    {"messages": [("user", "Hello")]},
    config={"configurable": {"thread_id": "t1"}},
)
```

### Before: Human-in-the-Loop

```python
# First invocation
result = graph.invoke(
    {"messages": [("user", "Transfer $10k")]},
    config={"configurable": {"thread_id": "t1"}},
)
# result contains interrupt

# Resume (must happen in same process or with shared checkpoint store)
result = graph.invoke(
    Command(resume="approved"),
    config={"configurable": {"thread_id": "t1"}},
)
```

### After: Human-in-the-Loop on Temporal

```python
# First invocation (returns interrupt info)
handle = await graph.astart(
    {"messages": [("user", "Transfer $10k")]},
    config={"configurable": {"thread_id": "t1"}},
)

# Poll for interrupt (or use webhook)
state = graph.get_state(config={"configurable": {"thread_id": "t1"}})
# state.interrupts = [Interrupt(value="Approve transfer of $10k?")]

# Resume from ANY process -- no shared state needed
graph.resume(
    config={"configurable": {"thread_id": "t1"}},
    value="approved",
)

# Wait for result
result = await handle.result()
```

## 11. Testing Strategy

### 11.1 Unit Tests

- **Converter tests**: Verify `compile_to_definition()` correctly extracts
  graph topology from various `Pregel` configurations.
- **State serialization tests**: Verify round-trip serialization of all channel
  types (LastValue, BinaryOperatorAggregate, Topic, EphemeralValue).
- **Channel update tests**: Verify `_apply_writes()` produces identical results
  to LangGraph's `apply_writes()` for all channel types.
- **Retry policy mapping tests**: Verify `to_temporal_retry_policy()` correctly
  maps all LangGraph `RetryPolicy` fields.

### 11.2 Integration Tests (with Temporal Test Server)

- **Basic execution**: Linear graph A->B->C produces correct output.
- **Conditional branching**: Agent loop with tool calls terminates correctly.
- **Parallel execution**: Fan-out via `Send` produces correct aggregated result.
- **Human-in-the-loop**: `interrupt()` + `resume_signal` round-trip.
- **Worker crash recovery**: Kill worker mid-Activity, restart, verify
  completion.
- **Subgraph as Child Workflow**: Parent + child execute correctly.
- **Streaming**: All stream modes produce expected events.
- **State queries**: `get_state()` and `get_state_history()` return correct
  results during and after execution.

### 11.3 Conformance Tests

Run the existing LangGraph test suite against the Temporal backend to verify
behavioral equivalence. The `libs/checkpoint-conformance` package provides a
pattern for this.

### 11.4 Performance Tests

- Measure overhead per node execution (Activity dispatch latency).
- Measure end-to-end latency for a 10-node graph.
- Measure streaming latency for token-by-token output.
- Measure recovery time after worker crash.

## 12. Supported Features Matrix (v0.1)

| Feature | Status | Notes |
|---|---|---|
| `StateGraph` + `compile()` | Supported | Core use case |
| Static edges (`add_edge`) | Supported | Direct mapping |
| Conditional edges (`add_conditional_edges`) | Supported | Evaluated as Local Activities |
| `invoke()` / `ainvoke()` | Supported | Via Temporal Workflow execution |
| `stream()` / `astream()` | Supported | Via StreamBackend (polling or Redis) |
| `get_state()` | Supported | Via Temporal Query |
| `get_state_history()` | Supported | Via Temporal Event History traversal |
| `update_state()` | Supported | Via Temporal Signal |
| `interrupt()` / `Command(resume=...)` | Supported | Via Temporal Signal + Activity re-execution |
| `interrupt_before` / `interrupt_after` | Supported | Checked in Workflow loop |
| `RetryPolicy` | Supported | Mapped to Temporal Activity RetryPolicy |
| `RetryPolicy.retry_on` (callable) | Supported | Evaluated inside Activity |
| `Command(goto=..., update=...)` from nodes | Supported | Processed via ChannelWrite writers |
| `Command(graph=PARENT)` | Supported | Propagated via Child Workflow failure |
| `Send` (from conditional edges) | Supported | Fan-out via parallel Activities |
| `Send` (dynamic, from within nodes) | Supported | Via PUSH task extraction |
| Subgraph composition | Supported | Via Temporal Child Workflows |
| `LastValue` channel | Supported | Actual channel objects reused |
| `BinaryOperatorAggregate` channel | Supported | Actual channel objects reused |
| `Topic` channel | Supported | Actual channel objects reused |
| `EphemeralValue` channel | Supported | Actual channel objects reused |
| `NamedBarrierValue` channel | Supported | Actual channel objects reused |
| `AnyValue` channel | Supported | Actual channel objects reused |
| `Overwrite` values | Supported | Serialized as tagged type |
| `UntrackedValue` channel | Supported | Excluded from event history |
| Per-node task queues | Supported | Via `node_task_queues` config |
| Activity heartbeats | Supported | Automatic in `execute_node` |
| Large payload offload | Supported | Via `LargePayloadCodec` |
| Encryption | Supported | Via `EncryptionCodec` |
| Continue-As-New | Supported | Automatic at threshold |
| `CachePolicy` | Partial | Activity results cached by Temporal replay |
| `ManagedValueMapping` (simple) | Partial | `IsLastStep` supported; `SharedValue` deferred |
| `BaseStore` injection | Deferred | Workers can inject locally; no Temporal sync |
| Functional API (`@entrypoint`/`@task`) | Deferred | Planned for v0.2 |
| `BaseStore` via Temporal | Deferred | Users use `BaseStore` implementations directly |
| Multi-graph workers | Deferred | One graph per worker in v0.1 |

## 13. Deployment Considerations

### 13.1 Infrastructure Requirements

- Temporal Server (self-hosted or Temporal Cloud).
- At least one Temporal Worker process with access to the graph's node
  functions, LLM API keys, and tool implementations.
- Optional: Redis or similar for streaming side-channel.
- Optional: External blob store (S3/GCS) for large payloads via PayloadCodec.

### 13.2 Versioning and Deployments

Temporal requires careful handling of Workflow code changes:

- **Workflow versioning**: Use `workflow.patched()` or Workflow ID-based
  routing to handle changes to graph topology between deployments.
- **Activity versioning**: Activities are loosely coupled; new versions can be
  deployed without affecting in-flight Workflows.
- **Graph version identifier**: Include a hash of the `GraphDefinition` in the
  Workflow ID or search attributes to route to the correct Workflow version.

### 13.3 Scaling

- **Horizontal scaling**: Add Worker processes to increase Activity throughput.
- **Task queue routing**: Use separate task queues for different node types
  to enable heterogeneous scaling (GPU vs CPU workers).
- **Workflow throughput**: Temporal supports thousands of concurrent Workflows
  per namespace.

## 14. Open Questions and Trade-offs

### Q1: Workflow Determinism and Conditional Edges [RESOLVED]

Conditional edge functions are user-defined Python callables. Running them
inside the Workflow would violate determinism (they may depend on external
state). Running them as regular Activities adds latency for every branching
decision.

**Resolution**: Evaluate conditional edges as **Local Activities**
(`workflow.execute_local_activity()`). Local Activities run on the same worker
as the Workflow, avoiding the task queue round-trip. Typical latency: <5ms
(vs 20-50ms for regular Activities). They still provide the non-determinism
boundary required by Temporal's programming model. The conditional edge
function receives state reflecting only the triggering node's writes
(`local_read` semantics from `_algo.py`).

### Q2: Graph Definition Serialization [RESOLVED]

The `Pregel` object contains Python callables (node functions, reducers,
conditional edge functions) that are not directly serializable. Two options:

- **(A)** Require the graph to be importable on the Worker (by module path).
  The Workflow references nodes by name; the Worker resolves them at runtime.
- **(B)** Serialize the full graph using `cloudpickle` or `dill`.

**Resolution**: Option (A). The Worker imports the graph module and registers
Activities. This is more reliable and aligns with Temporal best practices.
Pickle-based serialization creates fragile deployments, security risks, and
version compatibility issues.

### Q3: State Size Limits [RESOLVED]

Temporal has a recommended limit of ~2MB for Workflow state (carried in event
history). LangGraph state with large message histories or tool outputs may
exceed this.

**Resolution**: Use `LargePayloadCodec` to offload large payloads to external
storage (S3/GCS). Provide an `EncryptionCodec` for PII workloads. Provide
clear documentation on state size best practices.

### Q4: `interrupt()` Re-execution Semantics [RESOLVED]

LangGraph re-executes the entire node from the beginning when resuming from
`interrupt()`. This means all code before the `interrupt()` call runs again.
In the Temporal integration, this means the Activity is re-invoked from scratch.

**Resolution**: This is consistent with existing LangGraph semantics.
Documentation must prominently warn about LLM re-execution costs and
recommend structuring nodes to minimize work before `interrupt()` (e.g.,
split into separate nodes, or use `CachePolicy` for idempotent calls).
The Activity reconstructs `PregelScratchpad` with resume values so
`interrupt()` calls correctly return resumed values by index.

### Q5: Functional API Mapping

The `@entrypoint` / `@task` API compiles to a Pregel internally, but the
task scheduling mechanism (`call()` / `SyncAsyncFuture`) is different from
the Graph API's channel-based scheduling. The Temporal integration must
support both.

**Proposed resolution**: `@task` functions map to Activities. The
`@entrypoint` function becomes the Workflow body. `call()` becomes
`workflow.execute_activity()`.

### Q6: Streaming Architecture Decision [RESOLVED]

The side-channel approach (Strategy A) provides the best streaming experience
but requires additional infrastructure. The polling approach (Strategy B) is
simpler but has higher latency.

**Resolution**: Support both. Default to polling (Strategy B) with cursor-based
pagination for simplicity. Provide `RedisStreamBackend` for production use cases
requiring low-latency streaming. Streaming infrastructure failures must be
best-effort and must NOT block Activity execution (circuit breaker pattern).

### Q7: Event History Growth for Long-Running Agents [RESOLVED]

Temporal has a default limit of 50,000 events in event history. Agent loops
with 100+ iterations generate many events per step (scheduled + completed
events per Activity, plus signal/query events).

**Resolution**: Implement `workflow.continue_as_new()` after a configurable
threshold (default: 500 steps). The Workflow serializes full channel state,
`channel_versions`, `versions_seen`, and step counter as input to the new
Workflow run. `get_state_history()` traverses the Continue-As-New chain via
`first_execution_run_id` to provide complete history.

### Q8: Versioning Strategy for Graph Topology Changes

If a user deploys a modified graph (new nodes, changed edges), in-flight
Workflows will fail to replay due to non-determinism errors.

**Proposed resolution**: Use Temporal's Worker Versioning (Build IDs) to route
in-flight workflows to workers running the old graph version. Include a graph
topology hash in Search Attributes to enable filtering. Document that in-flight
workflows should complete or be terminated before deploying breaking graph
changes. For minor changes (Activity code updates), no versioning is needed
as Activities are loosely coupled.

### Q9: `update_state()` Race Condition with Running Activities

The `update_state_signal` handler modifies channel state while Activities may
be running. If a signal arrives between Activity dispatch and completion, the
Activity's writes may overwrite the signal's update.

**Proposed resolution**: `update_state_signal` applies updates through
`apply_writes` from `_algo.py`, which correctly updates `channel_versions`.
When the running Activity completes, its writes are also applied through
`apply_writes`. Since channel versions are tracked, the later write wins based
on version ordering. Document this behavior as eventually-consistent for
concurrent modifications.

### Q10: Idempotency for Non-Idempotent Tool Calls

If a worker crashes after a tool call completes but before reporting the
Activity result, Temporal will retry the Activity, causing duplicate side
effects (e.g., duplicate emails, duplicate trades).

**Proposed resolution**: Provide an idempotency key mechanism based on
`{workflow_id}/{step}/{node_name}`. The Activity can use this to check an
idempotency cache before executing expensive or non-idempotent operations.
This is the user's responsibility to implement for their specific tools;
the library provides the key and documentation.
```

---

### Critical Files for Implementation

- `/Users/pradithya/project/langgraph/libs/langgraph/langgraph/pregel/_loop.py` - Core execution loop (PregelLoop, tick(), after_tick()) that the Temporal Workflow must replicate
- `/Users/pradithya/project/langgraph/libs/langgraph/langgraph/pregel/main.py` - Pregel class with invoke/stream methods, node definitions, and subgraph handling that TemporalGraph must wrap
- `/Users/pradithya/project/langgraph/libs/checkpoint/langgraph/checkpoint/base/__init__.py` - BaseCheckpointSaver interface that TemporalCheckpointSaver must implement
- `/Users/pradithya/project/langgraph/libs/langgraph/langgraph/types.py` - Core types (RetryPolicy, Interrupt, Command, Send, PregelExecutableTask) that must be mapped to Temporal equivalents
- `/Users/pradithya/project/langgraph/libs/langgraph/langgraph/pregel/_runner.py` - PregelRunner task execution logic that the Temporal Activity layer replaces