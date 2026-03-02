# langgraph-temporal: Requirements Document

## 1. Problem Statement

LangGraph provides a powerful framework for building stateful, multi-actor agent
workflows through its StateGraph and Pregel execution engine. However, its current
durability model has fundamental limitations for production-grade, long-running
AI workloads:

1. **Checkpoint-based recovery is not true durable execution.** LangGraph persists
   state via `BaseCheckpointSaver` (Postgres, SQLite, in-memory). If a process
   crashes mid-node-execution, the incomplete node must be re-executed from
   scratch. There is no automatic replay of partially completed work. The
   checkpoint captures state *between* steps, not *within* them.

2. **No built-in distributed execution.** All nodes execute within a single
   process. There is no mechanism to distribute individual graph nodes across
   multiple workers, data centers, or availability zones. Scaling is vertical
   only.

3. **Human-in-the-loop consumes resources while waiting.** LangGraph's
   `interrupt()` function raises `GraphInterrupt`, which halts the execution
   loop and saves a checkpoint. Resuming requires the caller to invoke
   `Command(resume=...)`. During the wait (which could be hours, days, or weeks),
   no resources are consumed by the graph itself, but the calling infrastructure
   must maintain the ability to route the resume to the correct process and
   reconstruct the full execution context from the checkpoint. There is no
   built-in mechanism for push-based notification when the human responds.

4. **Retry policies are process-local.** `RetryPolicy` (defined in
   `/libs/langgraph/langgraph/types.py`) provides per-node retry with
   exponential backoff, but retries happen within the same process. If the
   process itself fails, the retry state is lost.

5. **No event-sourced audit trail.** Checkpoints capture snapshots, not the
   full causal history of every decision, LLM call, and tool invocation. For
   regulated industries (finance, healthcare), a complete, tamper-evident
   audit log is required.

6. **Timeout management is limited.** `step_timeout` on the `Pregel` class
   applies to individual steps, but there is no workflow-level timeout, no
   heartbeat mechanism for long-running nodes, and no automatic cancellation
   of stale workflows.

Temporal provides battle-tested solutions to all of these problems through its
workflow-as-code paradigm, event-sourced execution history, and distributed
activity task queues.

## 2. Goals

- **G-01**: Enable LangGraph graphs to run as Temporal Workflows with true
  durable execution -- automatic recovery from any failure without data loss.
- **G-02**: Map LangGraph node executions to Temporal Activities, enabling
  distributed execution across workers with automatic retry on failure.
- **G-03**: Provide a Temporal-backed human-in-the-loop mechanism using Signals,
  consuming zero compute resources while waiting for human input.
- **G-04**: Maintain full backward compatibility with the existing LangGraph
  `StateGraph` API. Users should be able to take an existing compiled graph and
  run it on Temporal with minimal code changes.
- **G-05**: Support all existing LangGraph stream modes over Temporal.
- **G-06**: Support subgraph composition by mapping subgraphs to Temporal Child
  Workflows.
- **G-07**: Provide a complete, event-sourced audit trail of all graph
  executions via Temporal's Event History.
- **G-08**: Support the LangGraph Functional API (`@entrypoint`, `@task`) in
  addition to the Graph API (`StateGraph`).

## 3. Non-Goals

- **NG-01**: Replacing the existing LangGraph checkpoint system. The Temporal
  integration is an *alternative* execution backend, not a replacement. Users
  who do not need Temporal can continue using `BaseCheckpointSaver` as-is.
- **NG-02**: Modifying the core LangGraph library. The integration must be a
  separate `langgraph-temporal` package that depends on `langgraph` and
  `temporalio`.
- **NG-03**: Supporting Temporal's Java, Go, or .NET SDKs. This integration
  targets the Python SDK (`temporalio`) only.
- **NG-04**: Providing a Temporal-based LangGraph Server or REST API. The
  integration is a library for embedding in user applications.
- **NG-05**: Automatic migration of in-flight checkpointed executions to
  Temporal workflows. Migration is supported only for new executions.
- **NG-06**: Supporting LangGraph's `BaseStore` (shared memory) via Temporal.
  Users can continue using `BaseStore` implementations directly.

## 4. Functional Requirements

### FR-01: Graph-to-Workflow Compilation

The library must provide a mechanism to compile a `StateGraph` (or its compiled
`Pregel` form) into a Temporal Workflow definition.

- **FR-01.1**: Accept any `CompiledStateGraph` (the output of
  `StateGraph.compile()`) as input.
- **FR-01.2**: Produce a Temporal Workflow class that faithfully reproduces the
  graph's execution semantics: node scheduling, conditional edges, channel
  updates, and termination conditions.
- **FR-01.3**: The Workflow must be deterministic. All non-deterministic
  operations (LLM calls, tool invocations, I/O) must be delegated to
  Activities.

### FR-02: Node-to-Activity Mapping

Each LangGraph node execution must be wrapped as a Temporal Activity.

- **FR-02.1**: Node functions (the callables passed to `add_node()`) must
  execute inside Temporal Activities.
- **FR-02.2**: Activity inputs must be serializable. The library must serialize
  the node's input state (the channel values read by the node) and deserialize
  the node's output (the writes to channels).
- **FR-02.3**: Activities must support configurable timeouts:
  `start_to_close_timeout`, `schedule_to_close_timeout`, and
  `heartbeat_timeout`.
- **FR-02.4**: Activities must report heartbeats for long-running operations
  (e.g., multi-step tool execution) to enable Temporal's automatic failure
  detection.
- **FR-02.5**: The library must support running different nodes on different
  Temporal Task Queues, enabling heterogeneous worker pools (e.g., GPU workers
  for ML nodes, CPU workers for data processing nodes).
- **FR-02.6**: Activities must reconstruct the full `RunnableConfig` for node
  execution, including `CONFIG_KEY_SEND`, `CONFIG_KEY_READ`, and
  `CONFIG_KEY_SCRATCHPAD` (the `PregelScratchpad`). Without these, `interrupt()`,
  dynamic `Send` emission, and conditional edge local reads will fail.
- **FR-02.7**: Activities must support returning `Command` objects from nodes,
  including `Command(goto=...)`, `Command(update=...)`, and
  `Command(graph=Command.PARENT)`. The `NodeActivityOutput` must capture full
  `Command` semantics, not just raw channel writes.
- **FR-02.8**: Activities must handle `Overwrite` values that bypass
  `BinaryOperatorAggregate` reducers, and must not persist `UntrackedValue`
  channel writes in Temporal's event history.
- **FR-02.9**: Conditional edge functions must be evaluated as Local Activities
  (not regular Activities) to minimize latency while preserving the
  non-determinism boundary required by Temporal's programming model.

### FR-03: State Management

The library must manage LangGraph channel state across Workflow steps.

- **FR-03.1**: The Workflow must instantiate and use actual LangGraph channel
  objects (`LastValue`, `BinaryOperatorAggregate`, `Topic`, `EphemeralValue`,
  `NamedBarrierValue`, `AnyValue`, `UntrackedValue`) rather than reimplementing
  channel update logic. Channel classes are purely in-memory with no I/O and are
  safe for Temporal Workflow determinism.
- **FR-03.2**: Channel update semantics must be preserved exactly, including
  `consume()` calls after reads, `finish()` calls at the end of supersteps,
  `bump_step` logic for `EphemeralValue` channels, and correct `versions_seen`
  per-trigger-channel updates (not full copies).
- **FR-03.3**: A `TemporalCheckpointSaver` implementing `BaseCheckpointSaver`
  must be provided. It must read state from the Temporal Workflow (via Queries)
  rather than from an external database, enabling `get_state()` and
  `get_state_history()` to work transparently. Exception handling must be
  specific (catch only `RPCError` with `NOT_FOUND` status), not blanket
  `except Exception`.
- **FR-03.4**: The library must support `channel_versions` and
  `versions_seen` tracking to maintain correct node scheduling semantics
  (determining which nodes should fire based on which channels were updated).
- **FR-03.5**: The library should reuse LangGraph's actual `prepare_next_tasks`
  (with `for_execution=False`) and `apply_writes` functions from `_algo.py`
  inside the Workflow rather than reimplementing them, to guarantee behavioral
  equivalence and handle edge cases like `PUSH` tasks, `Overwrite`, and
  `consume()`/`finish()` lifecycle.
- **FR-03.6**: Dynamic `Send` (PUSH tasks) emitted from within node execution
  must be supported. When a node emits a `Send` during execution, the Workflow
  must create and dispatch additional Activities within the same superstep.

### FR-04: Human-in-the-Loop

The library must support LangGraph's `interrupt()` / `Command(resume=...)`
pattern via Temporal Signals.

- **FR-04.1**: When a node calls `interrupt(value)`, the Workflow must
  emit the interrupt value (via a Query or Signal response) and then block
  on a Temporal Signal, consuming zero resources.
- **FR-04.2**: When a client sends a resume Signal with a value, the Workflow
  must continue from the point of interruption, passing the resume value
  back to the `interrupt()` call site.
- **FR-04.3**: Multiple `interrupt()` calls within a single node must be
  supported, with resume values matched by order (consistent with existing
  LangGraph semantics).
- **FR-04.4**: `interrupt_before` and `interrupt_after` node lists must be
  supported, pausing the Workflow before or after the specified node Activities.
- **FR-04.5**: The library must provide a client-side helper to send resume
  Signals and retrieve interrupt values.

### FR-05: Streaming

The library must support LangGraph's streaming modes over Temporal.

- **FR-05.1**: `"values"` mode: Emit full state after each step.
- **FR-05.2**: `"updates"` mode: Emit per-node updates after each step.
- **FR-05.3**: `"custom"` mode: Emit custom data from `StreamWriter` calls
  within nodes.
- **FR-05.4**: `"messages"` mode: Emit LLM token-by-token output.
- **FR-05.5**: The streaming mechanism must work across process boundaries
  (the Workflow Worker and the client may be different processes).
- **FR-05.6**: Streaming must be implemented via one of: Temporal Queries
  (poll-based), an external message broker, or a side-channel (e.g., Redis
  Pub/Sub, server-sent events).

### FR-06: Subgraph Composition

The library must support LangGraph's subgraph composition.

- **FR-06.1**: Subgraphs must be executed as Temporal Child Workflows.
- **FR-06.2**: The parent Workflow must await the Child Workflow's completion
  and integrate its output into the parent's channel state.
- **FR-06.3**: Checkpoint namespace tracking (`checkpoint_ns` with `NS_SEP`)
  must map to Temporal's Workflow ID hierarchy.
- **FR-06.4**: Cancellation of a parent Workflow must propagate to all Child
  Workflows.

### FR-07: Error Handling and Retry

- **FR-07.1**: LangGraph's `RetryPolicy` (initial_interval, backoff_factor,
  max_interval, max_attempts, jitter, retry_on) must map to Temporal's
  `RetryPolicy` on Activities. When `retry_on` is a callable predicate (not
  just exception types), the Activity must evaluate the predicate locally and
  re-raise as either a retryable `ApplicationError` or a non-retryable
  `ApplicationError(non_retryable=True)`.
- **FR-07.2**: Non-retryable errors (e.g., `InvalidUpdateError`,
  `GraphRecursionError`) must be configured as non-retryable error types in
  Temporal.
- **FR-07.3**: Activity failures must be surfaced to the Workflow with full
  exception context, matching LangGraph's error propagation behavior.
- **FR-07.4**: The library must support Temporal's workflow-level timeouts
  (`execution_timeout`, `run_timeout`) as an addition to LangGraph's
  `recursion_limit`.
- **FR-07.5**: The library must provide a failure handling strategy for
  permanently failed nodes (after retry exhaustion), including: persisting the
  last known good state, providing an `on_node_failure` callback hook, and
  guidance on dead-letter queue patterns.

### FR-08: Continue-As-New for Long-Running Workflows

- **FR-08.1**: The Workflow must implement `workflow.continue_as_new()` at
  configurable intervals (default: every 500 steps or when event history
  approaches 40,000 events) to prevent exceeding Temporal's event history
  size limits (50,000 events default).
- **FR-08.2**: Continue-As-New must serialize the full channel state,
  `channel_versions`, `versions_seen`, step counter, and any pending
  interrupts as input to the new Workflow run.
- **FR-08.3**: `get_state_history()` must be able to traverse multiple
  Workflow runs (linked via Continue-As-New chain) to provide a complete
  state history.

### FR-09: Functional API Support

- **FR-09.1**: The `@entrypoint` decorator (from `langgraph.func`) must be
  supported, mapping the entrypoint function to a Temporal Workflow.
- **FR-09.2**: The `@task` decorator must be supported, mapping task functions
  to Temporal Activities.
- **FR-09.3**: `call()` / `SyncAsyncFuture` must be mapped to
  `workflow.execute_activity()`.

### FR-10: Configuration and Initialization

- **FR-10.1**: The library must provide a `TemporalGraph` wrapper (or similar)
  that accepts a compiled LangGraph graph and Temporal configuration.
- **FR-10.2**: Configuration must include: Temporal server address, namespace,
  task queue names, worker configuration, and per-node Activity options.
- **FR-10.3**: The library must provide helpers to start and manage Temporal
  Workers that can execute the graph's Activities.
- **FR-10.4**: The library must provide a `TemporalGraph.local()` factory
  method that starts Temporal's test server
  (`temporalio.testing.WorkflowEnvironment`) for local development, removing
  the need for a full Temporal server during development.

## 5. Non-Functional Requirements

### NFR-01: Performance

- Activity overhead (serialization + Temporal scheduling) must add less than
  50ms latency per node execution compared to native LangGraph execution for
  the average case.
- Streaming latency for token-by-token output must not exceed 200ms additional
  delay over native LangGraph streaming.

### NFR-02: Reliability

- Workflows must survive worker crashes, network partitions, and Temporal
  server restarts without data loss.
- All state transitions must be idempotent to support Temporal's replay
  mechanism.

### NFR-03: Observability

- All Workflow and Activity executions must be visible in the Temporal UI.
- The library must integrate with Temporal's search attributes to enable
  filtering by graph name, thread ID, and node name.
- OpenTelemetry integration must be supported for distributed tracing.
- Structured logging with correlation IDs (Workflow ID, node name, step)
  must be injected into all Activity executions.
- LangGraph-specific metrics must be emitted: per-node execution duration,
  channel state size, interrupt wait duration, serialization time. Support
  Prometheus and OpenTelemetry exporters.

### NFR-04: Scalability

- The library must support hundreds of concurrent Workflow executions per
  worker.
- Activity task queues must support horizontal scaling by adding workers.
- Large state payloads (>2MB) must be handled via Temporal's payload codec
  or an external blob store.

### NFR-05: Security

- State serialization must support encryption at rest via Temporal's
  `PayloadCodec` mechanism. An `EncryptionCodec` implementation using
  AES-GCM or Fernet must be provided out of the box, not as an optional
  add-on, for PII-containing workloads.
- The library must not transmit secrets (API keys, credentials) through
  Temporal's event history. Such values must be resolved at the worker level.
- Documentation must prominently warn that all channel state (including
  `messages` lists containing user PII) flows through Temporal's event history
  and is queryable/exportable. Encryption guidance must be provided for
  regulated industries.

### NFR-06: Compatibility

- Python 3.10+ required (matching LangGraph's minimum).
- Compatible with `temporalio` SDK version 1.7+.
- Compatible with `langgraph` version 1.0+.

## 6. User Stories

### US-01: Basic Graph Execution on Temporal

*As a developer, I want to take my existing LangGraph StateGraph and run it on
Temporal so that my agent workflow survives process crashes and restarts
automatically.*

Acceptance criteria:
- Compile an existing `StateGraph` to a Temporal Workflow with one function call.
- Start the Workflow via a Temporal client.
- Kill and restart the worker mid-execution; the workflow resumes from the
  last completed node.

### US-02: Human Approval Workflow

*As a developer, I want my agent to pause and wait for human approval before
executing a high-risk tool, without consuming compute resources while waiting.*

Acceptance criteria:
- Node calls `interrupt("Approve transfer of $10,000?")`.
- Workflow blocks on a Signal; the worker can be scaled to zero.
- Human sends a resume Signal with `"approved"`.
- Workflow continues and executes the tool.

### US-03: Distributed GPU Inference

*As a developer, I want to run my LLM inference nodes on GPU workers and my
data processing nodes on CPU workers.*

Acceptance criteria:
- Configure different task queues for different node types.
- Start GPU workers on machines with GPUs, CPU workers on standard machines.
- The Workflow automatically routes Activities to the correct task queue.

### US-04: Audit Trail for Compliance

*As a compliance officer, I need a complete, tamper-evident record of every
decision my AI agent made, including all LLM calls, tool invocations, and
state transitions.*

Acceptance criteria:
- Every node execution is recorded in Temporal's Event History.
- The full input/output of each Activity is queryable.
- Event History can be exported for audit purposes.

### US-05: Long-Running Multi-Step Agent

*As a developer, I want to run an agent that performs research over multiple
days, pausing overnight and resuming the next morning, without losing progress.*

Acceptance criteria:
- Workflow execution spans multiple days.
- Worker restarts, deployments, and scaling events do not affect the Workflow.
- The full execution history is preserved and queryable.

### US-06: Streaming Token Output to UI

*As a developer, I want to stream LLM token output from my Temporal-backed
agent to a web UI in real time.*

Acceptance criteria:
- Tokens are emitted with less than 200ms additional latency.
- The streaming mechanism works across process boundaries.
- Backpressure is handled gracefully.

## 7. API Compatibility Requirements

### 7.1 Preserved APIs

The following LangGraph APIs must work identically when using the Temporal
backend:

| API | Behavior |
|-----|----------|
| `graph.invoke(input, config)` | Execute graph as Temporal Workflow, return final state |
| `graph.stream(input, config)` | Stream graph execution events |
| `graph.get_state(config)` | Retrieve current state via Temporal Query |
| `graph.get_state_history(config)` | Retrieve state history from Temporal Event History |
| `graph.update_state(config, values)` | Send state update via Temporal Signal |
| `interrupt(value)` | Pause workflow, emit interrupt via Signal/Query |
| `Command(resume=value)` | Resume workflow via Temporal Signal |
| `RetryPolicy(...)` | Map to Temporal Activity RetryPolicy |

### 7.2 New APIs

| API | Purpose |
|-----|---------|
| `TemporalGraph(graph, client, ...)` | Wrap a compiled graph for Temporal execution |
| `TemporalGraph.start(input, config)` | Start a Workflow (non-blocking) |
| `TemporalGraph.create_worker(...)` | Create a Temporal Worker for this graph |
| `activity_as_tool(activity_fn)` | Convert a Temporal Activity into a LangGraph tool |
| `TemporalGraph.get_workflow_handle(config)` | Get Temporal WorkflowHandle for a thread |

## 8. Migration Path

### Phase 1: Drop-in Wrapper (v0.1)

- `TemporalGraph` wraps a compiled graph.
- `invoke()` and `stream()` work as before.
- No code changes to the graph definition.
- Requires Temporal server and worker setup.

### Phase 2: Activity Customization (v0.2)

- Per-node Activity options (timeouts, task queues, retry policies).
- `activity_as_tool()` for Temporal-native tools.
- Streaming support via external side-channel.

### Phase 3: Advanced Features (v0.3)

- Child Workflow support for subgraphs.
- Temporal Search Attributes for graph metadata.
- `TemporalCheckpointSaver` for full `get_state()` / `get_state_history()`
  compatibility.
- Functional API (`@entrypoint`, `@task`) support.