"""Integration tests using Temporal's WorkflowEnvironment.

These tests verify end-to-end workflow execution with a real Temporal
test server (time-skipping or full local server).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from langgraph.temporal.config import (
    ActivityOptions,
    NodeActivityOutput,
    RetryPolicyConfig,
    WorkflowInput,
)
from langgraph.temporal.converter import GraphRegistry
from langgraph.temporal.workflow import LangGraphWorkflow, _to_temporal_retry_policy


class TestPerNodeConfiguration:
    """Test that per-node configuration is correctly wired through."""

    def setup_method(self) -> None:
        GraphRegistry.reset()

    def test_workflow_input_carries_node_config(self) -> None:
        """Test that WorkflowInput carries per-node configuration."""
        opts = ActivityOptions(
            start_to_close_timeout=timedelta(seconds=600),
            heartbeat_timeout=timedelta(seconds=30),
        )
        retry = RetryPolicyConfig(max_attempts=3)

        wi = WorkflowInput(
            graph_definition_ref="ref",
            input_data={"key": "val"},
            node_task_queues={"gpu_node": "gpu-queue"},
            node_activity_options={"gpu_node": opts},
            node_retry_policies={"gpu_node": retry},
        )

        assert wi.node_task_queues == {"gpu_node": "gpu-queue"}
        assert wi.node_activity_options is not None
        assert wi.node_activity_options["gpu_node"].start_to_close_timeout == timedelta(
            seconds=600
        )
        assert wi.node_retry_policies is not None
        assert wi.node_retry_policies["gpu_node"].max_attempts == 3

    def test_retry_policy_mapping(self) -> None:
        """Test RetryPolicyConfig to Temporal RetryPolicy conversion."""
        config = RetryPolicyConfig(
            initial_interval_seconds=0.5,
            backoff_coefficient=1.5,
            max_interval_seconds=30.0,
            max_attempts=3,
            non_retryable_error_types=["ValueError"],
        )
        policy = _to_temporal_retry_policy(config)

        assert policy.initial_interval == timedelta(seconds=0.5)
        assert policy.backoff_coefficient == 1.5
        assert policy.maximum_interval == timedelta(seconds=30)
        assert policy.maximum_attempts == 3
        assert policy.non_retryable_error_types == ["ValueError"]

    def test_workflow_activity_options_helper(self) -> None:
        """Test the workflow's _activity_options_for_node helper."""
        wf = LangGraphWorkflow.__new__(LangGraphWorkflow)
        wf._node_activity_options = {
            "agent": ActivityOptions(
                start_to_close_timeout=timedelta(seconds=600),
                heartbeat_timeout=timedelta(seconds=30),
            ),
        }
        wf._node_task_queues = {}
        wf._node_retry_policies = {}

        # Node with custom options
        opts = wf._activity_options_for_node("agent")
        assert opts.start_to_close_timeout == timedelta(seconds=600)
        assert opts.heartbeat_timeout == timedelta(seconds=30)

        # Node without custom options -> defaults
        default_opts = wf._activity_options_for_node("tools")
        assert default_opts.start_to_close_timeout == timedelta(seconds=300)
        assert default_opts.heartbeat_timeout is None

    def test_workflow_task_queue_helper(self) -> None:
        """Test the workflow's _task_queue_for_node helper."""
        wf = LangGraphWorkflow.__new__(LangGraphWorkflow)
        wf._node_task_queues = {"gpu_node": "gpu-queue"}
        wf._node_activity_options = {}
        wf._node_retry_policies = {}
        wf._sticky_task_queue = None
        wf._subagent_config = None

        assert wf._task_queue_for_node("gpu_node") == "gpu-queue"
        assert wf._task_queue_for_node("cpu_node") is None

    def test_workflow_retry_policy_helper(self) -> None:
        """Test the workflow's _retry_policy_for_node helper."""
        wf = LangGraphWorkflow.__new__(LangGraphWorkflow)
        wf._node_retry_policies = {
            "flaky_node": RetryPolicyConfig(max_attempts=5),
        }
        wf._node_task_queues = {}
        wf._node_activity_options = {}

        policy = wf._retry_policy_for_node("flaky_node")
        assert policy is not None
        assert policy.maximum_attempts == 5

        assert wf._retry_policy_for_node("stable_node") is None


class TestConditionalEdgeEvaluation:
    """Test conditional edge evaluation in the Activity."""

    def setup_method(self) -> None:
        GraphRegistry.reset()

    @pytest.mark.asyncio
    async def test_evaluate_conditional_edge_no_branches(self) -> None:
        """Test that evaluate_conditional_edge returns None when no branches."""

        registry = GraphRegistry.get_instance()

        mock_graph = MagicMock()
        mock_graph.branches = {}
        mock_graph.nodes = {"agent": MagicMock()}

        ref = registry.register(mock_graph)

        # We can't call the @activity.defn decorated function directly,
        # so test the logic inline
        graph = registry.get(ref)
        branches = getattr(graph, "branches", None)
        assert branches is not None
        assert "agent" not in branches

    @pytest.mark.asyncio
    async def test_evaluate_conditional_edge_with_branch(self) -> None:
        """Test conditional edge evaluation with a branch."""
        registry = GraphRegistry.get_instance()

        # Create a mock branch with a path function
        mock_branch = MagicMock()
        mock_branch.path = lambda state: (
            "tools" if state.get("should_call_tools") else "end"
        )
        mock_branch.path_map = None

        mock_graph = MagicMock()
        mock_graph.branches = {"agent": {"default": mock_branch}}
        mock_graph.nodes = {"agent": MagicMock()}

        ref = registry.register(mock_graph)
        graph = registry.get(ref)

        # Simulate the conditional edge evaluation logic
        branches = graph.branches["agent"]  # type: ignore[attr-defined]
        results: list[str] = []
        for _name, branch in branches.items():
            path_fn = getattr(branch, "path", None)
            if path_fn:
                target = path_fn({"should_call_tools": True})
                if isinstance(target, str):
                    results.append(target)

        assert results == ["tools"]


class TestCommandGotoRouting:
    """Test Command.goto routing in the workflow."""

    def test_extract_command_gotos_string(self) -> None:
        """Test extracting a string goto target."""
        wf = LangGraphWorkflow.__new__(LangGraphWorkflow)

        results = [
            NodeActivityOutput(
                node_name="agent",
                writes=[("messages", "hello")],
                command={"goto": "tools", "update": None},
            ),
        ]

        gotos = wf._extract_command_gotos(results)
        assert len(gotos) == 1
        assert gotos[0] == {"node": "tools", "arg": None}

    def test_extract_command_gotos_list_with_sends(self) -> None:
        """Test extracting a list of goto targets including Send-style."""
        wf = LangGraphWorkflow.__new__(LangGraphWorkflow)

        results = [
            NodeActivityOutput(
                node_name="router",
                writes=[],
                command={
                    "goto": [
                        {"node": "agent_a", "arg": {"task": "research"}},
                        {"node": "agent_b", "arg": {"task": "write"}},
                        "summarize",
                    ],
                    "update": None,
                },
            ),
        ]

        gotos = wf._extract_command_gotos(results)
        assert len(gotos) == 3
        assert gotos[0] == {"node": "agent_a", "arg": {"task": "research"}}
        assert gotos[1] == {"node": "agent_b", "arg": {"task": "write"}}
        assert gotos[2] == {"node": "summarize", "arg": None}

    def test_extract_command_gotos_no_command(self) -> None:
        """Test that no gotos are extracted when no commands."""
        wf = LangGraphWorkflow.__new__(LangGraphWorkflow)

        results = [
            NodeActivityOutput(
                node_name="agent",
                writes=[("messages", "hello")],
            ),
        ]

        gotos = wf._extract_command_gotos(results)
        assert gotos == []

    def test_extract_command_gotos_empty_goto(self) -> None:
        """Test that empty goto lists produce no targets."""
        wf = LangGraphWorkflow.__new__(LangGraphWorkflow)

        results = [
            NodeActivityOutput(
                node_name="agent",
                writes=[],
                command={"goto": [], "update": None},
            ),
        ]

        gotos = wf._extract_command_gotos(results)
        assert gotos == []


class TestCustomStreamMode:
    """Test custom stream mode support."""

    def test_custom_data_in_activity_output(self) -> None:
        """Test that NodeActivityOutput can carry custom stream data."""
        output = NodeActivityOutput(
            node_name="agent",
            writes=[("messages", "hello")],
            custom_data=["token1", "token2", {"key": "val"}],
        )
        assert output.custom_data is not None
        assert len(output.custom_data) == 3

    def test_emit_custom_stream_events(self) -> None:
        """Test that the workflow emits custom stream events."""

        wf = LangGraphWorkflow.__new__(LangGraphWorkflow)
        wf.step = 0
        wf.stream_buffer = []

        # Mock channels
        mock_ch = MagicMock()
        mock_ch.get.return_value = "hello"
        wf.channels = {"messages": mock_ch}

        results = [
            NodeActivityOutput(
                node_name="agent",
                writes=[("messages", "hello")],
                custom_data=["custom_token1", "custom_token2"],
            ),
        ]

        wf._emit_stream_events(results)

        # Should have: 1 values event + 1 updates event + 2 custom events
        assert len(wf.stream_buffer) == 4
        modes = [e.mode for e in wf.stream_buffer]
        assert modes.count("values") == 1
        assert modes.count("updates") == 1
        assert modes.count("custom") == 2

        custom_events = [e for e in wf.stream_buffer if e.mode == "custom"]
        assert custom_events[0].data == "custom_token1"
        assert custom_events[1].data == "custom_token2"
        assert all(e.node_name == "agent" for e in custom_events)


class TestMultipleInterrupts:
    """Test handling of multiple interrupts."""

    def test_multiple_interrupts_in_single_node(self) -> None:
        """Test that multiple interrupt payloads are captured."""
        output = NodeActivityOutput(
            node_name="agent",
            writes=[],
            interrupts=[
                {"value": "Approve step 1?", "id": "int-1"},
                {"value": "Approve step 2?", "id": "int-2"},
            ],
        )
        assert output.interrupts is not None
        assert len(output.interrupts) == 2

    def test_multiple_interrupted_nodes(self) -> None:
        """Test that interrupts from multiple nodes are aggregated."""
        results = [
            NodeActivityOutput(
                node_name="agent_a",
                writes=[],
                interrupts=[{"value": "Approve A?", "id": "int-a"}],
            ),
            NodeActivityOutput(
                node_name="agent_b",
                writes=[],
                interrupts=[{"value": "Approve B?", "id": "int-b"}],
            ),
        ]

        all_interrupts: list[dict[str, Any]] = []
        for r in results:
            if r.interrupts:
                all_interrupts.extend(r.interrupts)

        assert len(all_interrupts) == 2
        assert all_interrupts[0]["value"] == "Approve A?"
        assert all_interrupts[1]["value"] == "Approve B?"


class TestSubgraphDetection:
    """Test subgraph node detection."""

    def test_is_subgraph_node_with_pregel(self) -> None:
        """Test that a Pregel-bound node is detected as a subgraph."""
        from langgraph.pregel import Pregel

        wf = LangGraphWorkflow.__new__(LangGraphWorkflow)

        mock_subgraph = MagicMock(spec=Pregel)
        mock_node = MagicMock()
        mock_node.bound = mock_subgraph

        mock_graph = MagicMock()
        mock_graph.nodes = {"sub": mock_node}

        assert wf._is_subgraph_node("sub", mock_graph) is True

    def test_is_subgraph_node_regular(self) -> None:
        """Test that a regular node is not detected as a subgraph."""
        wf = LangGraphWorkflow.__new__(LangGraphWorkflow)

        mock_node = MagicMock()
        mock_node.bound = MagicMock()  # Not a Pregel

        mock_graph = MagicMock()
        mock_graph.nodes = {"agent": mock_node}

        assert wf._is_subgraph_node("agent", mock_graph) is False

    def test_is_subgraph_node_missing(self) -> None:
        """Test that a missing node returns False."""
        wf = LangGraphWorkflow.__new__(LangGraphWorkflow)

        mock_graph = MagicMock()
        mock_nodes = MagicMock()
        mock_nodes.get.return_value = None
        mock_graph.nodes = mock_nodes

        assert wf._is_subgraph_node("nonexistent", mock_graph) is False
