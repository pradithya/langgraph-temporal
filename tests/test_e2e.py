"""End-to-end integration tests against a real Temporal test server.

Each test builds a real StateGraph, compiles it, registers it in the
GraphRegistry, starts a Worker, and executes via the full Temporal
workflow pipeline.
"""

from __future__ import annotations

import asyncio
import operator
import uuid
from typing import Annotated, Any, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt
from temporalio.client import Client as TemporalClient
from temporalio.worker import UnsandboxedWorkflowRunner

from langgraph.temporal.graph import TemporalGraph

# LangGraph imports modules that are restricted in Temporal's sandbox.
# Use the unsandboxed runner for all integration tests.
_WORKER_KWARGS: dict[str, Any] = {"workflow_runner": UnsandboxedWorkflowRunner()}


class LinearState(TypedDict):
    value: str


class CondState(TypedDict):
    route: str
    value: str


class ParallelState(TypedDict):
    items_a: list[str]
    items_b: list[str]
    result: list[str]


class LoopState(TypedDict):
    count: int
    history: str


class QueryState(TypedDict):
    x: int
    y: int


class GotoState(TypedDict):
    from_a: bool
    from_b: bool


# Node functions must be defined at module level for Temporal Activity
# serialization (they need to be importable).


def linear_a(state: LinearState) -> dict:
    return {"value": "a"}


def linear_b(state: LinearState) -> dict:
    return {"value": state["value"] + "b"}


def cond_a(state: CondState) -> dict:
    return {"route": "go_b", "value": "a"}


def cond_b(state: CondState) -> dict:
    return {"value": state["value"] + "b"}


def cond_c(state: CondState) -> dict:
    return {"value": state["value"] + "c"}


def cond_router(state: CondState) -> str:
    return "b" if state.get("route") == "go_b" else "c"


def par_a(state: ParallelState) -> dict:
    return {"items_a": ["a"]}


def par_b(state: ParallelState) -> dict:
    return {"items_b": ["b"]}


def par_c(state: ParallelState) -> dict:
    a = state.get("items_a", [])
    b = state.get("items_b", [])
    return {"result": sorted(a + b)}


def loop_a(state: LoopState) -> dict:
    count = state.get("count", 0)
    return {"count": count + 1, "history": state.get("history", "") + "a"}


def loop_b(state: LoopState) -> dict:
    return {"history": state.get("history", "") + "b"}


def loop_should_continue(state: LoopState) -> str:
    return "a" if state.get("count", 0) < 3 else "end"


def query_a(state: QueryState) -> dict:
    return {"x": 1}


def query_b(state: QueryState) -> dict:
    return {"y": 2}


def goto_a(state: GotoState) -> Command:
    return Command(update={"from_a": True}, goto="b")


def goto_b(state: GotoState) -> dict:
    return {"from_b": True}


def interrupt_a(state: LinearState) -> dict:
    return {"value": "a"}


def interrupt_b(state: LinearState) -> dict:
    answer = interrupt("need input")
    return {"value": state.get("value", "") + f"b({answer})"}


# -- Long loop state and nodes --


class LongLoopState(TypedDict):
    count: int
    values: Annotated[list[str], operator.add]


def long_loop_body(state: LongLoopState) -> dict:
    count = state.get("count", 0)
    return {"count": count + 1, "values": [f"step-{count + 1}"]}


def long_loop_check(state: LongLoopState) -> str:
    return "body" if state.get("count", 0) <= 10 else "__end__"


# -- Scatter-gather state and nodes --


class ScatterGatherState(TypedDict):
    subjects: list[str]
    results: Annotated[list[str], operator.add]
    summary: str


class WorkerInput(TypedDict):
    subject: str


def scatter_node(state: ScatterGatherState) -> Command:
    sends = [Send("worker", {"subject": s}) for s in state["subjects"]]
    return Command(goto=sends)


def worker_node(state: WorkerInput) -> dict:
    subject = state["subject"]
    return {"results": [f"processed:{subject}"]}


def gather_node(state: ScatterGatherState) -> dict:
    return {"summary": ",".join(sorted(state.get("results", [])))}


@pytest.mark.integration
class TestSimpleLinearGraph:
    """A -> B -> END: basic node execution, channel writes, workflow completion."""

    async def test_simple_linear_graph(
        self, temporal_client: TemporalClient, task_queue: str
    ) -> None:
        builder = StateGraph(LinearState)
        builder.add_node("a", linear_a)
        builder.add_node("b", linear_b)
        builder.add_edge(START, "a")
        builder.add_edge("a", "b")
        builder.add_edge("b", END)
        graph = builder.compile()

        tg = TemporalGraph(graph, temporal_client, task_queue=task_queue)
        worker = tg.create_worker(**_WORKER_KWARGS)

        async with worker:
            result = await tg.ainvoke(
                {"value": ""},
                config={
                    "configurable": {"thread_id": f"linear-{uuid.uuid4().hex[:8]}"}
                },
            )

        assert result["value"] == "ab"


@pytest.mark.integration
class TestConditionalEdge:
    """A -> (condition) -> B or C -> END: conditional edge evaluation."""

    async def test_conditional_edge(
        self, temporal_client: TemporalClient, task_queue: str
    ) -> None:
        builder = StateGraph(CondState)
        builder.add_node("a", cond_a)
        builder.add_node("b", cond_b)
        builder.add_node("c", cond_c)
        builder.add_edge(START, "a")
        builder.add_conditional_edges("a", cond_router, {"b": "b", "c": "c"})
        builder.add_edge("b", END)
        builder.add_edge("c", END)
        graph = builder.compile()

        tg = TemporalGraph(graph, temporal_client, task_queue=task_queue)
        worker = tg.create_worker(**_WORKER_KWARGS)

        async with worker:
            result = await tg.ainvoke(
                {"route": "", "value": ""},
                config={"configurable": {"thread_id": f"cond-{uuid.uuid4().hex[:8]}"}},
            )

        assert result["value"] == "ab"


@pytest.mark.integration
class TestParallelNodes:
    """START -> [A, B] -> C -> END: parallel node execution."""

    async def test_parallel_nodes(
        self, temporal_client: TemporalClient, task_queue: str
    ) -> None:
        builder = StateGraph(ParallelState)
        builder.add_node("a", par_a)
        builder.add_node("b", par_b)
        builder.add_node("c", par_c)
        builder.add_edge(START, "a")
        builder.add_edge(START, "b")
        builder.add_edge("a", "c")
        builder.add_edge("b", "c")
        builder.add_edge("c", END)
        graph = builder.compile()

        tg = TemporalGraph(graph, temporal_client, task_queue=task_queue)
        worker = tg.create_worker(**_WORKER_KWARGS)

        async with worker:
            result = await tg.ainvoke(
                {"items_a": [], "items_b": [], "result": []},
                config={
                    "configurable": {"thread_id": f"parallel-{uuid.uuid4().hex[:8]}"}
                },
            )

        assert result["result"] == ["a", "b"]


@pytest.mark.integration
class TestInterruptAndResume:
    """A -> B(interrupt) -> END: human-in-the-loop interrupt and resume."""

    async def test_interrupt_and_resume(
        self, temporal_client: TemporalClient, task_queue: str
    ) -> None:
        builder = StateGraph(LinearState)
        builder.add_node("a", interrupt_a)
        builder.add_node("b", interrupt_b)
        builder.add_edge(START, "a")
        builder.add_edge("a", "b")
        builder.add_edge("b", END)
        graph = builder.compile()

        tg = TemporalGraph(graph, temporal_client, task_queue=task_queue)
        wf_id = f"interrupt-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": wf_id}}
        worker = tg.create_worker(**_WORKER_KWARGS)

        async with worker:
            handle = await tg.astart({"value": ""}, config)

            # Wait for the workflow to reach interrupted state
            for _ in range(30):
                state = await tg.get_state(config)
                if state["status"] == "interrupted":
                    break
                await asyncio.sleep(0.2)
            else:
                pytest.fail("Workflow did not reach interrupted state")

            assert state["status"] == "interrupted"

            # Resume with a value
            await tg.resume(config, "human_says_hi")

            # Wait for completion
            result = await handle.result()

        assert result.channel_values["value"] == "ab(human_says_hi)"


@pytest.mark.integration
class TestMultiStepGraph:
    """A -> B -> A (loop 3x) -> END: multiple steps with state accumulation."""

    async def test_multi_step_graph(
        self, temporal_client: TemporalClient, task_queue: str
    ) -> None:
        builder = StateGraph(LoopState)
        builder.add_node("a", loop_a)
        builder.add_node("b", loop_b)
        builder.add_edge(START, "a")
        builder.add_edge("a", "b")
        builder.add_conditional_edges("b", loop_should_continue, {"a": "a", "end": END})
        graph = builder.compile()

        tg = TemporalGraph(graph, temporal_client, task_queue=task_queue)
        worker = tg.create_worker(**_WORKER_KWARGS)

        async with worker:
            result = await tg.ainvoke(
                {"count": 0, "history": ""},
                config={"configurable": {"thread_id": f"loop-{uuid.uuid4().hex[:8]}"}},
            )

        assert result["count"] == 3
        assert result["history"] == "ababab"


@pytest.mark.integration
class TestStateQuery:
    """A -> B: query handler returns current state after completion."""

    async def test_state_query(
        self, temporal_client: TemporalClient, task_queue: str
    ) -> None:
        builder = StateGraph(QueryState)
        builder.add_node("a", query_a)
        builder.add_node("b", query_b)
        builder.add_edge(START, "a")
        builder.add_edge("a", "b")
        builder.add_edge("b", END)
        graph = builder.compile()

        tg = TemporalGraph(graph, temporal_client, task_queue=task_queue)
        wf_id = f"query-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": wf_id}}
        worker = tg.create_worker(**_WORKER_KWARGS)

        async with worker:
            handle = await tg.astart({"x": 0, "y": 0}, config)
            await handle.result()

            state = await tg.get_state(config)

        assert state["values"]["x"] == 1
        assert state["values"]["y"] == 2
        assert state["status"] == "done"


@pytest.mark.integration
class TestCommandGoto:
    """A -> (Command.goto) -> B -> END: Command-based routing."""

    async def test_command_goto(
        self, temporal_client: TemporalClient, task_queue: str
    ) -> None:
        builder = StateGraph(GotoState)
        builder.add_node("a", goto_a)
        builder.add_node("b", goto_b)
        builder.add_edge(START, "a")
        builder.add_edge("b", END)
        graph = builder.compile()

        tg = TemporalGraph(graph, temporal_client, task_queue=task_queue)
        worker = tg.create_worker(**_WORKER_KWARGS)

        async with worker:
            result = await tg.ainvoke(
                {"from_a": False, "from_b": False},
                config={"configurable": {"thread_id": f"goto-{uuid.uuid4().hex[:8]}"}},
            )

        assert result["from_a"] is True
        assert result["from_b"] is True


@pytest.mark.integration
class TestLongLoop:
    """body -> (check: count <= 10 -> body, else END): loop with 11 iterations."""

    async def test_long_loop(
        self, temporal_client: TemporalClient, task_queue: str
    ) -> None:
        builder = StateGraph(LongLoopState)
        builder.add_node("body", long_loop_body)
        builder.add_edge(START, "body")
        builder.add_conditional_edges(
            "body", long_loop_check, {"body": "body", "__end__": END}
        )
        graph = builder.compile()

        tg = TemporalGraph(graph, temporal_client, task_queue=task_queue)
        worker = tg.create_worker(**_WORKER_KWARGS)

        async with worker:
            result = await tg.ainvoke(
                {"count": 0, "values": []},
                config={
                    "configurable": {"thread_id": f"long-loop-{uuid.uuid4().hex[:8]}"}
                },
            )

        assert result["count"] == 11
        assert result["values"] == [f"step-{i}" for i in range(1, 12)]


@pytest.mark.integration
class TestScatterGather:
    """scatter -> [worker x N] -> gather -> END: fan-out/fan-in via Command(goto=Send)."""

    async def test_scatter_gather(
        self, temporal_client: TemporalClient, task_queue: str
    ) -> None:
        builder = StateGraph(ScatterGatherState)
        builder.add_node("scatter", scatter_node)
        builder.add_node("worker", worker_node)
        builder.add_node("gather", gather_node)
        builder.add_edge(START, "scatter")
        builder.add_edge("worker", "gather")
        builder.add_edge("gather", END)
        graph = builder.compile()

        tg = TemporalGraph(graph, temporal_client, task_queue=task_queue)
        worker = tg.create_worker(**_WORKER_KWARGS)

        subjects = ["alpha", "beta", "gamma"]
        async with worker:
            result = await tg.ainvoke(
                {"subjects": subjects, "results": [], "summary": ""},
                config={
                    "configurable": {"thread_id": f"scatter-{uuid.uuid4().hex[:8]}"}
                },
            )

        assert sorted(result["results"]) == [
            "processed:alpha",
            "processed:beta",
            "processed:gamma",
        ]
        assert result["summary"] == ("processed:alpha,processed:beta,processed:gamma")
