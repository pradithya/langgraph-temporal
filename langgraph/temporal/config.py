"""Configuration dataclasses for the LangGraph-Temporal integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any


@dataclass
class ActivityOptions:
    """Per-node Activity configuration options.

    Attributes:
        start_to_close_timeout: Maximum time for a single Activity execution.
        heartbeat_timeout: Maximum time between heartbeats before Activity is
            considered failed.
        schedule_to_close_timeout: Maximum time from scheduling to completion,
            including retries.
    """

    start_to_close_timeout: timedelta = field(
        default_factory=lambda: timedelta(seconds=300)
    )
    heartbeat_timeout: timedelta | None = None
    schedule_to_close_timeout: timedelta | None = None


@dataclass
class RestoredState:
    """State carried across continue-as-new boundaries.

    Attributes:
        checkpoint: The serialized Checkpoint TypedDict.
        step: The current step counter.
    """

    checkpoint: dict[str, Any]
    step: int


@dataclass
class RetryPolicyConfig:
    """Serializable retry policy configuration.

    Maps LangGraph's `RetryPolicy` to Temporal's `RetryPolicy` parameters.

    Attributes:
        initial_interval_seconds: Initial retry interval in seconds.
        backoff_coefficient: Multiplier for retry interval.
        max_interval_seconds: Maximum retry interval in seconds.
        max_attempts: Maximum number of retry attempts (0 = unlimited).
        non_retryable_error_types: Exception type names that should not be retried.
    """

    initial_interval_seconds: float = 1.0
    backoff_coefficient: float = 2.0
    max_interval_seconds: float = 100.0
    max_attempts: int = 0
    non_retryable_error_types: list[str] | None = None


@dataclass
class WorkflowInput:
    """Input to the LangGraphWorkflow Temporal Workflow.

    Attributes:
        graph_definition_ref: Reference to the registered graph in GraphRegistry.
        input_data: The user-provided input to the graph. None on continue-as-new.
        recursion_limit: Maximum number of steps before stopping.
        interrupt_before: Node names to pause before executing.
        interrupt_after: Node names to pause after executing.
        restored_state: State from a previous workflow run (continue-as-new).
        node_task_queues: Per-node task queue overrides.
        node_activity_options: Per-node Activity configuration (serialized).
        node_retry_policies: Per-node retry policy configuration.
    """

    graph_definition_ref: str
    input_data: dict[str, Any] | None = None
    recursion_limit: int = 25
    interrupt_before: list[str] | None = None
    interrupt_after: list[str] | None = None
    restored_state: RestoredState | None = None
    node_task_queues: dict[str, str] | None = None
    node_activity_options: dict[str, ActivityOptions] | None = None
    node_retry_policies: dict[str, RetryPolicyConfig] | None = None


@dataclass
class WorkflowOutput:
    """Output from the LangGraphWorkflow Temporal Workflow.

    Attributes:
        channel_values: The final channel state values.
        step: The final step counter.
    """

    channel_values: dict[str, Any]
    step: int


@dataclass
class NodeActivityInput:
    """Input to the execute_node Activity.

    Attributes:
        node_name: Name of the node to execute.
        input_state: Serialized channel values for the node's input.
        graph_definition_ref: Reference to retrieve the graph from GraphRegistry.
        resume_values: Resume values for interrupt() support.
        task_path: Deterministic path for ordering writes.
        send_input: Input override for dynamic Send (PUSH) tasks.
        triggers: Channel names that triggered this node.
    """

    node_name: str
    input_state: dict[str, Any]
    graph_definition_ref: str
    resume_values: list[Any] | None = None
    task_path: tuple[str | int, ...] = ()
    send_input: Any | None = None
    triggers: list[str] = field(default_factory=list)


@dataclass
class NodeActivityOutput:
    """Output from the execute_node Activity.

    Provides enough information for the Workflow to construct a
    WritesProtocol-compatible object for `apply_writes`.

    Attributes:
        node_name: Name of the node that executed.
        writes: Channel writes produced by the node.
        triggers: Channels that triggered this node.
        task_path: Deterministic path for ordering writes.
        interrupts: Interrupt payloads if node called interrupt().
        push_sends: Dynamic Send objects emitted during execution.
        command: Command metadata if node returned a Command.
    """

    node_name: str
    writes: list[tuple[str, Any]]
    triggers: list[str] = field(default_factory=list)
    task_path: tuple[str | int, ...] = ()
    interrupts: list[dict[str, Any]] | None = None
    push_sends: list[dict[str, Any]] | None = None
    command: dict[str, Any] | None = None
    custom_data: list[Any] | None = None


@dataclass
class ConditionalEdgeInput:
    """Input to the evaluate_conditional_edge Local Activity.

    Attributes:
        source_node: Name of the node whose output determines the edge.
        graph_definition_ref: Reference to the graph in GraphRegistry.
        channel_state: Current serialized channel state.
    """

    source_node: str
    graph_definition_ref: str
    channel_state: dict[str, Any]


@dataclass
class StateQueryResult:
    """Result of querying workflow state.

    Attributes:
        channel_values: Current channel state values.
        channel_versions: Channel version tracking dict.
        versions_seen: Per-node channel versions seen.
        step: Current step counter.
        status: Workflow status string.
        interrupts: Any pending interrupts.
    """

    channel_values: dict[str, Any]
    channel_versions: dict[str, Any]
    versions_seen: dict[str, dict[str, Any]]
    step: int
    status: str
    interrupts: list[dict[str, Any]]


@dataclass
class StreamQueryResult:
    """Result of polling the stream buffer.

    Attributes:
        events: Stream events since the cursor.
        next_cursor: Cursor position for the next poll.
    """

    events: list[Any]
    next_cursor: int


@dataclass
class StateUpdatePayload:
    """Payload for update_state Signal.

    Attributes:
        writes: Channel writes to apply.
    """

    writes: list[tuple[str, Any]]


@dataclass
class StreamEvent:
    """A single stream event.

    Attributes:
        mode: Stream mode (values, updates, custom, messages).
        data: Event data payload.
        node_name: Node that produced the event, if applicable.
        step: Step at which the event was produced.
    """

    mode: str
    data: Any
    node_name: str | None = None
    step: int = 0
