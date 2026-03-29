"""Tests for configuration dataclasses and serialization round-trips."""

from __future__ import annotations

from datetime import timedelta

from langgraph.temporal.config import (
    ActivityOptions,
    ConditionalEdgeInput,
    NodeActivityInput,
    NodeActivityOutput,
    RestoredState,
    StateQueryResult,
    StateUpdatePayload,
    StreamEvent,
    StreamQueryResult,
    SubAgentConfig,
    WorkflowInput,
    WorkflowOutput,
)


class TestActivityOptions:
    def test_defaults(self) -> None:
        opts = ActivityOptions()
        assert opts.start_to_close_timeout == timedelta(seconds=300)
        assert opts.heartbeat_timeout is None
        assert opts.schedule_to_close_timeout is None

    def test_custom_values(self) -> None:
        opts = ActivityOptions(
            start_to_close_timeout=timedelta(seconds=60),
            heartbeat_timeout=timedelta(seconds=10),
            schedule_to_close_timeout=timedelta(minutes=30),
        )
        assert opts.start_to_close_timeout == timedelta(seconds=60)
        assert opts.heartbeat_timeout == timedelta(seconds=10)
        assert opts.schedule_to_close_timeout == timedelta(minutes=30)


class TestWorkflowInput:
    def test_defaults(self) -> None:
        wi = WorkflowInput(graph_definition_ref="test_ref")
        assert wi.graph_definition_ref == "test_ref"
        assert wi.input_data is None
        assert wi.recursion_limit == 25
        assert wi.interrupt_before is None
        assert wi.interrupt_after is None
        assert wi.restored_state is None

    def test_full_input(self) -> None:
        wi = WorkflowInput(
            graph_definition_ref="test_ref",
            input_data={"key": "value"},
            recursion_limit=50,
            interrupt_before=["node_a"],
            interrupt_after=["node_b"],
            restored_state=RestoredState(
                checkpoint={"v": 4, "id": "1", "ts": "", "channel_values": {}},
                step=5,
            ),
        )
        assert wi.input_data == {"key": "value"}
        assert wi.recursion_limit == 50
        assert wi.interrupt_before == ["node_a"]
        assert wi.restored_state is not None
        assert wi.restored_state.step == 5


class TestWorkflowOutput:
    def test_creation(self) -> None:
        wo = WorkflowOutput(channel_values={"messages": ["hello"]}, step=3)
        assert wo.channel_values == {"messages": ["hello"]}
        assert wo.step == 3


class TestNodeActivityInput:
    def test_defaults(self) -> None:
        nai = NodeActivityInput(
            node_name="agent",
            input_state={"key": "value"},
            graph_definition_ref="test_ref",
        )
        assert nai.node_name == "agent"
        assert nai.resume_values is None
        assert nai.task_path == ()
        assert nai.send_input is None
        assert nai.triggers == []

    def test_with_resume(self) -> None:
        nai = NodeActivityInput(
            node_name="agent",
            input_state={},
            graph_definition_ref="ref",
            resume_values=["yes", "no"],
            task_path=("agent", 0),
        )
        assert nai.resume_values == ["yes", "no"]
        assert nai.task_path == ("agent", 0)


class TestNodeActivityOutput:
    def test_successful_output(self) -> None:
        nao = NodeActivityOutput(
            node_name="agent",
            writes=[("messages", "hello")],
            triggers=["start:agent"],
            task_path=("agent", 0),
        )
        assert nao.node_name == "agent"
        assert nao.writes == [("messages", "hello")]
        assert nao.interrupts is None
        assert nao.push_sends is None

    def test_interrupted_output(self) -> None:
        nao = NodeActivityOutput(
            node_name="agent",
            writes=[],
            interrupts=[{"value": "Approve?", "id": "int-1"}],
        )
        assert nao.interrupts is not None
        assert len(nao.interrupts) == 1

    def test_with_push_sends(self) -> None:
        nao = NodeActivityOutput(
            node_name="agent",
            writes=[("messages", "hello")],
            push_sends=[{"node": "tools", "arg": {"tool": "search"}}],
        )
        assert nao.push_sends is not None
        assert nao.push_sends[0]["node"] == "tools"


class TestConditionalEdgeInput:
    def test_creation(self) -> None:
        cei = ConditionalEdgeInput(
            source_node="agent",
            graph_definition_ref="ref",
            channel_state={"messages": []},
        )
        assert cei.source_node == "agent"


class TestStateQueryResult:
    def test_creation(self) -> None:
        sqr = StateQueryResult(
            channel_values={"messages": ["hi"]},
            channel_versions={"messages": 1},
            versions_seen={"agent": {"messages": 0}},
            step=2,
            status="running",
            interrupts=[],
        )
        assert sqr.step == 2
        assert sqr.status == "running"


class TestStreamQueryResult:
    def test_creation(self) -> None:
        sqr = StreamQueryResult(events=["a", "b"], next_cursor=2)
        assert len(sqr.events) == 2
        assert sqr.next_cursor == 2


class TestStateUpdatePayload:
    def test_creation(self) -> None:
        sup = StateUpdatePayload(writes=[("messages", "update")])
        assert sup.writes == [("messages", "update")]


class TestRetryPolicyConfig:
    def test_defaults(self) -> None:
        from langgraph.temporal.config import RetryPolicyConfig

        rpc = RetryPolicyConfig()
        assert rpc.initial_interval_seconds == 1.0
        assert rpc.backoff_coefficient == 2.0
        assert rpc.max_interval_seconds == 100.0
        assert rpc.max_attempts == 0
        assert rpc.non_retryable_error_types is None

    def test_custom_values(self) -> None:
        from langgraph.temporal.config import RetryPolicyConfig

        rpc = RetryPolicyConfig(
            initial_interval_seconds=0.5,
            backoff_coefficient=1.5,
            max_interval_seconds=30.0,
            max_attempts=3,
            non_retryable_error_types=["ValueError"],
        )
        assert rpc.max_attempts == 3
        assert rpc.non_retryable_error_types == ["ValueError"]


class TestWorkflowInputNodeConfig:
    def test_node_task_queues(self) -> None:
        wi = WorkflowInput(
            graph_definition_ref="ref",
            node_task_queues={"gpu": "gpu-queue"},
        )
        assert wi.node_task_queues == {"gpu": "gpu-queue"}

    def test_node_activity_options(self) -> None:
        wi = WorkflowInput(
            graph_definition_ref="ref",
            node_activity_options={
                "agent": ActivityOptions(heartbeat_timeout=timedelta(seconds=30))
            },
        )
        assert wi.node_activity_options is not None
        assert wi.node_activity_options["agent"].heartbeat_timeout == timedelta(
            seconds=30
        )

    def test_node_retry_policies(self) -> None:
        from langgraph.temporal.config import RetryPolicyConfig

        wi = WorkflowInput(
            graph_definition_ref="ref",
            node_retry_policies={
                "agent": RetryPolicyConfig(max_attempts=3),
            },
        )
        assert wi.node_retry_policies is not None
        assert wi.node_retry_policies["agent"].max_attempts == 3


class TestNodeActivityOutputCustomData:
    def test_with_custom_data(self) -> None:
        output = NodeActivityOutput(
            node_name="test",
            writes=[],
            custom_data=["data1", "data2"],
        )
        assert output.custom_data == ["data1", "data2"]

    def test_without_custom_data(self) -> None:
        output = NodeActivityOutput(
            node_name="test",
            writes=[],
        )
        assert output.custom_data is None


class TestSubAgentConfig:
    def test_defaults(self) -> None:
        sac = SubAgentConfig()
        assert sac.task_queue is None
        assert sac.sticky_task_queue is None
        assert sac.execution_timeout_seconds == 1800.0

    def test_custom_values(self) -> None:
        sac = SubAgentConfig(
            task_queue="subagent-queue",
            sticky_task_queue="sticky-subagent",
            execution_timeout_seconds=900.0,
        )
        assert sac.task_queue == "subagent-queue"
        assert sac.sticky_task_queue == "sticky-subagent"
        assert sac.execution_timeout_seconds == 900.0


class TestRestoredStateStickyQueue:
    def test_sticky_task_queue_default(self) -> None:
        rs = RestoredState(checkpoint={"v": 1}, step=0)
        assert rs.sticky_task_queue is None

    def test_sticky_task_queue_set(self) -> None:
        rs = RestoredState(
            checkpoint={"v": 1},
            step=5,
            sticky_task_queue="deep-agent-workspace-42",
        )
        assert rs.sticky_task_queue == "deep-agent-workspace-42"


class TestWorkflowInputStickyQueue:
    def test_sticky_task_queue(self) -> None:
        wi = WorkflowInput(
            graph_definition_ref="ref",
            sticky_task_queue="sticky-queue",
        )
        assert wi.sticky_task_queue == "sticky-queue"

    def test_subagent_config(self) -> None:
        sac = SubAgentConfig(task_queue="sub-queue")
        wi = WorkflowInput(
            graph_definition_ref="ref",
            subagent_config=sac,
        )
        assert wi.subagent_config is not None
        assert wi.subagent_config.task_queue == "sub-queue"


class TestNodeActivityOutputChildWorkflowRequests:
    def test_with_child_workflow_requests(self) -> None:
        output = NodeActivityOutput(
            node_name="tools",
            writes=[],
            child_workflow_requests=[
                {"subagent_type": "researcher", "instruction": "do research"}
            ],
        )
        assert output.child_workflow_requests is not None
        assert len(output.child_workflow_requests) == 1

    def test_without_child_workflow_requests(self) -> None:
        output = NodeActivityOutput(
            node_name="tools",
            writes=[],
        )
        assert output.child_workflow_requests is None


class TestStreamEvent:
    def test_defaults(self) -> None:
        se = StreamEvent(mode="values", data={"key": "val"})
        assert se.mode == "values"
        assert se.node_name is None
        assert se.step == 0

    def test_with_node(self) -> None:
        se = StreamEvent(mode="updates", data={}, node_name="agent", step=3)
        assert se.node_name == "agent"
        assert se.step == 3
