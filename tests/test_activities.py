"""Tests for Temporal Activities (node execution, conditional edges)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from langgraph.temporal.activities import _filter_untracked_writes, _make_counter
from langgraph.temporal.config import NodeActivityInput, NodeActivityOutput
from langgraph.temporal.converter import GraphRegistry

# We test the internal logic directly rather than calling the @activity.defn
# decorated function, which requires a Temporal Activity context.


async def _run_execute_node_logic(input: NodeActivityInput) -> NodeActivityOutput:
    """Run the core execute_node logic without the @activity.defn decorator."""
    import itertools

    from langgraph._internal._constants import (
        CONF,
        CONFIG_KEY_READ,
        CONFIG_KEY_SCRATCHPAD,
        CONFIG_KEY_SEND,
        TASKS,
    )
    from langgraph._internal._scratchpad import PregelScratchpad
    from langgraph.errors import GraphInterrupt
    from langgraph.types import Command, Send

    from langgraph.temporal.converter import GraphRegistry

    graph = GraphRegistry.get_instance().get(input.graph_definition_ref)
    node = graph.nodes[input.node_name]

    push_sends: list[dict[str, Any]] = []
    all_writes: list[tuple[str, Any]] = []

    def send_handler(writes: list[tuple[str, Any]]) -> None:
        for chan, val in writes:
            if chan == TASKS:
                if isinstance(val, Send):
                    push_sends.append({"node": val.node, "arg": val.arg})
            else:
                all_writes.append((chan, val))

    def read_handler(select: str | list[str], fresh: bool = False) -> Any:
        if isinstance(select, str):
            return input.input_state.get(select)
        return {k: input.input_state.get(k) for k in select}

    scratchpad = PregelScratchpad(
        step=0,
        stop=25,
        call_counter=itertools.count(0).__next__,
        interrupt_counter=itertools.count(0).__next__,
        get_null_resume=lambda consume=False: None,  # type: ignore[misc]
        resume=list(input.resume_values) if input.resume_values else [],
        subgraph_counter=itertools.count(0).__next__,
    )

    config: dict[str, Any] = {
        CONF: {
            CONFIG_KEY_SEND: send_handler,
            CONFIG_KEY_READ: read_handler,
            CONFIG_KEY_SCRATCHPAD: scratchpad,
        }
    }

    node_input = input.send_input if input.send_input is not None else input.input_state

    try:
        result = await node.bound.ainvoke(node_input, config)  # type: ignore[arg-type]

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
            if result.update is not None:
                for writer in node.writers:
                    writer.invoke(result, config)  # type: ignore[arg-type]
            if result.goto:
                goto_list = (
                    result.goto
                    if isinstance(result.goto, (list, tuple))
                    else [result.goto]
                )
                for target in goto_list:
                    if isinstance(target, Send):
                        push_sends.append({"node": target.node, "arg": target.arg})
        else:
            for writer in node.writers:
                writer.invoke(result, config)  # type: ignore[arg-type]

        from langgraph.channels.untracked_value import UntrackedValue

        serializable_writes = [
            (ch, val)
            for ch, val in all_writes
            if not isinstance(graph.channels.get(ch), UntrackedValue)
        ]

        return NodeActivityOutput(
            node_name=input.node_name,
            writes=serializable_writes,
            triggers=list(input.triggers),
            task_path=input.task_path,
            push_sends=push_sends if push_sends else None,
            command=command_data,
        )
    except GraphInterrupt as exc:
        interrupts = exc.args[0] if exc.args else []
        return NodeActivityOutput(
            node_name=input.node_name,
            writes=[],
            triggers=list(input.triggers),
            task_path=input.task_path,
            interrupts=[{"value": i.value, "id": i.id} for i in interrupts],
        )


class TestExecuteNodeLogic:
    def setup_method(self) -> None:
        GraphRegistry.reset()

    @pytest.mark.asyncio
    async def test_basic_node_execution(self) -> None:
        """Test executing a simple node that produces writes."""
        registry = GraphRegistry.get_instance()

        mock_bound = MagicMock()
        mock_bound.ainvoke = AsyncMock(return_value={"output": "result"})

        def mock_writer_invoke(result: object, config: dict[str, Any]) -> object:
            send = config["configurable"]["__pregel_send"]
            send([("output_channel", result)])
            return result

        mock_writer = MagicMock()
        mock_writer.invoke = mock_writer_invoke

        mock_node = MagicMock()
        mock_node.bound = mock_bound
        mock_node.writers = [mock_writer]

        mock_graph = MagicMock()
        mock_graph.nodes = {"test_node": mock_node}
        mock_graph.channels = {}

        ref = registry.register(mock_graph)

        input_data = NodeActivityInput(
            node_name="test_node",
            input_state={"key": "value"},
            graph_definition_ref=ref,
        )

        result = await _run_execute_node_logic(input_data)

        assert result.node_name == "test_node"
        assert len(result.writes) > 0
        assert result.interrupts is None

    @pytest.mark.asyncio
    async def test_node_with_interrupt(self) -> None:
        """Test node that calls interrupt()."""
        registry = GraphRegistry.get_instance()

        from langgraph.errors import GraphInterrupt
        from langgraph.types import Interrupt

        mock_bound = MagicMock()
        mock_bound.ainvoke = AsyncMock(
            side_effect=GraphInterrupt([Interrupt(value="Please approve", id="int-1")])
        )
        mock_node = MagicMock()
        mock_node.bound = mock_bound
        mock_node.writers = []

        mock_graph = MagicMock()
        mock_graph.nodes = {"agent": mock_node}
        mock_graph.channels = {}

        ref = registry.register(mock_graph)

        input_data = NodeActivityInput(
            node_name="agent",
            input_state={},
            graph_definition_ref=ref,
        )

        result = await _run_execute_node_logic(input_data)

        assert result.node_name == "agent"
        assert result.writes == []
        assert result.interrupts is not None
        assert len(result.interrupts) == 1
        assert result.interrupts[0]["value"] == "Please approve"

    @pytest.mark.asyncio
    async def test_node_with_send(self) -> None:
        """Test node that emits Send objects (PUSH tasks)."""
        registry = GraphRegistry.get_instance()

        from langgraph.types import Send

        mock_bound = MagicMock()
        mock_bound.ainvoke = AsyncMock(return_value="result")

        def mock_writer_invoke(result: object, config: dict[str, Any]) -> object:
            send = config["configurable"]["__pregel_send"]
            send([("__pregel_tasks", Send("tools", {"tool": "search"}))])
            send([("output", result)])
            return result

        mock_writer = MagicMock()
        mock_writer.invoke = mock_writer_invoke

        mock_node = MagicMock()
        mock_node.bound = mock_bound
        mock_node.writers = [mock_writer]

        mock_graph = MagicMock()
        mock_graph.nodes = {"agent": mock_node}
        mock_graph.channels = {}

        ref = registry.register(mock_graph)

        input_data = NodeActivityInput(
            node_name="agent",
            input_state={},
            graph_definition_ref=ref,
        )

        result = await _run_execute_node_logic(input_data)

        assert result.node_name == "agent"
        assert result.push_sends is not None
        assert len(result.push_sends) == 1
        assert result.push_sends[0]["node"] == "tools"

    @pytest.mark.asyncio
    async def test_node_with_untracked_filtering(self) -> None:
        """Test that UntrackedValue writes are filtered out."""
        registry = GraphRegistry.get_instance()

        from langgraph.channels.untracked_value import UntrackedValue

        mock_bound = MagicMock()
        mock_bound.ainvoke = AsyncMock(return_value="result")

        def mock_writer_invoke(result: object, config: dict[str, Any]) -> object:
            send = config["configurable"]["__pregel_send"]
            send([("messages", "hello"), ("scratch", "temp")])
            return result

        mock_writer = MagicMock()
        mock_writer.invoke = mock_writer_invoke

        mock_node = MagicMock()
        mock_node.bound = mock_bound
        mock_node.writers = [mock_writer]

        mock_graph = MagicMock()
        mock_graph.nodes = {"agent": mock_node}
        mock_graph.channels = {
            "messages": MagicMock(),
            "scratch": UntrackedValue(str),
        }

        ref = registry.register(mock_graph)

        input_data = NodeActivityInput(
            node_name="agent",
            input_state={},
            graph_definition_ref=ref,
        )

        result = await _run_execute_node_logic(input_data)

        channels_written = [ch for ch, _ in result.writes]
        assert "messages" in channels_written
        assert "scratch" not in channels_written


class TestChildWorkflowRequestsContextVar:
    def setup_method(self) -> None:
        GraphRegistry.reset()

    def test_context_var_no_default(self) -> None:
        """Context variable has no default; .get([]) returns fallback."""
        import contextvars

        from langgraph.temporal.activities import _child_workflow_requests_var

        ctx = contextvars.copy_context()
        result = ctx.run(lambda: _child_workflow_requests_var.get([]))
        assert result == []

    def test_context_var_isolation(self) -> None:
        """Each context gets its own list after .set()."""
        import contextvars

        from langgraph.temporal.activities import _child_workflow_requests_var

        def set_and_get() -> list[dict[str, Any]]:
            _child_workflow_requests_var.set([{"test": True}])
            return _child_workflow_requests_var.get()

        ctx1 = contextvars.copy_context()
        ctx2 = contextvars.copy_context()

        r1 = ctx1.run(set_and_get)
        r2 = ctx2.run(lambda: _child_workflow_requests_var.get([]))

        assert r1 == [{"test": True}]
        assert r2 == []  # ctx2 is unaffected

    def test_context_var_append_and_collect(self) -> None:
        """Verify append-then-collect pattern used by activities."""
        from langgraph.temporal.activities import _child_workflow_requests_var

        # Simulate what _execute_node_impl does
        _child_workflow_requests_var.set([])
        _child_workflow_requests_var.get().append(
            {"subagent_type": "researcher", "instruction": "research"}
        )
        _child_workflow_requests_var.get().append(
            {"subagent_type": "coder", "instruction": "code"}
        )

        collected = _child_workflow_requests_var.get([])
        assert len(collected) == 2
        assert collected[0]["subagent_type"] == "researcher"
        assert collected[1]["subagent_type"] == "coder"


class TestMakeCounter:
    def test_counter_increments(self) -> None:
        counter = _make_counter()
        assert counter() == 0
        assert counter() == 1
        assert counter() == 2


class TestFilterUntrackedWrites:
    def test_filters_untracked(self) -> None:
        from langgraph.channels.untracked_value import UntrackedValue

        mock_graph = MagicMock()
        mock_graph.channels = {
            "messages": MagicMock(),
            "scratch": UntrackedValue(str),
        }

        writes = [("messages", "hello"), ("scratch", "temp"), ("messages", "world")]
        filtered = _filter_untracked_writes(writes, mock_graph)

        assert len(filtered) == 2
        assert all(ch == "messages" for ch, _ in filtered)

    def test_no_untracked(self) -> None:
        mock_graph = MagicMock()
        mock_graph.channels = {"messages": MagicMock()}

        writes = [("messages", "hello")]
        filtered = _filter_untracked_writes(writes, mock_graph)

        assert filtered == writes

    def test_empty_writes(self) -> None:
        mock_graph = MagicMock()
        mock_graph.channels = {}

        filtered = _filter_untracked_writes([], mock_graph)
        assert filtered == []
