"""Tests for TemporalGraph wrapper."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from langgraph.temporal.config import ActivityOptions, WorkflowOutput
from langgraph.temporal.converter import GraphRegistry
from langgraph.temporal.graph import TemporalGraph


class TestTemporalGraphInit:
    def setup_method(self) -> None:
        GraphRegistry.reset()

    def test_defaults(self) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_graph.interrupt_before_nodes = []
        mock_graph.interrupt_after_nodes = []
        mock_client = MagicMock()

        tg = TemporalGraph(mock_graph, mock_client)

        assert tg.task_queue == "langgraph-default"
        assert tg.node_task_queues == {}
        assert tg.node_activity_options == {}
        assert tg.workflow_execution_timeout is None
        assert tg.workflow_run_timeout is None

    def test_custom_config(self) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_client = MagicMock()

        tg = TemporalGraph(
            mock_graph,
            mock_client,
            task_queue="custom-queue",
            node_task_queues={"gpu_node": "gpu-queue"},
            node_activity_options={
                "gpu_node": ActivityOptions(
                    start_to_close_timeout=timedelta(seconds=600)
                )
            },
            workflow_execution_timeout=timedelta(hours=24),
        )

        assert tg.task_queue == "custom-queue"
        assert tg.node_task_queues["gpu_node"] == "gpu-queue"
        assert tg.workflow_execution_timeout == timedelta(hours=24)

    def test_graph_registered(self) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_client = MagicMock()

        tg = TemporalGraph(mock_graph, mock_client)

        # The graph should be retrievable from registry
        graph = GraphRegistry.get_instance().get(tg._graph_ref)
        assert graph is mock_graph


class TestTemporalGraphWorkflowId:
    def setup_method(self) -> None:
        GraphRegistry.reset()

    def test_workflow_id_from_config(self) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_client = MagicMock()

        tg = TemporalGraph(mock_graph, mock_client)

        config = {"configurable": {"thread_id": "my-thread-123"}}
        wf_id = tg._get_workflow_id(config)
        assert wf_id == "my-thread-123"

    def test_workflow_id_generated(self) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_client = MagicMock()

        tg = TemporalGraph(mock_graph, mock_client)

        wf_id = tg._get_workflow_id(None)
        assert wf_id.startswith("langgraph-")

    def test_workflow_id_no_thread_id(self) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_client = MagicMock()

        tg = TemporalGraph(mock_graph, mock_client)

        config: dict[str, Any] = {"configurable": {}}
        wf_id = tg._get_workflow_id(config)
        assert wf_id.startswith("langgraph-")


class TestTemporalGraphBuildInput:
    def setup_method(self) -> None:
        GraphRegistry.reset()

    def test_build_basic_input(self) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_graph.interrupt_before_nodes = []
        mock_graph.interrupt_after_nodes = []
        mock_client = MagicMock()

        tg = TemporalGraph(mock_graph, mock_client)

        wi = tg._build_workflow_input({"key": "val"})
        assert wi.input_data == {"key": "val"}
        assert wi.recursion_limit == 25
        assert wi.interrupt_before is None
        assert wi.interrupt_after is None

    def test_build_input_with_interrupts(self) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_graph.interrupt_before_nodes = ["agent"]
        mock_graph.interrupt_after_nodes = ["tools"]
        mock_client = MagicMock()

        tg = TemporalGraph(mock_graph, mock_client)

        wi = tg._build_workflow_input({})
        assert wi.interrupt_before == ["agent"]
        assert wi.interrupt_after == ["tools"]

    def test_build_input_override_interrupts(self) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_graph.interrupt_before_nodes = ["agent"]
        mock_graph.interrupt_after_nodes = []
        mock_client = MagicMock()

        tg = TemporalGraph(mock_graph, mock_client)

        wi = tg._build_workflow_input({}, interrupt_before=["custom_node"])
        assert wi.interrupt_before == ["custom_node"]

    def test_build_input_with_recursion_limit(self) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_graph.interrupt_before_nodes = []
        mock_graph.interrupt_after_nodes = []
        mock_client = MagicMock()

        tg = TemporalGraph(mock_graph, mock_client)

        config = {"recursion_limit": 50, "configurable": {}}
        wi = tg._build_workflow_input({}, config)
        assert wi.recursion_limit == 50


class TestTemporalGraphInvoke:
    def setup_method(self) -> None:
        GraphRegistry.reset()

    @pytest.mark.asyncio
    async def test_ainvoke(self) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_graph.interrupt_before_nodes = []
        mock_graph.interrupt_after_nodes = []
        mock_client = AsyncMock()
        mock_client.execute_workflow = AsyncMock(
            return_value=WorkflowOutput(
                channel_values={"messages": ["result"]},
                step=3,
            )
        )

        tg = TemporalGraph(mock_graph, mock_client)

        result = await tg.ainvoke(
            {"messages": ["hello"]},
            {"configurable": {"thread_id": "test-thread"}},
        )

        assert result == {"messages": ["result"]}
        mock_client.execute_workflow.assert_called_once()


class TestTemporalGraphResume:
    def setup_method(self) -> None:
        GraphRegistry.reset()

    @pytest.mark.asyncio
    async def test_resume(self) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_client = MagicMock()

        mock_handle = AsyncMock()
        mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)

        tg = TemporalGraph(mock_graph, mock_client)

        config = {"configurable": {"thread_id": "test-thread"}}
        await tg.resume(config, "approved")

        mock_handle.signal.assert_called_once()


class TestTemporalGraphGetState:
    def setup_method(self) -> None:
        GraphRegistry.reset()

    @pytest.mark.asyncio
    async def test_get_state(self) -> None:
        from langgraph.temporal.config import StateQueryResult

        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_client = MagicMock()

        mock_handle = AsyncMock()
        mock_handle.query = AsyncMock(
            return_value=StateQueryResult(
                channel_values={"messages": ["hi"]},
                channel_versions={"messages": 1},
                versions_seen={},
                step=2,
                status="running",
                interrupts=[],
            )
        )
        mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)

        tg = TemporalGraph(mock_graph, mock_client)

        config = {"configurable": {"thread_id": "test-thread"}}
        state = await tg.get_state(config)

        assert state["values"] == {"messages": ["hi"]}
        assert state["step"] == 2
        assert state["status"] == "running"


class TestTemporalGraphUpdateState:
    def setup_method(self) -> None:
        GraphRegistry.reset()

    @pytest.mark.asyncio
    async def test_update_state(self) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_client = MagicMock()

        mock_handle = AsyncMock()
        mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)

        tg = TemporalGraph(mock_graph, mock_client)

        config = {"configurable": {"thread_id": "test-thread"}}
        await tg.update_state(config, {"messages": ["new"]})

        mock_handle.signal.assert_called_once()


class TestTemporalGraphGetStateHistory:
    def setup_method(self) -> None:
        GraphRegistry.reset()

    @pytest.mark.asyncio
    async def test_get_state_history(self) -> None:
        from langgraph.temporal.config import StateQueryResult

        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_client = MagicMock()

        mock_handle = AsyncMock()
        mock_handle.query = AsyncMock(
            return_value=StateQueryResult(
                channel_values={"messages": ["hi"]},
                channel_versions={"messages": 1},
                versions_seen={},
                step=2,
                status="running",
                interrupts=[],
            )
        )
        mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)

        tg = TemporalGraph(mock_graph, mock_client)

        config = {"configurable": {"thread_id": "test-thread"}}
        history = await tg.get_state_history(config)

        assert len(history) == 1
        assert history[0]["values"] == {"messages": ["hi"]}


class TestTemporalGraphBuildInputWithNodeConfig:
    def setup_method(self) -> None:
        GraphRegistry.reset()

    def test_build_input_with_node_task_queues(self) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_graph.interrupt_before_nodes = []
        mock_graph.interrupt_after_nodes = []
        mock_client = MagicMock()

        tg = TemporalGraph(
            mock_graph,
            mock_client,
            node_task_queues={"gpu_node": "gpu-queue"},
        )

        wi = tg._build_workflow_input({"key": "val"})
        assert wi.node_task_queues == {"gpu_node": "gpu-queue"}

    def test_build_input_with_activity_options(self) -> None:
        from datetime import timedelta

        from langgraph.temporal.config import ActivityOptions

        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_graph.interrupt_before_nodes = []
        mock_graph.interrupt_after_nodes = []
        mock_client = MagicMock()

        opts = ActivityOptions(start_to_close_timeout=timedelta(seconds=600))
        tg = TemporalGraph(
            mock_graph,
            mock_client,
            node_activity_options={"llm_node": opts},
        )

        wi = tg._build_workflow_input({"key": "val"})
        assert wi.node_activity_options is not None
        assert "llm_node" in wi.node_activity_options
        assert wi.node_activity_options["llm_node"].start_to_close_timeout == timedelta(
            seconds=600
        )

    def test_build_input_with_sticky_task_queue(self) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_graph.interrupt_before_nodes = []
        mock_graph.interrupt_after_nodes = []
        mock_client = MagicMock()

        tg = TemporalGraph(mock_graph, mock_client)

        config = {
            "configurable": {
                "sticky_task_queue": "sticky-workspace-1",
            }
        }
        wi = tg._build_workflow_input({"key": "val"}, config)
        assert wi.sticky_task_queue == "sticky-workspace-1"

    def test_build_input_with_subagent_config_dict(self) -> None:
        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_graph.interrupt_before_nodes = []
        mock_graph.interrupt_after_nodes = []
        mock_client = MagicMock()

        tg = TemporalGraph(mock_graph, mock_client)

        config = {
            "configurable": {
                "subagent_config": {
                    "task_queue": "sub-queue",
                    "execution_timeout_seconds": 600.0,
                }
            }
        }
        wi = tg._build_workflow_input({"key": "val"}, config)
        assert wi.subagent_config is not None
        assert wi.subagent_config.task_queue == "sub-queue"
        assert wi.subagent_config.execution_timeout_seconds == 600.0

    def test_build_input_with_subagent_config_object(self) -> None:
        from langgraph.temporal.config import SubAgentConfig

        mock_graph = MagicMock()
        mock_graph.name = "test"
        mock_graph.interrupt_before_nodes = []
        mock_graph.interrupt_after_nodes = []
        mock_client = MagicMock()

        tg = TemporalGraph(mock_graph, mock_client)

        sac = SubAgentConfig(task_queue="sub-q", execution_timeout_seconds=900.0)
        config = {"configurable": {"subagent_config": sac}}
        wi = tg._build_workflow_input({"key": "val"}, config)
        assert wi.subagent_config is sac
