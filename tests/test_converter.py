"""Tests for graph definition converter and registry."""

from __future__ import annotations

import threading

import pytest

from langgraph.temporal.converter import GraphRegistry, extract_graph_metadata


class TestGraphRegistry:
    def setup_method(self) -> None:
        GraphRegistry.reset()

    def test_singleton(self) -> None:
        r1 = GraphRegistry.get_instance()
        r2 = GraphRegistry.get_instance()
        assert r1 is r2

    def test_reset(self) -> None:
        r1 = GraphRegistry.get_instance()
        GraphRegistry.reset()
        r2 = GraphRegistry.get_instance()
        assert r1 is not r2

    def test_register_and_get(self) -> None:
        registry = GraphRegistry.get_instance()

        # Create a mock graph
        class MockGraph:
            name = "test_graph"

        graph = MockGraph()
        ref = registry.register(graph)  # type: ignore[arg-type]
        assert ref.startswith("test_graph_")

        retrieved = registry.get(ref)
        assert retrieved is graph

    def test_get_missing_raises(self) -> None:
        registry = GraphRegistry.get_instance()
        with pytest.raises(KeyError):
            registry.get("nonexistent_ref")

    def test_unregister(self) -> None:
        registry = GraphRegistry.get_instance()

        class MockGraph:
            name = "test_graph"

        graph = MockGraph()
        ref = registry.register(graph)  # type: ignore[arg-type]
        registry.unregister(ref)

        with pytest.raises(KeyError):
            registry.get(ref)

    def test_unregister_missing_is_noop(self) -> None:
        registry = GraphRegistry.get_instance()
        registry.unregister("nonexistent")  # Should not raise

    def test_thread_safety(self) -> None:
        registry = GraphRegistry.get_instance()
        refs: list[str] = []
        errors: list[Exception] = []

        class MockGraph:
            def __init__(self, name: str) -> None:
                self.name = name

        def register_graph(i: int) -> None:
            try:
                ref = registry.register(MockGraph(f"graph_{i}"))  # type: ignore[arg-type]
                refs.append(ref)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=register_graph, args=(i,)) for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(refs) == 10

        # All refs should be retrievable
        for ref in refs:
            registry.get(ref)


class TestExtractGraphMetadata:
    def test_extract_metadata(self) -> None:
        class MockChannel:
            pass

        class MockGraph:
            nodes = {"agent": None, "tools": None}
            channels = {"messages": MockChannel(), "status": MockChannel()}
            trigger_to_nodes = {"messages": ["agent"], "status": ["tools"]}
            interrupt_before_nodes = ["agent"]
            interrupt_after_nodes: list[str] = []
            input_channels = "messages"
            output_channels = "messages"
            stream_channels = None

        metadata = extract_graph_metadata(MockGraph())  # type: ignore[arg-type]

        assert set(metadata["node_names"]) == {"agent", "tools"}
        assert "messages" in metadata["channel_specs"]
        assert metadata["trigger_to_nodes"]["messages"] == ["agent"]
        assert metadata["interrupt_before_nodes"] == ["agent"]
        assert metadata["interrupt_after_nodes"] == []
        assert metadata["input_channels"] == "messages"
        assert metadata["output_channels"] == "messages"
