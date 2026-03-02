"""Tests for the LangGraphWorkflow Temporal Workflow."""

from __future__ import annotations

from langgraph.temporal.config import (
    StreamEvent,
    WorkflowInput,
    WorkflowOutput,
)
from langgraph.temporal.workflow import (
    CONTINUE_AS_NEW_THRESHOLD,
    _get_channel_values,
    increment,
)


class TestIncrement:
    def test_increment_from_none(self) -> None:
        assert increment(None, "any") == 1

    def test_increment_from_value(self) -> None:
        assert increment(3, "any") == 4

    def test_increment_from_zero(self) -> None:
        assert increment(0, "any") == 1


class TestGetChannelValues:
    def test_with_values(self) -> None:
        from unittest.mock import MagicMock

        ch1 = MagicMock()
        ch1.get.return_value = "hello"

        ch2 = MagicMock()
        ch2.get.return_value = 42

        channels = {"messages": ch1, "count": ch2}
        result = _get_channel_values(channels)  # type: ignore[arg-type]

        assert result == {"messages": "hello", "count": 42}

    def test_with_missing_values(self) -> None:
        from unittest.mock import MagicMock

        from langgraph._internal._typing import MISSING

        ch1 = MagicMock()
        ch1.get.return_value = "hello"

        ch2 = MagicMock()
        ch2.get.return_value = MISSING

        channels = {"messages": ch1, "empty": ch2}
        result = _get_channel_values(channels)  # type: ignore[arg-type]

        assert "messages" in result
        assert "empty" not in result

    def test_with_error_channel(self) -> None:
        from unittest.mock import MagicMock

        ch1 = MagicMock()
        ch1.get.return_value = "hello"

        ch2 = MagicMock()
        ch2.get.side_effect = Exception("channel error")

        channels = {"messages": ch1, "broken": ch2}
        result = _get_channel_values(channels)  # type: ignore[arg-type]

        assert "messages" in result
        assert "broken" not in result

    def test_empty_channels(self) -> None:
        result = _get_channel_values({})
        assert result == {}


class TestContinueAsNewThreshold:
    def test_threshold_value(self) -> None:
        assert CONTINUE_AS_NEW_THRESHOLD == 500


class TestWorkflowInputOutput:
    def test_workflow_input_for_continue_as_new(self) -> None:
        """Test creating WorkflowInput for continue-as-new scenario."""
        from langgraph.temporal.config import RestoredState

        wi = WorkflowInput(
            graph_definition_ref="ref",
            input_data=None,  # No input on continue-as-new
            recursion_limit=100,
            restored_state=RestoredState(
                checkpoint={
                    "v": 4,
                    "id": "step-50",
                    "ts": "2024-01-01T00:00:00",
                    "channel_values": {"messages": ["hello"]},
                    "channel_versions": {"messages": 5},
                    "versions_seen": {"agent": {"messages": 4}},
                },
                step=50,
            ),
        )
        assert wi.input_data is None
        assert wi.restored_state is not None
        assert wi.restored_state.step == 50

    def test_workflow_output(self) -> None:
        wo = WorkflowOutput(
            channel_values={"messages": ["final"]},
            step=10,
        )
        assert wo.channel_values["messages"] == ["final"]
        assert wo.step == 10


class TestToTemporalRetryPolicy:
    def test_basic_conversion(self) -> None:
        from datetime import timedelta

        from langgraph.temporal.config import RetryPolicyConfig
        from langgraph.temporal.workflow import _to_temporal_retry_policy

        config = RetryPolicyConfig(
            initial_interval_seconds=2.0,
            backoff_coefficient=3.0,
            max_interval_seconds=60.0,
            max_attempts=5,
        )
        policy = _to_temporal_retry_policy(config)
        assert policy.initial_interval == timedelta(seconds=2)
        assert policy.backoff_coefficient == 3.0
        assert policy.maximum_interval == timedelta(seconds=60)
        assert policy.maximum_attempts == 5

    def test_non_retryable_error_types(self) -> None:
        from langgraph.temporal.config import RetryPolicyConfig
        from langgraph.temporal.workflow import _to_temporal_retry_policy

        config = RetryPolicyConfig(
            non_retryable_error_types=["ValueError", "TypeError"],
        )
        policy = _to_temporal_retry_policy(config)
        assert policy.non_retryable_error_types == ["ValueError", "TypeError"]

    def test_empty_non_retryable(self) -> None:
        from langgraph.temporal.config import RetryPolicyConfig
        from langgraph.temporal.workflow import _to_temporal_retry_policy

        config = RetryPolicyConfig()
        policy = _to_temporal_retry_policy(config)
        assert policy.non_retryable_error_types == []


class TestExtractCommandGotos:
    def test_no_commands(self) -> None:
        from langgraph.temporal.config import NodeActivityOutput
        from langgraph.temporal.workflow import LangGraphWorkflow

        wf = LangGraphWorkflow.__new__(LangGraphWorkflow)
        results = [
            NodeActivityOutput(node_name="a", writes=[("x", 1)]),
        ]
        gotos = wf._extract_command_gotos(results)
        assert gotos == []

    def test_string_goto(self) -> None:
        from langgraph.temporal.config import NodeActivityOutput
        from langgraph.temporal.workflow import LangGraphWorkflow

        wf = LangGraphWorkflow.__new__(LangGraphWorkflow)
        results = [
            NodeActivityOutput(
                node_name="a",
                writes=[],
                command={"goto": "next_node", "update": None},
            ),
        ]
        gotos = wf._extract_command_gotos(results)
        assert len(gotos) == 1
        assert gotos[0] == {"node": "next_node", "arg": None}

    def test_list_goto(self) -> None:
        from langgraph.temporal.config import NodeActivityOutput
        from langgraph.temporal.workflow import LangGraphWorkflow

        wf = LangGraphWorkflow.__new__(LangGraphWorkflow)
        results = [
            NodeActivityOutput(
                node_name="a",
                writes=[],
                command={
                    "goto": [
                        {"node": "b", "arg": "data1"},
                        "c",
                    ],
                    "update": None,
                },
            ),
        ]
        gotos = wf._extract_command_gotos(results)
        assert len(gotos) == 2
        assert gotos[0] == {"node": "b", "arg": "data1"}
        assert gotos[1] == {"node": "c", "arg": None}


class TestStreamEvent:
    def test_values_event(self) -> None:
        event = StreamEvent(
            mode="values",
            data={"messages": ["hello"]},
            step=0,
        )
        assert event.mode == "values"

    def test_updates_event(self) -> None:
        event = StreamEvent(
            mode="updates",
            data={"messages": "new"},
            node_name="agent",
            step=1,
        )
        assert event.node_name == "agent"
